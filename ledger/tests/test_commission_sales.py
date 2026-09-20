from decimal import Decimal
import hashlib

import polars as pl
import pytest

from ledger.commission_engine import allocated_outputs
from ledger.commission_sales import attach, totals
from ledger.commission_registry import Registry
from ledger.commission_reports import _archived_allocated_outputs
from ledger.commission_profit import compose
from test_commission_profit import _persist


def frame(duties=None):
    data={'product_id':['p','p'],'person_id':['member','leader'],'person':['组员','组长'],
        'product_name':['商品','商品'],'spine_row':[1,1],'order_id':['o','o'],
        'order_at':['2026-06-05T00:00:00']*2,'status':['distribute']*2,
        'share':[.02,.03],'total_rate':[.05,.05],
        'participation_sales':[16736.38]*2,'participation_gross':[10237.81]*2,
        'participation_profit':[6059.18]*2,'original_base':[6059.18]*2,'amount':[121.18,181.78]}
    if duties is not None:data['duty']=duties
    return pl.DataFrame(data)


def snapshot(duties=('produce','cut'),start='2026-06-01T00:00:00',end=''):
    return (1,({'p':[{'valid_from':start,'valid_to':end,'mode':'distribute',
        'allocations':[{'person_id':pid,'duty':duty,'rate':rate} for pid,duty,rate in zip(['member','leader'],duties,['.02','.03'])]}]},[]))


@pytest.mark.parametrize('archived',[None,['',''],['produce','produce']])
def test_effective_duties_correct_sales_only_from_original_income(archived):
    original=frame(archived);before=allocated_outputs(original,production=True)
    result=totals(attach(None,'s','2026-06',original,snapshot=snapshot()),before)
    assert result['member']['sales']==16736.38 and result['leader']['sales']==0
    for pid in ('member','leader'):
        for field in ('gross','profit'):
            assert result[pid][field]==before[pid][field]
        assert result[pid]['sales_basis']==before[pid]['sales']
    assert original['amount'].to_list()==[121.18,181.78]


def test_unknown_duties_and_out_of_effective_range_are_not_guessed():
    for configured in (snapshot((None,None)),snapshot(start='2026-07-01T00:00:00'),snapshot(end='2026-06-04T00:00:00')):
        original=frame();before=allocated_outputs(original,production=True)
        result=totals(attach(None,'s','2026-06',original,snapshot=configured),before)
        assert all(v['sales'] is None and v['sales_pending_products']==['p'] for v in result.values())
        assert result['member']['profit']==before['member']['profit']


def test_two_producers_use_decimal_ratio_and_signed_income():
    original=frame(['produce','produce']).with_columns(pl.lit(-100.01).alias('participation_sales'))
    result=totals(attach(None,'s','2026-06',original,snapshot=snapshot(('produce','produce'))),allocated_outputs(original,production=True))
    assert result['member']['sales']==float((Decimal('-100.01')*Decimal('.02')/Decimal('.05')).quantize(Decimal('.01')))
    assert result['leader']['sales']==-60.01


def test_managed_archive_never_credits_person_sales_even_with_missing_duty():
    original=frame().with_columns(pl.lit(True).alias('managed'))
    result=totals(attach(None,'s','2026-06',original,snapshot=snapshot((None,None))),allocated_outputs(original,production=True))
    assert all(v['sales']==0 and not v['sales_pending_products'] for v in result.values())


@pytest.mark.parametrize('field',['product_id','order_at'])
def test_missing_match_inputs_cannot_confirm_an_inferred_role(field):
    original=frame(['produce','cut']).with_columns(pl.lit('').alias(field))
    resolved=attach(None,'s','2026-06',original,snapshot=snapshot())
    assert resolved['__sales_pending'].all()
    assert resolved['__sales_source'].unique().to_list()==['pending_input']


def test_explicit_empty_product_filter_returns_only_unknown_product(tmp_path):
    registry=Registry(tmp_path)
    a=registry.person_save({'name':'甲'},'test','登记')
    data=frame(['produce','produce']).with_columns(pl.lit(a['id']).alias('person_id'),
        pl.Series('product_id',['','known']),pl.Series('spine_row',[1,2]))
    _persist(registry,a['id'],rows=data.to_dict(as_series=False))
    result=compose(registry,'s1','2026-06',a['id'],11,product_id='')
    assert [p['product_id'] for p in result['products']]==['']


@pytest.mark.parametrize('field',['product_id','order_at'])
def test_uniform_fallback_requires_confirmed_identity_and_full_month(field):
    original=frame(['produce','cut']).with_columns(pl.lit('').alias(field),pl.lit('store_uniform_distribution').alias('fallback_reason'))
    resolved=attach(None,'s','2026-06',original,snapshot=snapshot())
    result=totals(resolved,allocated_outputs(original,production=True))
    assert result['member']['sales']==16736.38 and result['leader']['sales']==0
    assert resolved['__sales_source'].unique().to_list()==['effective_period_rule']
    incomplete=attach(None,'s','2026-06',original,snapshot=snapshot(start='2026-06-15T00:00:00'))
    assert incomplete['__sales_pending'].all()


