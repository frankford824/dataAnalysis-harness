"""Real source-byte drift blocks computation/publishing, never a finance waiver."""
from contextlib import contextmanager
from copy import deepcopy

import pytest

from ledger import service,version,automation_checker as checker,automation_actions as actions
from ledger.workspace import Workspace
from finance_fixture import monthly_model
from test_commission import _model
from test_code_integrity import make_release


@pytest.fixture
def approved(tmp_path,monkeypatch):
    monitor,source,_,_=make_release(tmp_path/'code')
    monkeypatch.setattr(version,'_CODE_MONITOR',monitor)
    assert version.assert_code_integrity()['healthy']
    return source


def drift(source):source.write_text('VALUE = 2\n')


def test_source_drift_stops_before_ingestion_and_is_technical_maintenance(tmp_path,monkeypatch,approved):
    ws=Workspace(tmp_path/'ws');model=_model();drift(approved)
    monkeypatch.setattr(service,'ingest',lambda *a,**k:pytest.fail('Unapproved source cannot ingest'))
    result=service.recompute(ws,model,model.store('s1'))
    assert result.failure['kind']=='maintenance' and result.failure['code']=='code_source_drift'
    assert result.periods==[] and not ws.conn.execute('SELECT 1 FROM run').fetchone()


def setup_real_compute(tmp_path,monkeypatch):
    from test_automation_deterministic import source
    ws,model,ing,*_=source(tmp_path)
    monkeypatch.setattr(service.order_feed,'enabled',lambda:False)
    monkeypatch.setattr(service,'ingest',lambda *a,**k:deepcopy(ing))
    return ws,model


def test_drift_after_real_engine_calculation_cannot_publish_a_new_run(tmp_path,monkeypatch,approved):
    ws,model=setup_real_compute(tmp_path,monkeypatch)
    before=ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]
    original=service.automation_period_proof.prepare
    def calculate_then_drift(*args,**kwargs):
        result=original(*args,**kwargs);drift(approved);return result
    monkeypatch.setattr(service.automation_period_proof,'prepare',calculate_then_drift)
    result=service.recompute(ws,model,model.store('s'))
    assert result.failure['code']=='code_source_drift'
    assert ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]==before


def test_drift_inside_publish_lock_still_cannot_record_a_run(tmp_path,monkeypatch,approved):
    from ledger import finance_guard
    ws,model=setup_real_compute(tmp_path,monkeypatch)
    before=ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]
    original=finance_guard.guard
    @contextmanager
    def locked_then_drift(*args,**kwargs):
        with original(*args,**kwargs):
            drift(approved)
            yield
    monkeypatch.setattr(finance_guard,'guard',locked_then_drift)
    result=service.recompute(ws,model,model.store('s'))
    assert result.failure['code']=='code_source_drift'
    assert ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]==before


def test_cached_reuse_result_is_not_reported_ready_after_source_drift(tmp_path,monkeypatch,approved):
    ws,model=setup_real_compute(tmp_path,monkeypatch)
    def cached_then_drift(*args,**kwargs):
        drift(approved)
        return [{'store_id':'s','period':'2026-07','can_close':True,'run_id':1}]
    monkeypatch.setattr(service.recompute_reuse,'load',cached_then_drift)
    result=service.recompute(ws,model,model.store('s'))
    assert result.failure['code']=='code_source_drift' and result.periods==[]


def test_checker_hard_block_and_native_action_guard_cannot_be_waived(tmp_path,approved):
    from ledger.automation_cost_actions import apply as cost_apply
    from ledger.automation_deterministic import apply_statement
    ws=Workspace(tmp_path/'ws');period='2030-06';model=monthly_model(_model(),'s1',period,0)
    checker.bind_model(ws,model)
    ws.record('s1',period,{'can_close':True,'findings':[],'missing_sources':[],
        'statement':[{'id':'n_receipt','value':100,'available':True}]},[])
    assert checker.check(ws,'s1',period,model=model)['allowed']
    drift(approved)
    checked=checker.check(ws,'s1',period,model=model,ignored_blockers=('code_source_drift',))
    assert not checked['allowed'] and any(i['code']=='code_source_drift' and i['hard'] for i in checked['issues'])
    month=actions.month(ws,model,period)
    assert month['stores'][0]['stage']=='maintenance' and month['status']=='processing'
    assert month['stores'][0]['actions']==[]
    assert month['decision_groups']==[] and not month['close']['eligible_store_ids']
    with pytest.raises(version.CodeIntegrityError):cost_apply(ws,model,{},None)
    with pytest.raises(version.CodeIntegrityError):apply_statement(ws,model,{},None)
    assert not ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()


