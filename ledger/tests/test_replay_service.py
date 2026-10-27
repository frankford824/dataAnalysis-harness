"""Service fixtures are deliberately separate from approved historical golden data."""
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import shutil
import socket

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import MODELS, write_xlsx
from test_order_feed import _fixture, _write
from ledger import freight_experiments_inputs as frozen
from ledger import replay_service
from ledger.commission_api import install
from ledger.model.loader import load_model
from ledger.model.schema import (Check,ColumnBinding,Metric,Model,ParseOptions,
    Overhead,Platform,SourceContract,StatementNode,Store,Template,ValueExpr)
from ledger.order_feed import OrderFeed
from ledger.workspace import Workspace


def segment(person,start='2026-06-01',end=''):
    return {'valid_from':start,'valid_to':end,'mode':'distribute','total_rate':'.05',
        'allocations':[{'person_id':person,'rate':'.05','role':'运营','duty':'produce'}]}


def seed():
    return {'people':[{'id':'p1','name':'甲'},{'id':'p2','name':'乙'}],
        'members':[{'store_id':s,'person_id':'p1','duty':'produce'} for s in ('s1','s2')],
        'schemes':[{'store_id':s,'product_id':p,'body':{'segments':[segment('p1')]}}
                   for s,p in [('s1','123456789001'),('s2','123456789002')]],
        'policies':[{'body':{'base_node':'net_profit','wages':'skip_preview'},'effective_from':'2026-06-01'}]}


