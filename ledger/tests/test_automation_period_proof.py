import json
from types import SimpleNamespace

import polars as pl
import pytest

from ledger import automation_period_proof as proof
from ledger.storage_integrity import seal
from ledger.workspace import Workspace
from test_commission import _model


@pytest.fixture
def fixed(tmp_path,monkeypatch):
    monkeypatch.setattr(proof,'engine_version',lambda:'release-a')
    monkeypatch.setattr('ledger.workspace.engine_version',lambda:'release-a')
    ws=Workspace(tmp_path/'ws');model=_model();store=model.store('s1')
    source=pl.DataFrame({'metric_id':['receipt'],'store':['测试店'],'period':['2030-06'],
        'order_id':['o1'],'sub_order_id':['c1'],'amount':[10.0],'contribution':[10.0],
        'file_sha':['old-whole-file-sha'],'file_name':['orders.csv'],'sheet':['orders'],'row_no':[2],
        'spine_row':[0]})
    sl=SimpleNamespace(period='2030-06',facts=source)
    ing=SimpleNamespace(items=[])
    payload={'can_close':True,'statement':[{'id':'n_receipt','value':10,'available':True}],
             'commission':{'registry_revision':1,'calculation_id':'random-a'}}
    candidate=proof.prepare(ws,model,store,ing,sl,payload,None)
    # No commission proof is needed for this focused source-frame fixture.
    payload['commission'].pop('calculation_id')
    rid=ws.record(store.id,sl.period,payload,[])
    source.write_parquet(ws.facts_path(rid));seal(ws.facts_path(rid))
    proof.save(ws,rid,candidate)
    yield ws,model,store,sl,ing,payload,candidate,rid
    ws.close()


def test_other_month_file_version_does_not_create_a_run_when_closure_is_equal(fixed):
    ws,model,store,sl,ing,payload,old,rid=fixed
    import io
    ws.keep('orders.csv',io.BytesIO(b'new rows belonging only to July'),store.id)
    sl.facts=sl.facts.with_columns(pl.lit('new-whole-file-sha').alias('file_sha'),pl.lit(100).alias('spine_row'))
    payload['commission']['registry_revision']=99
    candidate=proof.prepare(ws,model,store,ing,sl,payload,None)
    assert candidate['dependency_sha']==old['dependency_sha']
    assert proof.reusable(ws,store.id,sl.period,candidate)['id']==rid
    assert ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]==1
    validation=ws.conn.execute('SELECT manifest_json FROM automation_run_validation').fetchone()
    assert json.loads(validation[0])['version']>old['manifest']['version']