def test_approved_code_refresh_uses_existing_dedup_queue_once_for_open_results(tmp_path,approved):
    from conftest import scoped_manager as Manager
    from ledger.commission_registry import Registry
    ws=Workspace(tmp_path/'ws');model=_model();registry=Registry(ws.root)
    ws.record('s1','2030-06',{'can_close':False,'calculation_inputs':{'metric_totals':{}}},[])
    manager=Manager(lambda:ws,lambda:model)
    assert manager._enqueue_code_refresh(ws,registry)==1
    assert manager._enqueue_code_refresh(ws,registry)==1
    with registry.connect() as conn:
        queued=conn.execute("SELECT * FROM pending WHERE store_id='s1'").fetchone()
        assert queued['files_revision']==1
        assert conn.execute('SELECT count(*) FROM automation_dispatch').fetchone()[0]==1
    assert ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]==1


def test_resealed_bad_artifact_is_recomputed_as_new_run_not_reblessed(tmp_path,monkeypatch,approved):
    import polars as pl
    from ledger.storage_integrity import seal,digest
    ws,model=setup_real_compute(tmp_path,monkeypatch)
    first=service.recompute(ws,model,model.store('s'));assert not first.failure
    old=ws.latest_run('s','2026-07');old_id=old['id']
    manifest=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(old_id,)).fetchone()[0]
    before=pl.read_parquet(ws.facts_path(old_id))
    before.with_columns((pl.col('contribution')+1).alias('contribution')).write_parquet(ws.facts_path(old_id));seal(ws.facts_path(old_id))
    second=service.recompute(ws,model,model.store('s'));assert not second.failure
    new=ws.latest_run('s','2026-07')
    assert new['id']!=old_id
    assert pl.read_parquet(ws.facts_path(new['id']))['contribution'].to_list()==before['contribution'].to_list()
    assert ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(old_id,)).fetchone()[0]==manifest


def test_legacy_matching_summary_without_original_proof_gets_fresh_run(tmp_path,monkeypatch,approved):
    import json
    from ledger.storage_integrity import seal
    ws,model=setup_real_compute(tmp_path,monkeypatch)
    first=service.recompute(ws,model,model.store('s'));assert not first.failure
    old=ws.latest_run('s','2026-07')
    # Declared legacy/proofless snapshot, not a financial readiness certificate.
    legacy=ws.record('s','2026-07',json.loads(old['result']),json.loads(old['shas']),
        model_revision=old['model_revision'],input_fingerprint=old['input_fingerprint'])
    for suffix in ('.parquet','.pricing.parquet','.pricing-raw.parquet','.coverage.parquet','.allocation.parquet','.operations.parquet','.order-issues.parquet'):
        original=ws.facts_path(old['id']).with_suffix(suffix)
        if original.exists():
            target=ws.facts_path(legacy).with_suffix(suffix);target.write_bytes(original.read_bytes());seal(target)
    second=service.recompute(ws,model,model.store('s'));assert not second.failure
    assert ws.latest_run('s','2026-07')['id']!=legacy
    assert not ws.conn.execute('SELECT 1 FROM automation_run_proof WHERE run_id=?',(legacy,)).fetchone()


def test_new_approved_source_bytes_with_same_git_label_require_new_result(tmp_path,monkeypatch,approved):
    import json
    from ledger.release_gate import digest
    from test_release_gate import seal
    ws,model=setup_real_compute(tmp_path,monkeypatch)
    first=service.recompute(ws,model,model.store('s'));assert not first.failure
    old=ws.latest_run('s','2026-07');old_engine=old['engine']
    drift(approved)
    marker=approved.parents[2]/'RELEASE-ACCEPTANCE.json'
    value=json.loads(marker.read_text());manifest=version.source_code_manifest(approved.parents[2])
    value.update(source_code_manifest=manifest,source_code_sha=digest(manifest));seal(value);marker.write_text(json.dumps(value))
    monkeypatch.setattr(version,'_CODE_MONITOR',version.CodeIntegrityMonitor(approved.parents[2]))
    assert version.assert_code_integrity()['healthy']
    checked=checker.check(ws,'s','2026-07',model=model)
    assert 'unverified_code_change' in {issue['code'] for issue in checked['issues']}
    second=service.recompute(ws,model,model.store('s'));assert not second.failure
    new=ws.latest_run('s','2026-07')
    assert new['engine']==old_engine and new['id']!=old['id']