@pytest.fixture
def bundle(tmp_path):
    full = load_model(MODELS/'cn-ecommerce')
    goods = full.metric('goods_cost').for_platform('taobao').model_copy(update={'by_platform':()})
    template = Template(id='replay_orders',name='固定订单原表',source='order_detail',
        match_columns=('主订单号','子订单号','宝贝ID','买家实付','下单时间','物流单号'),
        parse=ParseOptions(header_row=0),time_slots={'order_date':'order_time'},
        bindings=tuple(ColumnBinding(role=role,columns=(column,),kind=kind)
            for role,column,kind in [('order_id','主订单号','text'),('sub_order_id','子订单号','text'),
            ('product_id','宝贝ID','text'),('buyer_paid','买家实付','number'),
            ('order_time','下单时间','time'),('tracking_no','物流单号','text')]))
    model = Model(id='service-fixture',name='服务整链夹具',
        platforms=(Platform(id='taobao',name='淘宝',cost_pricing='required'),),
        stores=(Store(id='s1',name='甲店',platform='taobao'),Store(id='s2',name='乙店',platform='taobao')),
        sources=(SourceContract(id='order_detail',name='订单原表',is_spine=True,owner_role='shop_owner',cadence='monthly'),
            SourceContract(id='order_cost',name='订单成本',owner_role='warehouse',cadence='monthly'),
            SourceContract(id='after_sales',name='售后',owner_role='warehouse',cadence='monthly',required_for_close=False)),
        templates=(template,),
        metrics=(Metric(id='income',name='收入',source='order_detail',value=ValueExpr(op='sum',of=('buyer_paid',)),
                        link={'key':'sub_order_id','to':'order.sub_order_id','grain':'order'}),goods),
        statement=(StatementNode(id='n_receipt',name='销售收入',level=2,headline='revenue',formula={'op':'add','of':['income']}),
            StatementNode(id='gross_profit',name='毛利',commission_base=True,is_total=True,formula={'op':'add','of':['income','goods_cost']}),
            StatementNode(id='net_profit',name='利润',commission_base=True,is_total=True,headline='profit',formula={'op':'add','of':['gross_profit']})),
        checks=(Check(id='goods_coverage',name='成本覆盖',kind='spine_coverage',metric='goods_cost',threshold=.95,blocking=True),))
    model=model.model_copy(update={'overheads':tuple(Overhead(period=period,amount=0,name='合成夹具已确认零兼职')
        for period in ('2026-06','2026-07'))})
    source,feed_root = tmp_path/'source',tmp_path/'feed'
    manifest = _fixture(feed_root)
    headers,items,costs = [],[],[]
    raw = {'s1':[],'s2':[]}; missing = {}
    for store_index,(sid,name,shop,product) in enumerate((('s1','甲店','10','123456789001'),('s2','乙店','20','123456789002'))):
        for month in (6,7):
            for index in range(20):
                internal = str(len(headers)+1); item_id = str(1000+int(internal))
                main = str(3310000000000000000+store_index*100000+month*100+index)
                child = str(int(main)+1000000000); day=f'2026-{month:02d}-02'
                tracking='W'+internal
                headers.append({'order_id':internal,'online_order_no':main,'order_store_id':shop,
                    'order_time':day+' 10:00:00','pay_time':day+' 10:01:00','ship_time':day+' 11:00:00',
                    'order_status_raw':'Sent','paid_amount':'10','refund_amount':'0','tracking_no':tracking,
                    'platform_order_ids':[main],'platform_order_identity_complete':True})
                items.append({'order_id':internal,'sub_order_id':item_id,'online_order_no':main,
                    'order_store_id':shop,'sku_id':'SKU'+internal,'merchant_sku':product,'shop_item_id':product,
                    'outer_sku':child,'product_name':'商品','quantity':'1','paid_amount':'10',
                    'line_amount':'10','refund_amount':'0','tracking_no':tracking,'is_gift':False,'is_suspect':False})
                costs.append({'order_id':internal,'sub_order_id':item_id,'sku_id':'SKU'+internal,
                    'quantity':'1','unit_cost':None if index==19 else '2','cost_amount':None if index==19 else '2',
                    'cost_source':None if index==19 else 'history','cost_status':'missing_price' if index==19 else 'priced',
                    'cost_as_of':day,'pricing_evidence':json.dumps({'order_date':day})})
                raw[sid].append([main,child,product,10,datetime(2026,month,2,10),tracking])
                if index==19:missing[sid+'/'+day[:7]]=main
    for filename,rows in [('orders.parquet',headers),('order_items.parquet',items),('order_costs.parquet',costs)]:
        manifest['objects'][filename]=_write(feed_root,filename,pl.from_dicts(rows,infer_schema_length=None))
    for filename in ('after_sales.parquet','after_sale_items.parquet'):
        frame=pl.read_parquet(feed_root/manifest['objects'][filename]['path']).clear()
        manifest['objects'][filename]=_write(feed_root,filename,frame)
    mapping=[{'order_store_id':shop,'ledger_store_id':sid,'mapping_status':'confirmed'} for sid,shop in [('s1','10'),('s2','20')]]
    feed = OrderFeed(source,feed_root=feed_root)
    feed._install_snapshot(manifest,{'stores':mapping},{'healthy':True},source_revision=10,source_latest_seq=10,revision_etag='fixed')
    ws=Workspace(source)
    for sid,name in [('s1','甲店'),('s2','乙店')]:
        file=write_xlsx(tmp_path/(name+'订单原表.xlsx'),[
            ['主订单号','子订单号','宝贝ID','买家实付','下单时间','物流单号'],*raw[sid]])
        ws.keep(file.name,file,sid)
    frozen.capture(source,feed_root,model,'2026-06',tmp_path/'bundle',
        {'version':'fixture','categories':[],'mapping':{},'erp_mapping':{}})
    ws.close()
    return tmp_path/'bundle',missing


def expected_scope(profit=162,total=8.1,person='p1',confirmed=None,cost=0,can_close=False):
    return {'state':'open','statement':{
        'n_receipt':{'value':200.0,'available':True},
        'gross_profit':{'value':float(profit),'available':True},
        'net_profit':{'value':float(profit),'available':True}},
        'commission_total':total,'commission_people':{person:{'base':float(profit),'amount':total}},
        # These fixtures retain a missing-cost review (or unacknowledged work).
        # The old raw-engine True did not mean the final Checker allowed close.
        'commission_complete':True,'can_close':can_close,'source_sync_pending':False,
        'confirmed_total':confirmed,'confirmed_people':{person:confirmed} if confirmed is not None else {},
        'cost_line_total':cost,'allocation_pending_count':0}


def all_expected(**overrides):
    return {'scopes':{key:overrides.get(key,expected_scope()) for key in
        ('s1/2026-06','s1/2026-07','s2/2026-06','s2/2026-07')}}


