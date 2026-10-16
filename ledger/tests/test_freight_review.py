import csv
import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.freight_review import build, export_csv
from ledger.freight_review_api import install
from ledger.model.schema import Store
from ledger.workspace import Workspace
from test_commission import _model


def payload(expense=10, **values):
    return {'statement':[{'id':'n_freight','value':-expense,'available':True}], **values}


def close(ws, run, sid='s1'):
    ws.conn.execute("UPDATE period SET state='closed',run_id=? WHERE store_id=? AND period='2026-06'",(run,sid))
    ws.conn.commit()


def test_review_preserves_close_and_manual_amounts_and_marks_difference_unverified(tmp_path):
    ws=Workspace(tmp_path);model=_model()
    old=ws.record('s1','2026-06',payload(10),[]);close(ws,old)
    ws.conn.execute('INSERT INTO manual_finance VALUES(?,?,?,?,?,?,?,?)',
        (old,'s1','2026-06',json.dumps(payload(12)),'{}','original','now','tester'))
    ws.conn.commit()
    new=ws.record('s1','2026-06',payload(17),[])
    before=[tuple(r) for r in ws.conn.execute('SELECT * FROM manual_finance')]
    found=build(ws,model,'2026-06')
    assert found['audit_status']=='comparison_only'
    assert found['summary']['shown_amount']==12 and found['summary']['trial_amount']==17
    assert found['summary']['change']==5
    assert found['stores'][0]['shown_run_id']==old and found['stores'][0]['trial_run_id']==new
    assert found['groups'][0]['id']=='changes'
    assert ws.state('s1','2026-06').run_id==old
    assert [tuple(r) for r in ws.conn.execute('SELECT * FROM manual_finance')]==before
    text=export_csv(found)
    row=list(csv.DictReader(io.StringIO(text.lstrip('\ufeff'))))[0]
    assert row['账内运费（元）']=='12.0' and row['试算差额（元，未核实）']=='5.0'
    assert '不是应补扣' in row['金额说明']


@pytest.mark.parametrize('extra,evidence_ready',[({'source_sync_pending':True},True),({},False)])
def test_incomplete_trial_is_not_added_as_a_complete_month(tmp_path,extra,evidence_ready):
    ws=Workspace(tmp_path);old=ws.record('s1','2026-06',payload(10),[]);close(ws,old)
    ws.record('s1','2026-06',payload(999,**extra),[],evidence_ready=evidence_ready)
    found=build(ws,_model(),'2026-06')
    assert found['summary']['shown_amount']==10
    assert found['summary']['trial_amount'] is None and found['summary']['change'] is None
    assert found['stores'][0]['trial_status']=='syncing'
    assert [g['id'] for g in found['groups']]==['syncing']


def test_unknown_amount_stays_unknown_and_true_zero_stays_zero(tmp_path):
    ws=Workspace(tmp_path)
    ws.record('s1','2026-06',payload(0),[])
    zero=build(ws,_model(),'2026-06')
    assert zero['summary']['shown_amount']==zero['summary']['trial_amount']==0
    assert zero['summary']['change']==0
    ws.record('s1','2026-06',{'statement':[{'id':'n_freight','available':False,'value':-3}]},[])
    unknown=build(ws,_model(),'2026-06')
    assert unknown['summary']['shown_amount'] is None
    assert unknown['summary']['trial_amount'] is None
    assert unknown['stores'][0]['shown_reason']=='运费金额依据不完整'
    assert [g['id'] for g in unknown['groups']]==['evidence']


def test_offsetting_store_changes_still_need_review_and_scope_does_not_leak(tmp_path):
    ws=Workspace(tmp_path)
    model=_model(stores=(Store(id='s1',name='甲',platform='taobao'),Store(id='s2',name='乙',platform='taobao')))
    for sid,amount in [('s1',20),('s2',40)]:
        old=ws.record(sid,'2026-06',payload(30),[]);close(ws,old,sid)
        ws.record(sid,'2026-06',payload(amount),[])
    ws.record('not-in-model','2026-06',payload(9999),[])
    found=build(ws,model,'2026-06')
    assert found['summary']['change']==0 and found['summary']['changed_store_count']==2
    assert found['summary']['increased_store_count']==found['summary']['decreased_store_count']==1
    assert found['groups'][0]['store_count']==2
    assert build(ws,model,'2026-06',store_id='s1')['summary']['change']==-10


