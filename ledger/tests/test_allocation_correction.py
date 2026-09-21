from copy import deepcopy
import hashlib
import json

import polars as pl
import pytest

from ledger.allocation_correction import preview,publish
from ledger.commission_registry import Registry
from ledger.workspace import Workspace,WorkspaceError
from ledger.storage_integrity import seal
from test_manual_cost import model
from test_commission_profit import _persist


def case(tmp_path):
    m=model();ws=Workspace(tmp_path);reg=Registry(tmp_path)
    a=reg.person_save({'name':'甲'},'test','test')['id'];b=reg.person_save({'name':'乙'},'test','test')['id']
    sid='taobao_mt9scjag';period='2026-06'
    data={'spine_row':[0,1],'order_id':['master']*2,'sub_order_id':['child1','child2'],
        'product_id':['p1','p2'],'product_name':['商品','赠品'],'person_id':[a,b],'person':['甲','乙'],
        'status':['distribute']*2,'share':[.05,.05],'total_rate':[.05,.05],
        'participation_sales':[50.,50.],'participation_gross':[40.,40.],
        'participation_profit':[35.,35.],'original_base':[35.,35.],'base':[35.,35.],'amount':[1.75,1.75]}
    details=pl.DataFrame(data)
    original=details.select('order_id','sub_order_id','product_id')
    current=original.with_columns(pl.Series('buyer_paid',[100.,0.]),pl.lit(0.).alias('refund_amount'))
    facts=pl.DataFrame({'metric_id':['trade_receipt','software_fee','goods_cost'],
        'major':['trade_receipt','software_fee',None],'link_key':['master']*3,
        'amount':[100.,-10.,-20.],'contribution':[100.,-10.,-20.], 'counted':[True]*3})
    summary={'base_node':'net_profit','base_total':70.,'on_loss':'deduct','total':3.5,
        'people':[{'person_id':a,'person':'甲','amount':1.75},{'person_id':b,'person':'乙','amount':1.75}],
        'products':[]}
    cid=_persist(reg,a,rows=data,run_id=1,store_id=sid,period=period);summary['calculation_id']=cid
    raw={'store_id':sid,'period':period,'can_close':True,'findings':[],'missing_sources':[],
         'commission':summary,'statement':[{'id':'net_profit','name':'利润','value':70.,'available':True}]}
    run=ws.record(sid,period,raw,[]);assert run==1
    facts.write_parquet(ws.facts_path(run));seal(ws.facts_path(run))
    manual=deepcopy(raw);manual['manual_payout']={'source_run_id':run}
    manual['commission'].update(total=3.33,manual_amounts_after_labor=True,manual_confirmed=True)
    manual['commission']['people'][0]['amount']=1.11;manual['commission']['people'][1]['amount']=2.22
    ws.close_period(sid,period,note='人工确认实发',expected_run_id=run,manual_result=manual,
                    manual_decision={'source_run_id':run,'payouts':[]},labor_cut='0.00')
    with reg.connect() as conn:sha=conn.execute('SELECT sha FROM calculation WHERE id=?',(cid,)).fetchone()[0]
    return m,ws,reg,sid,raw,facts,details,original,current,sha


def test_reallocation_changes_only_proven_income_and_platform_fee_parts(tmp_path):
    m,ws,reg,sid,raw,facts,details,original,current,sha=case(tmp_path)
    summary,updated,audit=preview(m,facts,details,original,current,raw['commission'])
    assert updated['participation_sales'].to_list()==[100.,0.]
    assert updated['participation_gross'].to_list()==[90.,-10.]
    assert updated['participation_profit'].to_list()==[80.,-10.]
    assert updated['amount'].to_list()==[4.,-.5]
    assert audit['verified_orders']==1 and audit['pending_orders']==[]
    assert all(float(v)==0 for v in audit['rounding_deltas'].values())
    assert details['participation_sales'].to_list()==[50.,50.]


def test_closed_correction_appends_audit_preserves_original_and_actual_payouts(tmp_path):
    m,ws,reg,sid,raw,facts,details,original,current,sha=case(tmp_path)
    before=ws.state(sid,'2026-06').result
    original_bytes=ws.facts_path(1).read_bytes()
    summary,updated,audit=preview(m,facts,details,original,current,raw['commission'])
    new_id=publish(ws,reg,1,summary,updated,audit,expected_calculation_sha=sha,by='test',reason='核实分配更正')
    assert new_id!=1
    after=ws.state(sid,'2026-06')
    assert after.closed and after.run_id==new_id
    assert after.result['statement']==before['statement']
    assert [p['amount'] for p in after.result['commission']['people']]==[1.11,2.22]
    assert ws.state_by_run(1).result==before
    assert json.loads(ws.conn.execute('SELECT result FROM run WHERE id=1').fetchone()[0])==raw
    assert ws.facts_path(1).read_bytes()==original_bytes==ws.facts_path(new_id).read_bytes()
    assert ws.conn.execute("SELECT count(*) FROM config_log WHERE kind='period-allocation-correction'").fetchone()[0]==1
    assert ws.facts_path(new_id).with_suffix('.allocation.parquet').exists()
    with pytest.raises(WorkspaceError,match='原结账版本已变化'):
        publish(ws,reg,1,summary,updated,audit,expected_calculation_sha=sha,by='test',reason='重复请求')


def test_no_guess_for_changed_membership_or_unprovable_old_ratio(tmp_path):
    m,ws,reg,sid,raw,facts,details,original,current,sha=case(tmp_path)
    _,updated,audit=preview(m,facts,details,original,current.head(1),raw['commission'])
    assert audit['verified_orders']==0 and len(audit['pending_orders'])==1
    assert updated['participation_sales'].to_list()==[50.,50.]
    with pytest.raises(WorkspaceError,match='原档案已有'):
        preview(m,facts,details,original.with_columns(pl.lit(.5).alias('alloc_ratio')),current,raw['commission'])


def test_managed_pool_tracks_reallocated_sales_without_counting_it_twice(tmp_path):
    m,ws,reg,sid,raw,facts,details,original,current,sha=case(tmp_path)
    details=details.with_columns(pl.Series('managed',[True,False]),pl.Series('managed_team_id',['team','']))
    summary,updated,audit=preview(m,facts,details,original,current,raw['commission'])
    assert summary['managed_sales']=={'team':100.0}
    assert sum(v['sales'] for v in summary['production_outputs'].values())==0