def test_real_service_files_feed_registry_and_unchanged_recompute_are_verified(bundle):
    path,_missing=bundle
    with replay_service.Session(path,seed()) as session:
        session.recompute('s1');session.recompute('s2')
        report=session.check(all_expected())
        assert report['status']=='passed',report['differences']
        before=session.snapshot()
        session.recompute('s1')
        after=session.snapshot()
        assert before['scopes']==after['scopes']
        assert before['proofs']==after['proofs']
        assert len(report['checked_scopes'])==4
        with pytest.raises(replay_service.ReplayError,match='禁止网络'):
            socket.create_connection(('example.invalid',80))
    assert not session.root.exists()


def test_cost_supplement_confirmation_and_person_relation_changes_do_not_change_other_scopes(bundle):
    path,missing=bundle
    with replay_service.Session(path,seed()) as session:
        session.recompute('s1');session.recompute('s2')
        before=session.snapshot()['scopes']
        session.supplement('s1','2026-06',amount='2.00',reason='原采购凭据确认商品成本',order_id=missing['s1/2026-06'])
        session.recompute('s1')
        session.recompute('s2')
        after=session.snapshot()['scopes']
        assert after['s1/2026-06']['cost_line_total']==2
        assert after['s1/2026-06']['statement']['net_profit']['value']==160
        from ledger.storage_integrity import verified
        updated=session.ws.state('s1','2026-06')
        projection=session.ws.facts_path(updated.run_id).with_suffix('.operations.parquet')
        assert verified(projection)
        manual=pl.read_parquet(projection).filter(pl.col('source_id')=='__manual_order_cost__')
        assert manual.height==1 and manual['amount'].sum()==-2
        assert manual['order_id'].item()==missing['s1/2026-06']
        assert after['s1/2026-07']==before['s1/2026-07']
        assert after['s2/2026-06']==before['s2/2026-06'] and after['s2/2026-07']==before['s2/2026-07']
        session.confirm('s1','2026-06',[{'person_id':'p1','amount':'11.00'}],'独立核定服务夹具')
        assert session.check(all_expected(**{'s1/2026-06':expected_scope(160,8,'p1',11,2)}))['status']=='passed'
        session.replace_scheme('s1','123456789001',{'segments':[
            segment('p2',end='2026-07-01'),segment('p1',start='2026-07-01')]},'仅六月换人')
        session.recompute('s1')
        session.recompute('s2')
        assert session.check(all_expected(**{'s1/2026-06':expected_scope(160,8,'p2',None,2)}))['status']=='passed'
        with session.registry.connect() as db:
            assert db.execute('SELECT count(*) FROM payout_confirmation').fetchone()[0]==1
            saved=json.loads(db.execute('SELECT payouts_json FROM payout_confirmation').fetchone()[0])
            assert saved[0]['person_id']=='p1' and saved[0]['amount']==11


def test_api_report_export_and_service_snapshot_share_real_runs(bundle):
    path,_missing=bundle
    with replay_service.Session(path,seed()) as session:
        session.recompute('s1');session.recompute('s2')
        session.confirm('s1','2026-06',[{'person_id':'p1','amount':'11.00'}],'已批准夹具金额')
        app=FastAPI();install(app,lambda:session.ws,lambda:session.model)
        client=TestClient(app)
        report=client.post('/api/commission-v2/reports/query',json={'start':'2026-06','end':'2026-07'}).json()
        assert len(report['run_ids'])==4
        assert {r['finance_run'] for r in report['coverage']}=={p['run_id'] for p in session.snapshot()['proofs'].values()}
        person=next(p for p in report['people'] if p['person_id']=='p1')
        assert person['confirmed_amount']==11 and person['confirmation_state']=='partial'
        export=client.post('/api/commission-v2/export/reports/store_people',json={
            'start':'2026-06','end':'2026-07','presentation':True,'run_ids':report['run_ids'],'fingerprint':report['fingerprint']})
        assert export.status_code==200,export.text
        assert '已核定实发' in export.text and '11' in export.text


