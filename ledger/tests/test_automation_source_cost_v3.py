"""V3 source proof primitives; these synthetic units grant no financial writes."""
from copy import deepcopy
from decimal import Decimal,localcontext
import pytest

from ledger import automation_source_cost_v3 as v3
from ledger.automation_cost_actions import Pending
from test_jst_source_members import fixture,prove,rebuild


def verified(f):
    return {'source_proof':prove(f),'plan':{'platform_order_id':'202600001','shop_ids':['77']}}


def test_original_decimal_quantity_and_quote_survive_until_aggregate():
    q='0.123456789012345678901234567890123456789';u='3.123456789012345678901234567890123456789'
    with localcontext() as context:
        context.prec=256;expected=Decimal(q)*Decimal(u)
    assert v3.exact_product(q,u)==expected
    assert sum((v3.exact_product('1','0.007') for _ in range(3)),Decimal(0))==Decimal('.021')
    assert v3.decimal_semantic('1.000')==v3.decimal_semantic('1')


@pytest.mark.parametrize('value',[True,1.25,'0','-1','NaN','Infinity','1E999999','1E-999999'])
def test_source_number_is_not_a_float_boolean_zero_or_exponent_bomb(value):
    with pytest.raises(Pending):v3.positive(value)


def test_container_status_and_optional_flag_absence_are_retained_with_positive_basis():
    result=verified(fixture(bundle=True));before=deepcopy(result)
    members=v3.selected_members(result,{'order_id':'202600001'},'alibaba1688')
    assert len(members)==2 and {m['quantity'] for m in members}=={'2','3'}
    assert {m['logical_quantity'] for m in members}=={'1.25'}
    assert all(m['observed_flags']['source_header']['status']['value']=='Split' for m in members)
    assert all(m['observed_flags']['physical_header']['status']['value']=='Sent' for m in members)
    assert all(m['observed_flags']['physical_member']['is_virtual']=={'present':False,'value':None} for m in members)
    assert result==before  # No fake false, root Sent or financial-ready mutation.


def test_missing_gift_flag_is_not_converted_into_not_gift():
    f=fixture();source,target=deepcopy(f[3]),deepcopy(f[4])
    source['items'][0].pop('is_gift');target['items'][0].pop('is_gift')
    result=verified(rebuild(f,source=source,target=target))
    assert result['source_proof']['status']=='proved'
    with pytest.raises(Pending,match='non_gift_positive_basis_missing'):
        v3.selected_members(result,{'order_id':'202600001'},'alibaba1688')


def test_sale_price_placeholder_does_not_invalidate_independently_proved_quantity():
    f=fixture();source,target=deepcopy(f[3]),deepcopy(f[4])
    source['items'][0]['sale_price']='99999';target['items'][0]['sale_price']='99999'
    members=v3.selected_members(verified(rebuild(f,source=source,target=target)),{'order_id':'202600001'},'alibaba1688')
    assert members[0]['quantity']=='1.25' and v3.exact_product(members[0]['quantity'],'3')==Decimal('3.75')


def test_explicit_unexplained_risk_stays_pending_without_claiming_quantity_is_invalid():
    f=fixture();target=deepcopy(f[4]);target['items'][0]['is_suspect']=True
    result=verified(rebuild(f,target=target))
    assert result['source_proof']['status']=='proved'
    with pytest.raises(Pending,match='explicit_source_member_risk_unresolved'):
        v3.selected_members(result,{'order_id':'202600001'},'alibaba1688')


def test_native_child_must_be_proved_in_child_grain_platforms():
    with pytest.raises(Pending,match='native_child_identity_unproved'):
        v3.selected_members(verified(fixture()),{'order_id':'202600001'},'taobao')


def test_quote_semantic_is_complete_price_interval_not_a_raw_capture_clock():
    quote={'schema':'ledger-history-quote.v1','sku':'A','company':'10','pricing_day':'2026-05-15',
        'unit_cost':'3.00','intervals':[{'begin_date':'2026-05-01','begin_unbounded':False,'end_date':'2026-06-01','unit_cost':'3'}],
        'source_response_sha':'old','quote_sha':'old','artifact_refs':[{'path':'old'}]}
    same=deepcopy(quote);same.update(source_response_sha='new',quote_sha='new',artifact_refs=[{'path':'new'}])
    assert v3.quote_semantic(same)==v3.quote_semantic(quote)
    for field in ('sku','company','pricing_day','unit_cost'):
        changed=deepcopy(quote);changed[field]='4' if field=='unit_cost' else 'changed'
        assert v3.quote_semantic(changed)!=v3.quote_semantic(quote)
    changed=deepcopy(quote);changed['intervals'][0]['end_date']='2026-06-02'
    assert v3.quote_semantic(changed)!=v3.quote_semantic(quote)
