"""GETs use existing artifacts and the same monthly company scope, without mkdir."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger import automation_actions as actions, operations_api, operations_readmodel as operations
from operations_fixture import create_fixture


def test_old_missing_run_directory_is_not_recreated_by_any_operations_get(tmp_path,monkeypatch):
    ws,model,registry,runs=create_fixture(tmp_path,overhead=10)
    try:
        original=ws.root/'runs';original.rename(tmp_path/'offline-runs')
        def forbidden(*args,**kwargs):raise AssertionError('GET called the artifact writer path')
        monkeypatch.setattr(ws,'facts_path',forbidden)
        app=FastAPI();operations_api.install(app,lambda:ws,lambda:model)
        with TestClient(app) as client:
            for level in ('store','product','person'):
                response=client.get('/api/operations/month',params={'period':'2030-01','level':level})
                assert response.status_code==200,response.text
                assert response.json()['is_final'] is False
            assert client.get('/api/operations/evidence?period=2030-01&store_id=s1').status_code==409
        assert not original.exists()
    finally:ws.close()


def test_archived_today_does_not_remove_an_unproven_historical_operating_store(tmp_path):
    ws,model,registry,runs=create_fixture(tmp_path,overhead=10)
    model=model.model_copy(update={'stores':tuple(store.model_copy(update={'archived':store.id=='s2'}) for store in model.stores)})
    try:
        historical='2026-07'
        month=actions.month(ws,model,historical)
        report=operations.read_month(ws,model,historical)
        assert {row['store_id'] for row in month['stores']}=={'s1','s2'}
        assert {row['store_id'] for row in report['rows']}=={'s1','s2'}
        assert report['summary']['net_income'] is None
        future='2031-01'
        assert {row['store_id'] for row in actions.month(ws,model,future)['stores']}=={'s1'}
        assert {row['store_id'] for row in operations.read_month(ws,model,future)['rows']}=={'s1'}
    finally:ws.close()


def test_income_compact_alias_resolves_verified_blob_without_changing_logical_proof(tmp_path):
    from test_operations_income import make_income_workspace
    from ledger import operations_income
    from ledger.snapshot_store import put
    case=make_income_workspace(tmp_path);ws=case['ws'];run=case['run_id']
    try:
        before=operations_income.load(ws,run,{},'alibaba1688',details=False)
        text=ws.conn.execute('SELECT overview_json FROM run_report_slice WHERE run_id=?',(run,)).fetchone()[0]
        with ws.conn:ws.conn.execute('UPDATE run_report_slice SET overview_json=? WHERE run_id=?',(put(ws.conn,text),run))
        after=operations_income.load(ws,run,{},'alibaba1688',details=False)
        assert before['net_income']==after['net_income']==180
        assert before['receipts']==after['receipts']==200
        assert before['refunds']==after['refunds']==20
        assert before['input_sha']==after['input_sha']
        assert before['proof']['net_source']==after['proof']['net_source']
    finally:ws.close()
