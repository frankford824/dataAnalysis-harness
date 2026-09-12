from pathlib import Path
from types import SimpleNamespace
import shutil
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ledger.labor_api import install
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
