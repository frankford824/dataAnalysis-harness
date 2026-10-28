"""Rejection assertions are useful evidence, never positive financial coverage."""
from datetime import datetime
import json

import polars as pl
import pytest

from conftest import MODELS,write_xlsx
from test_order_feed import _fixture,_write
from ledger import freight_experiments_inputs as frozen,replay_service
from ledger.commission_registry import Registry
from ledger.model.loader import load_model
from ledger.model.schema import (Check,ColumnBinding,Metric,Model,Overhead,ParseOptions,
    Platform,SourceContract,StatementNode,Store,Template,ValueExpr)
from ledger.order_feed import OrderFeed
from ledger.workspace import Workspace


def gap_bundle(tmp_path,*,store_count=1,overhead=True):
    """Explicit 20 targets / 19 priced: no total confirmation and no exemption."""
    full=load_model(MODELS/'cn-ecommerce')
    goods=full.metric('goods_cost').for_platform('taobao').model_copy(update={'by_platform':()})
    template=Template(id='rejection_order_source',name='合成订单原件',source='order_detail',
        match_columns=('主单','子单','商品','实付','创建时间','运单'),parse=ParseOptions(header_row=0),
        time_slots={'order_date':'order_time'},bindings=tuple(ColumnBinding(role=role,columns=(column,),kind=kind)
            for role,column,kind in [('order_id','主单','text'),('sub_order_id','子单','text'),
                ('product_id','商品','text'),('buyer_paid','实付','number'),('order_time','创建时间','time'),
                ('tracking_no','运单','text')]))
    model=Model(id='rejection-contract-fixture',name='合成拒绝合同',
        platforms=(Platform(id='taobao',name='淘宝',cost_pricing='required'),),
        stores=tuple(Store(id='s'+str(i),name='合成店'+str(i),platform='taobao') for i in range(1,store_count+1)),
        sources=(SourceContract(id='order_detail',name='原订单',is_spine=True,owner_role='shop_owner',cadence='monthly'),
            SourceContract(id='order_cost',name='原成本',owner_role='warehouse',cadence='monthly'),
            SourceContract(id='after_sales',name='原售后',owner_role='warehouse',cadence='monthly',required_for_close=False)),
        templates=(template,),metrics=(Metric(id='income',name='收入',source='order_detail',
            value=ValueExpr(op='sum',of=('buyer_paid',)),link={'key':'sub_order_id','to':'order.sub_order_id','grain':'order'}),goods),
        statement=(StatementNode(id='n_receipt',name='收入',level=2,headline='revenue',formula={'op':'add','of':['income']}),
            StatementNode(id='gross_profit',name='毛利',commission_base=True,is_total=True,formula={'op':'add','of':['income','goods_cost']}),
            StatementNode(id='net_profit',name='利润',commission_base=True,is_total=True,headline='profit',formula={'op':'add','of':['gross_profit']})),
        checks=(Check(id='goods_coverage',name='覆盖质量',kind='spine_coverage',metric='goods_cost',threshold=.95,blocking=True),),
        overheads=(Overhead(period='2026-09',amount=0,name='合成明确零兼职'),) if overhead else ())
    source=tmp_path/'source';feed_root=tmp_path/'feed';manifest=_fixture(feed_root)
    headers=[];items=[];costs=[];original=[];missing=None
    for index in range(20):
        internal=str(index+1);item_id=str(1001+index);main=str(3310000000000000000+index)
        child=str(int(main)+1000000000);day='2026-09-02';tracking='N'+internal
        headers.append({'order_id':internal,'online_order_no':main,'order_store_id':'10','order_time':day+' 10:00:00',
            'pay_time':day+' 10:01:00','ship_time':day+' 11:00:00','order_status_raw':'Sent',
            'paid_amount':'10','refund_amount':'0','tracking_no':tracking,'platform_order_ids':[main],
            'platform_order_identity_complete':True})
        items.append({'order_id':internal,'sub_order_id':item_id,'online_order_no':main,'order_store_id':'10',
            'sku_id':'SKU'+internal,'merchant_sku':'123456789001','shop_item_id':'123456789001','outer_sku':child,
            'product_name':'合成商品','quantity':'1','paid_amount':'10','line_amount':'10','refund_amount':'0',
            'tracking_no':tracking,'is_gift':False,'is_suspect':False})
        costs.append({'order_id':internal,'sub_order_id':item_id,'sku_id':'SKU'+internal,'quantity':'1',
            'unit_cost':None if index==19 else '2','cost_amount':None if index==19 else '2',
            'cost_source':None if index==19 else 'history','cost_status':'missing_price' if index==19 else 'priced',
            'cost_as_of':day,'pricing_evidence':json.dumps({'order_date':day})})
        original.append([main,child,'123456789001',10,datetime(2026,9,2,10),tracking])
        if index==19:missing=main
    for name,rows in [('orders.parquet',headers),('order_items.parquet',items),('order_costs.parquet',costs)]:
        manifest['objects'][name]=_write(feed_root,name,pl.from_dicts(rows,infer_schema_length=None))
    for name in ['after_sales.parquet','after_sale_items.parquet']:
        frame=pl.read_parquet(feed_root/manifest['objects'][name]['path']).clear();manifest['objects'][name]=_write(feed_root,name,frame)
    feed=OrderFeed(source,feed_root=feed_root)
    feed._install_snapshot(manifest,{'stores':[{'order_store_id':'10','ledger_store_id':'s1','mapping_status':'confirmed'}]},
        {'healthy':True},source_revision=10,source_latest_seq=10,revision_etag='fixed-synthetic')
    ws=Workspace(source)
    try:
        file=write_xlsx(tmp_path/'合成店1订单.xlsx',[['主单','子单','商品','实付','创建时间','运单'],*original]);ws.keep(file.name,file,'s1')
        bundle=tmp_path/'frozen'
        frozen.capture(source,feed_root,model,'2026-09',bundle,{'version':'synthetic-rejection-contract','categories':[],'mapping':{},'erp_mapping':{}})
    finally:ws.close()
    return bundle,missing