def test_missing_expected_scope_or_zero_instead_of_unknown_never_passes(bundle):
    path,_missing=bundle
    with replay_service.Session(path,seed()) as session:
        session.recompute('s1');session.recompute('s2')
        missing=all_expected();del missing['scopes']['s2/2026-07']
        report=session.check(missing)
        assert report['status']=='failed' and any('s2/2026-07' in d['path'] for d in report['differences'])
        wrong=all_expected();wrong['scopes']['s1/2026-06']['confirmed_total']=0
        assert session.check(wrong)['status']=='failed'
        with pytest.raises(replay_service.ReplayError,match='禁止自动记录'):
            session.check({})


def test_real_corpus_missing_materials_and_nonfinite_expected_fail_closed(tmp_path):
    with pytest.raises(replay_service.ReplayError,match='缺失'):
        replay_service.execute({'schema_version':1,'case_id':'real-202606','kind':'historical_golden',
            'approval_reference':'approved-golden-1','bundle':str(tmp_path/'absent'),'expected':{}})
    with pytest.raises(replay_service.ReplayError,match='非有限'):
        replay_service.differences({'amount':float('nan')},{'amount':0})
    assert replay_service.differences({'count':True},{'count':1})


def test_fully_supplemented_service_fixture_is_eligible_for_actual_month_close(bundle):
    from ledger.automation_checker import check
    path,missing=bundle
    with replay_service.Session(path,seed()) as session:
        session.drain_pending()
        for scope,order in missing.items():
            sid,period=scope.split('/')
            session.supplement(sid,period,amount='2.00',reason='合成原采购凭据已逐单审核',order_id=order)
        session.drain_pending()
        for scope in sorted(missing):
            sid,period=scope.split('/');state=session.ws.state(sid,period)
            assert state.result['cost_review']['requires_human'] is False
            checked=check(session.ws,sid,period,model=session.model,expected_run_id=state.run_id)
            assert checked['allowed'],checked['issues']
            closed=session.ws.close_period(sid,period,by='replay:approved-fixture',note='正向月结闭包',
                expected_run_id=state.run_id,model=session.model)
            assert closed.closed and closed.labor_cut=='0.00'
            assert session.snapshot()['scopes'][scope]['can_close'] is False


def changed_bundle(bundle,target,mutate):
    path,_missing=bundle
    shutil.copytree(path,target)
    body=json.loads((target/'model.json').read_text())
    mutate(body)
    (target/'model.json').write_text(json.dumps(body,ensure_ascii=False))
    from ledger.freight_trial_inputs import digest
    model=Model.model_validate(body)
    inputs=json.loads((target/'inputs.json').read_text());inputs['model_hash']=digest(json.loads(model.model_dump_json()))
    (target/'inputs.json').write_text(json.dumps(inputs,ensure_ascii=False));frozen.seal(target)
    return target


def workspace_source(tmp_path,decisions,*,fixture=True):
    body={'schema_version':1,'kind':'service_fixture_workspace_decisions' if fixture else 'approved_workspace_decisions',
          'approval_reference':'fixture-approved-finance' if fixture else 'approval/archived-finance', 'decisions':decisions}
    path=tmp_path/'workspace-decisions.json';path.write_text(json.dumps(body,ensure_ascii=False))
    return {'path':str(path.resolve()),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'approval_reference':body['approval_reference']}


def overhead_decisions():
    return [{'period':period,'store_id':'','kind':'overhead_total','values':{'amount':'10.00'},
             'reason':'固定夹具原始人工决定明确每月10元，不补未知为0'} for period in ('2026-06','2026-07')]


def complete_costs(session,missing,store_ids=('s1','s2')):
    session.drain_pending()
    for scope,order in missing.items():
        sid,period=scope.split('/')
        if sid in store_ids:session.supplement(sid,period,amount='2.00',reason='固定夹具逐单已审核成本',order_id=order)
    session.drain_pending()


