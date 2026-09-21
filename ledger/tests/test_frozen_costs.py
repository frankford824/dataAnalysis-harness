import csv
import hashlib
import io
import json
from decimal import Decimal

import polars as pl
import pytest
from fastapi.testclient import TestClient

from ledger import frozen_costs, manual_cost, view
from ledger.commission_registry import Registry
from ledger.workspace import Workspace, WorkspaceError
from test_manual_cost import model, result


def facts(rows):
    return pl.DataFrame([{'metric_id':mid,'amount':amount,'contribution':amount if counted else 0.,
        'counted':counted,'linked':counted,'link_key':'5120605334958025518',
        'order_id':'5120605334958016336','major':None,'minor':None,'subject':None,
        'file_name':'原始订单.csv','sheet':'订单','row_no':i+2,'source_note':None}
        for i,(mid,amount,counted) in enumerate(rows)])


def decision(goods='65602.51',dropship='0',reshipment='0',lines=None):
    confirmed={'goods':goods,'dropship':dropship,'reshipment':reshipment}
    return {'confirmed':confirmed,'line_reviews':lines or [],'reason':'人工确认'}, {'confirmed':confirmed}


def test_frozen_113_lines_reconcile_export_and_drill_without_changing_sources():
    m=model()
    raw=facts([('goods_cost',-42232.7914,True),('goods_cost',-99.,False),('freight_cost',-8.,True)])
    before=raw.clone()
    values=['22784']+['5.23']*111+['5.19']
    lines=[{'coverage_key':str(5120605334958025518+i),'order_id':'5120605334958016336',
            'amount':amount,'reason':'人工','revision':i+1} for i,amount in enumerate(values)]
    d,s=decision(lines=lines)
    projected=frozen_costs.append(raw,m,d,s,run_id=197429,at='2026-09-16T17:24:29+08:00',by='人工操作')
    assert raw.equals(before)
    exported=list(csv.DictReader(io.StringIO(view.fees_csv(projected,m))))
    supplements=[r for r in exported if r['记录类型']=='结账人工补录']
    assert len(supplements)==113
    assert sum(Decimal(r['进账']) for r in supplements)==Decimal('-23369.72')
    assert all(r['结账核算记录']=='197429' for r in exported)
    goods=[r for r in exported if r['科目']=='商品成本']
    assert sum(Decimal(r['进账']) for r in goods)==Decimal('-65602.5100000000')
    assert any(r['是否进账']=='否' and Decimal(r['金额'])==-99 and Decimal(r['进账'])==0 for r in goods)
    assert any(r['记录类型']=='结账舍入调整' for r in goods)
    assert supplements[0]['订单号']=='="5120605334958025518"'
    drilled=view.drill(projected,m,'n_goods',value=-65602.51)
    assert drilled['source_total']==drilled['value']==-65602.51
    assert any(r['file']=='结账人工成本（冻结快照）' for r in drilled['by_file'])
    only_manual=view.drill(projected,m,'n_goods',file='结账人工成本（冻结快照）')
    assert all(r['record_type']!='原始流水' for r in only_manual['sample'])


def test_returns_and_all_cost_components_match_the_frozen_statement():
    m=model()
    raw=facts([('goods_cost',-100.,True),('goods_return_cost',10.,True),
               ('dropship_cost',-20.,True),('reshipment_cost',-30.,True)])
    d,s=decision('110','15','40',[{'coverage_key':'x','amount':'5'}])
    projected=frozen_costs.append(raw,m,d,s,run_id=1)
    for mid,target in [('goods_cost',-110),('goods_return_cost',0),('dropship_cost',-15),('reshipment_cost',-40)]:
        assert projected.filter(pl.col('metric_id')==mid)['contribution'].sum()==target
    assert view.drill(projected,m,'g_goods')['source_total']==-165
    assert projected.filter(pl.col('record_type')=='结账总额调整').height==4


@pytest.mark.parametrize('value',['0','-20','600'])
def test_zero_negative_and_unchanged_cost_confirmation(value):
    m=model();raw=facts([('goods_cost',-600.,True)])
    d,s=decision(value)
    projected=frozen_costs.append(raw,m,d,s,run_id=1)
    assert view.drill(projected,m,'n_goods')['source_total']==-float(value)


def test_inconsistent_snapshot_and_duplicate_lines_fail_closed():
    m=model();raw=facts([('goods_cost',-1.,True)])
    d,s=decision('2')
    with pytest.raises(WorkspaceError,match='快照不完整'):
        frozen_costs.append(raw,m,d,{'confirmed':{'goods':'999'}},run_id=1)
    d['line_reviews']=[{'coverage_key':'a','amount':'1'}]*2
    with pytest.raises(WorkspaceError,match='唯一订单键'):
        frozen_costs.append(raw,m,d,s,run_id=1)
    d,s=decision('2')
    d['observed']={'goods':9}
    with pytest.raises(WorkspaceError,match='原始成本不一致'):
        frozen_costs.append(raw,m,d,s,run_id=1)


def test_api_uses_closed_snapshot_not_later_supplements_or_new_run(tmp_path,monkeypatch):
    import ledger.api as api
    ws=Workspace(tmp_path);m=model();reg=Registry(tmp_path)
    person=reg.person_save({'name':'甲'},'test','test')
    raw=result(m,person);sid=raw['store_id'];period='2026-06'
    run=ws.record(sid,period,raw,[])
    facts([('goods_cost',-600.,True)]).write_parquet(ws.facts_path(run))
    original_sha=hashlib.sha256(ws.facts_path(run).read_bytes()).hexdigest()
    confirmed,audit=manual_cost.certified(m,raw,run,{'goods':'700','dropship':'0','reshipment':'0'},
        [{'person_id':person['id'],'amount':'10'}],False,'已确认')
    audit['line_reviews']=[{'coverage_key':'5120605334958025518','order_id':'5120605334958016336','amount':'100','reason':'冻结时补录','revision':1}]
    ws.close_period(sid,period,note='已确认',expected_run_id=run,manual_result=confirmed,manual_decision=audit)
    with ws.conn as conn:
        conn.execute('INSERT INTO cost_line_log(store_id,period,coverage_key,context_sha,source_run_id,action,amount,reason,at,by) VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (sid,period,'5120605334958025518','x'*64,run,'save','9999','后续修改','later','test'))
    ws.reopen_period(sid,period,note='后续核算')
    new=ws.record(sid,period,raw,[])
    facts([('goods_cost',-888.,True)]).write_parquet(ws.facts_path(new))
    monkeypatch.setattr(api,'workspace',lambda:ws)
    monkeypatch.setattr(api,'_model',lambda:m)
    client=TestClient(api.app)
    response=client.get(f'/api/runs/{run}/fees.csv')
    assert response.status_code==200,response.text
    rows=list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert sum(Decimal(r['进账']) for r in rows)==-700
    assert sum(r['记录类型']=='结账人工补录' for r in rows)==1
    assert '9999' not in response.text
    assert '已含本次结账冻结的人工成本' in response.text
    drilled=client.get(f'/api/runs/{run}/drill/n_goods').json()
    assert drilled['source_total']==drilled['value']==-700
    newer=client.get(f'/api/runs/{new}/fees.csv')
    assert newer.status_code==200
    assert '结账人工补录' not in newer.text
    assert hashlib.sha256(ws.facts_path(run).read_bytes()).hexdigest()==original_sha
    assert json.loads(ws.conn.execute('SELECT result FROM run WHERE id=?',(run,)).fetchone()[0])==raw
    ws.close()