def test_allocation_pending_does_not_turn_into_an_extra_expense(tmp_path):
    ws=Workspace(tmp_path)
    ws.record('s1','2026-06',payload(10,allocation_pending=[
        {'metric_id':'freight_cost','amount':-3,'reason':'reship_freight_basis'},
        {'metric_id':'software_fee','amount':-100,'reason':'missing_payment_basis'}]),[])
    found=build(ws,_model(),'2026-06')
    assert found['summary']['shown_amount']==found['summary']['trial_amount']==10
    assert found['stores'][0]['allocation_count']==1
    assert [g['id'] for g in found['groups']]==['allocation']
    assert '已计入店铺' in found['stores'][0]['allocation_reasons'][0]


def test_absent_followup_and_no_data_are_not_reported_as_a_zero_difference(tmp_path):
    ws=Workspace(tmp_path)
    empty=build(ws,_model(),'2026-06')
    assert empty['summary']['store_count']==0 and empty['summary']['shown_amount'] is None
    old=ws.record('s1','2026-06',payload(10),[]);close(ws,old)
    found=build(ws,_model(),'2026-06')
    assert found['summary']['change'] is None
    assert [g['id'] for g in found['groups']]==['no_trial']


def test_partial_comparison_group_and_export_do_not_imply_a_complete_month(tmp_path):
    ws=Workspace(tmp_path)
    model=_model(stores=(Store(id='s1',name='甲',platform='taobao'),Store(id='s2',name='乙',platform='taobao')))
    for sid in ('s1','s2'):
        original=ws.record(sid,'2026-06',payload(10),[]);close(ws,original,sid)
    ws.record('s1','2026-06',payload(12),[])
    ws.record('s2','2026-06',payload(999,source_sync_pending=True),[])
    data=build(ws,model,'2026-06')
    assert data['summary']['change'] is None
    assert data['summary']['trial_available_count']==1
    assert data['groups'][0]['net_change']==2
    rows=list(csv.DictReader(io.StringIO(export_csv(data,'changes').lstrip('\ufeff'))))
    assert len(rows)==1 and rows[0]['店铺']=='甲'
    assert rows[0]['后续试算运费（元）']=='12.0'
    waiting=list(csv.DictReader(io.StringIO(export_csv(data,'syncing').lstrip('\ufeff'))))[0]
    assert waiting['后续试算运费（元）']==''
    assert waiting['试算差额（元，未核实）']==''
    assert waiting['试算状态']=='试算资料未齐'


def test_export_preserves_allocation_reason_and_treats_store_name_as_text(tmp_path):
    ws=Workspace(tmp_path)
    ws.record('s1','2026-06',payload(10,allocation_pending=[
        {'metric_id':'freight_cost','amount':-3,'reason':'reship_freight_basis'}]),[])
    data=build(ws,_model(),'2026-06',store_names={'s1':'=formula'})
    row=list(csv.DictReader(io.StringIO(export_csv(data).lstrip('\ufeff'))))[0]
    assert row['店铺']=="'=formula"
    assert row['商品分摊说明']==data['stores'][0]['allocation_reasons'][0]


@pytest.mark.parametrize('params',[{'period':'2026-13'},{'period':'2026-06','store_id':'other'},
                                  {'period':'2026-06','platform':'other'}])
def test_bad_scope_is_rejected(tmp_path,params):
    with pytest.raises(ValueError):build(Workspace(tmp_path),_model(),**params)


def test_api_export_is_bound_to_the_same_read_and_cannot_modify_finance(tmp_path):
    ws=Workspace(tmp_path);ws.record('s1','2026-06',payload(10),[])
    app=FastAPI();install(app,lambda:ws,_model,lambda:{'s1':'测试店'})
    client=TestClient(app);params={'period':'2026-06'}
    found=client.get('/api/freight-review',params=params).json()
    export='/api/freight-review/export.csv'
    assert client.get(export,params={**params,'fingerprint':found['fingerprint']}).status_code==200
    assert client.get(export,params={**params,'fingerprint':found['fingerprint'],'group':'unknown'}).status_code==400
    ws.record('s1','2026-06',payload(20),[])
    assert client.get(export,params={**params,'fingerprint':found['fingerprint']}).status_code==409
    assert client.post('/api/freight-review',json={}).status_code==405
