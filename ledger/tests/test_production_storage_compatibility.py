"""Captured production protocols exercised only in temporary synthetic stores."""
import hashlib
import json
import sqlite3

import polars as pl
import pytest

from ledger import automation_checker as checker
from ledger.automation_period_proof import valid_artifact
from ledger.commission_registry import Registry
from ledger.snapshot_store import PREFIX,put,resolve
from ledger.storage_integrity import digest,seal
from ledger.storage_maintenance import manifest
from ledger.workspace import Workspace
from test_commission import _model
from finance_fixture import monthly_model


def payload(pending=False):
    return {'can_close':True,'store':'synthetic','period':'2030-06','source_sync_pending':pending,
        'findings':[],'missing_sources':[],'statement':[{'id':'n_receipt','value':100,'available':True}],
        'memo':'格式与金额原样保留 '*300}


def test_existing_compact_references_are_read_without_enabling_compact_writes(tmp_path,monkeypatch):
    monkeypatch.setenv('LEDGER_COMPACT_SNAPSHOTS','0')
    ws=Workspace(tmp_path);model=monthly_model(_model(),'s1','2030-06',0);checker.bind_model(ws,model)
    raw=payload(True);run=ws.record('s1','2030-06',raw,[])
    text=json.dumps(raw,ensure_ascii=False,indent=2)
    with ws.transaction() as conn:
        reference=put(conn,text)
        conn.execute('UPDATE run SET result=? WHERE id=?',(reference,run))
        conn.execute('UPDATE run_report_slice SET overview_json=?,statement_json=? WHERE run_id=?',
            (reference,put(conn,json.dumps(raw['statement'])),run))
    assert ws.latest_run('s1','2030-06')['result']==text
    assert ws.state('s1','2030-06').result==raw
    assert ws.period_headers('s1')[0]['can_close'] is True
    assert ws.previous_gap_basis('s1','2030-07')=={'statement':raw['statement']}
    company=checker.company_basis(ws,model,'2030-06')
    assert company['missing_store_ids']==['s1']  # compressed source_pending cannot become false.
    assert 'source_sync_pending' in {issue['code'] for issue in checker.check(ws,'s1','2030-06',model=model)['issues']}


def test_compact_write_policy_preserves_logical_json_and_evidence_failure(tmp_path,monkeypatch):
    monkeypatch.setenv('LEDGER_COMPACT_SNAPSHOTS','1')
    ws=Workspace(tmp_path);raw=payload();run=ws.record('s1','2030-06',raw,[])
    with sqlite3.connect(ws.root/'workspace.db') as physical:
        assert physical.execute('SELECT result FROM run WHERE id=?',(run,)).fetchone()[0].startswith(PREFIX)
        assert physical.execute('SELECT overview_json FROM run_report_slice WHERE run_id=?',(run,)).fetchone()[0].startswith(PREFIX)
    assert ws.state('s1','2030-06').result==raw
    ws.mark_evidence(run,ready=False,error='synthetic unavailable archive')
    result=ws.state('s1','2030-06').result
    assert result['statement']==raw['statement'] and result['memo']==raw['memo']
    assert not result['can_close'] and any(f['id']=='evidence_archive' for f in result['findings'])


def test_reader_does_not_interpret_free_text_or_cache_an_rolled_back_blob(tmp_path):
    ws=Workspace(tmp_path);text='{ "amount": 3.335, "中文": "原字节" }'
    with ws.transaction() as conn:
        ref=put(conn,text)
        row=conn.execute('SELECT ? AS name, ? AS result',(ref,ref)).fetchone()
        assert row['name']==ref and row['result']==text
    ws.conn.execute('BEGIN IMMEDIATE')
    vanished=put(ws.conn,'{"uncommitted":true}')
    assert ws.conn.execute('SELECT ? AS result',(vanished,)).fetchone()[0]=='{"uncommitted":true}'
    ws.conn.rollback()
    with pytest.raises(ValueError,match='missing'):resolve(ws.conn,vanished)


