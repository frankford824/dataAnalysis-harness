from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from ledger.model.loader import load_model
from ledger.operations_readmodel import StoreInput, build_month, capture_projection, comparison, select_report, spread, store_metrics


@pytest.fixture
def model():
    return load_model(Path(__file__).resolve().parents[2] / 'models/cn-ecommerce')


def statement(receipts=100, refunds=20, goods=30):
    values={'n_receipt':receipts,'n_refund':-refunds,'n_goods':-goods,'n_dropship':-5,'n_reshipment':-5,
            'n_ad':-10,'g_platform':-5,'n_freight':-3,'n_brushing':-2,'n_small_payment':-4}
    return [{'id':key,'value':value,'available':value is not None} for key,value in values.items()]


def projections(receipts=100,spine=0,product='123',refunds=20):
    values={'trade_receipt':receipts,'trade_refund':-refunds,'goods_cost':-30,'dropship_cost':-5,'reshipment_cost':-5,
            'ad_cost':-10,'software_fee':-5,'freight_cost':-3,'brushing_cost':-2,'small_payment_cost':-4}
    return [{'metric_id':key,'amount':amount,'spine_row':spine,'product_id':product,'order_id':f'o{spine}',
             'sub_order_id':f'c{spine}','link_key':f'o{spine}','factor':1} for key,amount in values.items()]


def crew(pid='p',share=1,duty='produce',managed=False,spine=0,amount='1'):
    return {'spine_row':spine,'product_id':'123','person_id':pid,'person':pid,'share':str(share),'total_rate':'1',
            'duty':duty,'managed':managed,'status':'distribute','amount':amount}


def sample(**updates):
    values=dict(store_id='s',store='测试店',platform='taobao',period='2030-01',run_id=1,state='closed',
                statement=statement(),projections=projections(),assignments=[crew()],evidence_complete=True)
    values.update(updates)
    return StoreInput(**values)


def test_management_definition_is_independent_and_no_double_count(model):
    item=sample(); before=deepcopy(item.statement)
    month=build_month([item],model,'2030-01','10')
    report=select_report(month)
    assert report['summary']['net_income']=='80.00'
    assert report['summary']['goods_cost']=='40.00'
    assert report['summary']['gross_profit']=='40.00'
    assert report['summary']['promotion_cost']=='10.00'
    assert report['summary']['aftersales_cost']=='6.00'
    assert report['summary']['profit']=='16.00'
    assert report['summary']['profit_after_labor']=='6.00'
    assert report['summary']['gross_margin']=='0.50000000'
    assert item.statement==before


def test_doers_own_all_business_and_cut_is_non_additive_reference(model):
    item=sample(assignments=[crew('a','.4',managed=True),crew('b','.6',managed=True),crew('cut','.2','cut',True)])
    month=build_month([item],model,'2030-01','10')
    report=select_report(month,level='person')
    people={row['id']:row for row in report['rows']}
    assert set(people)=={'a','b'}
    assert people['a']['metrics']['net_income']=='32.00'
    assert people['b']['metrics']['net_income']=='48.00'
    assert sum(Decimal(row['metrics']['labor_cost']) for row in people.values())==10
    assert report['summary']['net_income']=='80.00'
    assert report['reference_rows'][0]['person_id']=='cut'
    assert select_report(month,level='person',managed='standard')['rows']==[]
    assert select_report(month,level='person',managed='managed')['summary']['net_income']=='80.00'


def test_aftersales_components_are_visible_without_changing_profit(model):
    report=select_report(build_month([sample()],model,'2030-01','10'),level='person')
    summary=report['summary']
    assert summary['small_payment_cost']=='4.00' and summary['brushing_cost']=='2.00'
    assert summary['aftersales_cost']=='6.00' and summary['promotion_cost']=='10.00'
    assert summary['profit']=='16.00'
    assert [item.get('component_of') for item in report['metrics'] if item['key'] in ('small_payment_cost','brushing_cost')]==['aftersales_cost']*2
    unknown=sample();unknown.statement[-1]['available']=False
    store=select_report(build_month([unknown],model,'2030-01','10'))['summary']
    assert store['small_payment_cost'] is None and store['brushing_cost']=='2.00'
    assert store['aftersales_cost'] is None and store['profit'] is None


def test_cut_identity_groups_show_complete_order_basis_per_person_and_stay_non_additive(model):
    item=sample(assignments=[crew('a',managed=True,amount='3'),crew('cut','.1','cut',True,amount='1'),crew('cut','.1','cut',True,amount='.5'),
                             crew('other','.2','cut',True,amount='2')])
    month=build_month([item],model,'2030-01','10')
    person=select_report(month,level='person')
    groups={row['person_id']:row for row in person['reference_groups']}
    assert set(groups)=={'cut','other'}
    assert groups['cut']['metrics']['net_income']=='80.00'
    assert groups['cut']['metrics']['profit_after_labor']=='6.00'
    assert groups['cut']['metrics']['commission']=='1.50'
    assert groups['other']['metrics']['commission']=='2.00'
    assert groups['cut']['order_count']==1 and groups['cut']['duty']=='cut'
    assert person['summary']['net_income']=='80.00' and person['summary']['commission']=='3.00'
    store=select_report(month,level='store',managed='managed')
    assert sorted(row['person_id'] for row in store['reference_groups'])==['cut','other']
    assert {row['metrics']['net_income'] for row in store['reference_groups']}=={'80.00'}