def fixed_seed():
    return {'people':[{'id':'p1','name':'合成人员'}],
        'members':[{'store_id':'s1','person_id':'p1','duty':'produce'}],
        'schemes':[{'store_id':'s1','product_id':'123456789001','body':{'segments':[{
            'valid_from':'2026-09-01','mode':'distribute','total_rate':'.05',
            'allocations':[{'person_id':'p1','rate':'.05','role':'运营','duty':'produce'}]}]}}],
        'policies':[{'body':{'base_node':'net_profit','wages':'skip_preview'},'effective_from':'2026-09-01'}]}


def observation(*,codes,run=True,outcome='pending',source='captured',failure=None):
    return {'run_present':run,'state':'open' if run else None,'outcome':outcome,'source_state':source,
        'checker_allowed':False,'reason_codes':sorted(codes),'failure':failure}


GAP_CODES={'required_cost_unresolved','profit_unknown','cost_decision_required','commission_incomplete'}


def gap_expected(*,failure=None,outcome='pending',source='captured'):
    return {'schema_version':1,'category':'incomplete_data','scopes':{'s1/2026-09':{
        'observation':observation(codes=GAP_CODES,failure=failure,outcome=outcome,source=source),
        'proved':{'statement':{'n_receipt':{'value':200,'available':True}},
            'can_close':False,'source_sync_pending':False,'commission_complete':False},
        'unknown':{'statement':{'gross_profit':{'value':None,'available':False},
            'net_profit':{'value':None,'available':False}}}}}}


def open_gap(tmp_path):
    bundle,missing=gap_bundle(tmp_path)
    return replay_service.Session(bundle,fixed_seed(),publication_scopes=['s1/2026-09']),missing


def test_approved_52_scope_enumerates_no_run_without_invented_financial_results(tmp_path):
    bundle,_=gap_bundle(tmp_path,store_count=52,overhead=False)
    keys=['s'+str(i)+'/2026-09' for i in range(1,53)]
    with replay_service.Session(bundle,{'people':[],'members':[],'schemes':[]},publication_scopes=keys) as session:
        snap=session.snapshot()
        assert snap['scopes']=={} and snap['proofs']=={}
        assert set(snap['observations'])==set(keys)
        expected={'schema_version':1,'category':'scope_coverage','scopes':{key:{
            'observation':observation(codes={'no_result','company_revenue_unknown','overhead_total_unknown'},
                run=False,outcome='not_started',source='not_attempted'),
            'proved':{},'unknown':{}} for key in keys}}
        result=session.check_rejection(expected)
        assert result['status']=='passed',result['differences']
        assert result['positive_scope_count']==result['source_rejection_scope_count']==0
        assert result['eligible_for_financial_release'] is False
        assert all(o['company']['total'] is None and o['company']['basis_total'] is None
                   and o['company']['income_complete'] is False for o in snap['observations'].values())
        del expected['scopes'][keys[-1]]
        with pytest.raises(replay_service.ReplayError,match='全部批准scope'):session.check_rejection(expected)