def test_final_snapshot_refuses_local_ready_when_another_operating_store_has_no_revenue(bundle):
    path,missing=bundle
    fixed=seed()
    fixed['members']=[r for r in fixed['members'] if r['store_id']=='s1']
    fixed['schemes']=[r for r in fixed['schemes'] if r['store_id']=='s1']
    with replay_service.Session(path,fixed) as session:
        complete_costs(session,missing,('s1',))
        assert session.ws.latest_run('s2','2026-06') is None
        snapshot=session.snapshot()
        for period in ('2026-06','2026-07'):
            key='s1/'+period
            assert snapshot['proofs'][key]['engine_can_close'] is True
            assert snapshot['scopes'][key]['can_close'] is False
            assert 'company_revenue_unknown' in snapshot['proofs'][key]['close_issue_codes']


def test_final_snapshot_refuses_unknown_overhead_until_approved_nonzero_source_is_restored(bundle,tmp_path):
    path=changed_bundle(bundle,tmp_path/'no-overhead',lambda body:body.update(overheads=[]))
    _original,missing=bundle
    reference=workspace_source(tmp_path,overhead_decisions())
    with replay_service.Session(path,seed()) as session:
        complete_costs(session,missing)
        before=session.snapshot()
        assert all(v['engine_can_close'] for v in before['proofs'].values())
        assert all(not v['can_close'] for v in before['scopes'].values())
        assert all('overhead_total_unknown' in v['close_issue_codes'] for v in before['proofs'].values())
        receipt=session.restore_workspace_decisions(reference,fixture=True)
        after=session.snapshot()
        assert all(v['can_close'] for v in after['scopes'].values())
        assert receipt['source_sha256']==reference['sha256'] and receipt['offline_only'] is True
        assert after['workspace_decisions']==receipt
        assert session.ws.conn.execute("SELECT count(*) FROM automation_authorization WHERE identity_kind='human' AND action='decision.apply'").fetchone()[0]==2
        for key in before['scopes']:
            assert before['scopes'][key]['statement']==after['scopes'][key]['statement']
        Path(reference['path']).write_text('changed approved material')
        with pytest.raises(replay_service.ReplayError,match='SHA'):session.snapshot()


def test_approved_scope_uses_real_decision_rules_without_fake_revenue_or_zero_overhead(bundle,tmp_path):
    def add_store(body):
        body['stores'].append(Store(id='s3',name='明确停业店',platform='taobao').model_dump(mode='json'))
        body['overheads']=[]
    path=changed_bundle(bundle,tmp_path/'three-store-model',add_store)
    _old,missing=bundle
    reference=workspace_source(tmp_path,[{'period':'2026-06','store_id':'s3','kind':'store_scope',
        'values':{'included':False,'valid_from':'2026-06','valid_until':'2026-07'},'reason':'已批准两月未营业'},*overhead_decisions()])
    with replay_service.Session(path,seed()) as session:
        complete_costs(session,missing)
        before=session.snapshot()
        assert all(not row['can_close'] for row in before['scopes'].values())
        session.restore_workspace_decisions(reference,fixture=True)
        from ledger.automation_checker import company_basis
        for period in ('2026-06','2026-07'):
            company=company_basis(session.ws,session.model,period)
            assert company['total']=='10.00' and company['missing_store_ids']==[]
            assert {r['store_id'] for r in company['basis']}=={'s1','s2'}
            assert session.ws.latest_run('s3',period) is None
        assert all(row['can_close'] for row in session.snapshot()['scopes'].values())


def test_workspace_source_cannot_import_authority_or_write_an_actual_workspace(tmp_path):
    reference=workspace_source(tmp_path,[{'period':'2026-06','store_id':'s1','kind':'rule_grant',
        'values':{},'reason':'拒绝扩大回放权限'}])
    with pytest.raises(replay_service.ReplayError,match='只允许'):replay_service.load_workspace_decisions(reference,fixture=True)
    reference=workspace_source(tmp_path,overhead_decisions())
    ws=Workspace(tmp_path/'not-a-disposable-replay')
    from ledger.commission_registry import Registry
    try:
        with pytest.raises(replay_service.ReplayError,match='仅允许'):
            replay_service.restore_workspace_decisions(ws,Registry(ws.root),Model(id='t',name='t'),reference,fixture=True)
        assert ws.conn.execute('SELECT count(*) FROM automation_decision').fetchone()[0]==0
    finally:ws.close()