def test_uniform_money_is_not_proof_of_uniform_duty():
    original=frame(['produce','cut']).with_columns(pl.lit('').alias('product_id'),pl.lit('store_uniform_distribution').alias('fallback_reason'))
    configured=snapshot()[1][0]
    configured['other']=snapshot(('cut','produce'))[1][0]['p']
    resolved=attach(None,'s','2026-06',original,snapshot=(1,(configured,[])))
    assert resolved['__sales_pending'].all()


def test_default_rule_fallback_does_not_resurrect_expired_product_rule():
    rules=snapshot()[1][0]
    rules['*']=rules.pop('p')
    original=frame();before=allocated_outputs(original,production=True)
    assert totals(attach(None,'s','2026-06',original,snapshot=(1,(rules,[]))),before)['member']['sales']==16736.38
    rules['p']=[{**rules['*'][0],'valid_to':'2026-06-04T00:00:00'}]
    assert totals(attach(None,'s','2026-06',original,snapshot=(1,(rules,[]))),before)['member']['sales'] is None


def test_rule_revision_refreshes_sales_but_keeps_archive_profit_and_cost(tmp_path):
    registry=Registry(tmp_path)
    a=registry.person_save({'id':'member','name':'组员'},'test','登记')
    b=registry.person_save({'id':'leader','name':'组长'},'test','登记')
    # Person creation may assign its own IDs; use the returned durable identities.
    data=frame().with_columns(pl.Series('person_id',[a['id'],b['id']]))
    calculation=_persist(registry,a['id'],rows=data.to_dict(as_series=False))
    c={'calculation_id':calculation}
    before=compose(registry,'s1','2026-06',a['id'],11)
    assert before['products'][0]['sales'] is None
    assert _archived_allocated_outputs(registry,c,production=True,sales=True)[a['id']]['sales'] is None
    with registry.connect() as conn: path=registry.root/'calculations'/conn.execute('select path from calculation where id=?',(calculation,)).fetchone()[0]
    checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    registry.save_setting({'store_id':'s1','product_id':'p','valid_from':'2026-06-01',
        'allocations':[{'person_id':a['id'],'duty':'produce','rate':'.02'}, {'person_id':b['id'],'duty':'cut','rate':'.03'}]},'test')
    after=compose(registry,'s1','2026-06',a['id'],11)
    assert after['products'][0]['sales']==16736.38
    assert after['products'][0]['gross']==before['products'][0]['gross']
    assert after['products'][0]['profit']==before['products'][0]['profit']
    assert after['products'][0]['cost']==before['products'][0]['cost']
    assert after['commission_trial']==before['commission_trial']
    assert _archived_allocated_outputs(registry,c,production=True,sales=True)[a['id']]['sales']==16736.38
    assert hashlib.sha256(path.read_bytes()).hexdigest()==checksum
    leader=compose(registry,'s1','2026-06',b['id'],11,duties={b['id']:{'duty':'cut'}})
    assert leader['products'][0]['sales']==0 and leader['commission_trial']==181.78


def test_closed_report_and_pending_export_share_sales_resolution(tmp_path):
    import csv
    import io
    from test_commission_reports import fixture
    ws,registry,people,client=fixture(tmp_path)
    a,b=people[0]['id'],people[1]['id']
    data=frame().with_columns(pl.Series('person_id',[a,b]))
    calculation=_persist(registry,a,rows=data.to_dict(as_series=False),run_id=1)
    run=ws.record('s1','2026-06',{'can_close':True,'findings':[],'missing_sources':[],
        'statement':[{'id':'n_receipt','value':16736.38,'available':True},{'id':'gross','value':10237.81,'available':True}],
        'commission':{'engine':'commission-v2','calculation_id':calculation,'base_total':6059.18,
        'people':[{'person_id':a,'person':'甲','amount':121.18},{'person_id':b,'person':'乙','amount':181.78}],
        'total':302.96,'amount_complete':True}},[])
    assert run==1
    ws.close_period('s1','2026-06',by='test',note='冻结')
    scope={'start':'2026-06','end':'2026-06','store_ids':['s1']}
    before=client.post('/api/commission-v2/reports/query',json=scope).json()
    assert before['sales_pending_scopes']
    response=client.get('/api/commission-v2/sales-attribution/audit',params={'start':'2026-06','end':'2026-06','store_ids':'s1'})
    assert response.status_code==200,response.text
    assert len(list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff')))))==2
    registry.save_setting({'store_id':'s1','product_id':'p','valid_from':'2026-06-01',
        'allocations':[{'person_id':a,'duty':'produce','rate':'.02'},{'person_id':b,'duty':'cut','rate':'.03'}]},'test')
    after=client.post('/api/commission-v2/reports/query',json=scope).json()
    assert after['run_ids']==before['run_ids']==[1] and after['total']==before['total']==302.96
    assert not after['sales_pending_scopes']
    rows={r['person_id']:r for r in after['store_people'] if r['kind']=='person'}
    assert rows[a]['sales']==16736.38 and rows[b]['sales']==0
    assert ws.state('s1','2026-06').run_id==1
