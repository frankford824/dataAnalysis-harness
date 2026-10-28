"""Real refund XLSX and date publisher exercise the durable-outbox crash gap.

All business amounts are declared synthetic inputs. No production database,
SDK, original historical result, threshold or golden is modified.
"""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import hashlib,json
import pytest

from conftest import write_xlsx,MODELS
from test_jst_source_members import order,html
from test_publication_scope import configure
from test_replay_service import seed
from ledger import automation_actions as actions,automation_checker as checker,source_dates as dates,service,automation_store as journal
from ledger import commission_confirm
from ledger.commission_registry import Registry
from ledger.commission_manager import Manager
from ledger.model.loader import load_model
from ledger.model.schema import Model,Platform,Store,SourceContract,Template,ParseOptions,ColumnBinding,Metric,StatementNode,Overhead
from ledger.workspace import Workspace,WorkspaceError

PERIOD='2026-09'
HUMAN={'id':'declared-finance','kind':'human','authenticated':True}


def publish(c,day,nonce,pid='202600001',sid='s1'):
    actions.sync_month(c.ws,c.model,PERIOD)
    case=dict(c.ws.conn.execute('SELECT * FROM automation_case WHERE store_id=? AND period=?',(sid,PERIOD)).fetchone())
    task='source-'+hashlib.sha256(nonce.encode()).hexdigest()[:40];job=hashlib.sha256((nonce+'-job').encode()).hexdigest()[:32]
    folder=c.ws.root/'source-recovery'/task/job;folder.mkdir(parents=True)
    raw=html(order('100',[],order_date=day+' 12:00:00',so_id=pid))
    sha=hashlib.sha256(raw).hexdigest();(folder/(sha+'.raw')).write_bytes(raw)
    receipt=json.dumps({'nodes':[{'order_id':'100','sha256':sha}]},separators=(',',':')).encode()
    (folder/'receipt.json').write_bytes(receipt)
    return dates.publish_from_task(c.ws,c.model,case,{'platform_order_id':pid,'shop_ids':['77']},task,job,hashlib.sha256(receipt).hexdigest(),
        {'id':'declared-source-worker','kind':'service','authenticated':True,'scopes':['business']})


def drain(c):
    actions.deliver(c.ws)
    for _ in range(12):
        with c.reg.connect() as db:count=db.execute('SELECT count(*) FROM pending').fetchone()[0]
        if not count:return
        c.manager.once()
    raise AssertionError('real deterministic Manager did not finish finite source input')