def test_descriptor_can_consume_real_queue_become_ready_then_close_via_original_human_gate(bundle):
    path,missing=bundle
    steps=[{'operation':'drain_pending'},*[{'operation':'supplement','store_id':key.split('/')[0],
        'period':key.split('/')[1],'amount':'2.00','reason':'已批准夹具逐单原成本','order_id':order}
        for key,order in sorted(missing.items())],{'operation':'drain_pending'}]
    expected=all_expected(**{key:expected_scope(160,8,cost=2,can_close=True) for key in missing})
    case={'schema_version':1,'case_id':'complete-service-fixture','kind':'service_fixture',
        'approval_reference':'fixture-approved-close','bundle':str(path),'registry':seed(),
        'steps':steps,'expected':expected}
    ready=replay_service.execute(case)
    assert ready['status']=='passed',ready['differences']
    assert all(row['can_close'] for row in ready['actual']['scopes'].values())
    assert any(row['operation']=='commission_manager.once' and row['remaining_pending']==0 for row in ready['actual']['trace'])
    from copy import deepcopy
    closed_case=deepcopy(case)
    closed_case['steps'].append({'operation':'close_approved','store_id':'s1','period':'2026-06',
        'approval_reference':case['approval_reference'],'reason':'明确批准本合成案例离线月结验证'})
    closed_case['expected']['scopes']['s1/2026-06'].update(state='closed',can_close=False)
    closed=replay_service.execute(closed_case)
    assert closed['status']=='passed',closed['differences']
    final=closed['actual']['scopes']['s1/2026-06']
    assert final['state']=='closed' and final['can_close'] is False
    assert final['statement']['net_profit']['value']==160 and final['commission_total']==8 and final['cost_line_total']==2
    assert any(row['operation']=='close.confirm' and row['approval_reference']==case['approval_reference']
               and row['input_sha'] for row in closed['actual']['trace'])


def test_descriptor_cannot_close_without_case_approval_or_with_unfinished_conditions(bundle):
    path,_missing=bundle
    case={'schema_version':1,'case_id':'unfinished-service-fixture','kind':'service_fixture','bundle':str(path),
        'registry':seed(),'steps':[{'operation':'close_approved','store_id':'s1','period':'2026-06',
        'approval_reference':'unbound','reason':'不能以声明绕过审批'}],'expected':all_expected()}
    with pytest.raises(replay_service.ReplayError,match='批准引用'):replay_service.execute(case)
    case['approval_reference']='unbound'
    with pytest.raises(replay_service.ReplayError,match='未通过原有Checker'):replay_service.execute(case)


def test_descriptor_restores_bound_scope_and_nonzero_overhead_before_real_service_steps(bundle,tmp_path):
    def changed(body):
        body['stores'].append(Store(id='s3',name='已批准停业店',platform='taobao').model_dump(mode='json'))
        body['overheads']=[]
    path=changed_bundle(bundle,tmp_path/'approved-scope-bundle',changed)
    _old,missing=bundle
    reference=workspace_source(tmp_path,[{'period':'2026-06','store_id':'s3','kind':'store_scope',
        'values':{'included':False,'valid_from':'2026-06','valid_until':'2026-07'},'reason':'本合成案例明确批准两月停业'},*overhead_decisions()])
    case={'schema_version':1,'case_id':'approved-workspace-service-fixture','kind':'service_fixture',
        'approval_reference':'fixture-explicit-decisions','bundle':str(path),'registry':seed(),'workspace_decisions':reference,
        'steps':[{'operation':'drain_pending'},*[{'operation':'supplement','store_id':key.split('/')[0],
            'period':key.split('/')[1],'amount':'2.00','reason':'固定原件逐单审核成本','order_id':order}
            for key,order in sorted(missing.items())],{'operation':'drain_pending'}],
        'expected':all_expected(**{key:expected_scope(160,8,cost=2,can_close=True) for key in missing})}
    result=replay_service.execute(case)
    assert result['status']=='passed',result['differences']
    assert result['actual']['workspace_decisions']['source_sha256']==reference['sha256']
    trace=result['actual']['trace']
    assert trace[0]['operation']=='workspace_decisions.restore'
    assert len(result['actual']['scopes'])==4 and all(row['can_close'] for row in result['actual']['scopes'].values())
