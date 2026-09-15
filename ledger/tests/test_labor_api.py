from pathlib import Path
from types import SimpleNamespace
import shutil
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ledger.labor_api import install
from ledger.labor_api import share_at_close
from ledger.workspace import WorkspaceError
from ledger.model.loader import load_model


def test_edit_total_name_refresh_and_closed_month_freeze(tmp_path):
    root=tmp_path/'model';shutil.copytree(Path(__file__).resolve().parents[2]/'models/cn-ecommerce',root)
    model=lambda:load_model(root)
    node=next(n.id for n in model().statement if n.headline=='revenue')
    states=[SimpleNamespace(period='2030-01',store_id=sid,state='open',result={'statement':[{'id':node,'value':sales,'available':True}]}) for sid,sales in [('a',100),('b',300)]]
    audit=[]
    ws=SimpleNamespace(overview=lambda:states,log_config=lambda *a,**k:audit.append(k))
    app=FastAPI();install(app,lambda:ws,model,root,lambda:None,lambda req:{'name':'test'})
    client=TestClient(app)
    initial=client.get('/api/commission-v2/labor?period=2030-01').json()
    assert initial['amount'] is None
    assert initial['basis_total']==400
    assert [r['share'] for r in initial['rows']]==[0.25,0.75]
    response=client.post('/api/commission-v2/labor',json={'period':'2030-01','name':'兼职人工','amount':'100.00','revision':initial['revision']})
    assert response.status_code==200
    result=response.json();assert [r['amount'] for r in result['rows']]==[25,75]
    assert client.get('/api/commission-v2/labor?period=2030-01').json()['name']=='兼职人工'
    assert client.post('/api/commission-v2/labor',json={'period':'2030-01','name':'兼职','amount':50,'revision':initial['revision']}).status_code==409
    changed=client.post('/api/commission-v2/labor',json={'period':'2030-01','name':'临时人工','amount':'200.00','revision':result['revision']}).json()
    assert sum(r['amount'] for r in changed['rows'])==200
    assert len(audit)==2
    states[0].state='closed'
    assert client.post('/api/commission-v2/labor',json={'period':'2030-01','name':'兼职','amount':300,'revision':changed['revision']}).status_code==200
    assert client.get('/api/commission-v2/labor?period=2030-01').json()['amount']==300
    assert states[0].state=='closed'
    assert states[0].result['statement'][0]['value']==100
    from ledger.labor_api import frozen_shares
    frozen=frozen_shares(root)
    assert float(frozen[('2030-01','a','None')]['amount'])==50
    current=client.get('/api/commission-v2/labor?period=2030-01').json()
    assert current['locked'] is False and current['closed_stores']==1
    assert client.post('/api/commission-v2/labor',json={'period':'2030-01','name':'兼职','amount':400,'revision':current['revision']}).status_code==200
    assert frozen_shares(root)==frozen
    assert client.get('/api/commission-v2/labor?period=2030-13').status_code==400


def test_share_at_close_uses_current_store_revenue_and_requires_evidence(tmp_path):
    root=tmp_path/'model';shutil.copytree(Path(__file__).resolve().parents[2]/'models/cn-ecommerce',root)
    base=load_model(root)
    from ledger.model.schema import Overhead
    model=base.model_copy(update={'overheads':(*base.overheads,Overhead(period='2030-01',amount=100))})
    node=next(n.id for n in model.statement if n.headline=='revenue')
    states=[SimpleNamespace(period='2030-01',store_id=sid,result={
        'statement':[{'id':node,'value':sales,'available':True}]})
        for sid,sales in [('a',100),('b',300)]]
    ws=SimpleNamespace(overview=lambda:states)
    assert share_at_close(ws,model,'2030-01','a')=='25.00'
    assert share_at_close(ws,model,'2030-01','b')=='75.00'
    states[0].result['statement'][0]['available']=False
    with pytest.raises(WorkspaceError,match='销售收入尚未确定'):
        share_at_close(ws,model,'2030-01','a')


def test_new_closed_share_is_not_rewritten_into_legacy_csv(tmp_path):
    from ledger.labor_api import save, frozen_shares, LaborChange
    root=tmp_path/'model';shutil.copytree(Path(__file__).resolve().parents[2]/'models/cn-ecommerce',root)
    from ledger.model.transaction import model_revision
    body=LaborChange(period='2030-01',name='兼职人工费用',amount='100.00',revision=model_revision(root))
    save(root,body,[{'store_id':'s1','run_id':7,'amount':25,'frozen_amount':'20.00'}])
    assert frozen_shares(root)=={}