def frozen_hash(c):
    row=c.ws.conn.execute("SELECT * FROM period WHERE store_id='s2' AND period=?",(PERIOD,)).fetchone()
    rid=row['run_id']
    objects=[dict(row),dict(c.ws.conn.execute('SELECT * FROM run WHERE id=?',(rid,)).fetchone()),
        dict(c.ws.conn.execute('SELECT * FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone()),
        [dict(r) for r in c.ws.conn.execute('SELECT * FROM run_labor WHERE run_id=?',(rid,))],
        [dict(r) for r in c.ws.conn.execute('SELECT * FROM automation_overhead_close WHERE period=?',(PERIOD,))],
        hashlib.sha256(c.ws.facts_path(rid).read_bytes()).hexdigest()]
    with c.reg.connect() as db:objects.append([dict(r) for r in db.execute("SELECT * FROM payout_confirmation WHERE store_id='s2'")])
    return journal.digest(objects)


@pytest.fixture
def date_case(tmp_path,monkeypatch,request):
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','0')
    monkeypatch.setenv('LEDGER_SOURCE_RECOVERY_PERIODS',PERIOD)
    ws=Workspace(tmp_path/'workspace');reg=Registry(ws.root)
    def template(name,source,columns,time_slots):
        return Template(id=name,name=name,source=source,parse=ParseOptions(header_row=0),match_columns=tuple(label for _,label,kind in columns),
            time_slots=time_slots,bindings=tuple(ColumnBinding(role=role,columns=(label,),kind=kind) for role,label,kind in columns))
    nt=template('cash-native','order_detail',[('order_id','主订单','text'),('sub_order_id','子订单','text'),('product_id','商品','text'),
        ('buyer_paid','实收','number'),('quantity','数量','number'),('order_time','下单时间','time'),('store_name','店铺','text')],{'order_date':'order_time'})
    ct=template('cash-cost','order_cost',[('order_id','主订单','text'),('sub_order_id','子订单','text'),('sku','SKU','text'),
        ('quantity','数量','number'),('unit_cost','单位成本','number'),('order_time','下单时间','time'),('store_name','店铺','text')],{'order_date':'order_time'})
    rt=template('actual-refund','refunds',[('order_id','退款原单','text'),('amount','退款金额','number'),('settle_time','发生时间','time'),('store_name','店铺','text')],{'settle_date':'settle_time'})
    full=load_model(MODELS/'cn-ecommerce');contract=next(s for s in full.sources if s.id=='refund_order_dates');date_template=next(t for t in full.templates if t.id=='refund_order_dates_v1')
    model=Model(id='date-outbox-actual-source',name='Declared synthetic actual refund publisher test',
        platforms=(Platform(id='taobao',name='淘宝',cost_pricing='provided'),),
        stores=(Store(id='s1',name='甲店',platform='taobao'),Store(id='s2',name='乙店',platform='taobao')),
        sources=(SourceContract(id='order_detail',name='原订单',is_spine=True,owner_role='shop_owner',cadence='monthly'),
            SourceContract(id='order_cost',name='原成本',owner_role='warehouse',cadence='monthly'),
            SourceContract(id='refunds',name='退款原件',owner_role='finance',cadence='monthly',provides=['trade_refund']),contract),
        templates=(nt,ct,rt,date_template),
        metrics=(Metric(id='income',name='销售',source='order_detail',value={'op':'sum','of':['buyer_paid']},time_basis='order_date',link={'key':'sub_order_id','to':'order.sub_order_id','grain':'order'}),
            Metric(id='goods_cost',name='成本',source='order_cost',value={'op':'sum_product','of':['quantity','unit_cost']},sign='abs_negate',time_basis='order_date',link={'key':'sub_order_id','to':'order.sub_order_id','grain':'order'}),
            Metric(id='trade_refund',name='退款',source='refunds',value={'op':'sum','of':['amount']},keep_zero_rows=True,posting_basis='order_number',time_basis='order_date',orderless_time_basis='settle_date',link={'key':'order_id','to':'order.order_id','grain':'order'})),
        statement=(StatementNode(id='n_receipt',name='未扣退款销售',headline='revenue',formula={'op':'add','of':['income']}),
            StatementNode(id='gross_profit',name='毛利',formula={'op':'add','of':['income','goods_cost']}),
            StatementNode(id='net_profit',name='利润',headline='profit',commission_base=True,formula={'op':'add','of':['gross_profit','trade_refund']})),
        overheads=(Overhead(period=PERIOD,amount=0,name='Declared confirmed zero fixture overhead'),))
    configure(monkeypatch,ws.root,(('s1',PERIOD),('s2',PERIOD)))
    for sid,name,pid,child,product,revenue,cost in [('s1','甲店','202600002','C1','123456789001',100,50),('s2','乙店','202600003','C2','123456789002',300,150)]:
        native=write_xlsx(tmp_path/(sid+'-native.xlsx'),[['主订单','子订单','商品','实收','数量','下单时间','店铺'],[pid,child,product,revenue,1,datetime(2026,9,2),name]])
        costs=write_xlsx(tmp_path/(sid+'-cost.xlsx'),[['主订单','子订单','SKU','数量','单位成本','下单时间','店铺'],[pid,child,'A',1,cost,datetime(2026,9,2),name]])
        ws.keep(native.name,native,sid);ws.keep(costs.name,costs,sid)
    bank=write_xlsx(tmp_path/'2026-09-refund-original.xlsx',[['退款原单','退款金额','发生时间','店铺'],['202600001',-20,datetime(2026,9,8),'甲店'],['202600002',0,datetime(2026,9,8),'甲店'],['202600003',0,datetime(2026,9,8),'乙店']])
    bank_ref=ws.keep(bank.name,bank,'__shared__')
    fixed=seed()
    for person in fixed['people']:reg.person_save(person,'declared fixture','independent synthetic identities')
    for row in fixed['members']:reg.save_store_member(row['store_id'],row['person_id'],row['duty'],'','declared fixture','independent synthetic role')
    for row in fixed['schemes']:reg.save_scheme(row['store_id'],row['product_id'],row['body'],'declared fixture','independent synthetic 5 percent scheme',publish=True)
    for row in fixed['policies']:reg.save_policy(row['body'],row['effective_from'],'declared fixture','independent synthetic policy')
    c=SimpleNamespace(ws=ws,reg=reg,model=model,manager=Manager(lambda:ws,lambda:model),bank_sha=bank_ref.sha)
    for store in model.stores:
        result=service.recompute(ws,model,store);assert result.failure is None,result.failure
    publish(c,'2026-07-01','initial-july-original-day')
    drain(c)
    for sid in ['s1','s2']:
        checked=checker.check(ws,sid,PERIOD,model=model,verify_artifacts=True)
        assert checked['allowed'],checked['issues']
    rid=ws.latest_run('s1',PERIOD)['id'];manifest=json.loads(ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone()[0])
    assert '202600001' in manifest['original_date_dependencies']['potential_parent_ids']
    assert c.bank_sha in manifest['source_refs']
    if getattr(request,'param',{}).get('freeze_other',True):
        # Freeze an actually confirmed other store; the affected store stays open.
        context=commission_confirm.context(ws,reg,model,'s2',PERIOD,expected_run=ws.latest_run('s2',PERIOD)['id'])
        commission_confirm.confirm(ws,reg,model,store_id='s2',period=PERIOD,run_id=context['run_id'],source_sha=context['source_sha'],
            expected_confirmation_id=context['latest']['id'] if context['latest'] else '',payouts=[{'person_id':'p1','amount':'7.50'}],no_payout=False,reason='declared 300-150 times 5 percent approval',actor='declared finance')
        preview=actions.close_preview(ws,model,request_id='freeze-unrelated-store',period=PERIOD,store_ids=['s2'],actor=HUMAN)
        assert preview['eligible'] and not preview['blocked']
        result=actions.close_confirm(ws,model,request_id='freeze-unrelated-store-confirm',proposal_id=preview['proposal_id'],proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=HUMAN)
        assert result['status']=='completed'
    yield c
    ws.close()


def net(c):
    payload=json.loads(c.ws.latest_run('s1',PERIOD)['result'])
    return next(n['value'] for n in payload['statement'] if n['id']=='net_profit')


def test_actual_refund_date_commit_delivery_gap_blocks_fresh_close_and_real_recompute_clears(date_case):
    c=date_case;old=net(c);old_run=c.ws.latest_run('s1',PERIOD)['id'];protected=frozen_hash(c)
    company=checker.company_basis(c.ws,c.model,PERIOD)['input_sha']
    from ledger.operations_readmodel import read_month
    before_report=read_month(c.ws,c.model,PERIOD,store_id='s1')
    assert before_report['is_final'] is False
    result=publish(c,'2026-09-01','corrected-september-original-day')
    assert result['status']=='source_input_queued'
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='dispatch_pending'").fetchone()[0]==1
    with c.reg.connect() as db:assert db.execute('SELECT count(*) FROM pending').fetchone()[0]==0
    checked=checker.check(c.ws,'s1',PERIOD,model=c.model,verify_artifacts=True)
    assert not checked['allowed'] and 'original_date_input_unverified' in {r['code'] for r in checked['issues']}
    row=next(r for r in actions.month(c.ws,c.model,PERIOD)['stores'] if r['store_id']=='s1')
    assert row['status']=='processing' and row['stage']=='calculating'
    view=checker.close_view(c.ws,c.model,'s1',PERIOD,{**json.loads(c.ws.latest_run('s1',PERIOD)['result']),'run_id':old_run})
    assert not view['can_close'] and 'original_date_input_unverified' in {r['code'] for r in view['close_verification']['issues']}
    report=read_month(c.ws,c.model,PERIOD,store_id='s1')
    assert report['is_final'] is False
    # Readmodel keeps the saved run identity/amounts as a stage result; the
    # current close-verification identity separately reports pending recompute.
    assert report['input_sha']==before_report['input_sha']
    fresh=actions.close_preview(c.ws,c.model,request_id='fresh-preview-after-date-commit',period=PERIOD,store_ids=['s1'],actor=HUMAN)
    assert not fresh['eligible'] and fresh['blocked']
    if fresh.get('proposal_id'):
        done=actions.close_confirm(c.ws,c.model,request_id='reject-fresh-old-run',proposal_id=fresh['proposal_id'],proposal_sha=fresh['proposal_sha'],input_sha=fresh['input_sha'],actor=HUMAN)
        assert not done['completed']
    assert not c.ws.state('s1',PERIOD).closed
    assert c.ws.latest_run('s1',PERIOD)['id']==old_run
    assert checker.company_basis(c.ws,c.model,PERIOD)['input_sha']==company
    assert frozen_hash(c)==protected
    drain(c)
    checked=checker.check(c.ws,'s1',PERIOD,model=c.model,verify_artifacts=True)
    assert checked['allowed'] and 'original_date_input_unverified' not in {r['code'] for r in checked['issues']}
    assert net(c)==old-20 and not c.ws.state('s1',PERIOD).closed
    assert frozen_hash(c)==protected
    run_id=c.ws.latest_run('s1',PERIOD)['id'];input_sha=checked['input_sha']
    assert publish(c,'2026-09-01','same-day-new-raw-observation')['status']=='source_input_reused'
    drain(c);c.manager.once()
    assert c.ws.latest_run('s1',PERIOD)['id']==run_id
    assert checker.check(c.ws,'s1',PERIOD,model=c.model)['input_sha']==input_sha
    assert frozen_hash(c)==protected


@pytest.mark.parametrize('date_case',[{'freeze_other':False}],indirect=True)
def test_unrelated_pid_and_other_store_date_facts_do_not_invalidate_sealed_refund_scope(date_case):
    c=date_case;before=checker.check(c.ws,'s1',PERIOD,model=c.model)
    assert publish(c,'2026-09-01','unrelated-parent-proof',pid='202699999')['status']=='source_input_queued'
    own=checker.check(c.ws,'s1',PERIOD,model=c.model)
    assert own['input_sha']==before['input_sha'] and own['allowed']
    assert publish(c,'2026-09-02','other-store-proof',pid='202600003',sid='s2')['status']=='source_input_queued'
    current=checker.check(c.ws,'s1',PERIOD,model=c.model)
    assert current['input_sha']==before['input_sha'] and current['allowed']
