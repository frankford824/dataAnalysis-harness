import csv
import io
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.commission_api import install
from ledger.commission_reports import attributed_labor, attributed_profit
from ledger.model.schema import Overhead, StatementNode, Store
from ledger.workspace import Workspace
from test_commission import _model
from test_commission_managed_report import sample
from test_commission_profit import _persist


def dashboard(tmp_path, *, missing=False):
    reg, a, b, _, _, rows = sample(tmp_path)
    ws = Workspace(tmp_path)
    cid = _persist(reg, a, rows=rows, run_id=1)
    model = _model(stores=(Store(id='s1', name='测试店', platform='taobao'),))
    model = model.model_copy(update={
        'overheads': (Overhead(period='2026-06', amount=20),),
        'statement': (model.statement[0].model_copy(update={'headline':'revenue'}), *model.statement[1:],
                      StatementNode(id='net_profit', name='利润', level=1, is_total=True,
                                    headline='profit', formula={'op':'add','of':['gross']})),
    })
    c = {'engine':'commission-v2','base_node':'net_profit','on_loss':'deduct','base_total':100,
         'calculation_id':cid if not missing else None,'managed_sales':{b:100},'total':5,
         'amount_complete':True,'people':[{'person_id':a,'person':'做货甲','amount':2,'sales':200},
                                         {'person_id':b,'person':'抽点乙','amount':3,'sales':200}],
         'products':[{'product_id':'managed','total_rate':.05,'people':[{'person_id':a,'amount':1},{'person_id':b,'amount':1.5}]}]}
    run = ws.record('s1','2026-06', {'can_close':True, 'commission':c,
        'statement':[{'id':model.statement[0].id,'value':200,'available':True},
                     {'id':'gross','value':120,'available':True},
                     {'id':'net_profit','value':100,'available':True}]}, [])
    app=FastAPI();install(app,lambda:ws,lambda:model)
    return ws, reg, a, b, run, TestClient(app)


def test_person_managed_labor_and_net_profit_align_with_export_and_filters(tmp_path):
    ws, reg, a, b, run, client = dashboard(tmp_path)
    selection={'start':'2026-06','end':'2026-06','view':'store_people'}
    report=client.post('/api/commission-v2/reports/query',json=selection).json()
    people={r['person_id']:r for r in report['items'] if r['kind']=='person'}
    assert people[a]['managed_sales']==100 and people[b]['managed_sales']==0
    assert people[a]['labor_cost']==20 and people[b]['labor_cost']==0
    assert people[a]['profit_after_labor']==80 and people[b]['profit_after_labor']==0
    assert people[a]['store_labor_cost']==people[b]['store_labor_cost']==20
    assert people[a]['trial_amount']==1.6 and people[b]['trial_amount']==2.4
    assert report['total']==4
    assert sum(Decimal(str(p['labor_cost'])) for p in people.values())==Decimal('20')
    selected=client.post('/api/commission-v2/reports/query',json={**selection,'person_ids':[a]}).json()
    selected_a=next(r for r in selected['items'] if r.get('person_id')==a)
    assert [selected_a[k] for k in ('managed_sales','labor_cost','profit_after_labor')]==[100,20,80]
    filtered_b=client.post('/api/commission-v2/reports/query',json={**selection,'person_ids':[b]}).json()
    assert next(r for r in filtered_b['items'] if r.get('person_id')==b)['managed_sales']==0
    for presentation in (True,False):
        response=client.post('/api/commission-v2/export/reports/store_people',json={**selection,'presentation':presentation})
        assert response.status_code==200,response.text
        exported=list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
        first=next(r for r in exported if r['分配人']=='做货甲')
        assert Decimal(first['托管类销售额'])==100 and Decimal(first['兼职额'])==20
        assert Decimal(first['利润额（扣兼职）' if presentation else '利润额'])==80
    grouped=client.post('/api/commission-v2/reports/query',json={**selection,'view':'people'}).json()
    assert next(r for r in grouped['items'] if r['person_id']==a)['managed_sales']==100
    # Reads and exports do not create payouts or rewrite the source calculation.
    with reg.connect() as conn:
        assert conn.execute('select count(*) from payout_confirmation').fetchone()[0]==0
    assert ws.latest_run('s1','2026-06')['id']==run


def test_missing_managed_evidence_is_unknown_not_zero(tmp_path):
    _, _, a, _, _, client = dashboard(tmp_path, missing=True)
    result=client.post('/api/commission-v2/reports/query',json={'start':'2026-06','end':'2026-06','view':'store_people'}).json()
    row=next(r for r in result['items'] if r.get('person_id')==a)
    assert row['managed_sales'] is None and row['managed_sales_pending']


def test_labor_rounding_is_exact_and_uses_the_same_profit_basis(tmp_path):
    ws, registry, a, b, _, _ = dashboard(tmp_path)
    c={'people':[{'person_id':a,'allocated_sales':1,'allocated_gross':5,'allocated_profit':5},
                  {'person_id':b,'allocated_sales':2,'allocated_gross':5,'allocated_profit':5}]}
    costs=attributed_labor(c,.01,registry)
    assert costs=={a:0,b:.01}
    net=attributed_profit(c,10,.01,registry,production=True)
    assert net=={a:5,b:4.99}
    assert attributed_labor(c,None,registry)=={}
    c['production_outputs']={a:{'sales_basis':1},b:{'sales_basis':3}}
    assert attributed_labor(c,20,registry)=={a:5,b:15}  # profit can still be unavailable