@pytest.mark.parametrize('statement',[
    "UPDATE automation_run_proof SET manifest_json='{}' WHERE run_id=?",
    'DELETE FROM automation_run_proof WHERE run_id=?',
])
def test_first_run_manifest_cannot_be_updated_or_deleted(fixed,statement):
    import sqlite3
    ws,model,store,sl,ing,payload,old,rid=fixed
    before=dict(ws.conn.execute('SELECT * FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone())
    with pytest.raises(sqlite3.IntegrityError,match='immutable automation journal'):
        with ws.transaction():ws.conn.execute(statement,(rid,))
    assert dict(ws.conn.execute('SELECT * FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone())==before
    # Saving the same proof remains a harmless no-op. Equivalence observations
    # use their own append-only table, including repeated valid observations.
    proof.save(ws,rid,old)
    assert proof.reusable(ws,store.id,sl.period,old)['id']==rid
    assert proof.reusable(ws,store.id,sl.period,old)['id']==rid
    assert ws.conn.execute('SELECT count(*) FROM automation_run_validation WHERE run_id=?',(rid,)).fetchone()[0]==2
    assert dict(ws.conn.execute('SELECT * FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone())==before


def test_duplicate_insert_or_replace_cannot_replace_first_manifest(fixed):
    ws,model,store,sl,ing,payload,old,rid=fixed
    before=tuple(ws.conn.execute('SELECT * FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone())
    with ws.transaction():
        ws.conn.execute('INSERT OR REPLACE INTO automation_run_proof VALUES(?,?,?,?,?,?)',
            (rid,'changed','changed','changed','{}','later'))
    assert tuple(ws.conn.execute('SELECT * FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone())==before


@pytest.mark.parametrize('change',['quantity','identity','evidence','finding','person','engine'])
def test_equal_headline_amount_is_not_enough_to_reuse(fixed,change,monkeypatch):
    ws,model,store,sl,ing,payload,old,rid=fixed
    if change=='quantity':sl.facts=sl.facts.with_columns(pl.lit(2).alias('quantity'))
    if change=='identity':sl.facts=sl.facts.with_columns(pl.lit('another-child').alias('sub_order_id'))
    if change=='evidence':sl.facts=sl.facts.with_columns(pl.lit('different evidence').alias('source_note'))
    if change=='finding':payload['findings']=[{'id':'missing','passed':False}]
    if change=='person':payload['commission']['people']=[{'person_id':'new-person','amount':0}]
    if change=='engine':monkeypatch.setattr(proof,'engine_version',lambda:'release-b')
    new=proof.prepare(ws,model,store,ing,sl,payload,None)
    assert new['dependency_sha']!=old['dependency_sha']
    assert proof.reusable(ws,store.id,sl.period,new) is None


def test_same_amount_corrupt_artifact_is_not_reused(fixed):
    ws,model,store,sl,ing,payload,old,rid=fixed
    ws.facts_path(rid).write_bytes(b'corrupt')
    assert proof.reusable(ws,store.id,sl.period,old) is None


def test_manifest_expected_digest_still_blocks_resealed_corrupt_artifact(fixed):
    from ledger import automation_checker
    ws,model,store,sl,ing,payload,old,rid=fixed
    ws.facts_path(rid).write_bytes(b'changed bytes with a freshly changed sidecar')
    seal(ws.facts_path(rid))
    checked=automation_checker.check(ws,store.id,sl.period,model=model,verify_artifacts=True)
    assert 'evidence_archive' in {issue['code'] for issue in checked['issues']}
    assert proof.reusable(ws,store.id,sl.period,old) is None


def test_applied_history_cost_source_bytes_are_part_of_period_evidence(fixed):
    from ledger import automation_store as journal
    ws,model,store,sl,ing,payload,old,rid=fixed
    raw=ws.root/'retained-history.json';raw.write_bytes(b'{"unit_cost":"2.00"}')
    source={'line_log_id':7,'quote_sha':'verified-quote','source_refs':[{'path':str(raw),'sha256':proof.file_digest(raw)}]}
    ws.conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('cost-proof','cost-source','history_cost_single_component.v1',sl.period,store.id,'apply','',journal.encoded(source),
         '固定来源凭据核对','input','proposal','service:fixture',journal.now()))
    payload['cost_line_posting']={'reviews':[{'revision':7}]}
    candidate=proof.prepare(ws,model,store,ing,sl,payload,None)
    assert candidate['evidence_sha']!=old['evidence_sha']
    artifact=candidate['manifest']['approved_cost_sources'][0]['artifacts'][0]
    assert proof.valid_artifact(ws,artifact)
    raw.write_bytes(b'changed historical quote')
    assert not proof.valid_artifact(ws,artifact)


def test_dirty_engine_cannot_certify_equivalence(fixed,monkeypatch):
    ws,model,store,sl,ing,payload,old,rid=fixed
    monkeypatch.setattr(proof,'engine_version',lambda:'release-a-dirty')
    new=proof.prepare(ws,model,store,ing,sl,payload,None)
    assert proof.reusable(ws,store.id,sl.period,new) is None


def test_equivalent_other_month_update_does_not_invalidate_frozen_period(fixed):
    ws,model,store,sl,ing,payload,old,rid=fixed
    import io
    # Seed an existing frozen pointer to isolate read-side stale semantics.
    with ws.conn:ws.conn.execute("UPDATE period SET state='closed',run_id=?,at_version=0",(rid,))
    ws.keep('orders.csv',io.BytesIO(b'new July content'),store.id)
    assert ws.state(store.id,sl.period).stale
    candidate=proof.prepare(ws,model,store,ing,sl,payload,None)
    assert proof.reusable(ws,store.id,sl.period,candidate)['id']==rid
    assert not ws.state(store.id,sl.period).stale
    assert not ws.period_headers(store.id)[0]['stale']
