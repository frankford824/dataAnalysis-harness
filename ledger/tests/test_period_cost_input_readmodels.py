"""Store-only cost authority must not manufacture product/person financial facts."""
from copy import deepcopy
import pytest
from test_operations_readmodel import model,sample,statement,projections,crew
from ledger.operations_readmodel import build_month,select_report
from ledger.commission_reports import attributed_profit,suggested_payouts

@pytest.mark.parametrize('level',['person','team','product','order'])
def test_aggregate_approval_keeps_source_income_but_no_zero_cost_or_person_profit(model,level):
    original=sample(statement=statement(),projections=projections(),cost_input_scope='approved_store_period_total')
    before=deepcopy(original)
    month=build_month([original],model,'2030-01','10')
    store=select_report(month)
    assert store['summary']['goods_cost']=='40.00' and store['summary']['profit']=='16.00'
    assert store['summary']['profit_after_labor']=='6.00'
    detail=select_report(month,level=level)
    assert detail['summary']['net_income']=='80.00'
    for key in ['goods_cost','gross_profit','profit','profit_after_labor','commission']:
        assert detail['summary'][key] is None
        assert all(row['metrics'][key] is None for row in detail['rows'])
    assert all(row['rank'] is None for row in detail['rows'])
    assert detail['is_final'] is False
    assert detail['issues']
    assert original==before

def test_complete_source_without_aggregate_override_keeps_proven_person_financials(model):
    month=build_month([sample()],model,'2030-01','10')
    detail=select_report(month,level='person')
    assert detail['summary']['goods_cost']=='40.00'
    assert detail['summary']['profit_after_labor']=='6.00'

def test_no_suggested_or_attributed_person_profit_from_aggregate_cost_even_single_owner():
    commission={'period_cost_input_scope':'store_period_only','base_total':162,'amount_complete':False,
      'people':[{'person_id':'p1','sales':200,'gross':None,'amount':16.2,'allocated_sales':200,'allocated_gross':162,'allocated_profit':162}]}
    before=deepcopy(commission)
    assert attributed_profit(commission,160,4,None)=={}
    assert suggested_payouts(commission,4,operating=160)=={}
    assert commission==before

def test_independently_confirmed_payout_amount_survives_without_implying_person_profit():
    commission={'period_cost_input_scope':'store_period_only','manual_amounts_after_labor':True,'manual_confirmed':True,
      'base_total':162,'people':[{'person_id':'p1','amount':12.34}]}
    assert suggested_payouts(commission,4,operating=160)=={'p1':12.34}
    assert attributed_profit(commission,160,4,None)=={}

@pytest.mark.parametrize('production',[True,False])
def test_person_sales_survive_but_no_gross_is_inferred_from_store_control(production):
    from ledger.commission_reports import attributed_outputs
    commission={'period_cost_input_scope':'store_period_only','amount_complete':False,
      'people':[{'person_id':'p1','sales':200,'gross':None,'allocated_sales':200,'allocated_gross':162}]}
    before=deepcopy(commission)
    result=attributed_outputs(commission,200,160,None,production=production)
    assert result['p1']['sales']==200
    assert result['p1']['gross'] is None
    assert result['p1']['cost_input_scope']=='store_period_only'
    assert commission==before