def test_managed_split_is_exact_for_classified_rows_and_unknown_otherwise(model):
    first=projections(60,spine=0,refunds=0);second=projections(40,spine=1,refunds=0)
    item=sample(statement=statement(100,0,60),projections=[*first,*second],
                assignments=[crew('a',spine=0,managed=True),crew('a',spine=1,managed=False),crew('b',spine=1,managed=False)])
    month=build_month([item],model,'2030-01','10')
    rows={row['id']:row for row in select_report(month,level='person')['rows']}
    assert rows['a']['metrics']['net_income']=='80.00'
    assert rows['a']['managed_metrics']['net_income']=='60.00'
    assert rows['b']['managed_metrics']['net_income']=='0.00'
    assert rows['a']['team']=='未分团队' and rows['a']['platform']=='taobao'
    mixed=sample(assignments=[crew('a',managed=True),crew('b',managed=False)])
    report=select_report(build_month([mixed],model,'2030-01','10'),level='person')
    assert all(row['managed_metrics'] is None for row in report['rows'])
    assert report['managed_summary'] is None
    summary_only=build_month([sample(detail_loaded=False)],model,'2030-01','10')
    assert select_report(summary_only)['rows'][0]['managed_metrics'] is None


def test_labor_uses_full_claimed_pre_refund_sales_and_includes_managed(model):
    first=projections(60,spine=0,refunds=50)
    second=projections(40,spine=1,refunds=0)
    unclaimed=projections(100,spine=2,refunds=0)
    item=sample(statement=statement(200,50,90),projections=[*first,*second,*unclaimed],
                assignments=[crew('a',spine=0,managed=True),crew('b',spine=1)])
    month=build_month([item],model,'2030-01','10.01')
    rows={row['id']:row for row in select_report(month,level='person')['rows']}
    assert rows['a']['metrics']['labor_cost']=='6.01'
    assert rows['b']['metrics']['labor_cost']=='4.00'
    assert rows['__unclaimed__']['metrics']['labor_cost']=='0.00'
    assert rows['__unclaimed__']['metrics']['net_income']=='100.00'
    assert rows['__unclaimed__']['rank'] is None


def test_missing_is_not_zero_and_legacy_detail_is_not_invented(model):
    item=sample(projections=[{'metric_id':'trade_receipt','amount':100,'spine_row':0,'product_id':'123'}],evidence_complete=False)
    month=build_month([item],model,'2030-01',None)
    person=select_report(month,level='person')
    assert person['summary']['goods_cost'] is None
    assert person['summary']['profit_after_labor'] is None
    assert select_report(month)['summary']['net_income']=='80.00'
    assert select_report(month)['is_final'] is False
    item.statement[0]['available']=False
    assert store_metrics(item.statement)['receipts'] is None


def test_ratios_from_total_amounts_and_store_product_identity(model):
    first=sample(store_id='s1',statement=statement(100,0),projections=projections(100,refunds=0))
    second=sample(store_id='s2',statement=statement(300,100),projections=projections(300,refunds=100))
    month=build_month([first,second],model,'2030-01','0')
    report=select_report(month,level='product')
    assert {row['id'] for row in report['rows']}=={'s1:123','s2:123'}
    assert report['summary']['refund_rate']=='0.25000000'
    assert report['summary']['net_income']=='300.00'


def test_input_identity_stays_same_across_scope_but_scope_identity_differs(model):
    month=build_month([sample()],model,'2030-01','10')
    company=select_report(month)
    person=select_report(month,level='person',store_id='s')
    assert company['input_sha']==person['input_sha']
    assert company['scope_sha']!=person['scope_sha']


def test_unknown_ownership_is_not_a_final_person_result_even_with_zero_labor(model):
    month=build_month([sample(assignments=[crew('cut',1,'cut')])],model,'2030-01','0')
    person=select_report(month,level='person')
    assert person['attribution_pending'] is True
    assert person['is_final'] is False
    assert person['rows'][0]['unclaimed'] is True


def test_comparison_never_divides_negative_or_zero_base():
    assert comparison(Decimal(5),Decimal(0))['state']=='zero_base'
    assert comparison(Decimal(5),Decimal(-2))['rate'] is None
    assert comparison(None,Decimal(1))['state']=='unavailable'
    assert comparison(Decimal(5),Decimal(10))['direction']=='down'
    assert comparison(Decimal('.2515'),Decimal('.2512'),rate_metric=True)['delta']=='0.00030000'
    assert sum(spread(Decimal('.01'),{'a':Decimal(1),'b':Decimal(1),'c':Decimal(1)}).values())==Decimal('.01')


def test_capture_is_before_recipient_expansion_and_preserves_null_target():
    projected=pl.DataFrame({'spine_row':[0,None],'metric_id':['ad_cost','freight_cost'],'amount':[-10.,-3.],'link_key':['o1','unknown']})
    context=pl.DataFrame({'spine_row':[0],'product_id':['123'],'order_id':['o1']},schema_overrides={'spine_row':pl.UInt32})
    projected=projected.with_columns(pl.col('spine_row').cast(pl.UInt32))
    captured=capture_projection(projected,context)
    assert captured.height==2
    assert captured['amount'].sum()==-13
    assert captured['product_id'].to_list()==['123',None]
