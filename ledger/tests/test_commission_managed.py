"""Managed sales are a versioned team pool, never a change to payout economics."""
import pytest
import polars as pl

from ledger.commission_engine import calculate, allocated_outputs, managed_sales
from ledger.commission_registry import RegistryError
from ledger.commission_reports import attributed_profit
from ledger.commission_batch import preview, apply
from test_commission_v2 import registry
from test_commission import _model, _run
from test_commission_reports import fixture


def test_effective_team_changes_preserve_money_and_history(tmp_path):
    r, a, b = registry(tmp_path)
    data = {'store_id':'s1','product_id':'p1','valid_from':'2026-05-01',
            'allocations':[{'person_id':a,'rate':'.03','duty':'produce'},
                           {'person_id':b,'rate':'.02','duty':'cut'}]}
    first = r.save_setting(data, 'test')
    orders = _run([('a','p1','2026-05-02',100),('b','p1','2026-05-12',200),
                   ('c','p1','2026-05-22',-50)])
    before, _, _ = calculate(orders, _model(), 's1', '2026-05', r)
    r.save_setting({**data,'expected_revision':1,'valid_from':'2026-05-10',
                    'managed':True,'managed_team_id':a}, 'test')
    r.save_setting({**data,'expected_revision':2,'valid_from':'2026-05-20',
                    'managed':True,'managed_team_id':b}, 'test')
    after, details, _ = calculate(orders, _model(), 's1', '2026-05', r)
    assert after['total'] == before['total']
    assert after['people'] == before['people']
    assert after['managed_sales'] == {a:200, b:-50}
    assert after['production_outputs'][a]['sales'] == 100
    assert after['production_outputs'][b]['sales'] == 0
    for pid in (a,b):
        assert after['production_outputs'][pid]['gross'] == before['production_outputs'][pid]['gross']
    old = r.scheme(first['id'])['versions'][-1]['body']['segments'][0]
    assert old['managed'] is False
    # Original output basis, not managed-zero personal sales, allocates labor.
    before_profit = attributed_profit(before, 100, 20, r, production=True)
    after_profit = attributed_profit(after, 100, 20, r, production=True)
    assert after_profit == before_profit
    # Changes to hierarchy do not mutate the recorded destination.
    r.org_move([a], b, 'test', '调组')
    assert managed_sales(details) == {a:200, b:-50}


def test_managed_pool_deduplicates_and_excludes_expired(tmp_path):
    frame = pl.DataFrame({'status':['distribute','distribute','exclude','expired','hold'],
        'managed':[True]*5,'managed_team_id':['t']*5,'spine_row':[1,1,2,3,4],
        'product_id':['p']*5,'participation_sales':[100.,100.,20.,900.,-5.]})
    assert managed_sales(frame) == {'t':115}
    assert managed_sales(frame.drop('managed')) == {}


def test_api_requires_team_and_retains_flag_in_ordinary_edits(tmp_path):
    ws, r, people, client = fixture(tmp_path)
    data = {'store_id':'s1','product_id':'123456789001','valid_from':'2026-05-01',
            'mode':'exclude','managed':True}
    assert client.post('/api/commission-v2/settings',json=data).status_code == 400
    response = client.post('/api/commission-v2/settings',json={**data,'managed_team_id':people[0]['id']})
    assert response.status_code == 200, response.text
    saved = response.json()
    response = client.post('/api/commission-v2/settings',json={
        'store_id':'s1','product_id':'123456789001','valid_from':'2026-06-01',
        'mode':'exclude','expected_revision':1})
    assert response.status_code == 200, response.text
    segment = response.json()['body']['segments'][-1]
    assert segment['managed'] and segment['managed_team_id'] == people[0]['id']
    assert r.scheme(saved['id'])['versions'][-1]['body']['segments'][0]['managed']


def test_report_team_pool_and_person_filter(tmp_path):
    ws, r, people, client = fixture(tmp_path)
    a, b = people[0]['id'], people[1]['id']
    ws.record('s1','2026-06',{'can_close':True,'findings':[],'missing_sources':[],
        'statement':[{'id':'n_receipt','value':100,'available':True},{'id':'gross','value':60,'available':True}],
        'commission':{'engine':'commission-v2','total':3,'amount_complete':True,
            'people':[{'person_id':a,'person':'甲','amount':3,'base':60}],
            'managed_sales':{b:100},'production_outputs':{a:{'sales':0,'sales_basis':100,'gross':60,'profit':60}}}},[])
    scope={'start':'2026-06','end':'2026-06','store_ids':['s1']}
    report=client.post('/api/commission-v2/reports/query',json=scope).json()
    teams={t['team_id']:t for t in report['teams']}
    assert teams[a]['sales']==0 and teams[b]['sales']==100 and teams[b]['managed_sales']==100
    assert report['people'][0]['sales']==0 and report['total']==3
    assert sum(row['sales'] or 0 for row in report['store_people'] if row['kind']!='store')==100
    selected=client.post('/api/commission-v2/reports/query',json={**scope,'person_ids':[a]}).json()
    assert all(t['managed_sales']==0 for t in selected['teams'])
    assert not any(row['kind']=='managed' for row in selected['store_people'])


