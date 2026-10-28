"""Synthetic publication-boundary tests; never independent financial goldens."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import hashlib
import json
from pathlib import Path

import pytest
import polars as pl

from ledger import publication_scope as ps, service, recompute_reuse, automation_actions as actions
from ledger.commission_manager import Manager
from ledger.commission_registry import Registry
from ledger.engine.runtime import Ingestion, run
from ledger.model.loader import load_model
from ledger.model.schema import Model, Platform, SourceContract, Store, StatementNode, Metric, ValueExpr, Overhead
from ledger.workspace import Workspace
from conftest import MODELS
from test_reship_period_and_cost_drill import item


def configure(monkeypatch, root, pairs=(('s1','2026-09'),)):
    path=root.parent/(root.name+'-scope.json')
    body={'schema':ps.SCHEMA,'kind':'automatic_publication','workspace_root':str(root.resolve()),
          'approval_reference':'synthetic-explicit-scope-approval',
          'scopes':[{'store_id':sid,'period':period} for sid,period in pairs]}
    path.write_text(json.dumps(body));monkeypatch.setenv(ps.FILE_ENV,str(path));monkeypatch.setenv(ps.SHA_ENV,hashlib.sha256(path.read_bytes()).hexdigest())
    return path,body


@pytest.mark.parametrize('pairs', [[],[('s1','*')],[('*','2026-09')],[('s1','2026-09'),('s1','2026-09')]])
def test_unbounded_or_duplicate_scope_is_never_automatic_default(tmp_path,monkeypatch,pairs):
    root=tmp_path/'workspace';root.mkdir();configure(monkeypatch,root,pairs)
    with pytest.raises(ps.PublicationScopeError):ps.load_automatic(root)


def test_missing_scope_and_service_identity_none_cannot_fall_back_to_manual(tmp_path,monkeypatch):
    from ledger import write_auth
    monkeypatch.delenv(ps.FILE_ENV,raising=False);monkeypatch.delenv(ps.SHA_ENV,raising=False)
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    ws=Workspace(tmp_path/'workspace');model=Model(id='scope-fixture',name='scope-fixture',stores=(Store(id='s1',name='shop',platform='taobao'),))
    with pytest.raises(ps.PublicationScopeError):Manager(lambda:ws,lambda:model).once()
    manager=Manager(lambda:ws,lambda:model);manager.start()
    assert manager.thread is None and manager.last_error
    token=write_auth._identity.set({'id':'worker','kind':'service','authenticated':True})
    try:
        result=service.recompute(ws,model,model.store('s1'),publication_scope=None)
    finally:write_auth._identity.reset(token)
    assert result.failure['code']=='publication_scope_unverified'
    assert not ws.conn.execute('SELECT 1 FROM run').fetchone()
    manual=service.recompute(ws,model,model.store('s1'))
    assert manual.failure and manual.failure.get('code')!='publication_scope_unverified'


@pytest.mark.parametrize('change',['bytes','environment','workspace'])
def test_loaded_scope_is_bound_and_never_silently_reloaded(tmp_path,monkeypatch,change):
    root=tmp_path/'workspace';root.mkdir();path,body=configure(monkeypatch,root)
    scope=ps.load_automatic(root)
    if change=='bytes':path.write_text(json.dumps({**body,'scopes':[{'store_id':'s1','period':'2026-08'}]}))
    elif change=='environment':monkeypatch.setenv(ps.SHA_ENV,'f'*64)
    with pytest.raises(ps.PublicationScopeError):scope.require('s1','2026-09',workspace_root=tmp_path/'other' if change=='workspace' else root)


def test_scope_does_not_infer_a_month_from_the_clock_or_pricing_day(tmp_path,monkeypatch):
    root=tmp_path/'workspace';root.mkdir();configure(monkeypatch,root)
    scope=ps.load_automatic(root)
    assert scope.allows('s1','2026-09') and not scope.allows('s1','2026-10') and not scope.allows('s1','2026-05')
    with pytest.raises(ps.PublicationScopeError):scope.require('s1','2026-05')
    with pytest.raises(ps.PublicationScopeError):ps.PublicationScope.for_isolated('/home/not-a-temporary-financial-workspace',[('s1','2026-09')],approval_reference='fake')


def case(ws,sid,period):
    cid=hashlib.sha256((sid+'/'+period).encode()).hexdigest()[:32]
    ws.conn.execute('INSERT INTO automation_case(id,store_id,period,status,stage,input_sha,run_id,issues_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
        (cid,sid,period,'processing','processing','fixed-old',None,'[]','old-time'))
    return cid


def attempt(ws,cid,sid,label):
    ws.conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',(label,cid,label,'old-input','dispatch_pending',
        json.dumps({'store_id':sid,'fingerprint':'automation:'+hashlib.sha256(label.encode()).hexdigest()}),'old-time'))


def table_rows(conn,table,where='',args=()):
    return [dict(r) for r in conn.execute('SELECT * FROM '+table+(' WHERE '+where if where else '')+' ORDER BY rowid',args)]


def test_dispatch_retries_and_checker_sync_do_not_mutate_other_periods_or_stores(tmp_path,monkeypatch):
    from test_commission import _model
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    ws=Workspace(tmp_path/'workspace');registry=Registry(ws.root)
    model=_model(stores=(Store(id='s1',name='A',platform='taobao'),Store(id='s2',name='B',platform='taobao')))
    with ws.transaction():
        old=case(ws,'s1','2026-08');current=case(ws,'s1','2026-09');other=case(ws,'s2','2026-09')
        attempt(ws,old,'s1','old-attempt');attempt(ws,current,'s1','new-attempt');attempt(ws,other,'s2','other-attempt');attempt(ws,'missing-case','s1','orphan')
    before=table_rows(ws.conn,'automation_case','id IN (?,?)',(old,other))
    configure(monkeypatch,ws.root);scope=ps.load_automatic(ws.root,model)
    with ps.bind(scope,ws.root):
        assert actions.deliver(ws)==1
        assert actions.deliver(ws)==0
        actions.sync_month(ws,model,'2026-09')
        actions.sync_month(ws,model,'2026-08')
    assert table_rows(ws.conn,'automation_case','id IN (?,?)',(old,other))==before
    states={r['id']:r['state'] for r in ws.conn.execute('SELECT id,state FROM automation_attempt')}
    assert states=={'old-attempt':'dispatch_pending','new-attempt':'queued','other-attempt':'dispatch_pending','orphan':'dispatch_pending'}
    assert len(list((ws.root/'publication-holds').glob('*.json')))==3
    with registry.connect() as conn:assert [r['store_id'] for r in conn.execute('SELECT store_id FROM pending')]==['s1']


def test_old_token_and_valid_old_rule_do_not_expand_the_rollout_scope(tmp_path,monkeypatch):
    from ledger.automation_deterministic import approved_rule,RULE,RULE_SHA
    from test_commission import _model
    ws=Workspace(tmp_path/'workspace');model=_model();stamp=datetime.now(timezone.utc)
    human={'id':'finance','kind':'human','authenticated':True}
    plan=actions.decision_preview(ws,model,request_id='old-grant-preview',store_id='s1',period='2026-05',kind='rule_grant',
        values={'rule_id':RULE,'rule_sha':RULE_SHA,'enabled':True,'valid_from':(stamp-timedelta(days=1)).isoformat(),'valid_until':(stamp+timedelta(days=1)).isoformat()},reason='synthetic old existing approval',actor=human)
    actions.decision_apply(ws,model,request_id='old-grant-apply',proposal_id=plan['proposal_id'],proposal_sha=plan['proposal_sha'],input_sha=plan['input_sha'],actor=human)
    configure(monkeypatch,ws.root)
    with pytest.raises(PermissionError,match='自动发布范围'):approved_rule(ws,'s1','2026-05')


def input_fixture(tmp_path,monkeypatch):
    """Full cross-month rows: July unknown, August original, September events."""
    builtin=load_model(MODELS/'cn-ecommerce')
    goods=builtin.metric('goods_cost').for_platform('taobao').model_copy(update={'by_platform':()})
    reship=builtin.metric('reshipment_cost').for_platform('taobao').model_copy(update={'by_platform':()})
    income=Metric(id='income',name='income',source='order_detail',value=ValueExpr(op='sum',of=('buyer_paid',)),link={'key':'sub_order_id','to':'order.sub_order_id'})
    refund=Metric(id='refund',name='refund',source='settlement',value=ValueExpr(op='sum',of=('amount',)),posting_basis='transaction',time_basis='settle_date',link={'key':'sub_order_id','to':'order.sub_order_id'})
    model=Model(id='scope-fixture',name='synthetic publication boundary',platforms=(Platform(id='taobao',name='TB',cost_pricing='required'),),
        stores=(Store(id='s1',name='shop',platform='taobao'),Store(id='s2',name='other',platform='taobao')),
        sources=(SourceContract(id='order_detail',name='orders',is_spine=True,owner_role='shop_owner',cadence='monthly'),SourceContract(id='order_cost',name='costs',owner_role='warehouse',cadence='monthly'),SourceContract(id='settlement',name='refund',owner_role='finance',cadence='monthly')),
        metrics=(income,goods,reship,refund),statement=(StatementNode(id='n_receipt',name='income',headline='revenue',formula={'op':'add','of':['income']}),StatementNode(id='gross_profit',name='gross',formula={'op':'add','of':['income','goods_cost','reshipment_cost','refund']}),StatementNode(id='net_profit',name='profit',headline='profit',commission_base=True,formula={'op':'add','of':['gross_profit']})))
    orders=[dict(order_id='O'+str(m),sub_order_id='C'+str(m),product_id='123456789001',buyer_paid=100.,store_name='shop',order_time=datetime(2026,m,2),order_type='销售订单',order_state='Sent',tracking_no='W'+str(m)) for m in (7,8,9)]
    orders.append({**orders[1],'sub_order_id':'R9','buyer_paid':0.,'order_time':datetime(2026,9,12),'order_type':'补发订单'})
    costs=[{**o,'original_order_id':o['order_id'],'internal_order_id':'internal-'+o['sub_order_id'],'sku':'SKU'+o['sub_order_id'],'quantity':2. if o['sub_order_id']=='R9' else 1.,'unit_cost':None if o['sub_order_id']=='C7' else 3. if o['sub_order_id']=='R9' else 20.} for o in orders]
    refunds=[dict(order_id='O8',sub_order_id='C8',store_name='shop',order_time=datetime(2026,9,15),settle_time=datetime(2026,9,15),amount=-5.)]
    refund_item=item('settlement',refunds,refunds[0]);refund_item.template=refund_item.template.model_copy(update={'time_slots':{'settle_date':'settle_time'}})
    refund_item.frame=refund_item.frame.with_columns(pl.col('settle_time').alias('settle_date'))
    ingestion=Ingestion(model=model,items=[item('order_detail',orders,orders[0]),item('order_cost',costs,costs[0]),refund_item])
    ws=Workspace(tmp_path/'workspace');ws.keep('synthetic-input.csv',BytesIO(b'explicit synthetic full-source input'),'s1')
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0');monkeypatch.setattr(service,'ingest',lambda *a,**k:deepcopy(ingestion))
    return ws,model,ingestion


def protected(ws,registry):
    out={table:table_rows(ws.conn,table,"NOT(store_id='s1' AND period='2026-09')") for table in ['run','period','manual_finance','cost_line_log','automation_case','automation_decision']}
    ids=[r['id'] for r in out['run']]
    for table in ['automation_run_proof','automation_run_validation']:
        out[table]=[r for r in table_rows(ws.conn,table) if r['run_id'] in ids]
    with registry.connect() as conn:out['payout_confirmation']=table_rows(conn,'payout_confirmation')
    out['files']={str(p.relative_to(ws.root)):hashlib.sha256(p.read_bytes()).hexdigest() for rid in ids for p in ws.facts_path(rid).parent.glob(str(rid)+'.*') if p.is_file()}
    return out


def test_cold_reuse_and_manager_retry_publish_only_september_with_full_cross_month_math(tmp_path,monkeypatch):
    ws,model,ingestion=input_fixture(tmp_path,monkeypatch);registry=Registry(ws.root)
    # Protected synthetic old open/closed/confirmed inputs, not a close golden.
    payload={'store_id':'s1','can_close':False,'statement':[{'id':'net_profit','name':'profit','value':777.,'available':True}],'commission':{'people':[],'total':9}}
    for period in ('2026-07','2026-08'):
        rid=ws.record('s1',period,{**payload,'period':period},[])
        pl.DataFrame({'original':[rid]}).write_parquet(ws.facts_path(rid))
    old=ws.latest_run('s1','2026-08')['id']
    with ws.conn:ws.conn.execute("UPDATE period SET state='closed',run_id=? WHERE store_id='s1' AND period='2026-08'",(old,))
    with registry.transaction() as conn:conn.execute('INSERT INTO payout_confirmation VALUES(?,?,?,?,?,?,?,?,?,?,?)',('fixed-payout','s1','2026-08',old,'old-time','human','protected input','old-source','[]','{}','9.00'))
    baseline=protected(ws,registry);configure(monkeypatch,ws.root);scope=ps.load_automatic(ws.root,model)
    raw=run(deepcopy(ingestion),'taobao')
    assert not raw.slice('shop','2026-07').nodes['net_profit'].available
    assert raw.facts.filter((pl.col('period')=='2026-08') & (pl.col('metric_id')=='reshipment_cost'))['contribution'].sum()==-6
    assert raw.slice('shop','2026-09').nodes['net_profit'].value==75  # transaction refund: 100-20-5
    monkeypatch.setattr(recompute_reuse,'engine_version',lambda:'verified-fixture-release')
    first=service.recompute(ws,model,model.store('s1'),publication_scope=scope)
    assert first.failure is None,first.failure
    assert [p['period'] for p in first.periods]==['2026-09']
    assert next(p['value'] for p in first.periods[0]['statement'] if p['id']=='net_profit')==75
    assert protected(ws,registry)==baseline
    rid=first.periods[0]['run_id'];again=service.recompute(ws,model,model.store('s1'),publication_scope=scope)
    assert again.periods[0]['run_id']==rid and protected(ws,registry)==baseline
    registry.enqueue_automation({'s1'},'automation:'+hashlib.sha256(b'scope-test').hexdigest());Manager(lambda:ws,lambda:model,publication_scope=scope).once()
    assert protected(ws,registry)==baseline
    registry.enqueue_automation({'s2'},'automation:'+hashlib.sha256(b'other-unapproved-store').hexdigest())
    Manager(lambda:ws,lambda:model,publication_scope=scope).once()
    assert not ws.conn.execute("SELECT 1 FROM run WHERE store_id='s2'").fetchone()
    assert protected(ws,registry)==baseline


def test_scope_changed_during_math_publishes_nothing(tmp_path,monkeypatch):
    ws,model,ingestion=input_fixture(tmp_path,monkeypatch);path,body=configure(monkeypatch,ws.root);scope=ps.load_automatic(ws.root,model)
    original=service.run
    def changed(*args,**kwargs):
        value=original(*args,**kwargs);path.write_text(json.dumps({**body,'approval_reference':'changed'}));return value
    monkeypatch.setattr(service,'run',changed)
    result=service.recompute(ws,model,model.store('s1'),publication_scope=scope)
    assert result.failure['code']=='publication_scope_unverified'
    assert not ws.conn.execute('SELECT 1 FROM run').fetchone()


def test_already_closed_approved_month_is_read_only_even_with_changed_sources(tmp_path,monkeypatch):
    ws,model,_=input_fixture(tmp_path,monkeypatch)
    body={'store_id':'s1','period':'2026-09','can_close':False,'statement':[{'id':'net_profit','name':'profit','value':777.,'available':True}]}
    rid=ws.record('s1','2026-09',body,[])
    with ws.conn:ws.conn.execute("UPDATE period SET state='closed',run_id=? WHERE store_id='s1' AND period='2026-09'",(rid,))
    before={name:table_rows(ws.conn,name) for name in ['run','period','automation_run_proof','automation_run_validation']}
    configure(monkeypatch,ws.root);scope=ps.load_automatic(ws.root,model)
    result=service.recompute(ws,model,model.store('s1'),publication_scope=scope)
    assert result.failure is None and result.periods[0]['run_id']==rid
    assert next(r['value'] for r in result.periods[0]['statement'] if r['id']=='net_profit')==777
    assert {name:table_rows(ws.conn,name) for name in before}==before


def test_no_september_source_does_not_publish_old_months_as_success(tmp_path,monkeypatch):
    ws,model,ingestion=input_fixture(tmp_path,monkeypatch)
    for record in ingestion.items:
        record.frame=record.frame.filter(pl.col('order_time').dt.month()!=9)
    configure(monkeypatch,ws.root);scope=ps.load_automatic(ws.root,model)
    result=service.recompute(ws,model,model.store('s1'),publication_scope=scope)
    assert result.failure['code']=='approved_period_no_result' and not result.periods
    assert not ws.conn.execute('SELECT 1 FROM run').fetchone()