def test_95_percent_missing_required_amount_remains_unknown_and_rejected(tmp_path):
    session,_=open_gap(tmp_path)
    with session:
        session.recompute('s1');session.drain_pending()
        snap=session.snapshot();payload=session.ws.state('s1','2026-09').result
        assert payload['cost_coverage']['coverage']==pytest.approx(.95)
        assert payload['cost_coverage']['passed'] is True  # The quality line stays 95%.
        for node in ('gross_profit','net_profit'):
            assert snap['scopes']['s1/2026-09']['statement'][node]=={'value':None,'available':False}
        report=session.check_rejection(gap_expected())
        assert report['status']=='passed',report['differences']
        assert report['source_rejection_scope_count']==1
        assert report['ready_scope_count']==report['closed_scope_count']==report['positive_scope_count']==0
        assert report['eligible_for_financial_release'] is False
        with pytest.raises(replay_service.ReplayError,match='必需金额'):
            session.check({'scopes':snap['scopes']})
        wrong=gap_expected();wrong['scopes']['s1/2026-09']['proved']['statement']['n_receipt']['value']=199
        assert session.check_rejection(wrong)['status']=='failed'
        wrong=gap_expected();wrong['scopes']['s1/2026-09']['observation']['reason_codes'].remove('required_cost_unresolved')
        assert session.check_rejection(wrong)['status']=='failed'
        wrong=gap_expected();wrong['scopes']['s1/2026-09']['unknown']['statement']['net_profit']['value']=0
        with pytest.raises(replay_service.ReplayError,match='不能补零'):session.check_rejection(wrong)


def test_real_close_entry_expected_rejection_preserves_actual_checker_reasons(tmp_path):
    session,_=open_gap(tmp_path)
    with session:
        session.recompute('s1');session.drain_pending()
        expected_failure={'exception_type':'ReplayActionRejection','kind':'checker_rejected',
            'code':'business_action_rejected','reason_codes':sorted(GAP_CODES)}
        session.expect_failure('close_approved',{'store_id':'s1','period':'2026-09',
            'approval_reference':'synthetic-explicit-close','reason':'合成明确批准不能免除必需成本'},expected_failure)
        report=session.check_rejection(gap_expected(failure=expected_failure,outcome='rejected',source='rejected'))
        assert report['status']=='passed',report['differences']
        assert session.ws.state('s1','2026-09').state=='open'
        with session.registry.connect() as db:assert db.execute('SELECT count(*) FROM payout_confirmation').fetchone()[0]==0


def test_context_bound_approved_line_cost_completes_amount_without_lowering_quality_line(tmp_path):
    session,missing=open_gap(tmp_path)
    with session:
        session.recompute('s1');session.drain_pending()
        assert session.check_rejection(gap_expected())['status']=='passed'
        session.supplement('s1','2026-09',amount='2.00',reason='合成已批准原采购凭据',order_id=missing)
        session.recompute('s1');session.drain_pending()
        result=session.snapshot()['scopes']['s1/2026-09']
        assert result['statement']['net_profit']=={'value':160.0,'available':True}
        assert result['cost_line_total']==2 and result['can_close'] is True
        # A successful workflow cannot get rejection credit merely by its kind.
        assert session.check_rejection(gap_expected())['status']=='failed'


def test_explicit_approved_total_is_separate_from_unproved_95_percent_gap(tmp_path):
    from ledger import manual_cost
    session,_=open_gap(tmp_path)
    with session:
        session.recompute('s1');session.drain_pending();state=session.ws.state('s1','2026-09')
        result,decision=manual_cost.certified(session.model,state.result,state.run_id,
            {'goods':'40.00','dropship':'0.00','reshipment':'0.00'},
            [{'person_id':'p1','amount':'8.00'}],False,'合成已批准整期三类成本及逐人金额',labor_cut='0.00')
        session.ws.close_period('s1','2026-09',by='合成已批准财务',note='明确整期确认而非豁免缺口',
            expected_run_id=state.run_id,labor_cut='0.00',manual_result=result,manual_decision=decision)
        assert session.ws.state('s1','2026-09').closed
        assert session.ws.state('s1','2026-09').result['statement'][-1]['value']==160