def test_bulk_replacement_and_termination_preserve_scheduled_classification(tmp_path):
    from test_commission_batch import setup
    r,model,a,b=setup(tmp_path)
    common={'store_id':'s1','product_id':'123456789001','allocations':[{'person_id':a['id'],'rate':'.05'}]}
    r.save_setting({**common,'expected_revision':1,'valid_from':'2030-01-01','managed':True,'managed_team_id':b['id']},'test')
    plan=preview(r,model,{'targets':[{'store_id':'s1','product_id':'123456789001','revision':2}],
        'template':{'valid_from':'2026-01-01','allocations':[{'person_id':a['id'],'rate':'.06'}]},'operation':'replace'},'test')
    apply(r,model,plan['id'],'test')
    saved=r.active('s1')[1][0]
    segments=saved['body']['segments']
    assert next(s for s in segments if s['valid_from'].startswith('2026'))['managed'] is False
    future=next(s for s in segments if s['valid_from'].startswith('2030'))
    assert future['managed'] and future['managed_team_id']==b['id'] and float(future['total_rate'])==.06
    result=r.save_setting({**common,'expected_revision':3,'valid_from':'2031-01-01'},'test')
    ended=r.terminate(result['id'], '2032-01-01', 4, 'test', '停止提成')
    assert ended['body']['segments'][-1]['managed'] is True


def test_ambiguous_missing_identity_preserves_payout_but_not_sales_guess(tmp_path):
    r,a,b=registry(tmp_path)
    for product,managed in [('p1',True),('p2',False)]:
        r.save_setting({'store_id':'s1','product_id':product,'valid_from':'2026-05-01',
            'managed':managed,'managed_team_id':b,'allocations':[{'person_id':a,'rate':'.05'}]},'test')
    result,details,_=calculate(_run([('missing','', '2026-05-02',100)]),_model(),'s1','2026-05',r)
    assert result['total']==5 and result['managed_sales']=={}
    assert result['production_outputs'][a]['sales']==0
    assert details['sales_unassigned'].to_list()==[True]


def test_negative_managed_sales_do_not_reduce_nonmanaged_person_sales(tmp_path):
    from ledger.commission_reports import attributed_outputs
    r,a,b=registry(tmp_path)
    c={'people':[{'person_id':a}], 'managed_sales':{b:-100},
       'production_outputs':{a:{'sales':200,'sales_basis':100,'gross':50}}}
    assert attributed_outputs(c,100,50,r,production=True)[a]['sales']==200


@pytest.mark.parametrize('mode',['exclude','hold'])
def test_managed_sales_do_not_require_commission_recipients(tmp_path,mode):
    r,a,b=registry(tmp_path)
    r.save_setting({'store_id':'s1','product_id':'p1','valid_from':'2026-05-01',
        'mode':mode,'managed':True,'managed_team_id':b},'test')
    summary,_,_=calculate(_run([('a','p1','2026-05-02',100)]),_model(),'s1','2026-05',r)
    assert summary['managed_sales']=={b:100}
    assert not summary['people'] and summary['total']==0


def test_excel_classification_and_duty_are_not_silently_lost(tmp_path):
    import io
    import openpyxl
    from ledger.commission_batch import parse_excel
    from test_commission_batch import setup
    r,model,a,b=setup(tmp_path)
    workbook=openpyxl.Workbook(); sheet=workbook.active
    sheet.append(['店铺','宝贝ID','人员','提成比例','生效时间','身份','商品归类','托管团队ID'])
    sheet.append(['s1','123456789001','甲','5%','2026-06-01','抽点','托管商品',b['id']])
    stream=io.BytesIO();workbook.save(stream)
    changes=parse_excel(stream.getvalue(),'rules.xlsx',model)
    assert not changes.get('errors')
    plan=preview(r,model,changes,'test'); apply(r,model,plan['id'],'test')
    segment=r.active('s1')[1][0]['body']['segments'][-1]
    assert segment['managed'] and segment['managed_team_id']==b['id']
    assert segment['allocations'][0]['duty']=='cut'


@pytest.mark.parametrize('managed',[True,False])
def test_classification_only_preserves_exact_rules_and_future(tmp_path,managed):
    from test_commission_batch import setup
    r,model,a,b=setup(tmp_path)
    original=r.active('s1')[1][0]['body']['segments'][0]['allocations']
    before_revision=r.revision()
    body={'targets':[{'store_id':'s1','product_id':'123456789001','revision':1}],
          'operation':'classification','template':{'valid_from':'2026-06-01',
          'managed':managed,'managed_team_id':b['id'], 'mode':'exclude',
          'allocations':[{'name':'不得新增的人','rate':'.9'}]}}
    plan=preview(r,model,body,'test')
    assert r.revision()==before_revision and len(r.people())==2
    assert plan['rows'][0]['mode']=='distribute'
    apply(r,model,plan['id'],'test')
    after=r.active('s1')[1][0]['body']['segments'][-1]
    assert after['managed'] is managed
    assert after['allocations']==original and after['mode']=='distribute'
    assert after['managed_team_id']==(b['id'] if managed else '')


def test_classification_only_rejects_missing_effective_rule(tmp_path):
    from test_commission_batch import setup
    r,model,a,b=setup(tmp_path)
    with pytest.raises(RegistryError,match='没有有效设置'):
        preview(r,model,{'changes':[{'store_id':'s1','product_id':'123456789002',
            'valid_from':'2026-06-01','managed':False}], 'operation':'classification'},'test')


def test_classification_window_restores_original_rule_after_end(tmp_path):
    from test_commission_batch import setup
    r,model,a,b=setup(tmp_path)
    original=r.active('s1')[1][0]['body']['segments'][0]['allocations']
    plan=preview(r,model,{'targets':[{'store_id':'s1','product_id':'123456789001','revision':1}],
        'operation':'classification','template':{'managed':True,'managed_team_id':b['id'],
        'valid_from':'2026-06-01','valid_to':'2026-07-01'}},'test')
    apply(r,model,plan['id'],'test')
    segments=r.active('s1')[1][0]['body']['segments']
    assert [s['managed'] for s in segments]==[False,True,False]
    assert all(s['allocations']==original for s in segments)
    assert segments[-1]['valid_from']=='2026-07-01T00:00:00' and segments[-1]['valid_to']==''