def test_registry_summary_reference_can_be_used_by_json_sql(tmp_path):
    registry=Registry(tmp_path)
    raw=json.dumps({'total':6.67,'products':[{'id':'p'}],'people':[{'id':'person'}]})
    with registry.transaction() as conn:
        ref=put(conn,raw)
        actual=conn.execute("SELECT json_remove(snapshot_json(?),'$.products','$.people') AS summary_json",(ref,)).fetchone()[0]
        assert json.loads(actual)=={'total':6.67}


def bundle(ws,archive,run=1):
    target=archive/('run-'+str(run));target.mkdir(parents=True)
    parent=ws.root/'runs';parent.mkdir(exist_ok=True)
    (parent/str(run)).symlink_to(target,target_is_directory=True)
    files=[];artifacts=[]
    for suffix in ('.parquet','.pricing.parquet','.pricing-raw.parquet','.coverage.parquet',
        '.allocation.parquet','.operations.parquet','.order-issues.parquet'):
        path=target/(str(run)+suffix)
        pl.DataFrame({'fixture':['retained original bytes']}).write_parquet(path);seal(path)
        sha=digest(path)
        files.append({'name':path.name,'sha256':sha,'bytes':path.stat().st_size})
        artifacts.append({'path':f'runs/{run}/'+path.name,'sha':sha})
    raw=json.dumps({'run_id':run,'files':files},sort_keys=True).encode()
    (target/'manifest.json').write_bytes(raw)
    with manifest(ws.root) as conn,conn:
        conn.execute('INSERT INTO artifact_archive VALUES(?,?,?,?,?,?)',
            (f'runs/{run}',hashlib.sha256(raw).hexdigest(),sum(item['bytes'] for item in files),str(target),'archived','synthetic'))
    return target,artifacts


def test_registered_directory_bundle_reads_all_m3_sidecars_and_keeps_expected_sha(tmp_path):
    from ledger.run_artifacts import verified_path
    ws=Workspace(tmp_path/'workspace');target,artifacts=bundle(ws,tmp_path/'archive')
    assert ws.facts_path(1)==ws.root/'runs'/'1'/'1.parquet'
    assert all(valid_artifact(ws,artifact) for artifact in artifacts)
    assert verified_path(ws.root,1,'.operations.parquet')==ws.facts_path(1).with_suffix('.operations.parquet')
    changed=target/'1.operations.parquet'
    pl.DataFrame({'fixture':['changed and resealed']}).write_parquet(changed);seal(changed)
    assert not valid_artifact(ws,next(a for a in artifacts if a['path'].endswith('.operations.parquet')))
    assert verified_path(ws.root,1,'.operations.parquet') is None


def test_missing_or_unregistered_bundle_never_falls_back_to_a_flat_file(tmp_path):
    from ledger.run_artifacts import verified_path
    ws=Workspace(tmp_path/'workspace');target,artifacts=bundle(ws,tmp_path/'archive')
    with manifest(ws.root) as conn,conn:conn.execute('DELETE FROM artifact_archive')
    assert not valid_artifact(ws,artifacts[0])
    assert verified_path(ws.root,1) is None
    flat=ws.root/'runs'/'1.parquet';flat.write_bytes(b'different flat snapshot');seal(flat)
    target.rename(tmp_path/'offline-bundle')
    assert ws.facts_path(1)==ws.root/'runs'/'1'/'1.parquet'
    assert not ws.facts_path(1).exists() and flat.exists()
    assert not valid_artifact(ws,artifacts[0])
    assert verified_path(ws.root,1) is None


def test_pure_readback_accessor_does_not_create_an_absent_workspace(tmp_path):
    from ledger.run_artifacts import verified_path
    absent=tmp_path/'never-initialized'
    assert verified_path(absent,1) is None
    assert not absent.exists()