def test_only_trusted_exact_service_failure_is_observed_not_unexpected_exceptions(tmp_path,monkeypatch):
    from ledger import service
    session,_=open_gap(tmp_path)
    with session:
        def fail(*args,**kwargs):
            return service.Recomputed(store_id='s1',failure={'kind':'source_unavailable','code':'fixed_source_unavailable','why':'合成来源拒绝'})
        monkeypatch.setattr(service,'recompute',fail)
        failure={'exception_type':'ReplayServiceFailure','kind':'source_unavailable','code':'fixed_source_unavailable'}
        session.expect_failure('recompute',{'store_id':'s1'},failure)
        observed=session.snapshot()['observations']['s1/2026-09']
        assert observed['outcome']=='failed' and observed['failure']==failure and not observed['run_present']
        def unexpected(*args,**kwargs):raise ValueError('unexpected implementation bug')
        monkeypatch.setattr(service,'recompute',unexpected)
        with pytest.raises(ValueError,match='unexpected implementation'):
            session.expect_failure('recompute',{'store_id':'s1'},failure)


def test_technical_failure_is_not_real_source_incompleteness_credit(tmp_path,monkeypatch):
    from ledger import service
    session,_=open_gap(tmp_path)
    with session:
        def fail(*args,**kwargs):return service.Recomputed(store_id='s1',failure={'kind':'maintenance','code':'code_source_drift'})
        monkeypatch.setattr(service,'recompute',fail)
        failure={'exception_type':'ReplayServiceFailure','kind':'maintenance','code':'code_source_drift'}
        session.expect_failure('recompute',{'store_id':'s1'},failure)
        expected={'schema_version':1,'category':'incomplete_data','scopes':{'s1/2026-09':{
            'observation':observation(codes={'no_result','company_revenue_unknown','recompute_pending'},
                run=False,outcome='failed',source='failed',failure=failure),'proved':{},'unknown':{}}}}
        with pytest.raises(replay_service.ReplayError,match='技术故障'):session.check_rejection(expected)


@pytest.mark.parametrize('value',[float('nan'),float('inf')])
def test_rejection_known_amount_still_rejects_nonfinite_values(value):
    with pytest.raises(replay_service.ReplayError,match='非有限'):
        replay_service._validate_rejection_financial({'statement':{'n_receipt':{'value':value,'available':True}}},unknown=False)


def test_partial_comparison_never_treats_absent_as_none_or_empty_people_as_wildcard():
    assert replay_service._partial_differences({'amount':None},{})
    assert replay_service._partial_differences({'people':{}},{'people':{'p1':10}})
    assert not replay_service._partial_differences({'known':2},{'known':2,'unasserted':None})


def test_supplement_total_known_empty_is_zero_but_unbound_saved_context_is_unknown(tmp_path):
    ws=Workspace(tmp_path/'local-closed-log-source')
    try:
        assert replay_service._posted_cost_line_total(ws,'s1','2026-09',None,{}) is None
        run=ws.record('s1','2026-09',{},[],evidence_ready=True)
        assert replay_service._posted_cost_line_total(ws,'s1','2026-09',run,{})==0
        assert replay_service._posted_cost_line_total(ws,'s1','2026-09',run,{'cost_line_posting':{}})==0
        with ws.conn:
            ws.conn.execute('''INSERT INTO cost_line_log(store_id,period,coverage_key,context_sha,source_run_id,
                action,amount,reason,at,by) VALUES(?,?,?,?,?,'save','2.00','synthetic saved source','2026-10-01','fixture')''',
                ('s1','2026-09','fixed-key','fixed-context',run))
        # A save exists, but nothing proves that this run actually posted it.
        assert replay_service._posted_cost_line_total(ws,'s1','2026-09',run,{}) is None
        with ws.conn:
            ws.conn.execute('''INSERT INTO automation_run_proof
                (run_id,dependency_sha,result_sha,evidence_sha,manifest_json,at) VALUES(?,?,?,?,?,?)''',
                (run,'fixed-dependency','fixed-result','fixed-evidence',json.dumps({'manual_revision':0}),'2026-10-01'))
        # Captured revision 0 proves this old run had no supplements; the new
        # 2.00 pending save must neither replace it nor become an unknown zero.
        assert replay_service._posted_cost_line_total(ws,'s1','2026-09',run,{})==0
        assert replay_service._posted_cost_line_total(ws,'s1','2026-09',run,{'cost_line_posting':{'total':2}})==2
    finally:ws.close()
