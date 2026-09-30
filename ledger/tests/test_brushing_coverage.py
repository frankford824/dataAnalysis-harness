import json
from datetime import datetime

import polars as pl
import pytest

from conftest import MODELS
from test_reship_period_and_cost_drill import item
from ledger.brushing_zero import capture
from ledger.engine.runtime import Ingestion, run
from ledger.model.loader import load_model
from ledger.model.schema import Model, Store, SourceContract, StatementNode, Check
from ledger.order_feed import OrderFeed
from ledger.order_flags import seller_flags, fill_missing, member_cost_flags


STORE = Store(id='s', name='一号店', platform='taobao')


def header(internal='15617602', parents=('A', 'B'), remark='by陈慨发空包;by 陈慨发空包', **extra):
    return {'order_id': internal, 'order_store_id': '17310409',
            'seller_flag_code': '4', 'seller_flag_name': '蓝色旗帜',
            'seller_flag_source': 'jst_order.extra.seller_flag',
            'seller_flag_scope': 'internal_order_header', 'order_remark': remark,
            'platform_order_ids': json.dumps(list(parents)),
            'platform_order_identity_complete': True,
            'platform_order_identity_scope': 'same_store_header_membership',
            'source_evidence_version': 'jst-header-evidence.v1', **extra}


def member(parent='A', **extra):
    return {'platform_order_id': parent, 'original_internal_order_id': 'original-' + parent,
            'order_store_id': '17310409', 'seller_flag_code': '4', 'seller_flag_name': '蓝色旗帜',
            'order_remark': 'by陈慨发空包', 'scope': 'platform_order_header', 'source': 'jst.order_detail',
            'captured_at': '2026-09-30T08:00:00Z', 'source_modified': '2026-07-03 08:00:00',
            'evidence_sha': 'a' * 64, 'status': 'verified', 'reason': None,
            'source_is_merge': False, **extra}


def member_header(members, **extra):
    return header(platform_seller_evidence_json=json.dumps({'version': 'jst-member-evidence.v1',
        'root_order_id': '15617602', 'order_store_id': '17310409', 'members': members, 'complete': False}),
        platform_seller_evidence_version='jst-member-evidence.v1', **extra)


def evidence(headers=None, flags=None):
    headers = headers or [header()]
    orders = pl.DataFrame(headers)
    items = pl.DataFrame({'order_id': [h['order_id'] for h in headers],
                          'order_flag': flags or [None] * len(headers)}, schema_overrides={'order_flag': pl.String})
    return capture(orders, seller_flags(items, orders), STORE)


def calculate(proof, *, platform='taobao', orders=None, costs=None, scope='platform_order'):
    full = load_model(MODELS / 'cn-ecommerce')
    metric = full.metric('goods_cost').for_platform(platform)
    metric = metric.model_copy(update={'brushing_scope': scope})
    model = Model(id='brushing-coverage', name='刷单成本核对', platforms=full.platforms,
        stores=(Store(id='s', name=STORE.name, platform=platform), Store(id='s2', name='二号店', platform=platform)),
        sources=(SourceContract(id='order_detail', name='订单', is_spine=True, owner_role='shop_owner', cadence='monthly'),
                 SourceContract(id='order_cost', name='成本', owner_role='shop_owner', cadence='monthly')),
        metrics=(metric,), checks=(Check(id='coverage', name='商品成本覆盖', kind='spine_coverage', metric='goods_cost', threshold=.95),),
        statement=(StatementNode(id='cost', name='商品成本', formula={'op': 'add', 'of': ['goods_cost']}),))
    orders = orders or [dict(order_id='A', sub_order_id='A1', product_id='P1',
        store_name=STORE.name, order_type='销售订单', tracking_no='T1',
        order_time=datetime(2026, 7, 2)), dict(order_id='A', sub_order_id='A2', product_id='P2',
        store_name=STORE.name, order_type='销售订单', tracking_no='T2', order_time=datetime(2026, 7, 2)),
        dict(order_id='B', sub_order_id='B1', product_id='P3', store_name=STORE.name,
             order_type='销售订单', tracking_no='T3', order_time=datetime(2026, 7, 2))]
    frames = [item('order_detail', orders, orders[0].keys())]
    if costs:
        frames.append(item('order_cost', costs, costs[0].keys()))
    return run(Ingestion(model=model, items=frames, brushing_evidence=proof), platform)


def missing_cost(store=STORE.name):
    return dict(order_id='A', original_order_id='A', sub_order_id='A1', internal_order_id='15617602',
        sku='SKU', order_type='销售订单', order_state='Sent', store_name=store,
        order_time=datetime(2026, 7, 2), quantity=1., unit_cost=None,
        cost_source=None, cost_status='pending')


@pytest.mark.parametrize('platform', ['taobao', 'douyin', 'alibaba1688', 'jd'])
def test_merged_brushing_orders_are_known_zero_without_physical_cost_rows(platform):
    result = calculate(evidence(), platform=platform, scope='internal_order_header')
    sl = result.slices[(STORE.name, '2026-07')]
    assert sl.nodes['cost'].value == 0
    assert sl.cost_coverage['uncovered'] == 0
    assert sl.cost_coverage['coverage'] == 1
    assert sl.coverage_gap_rows.is_empty() and result.pricing_gaps.is_empty()
    assert sl.cost_coverage['brushing_zero_covered'] == sl.cost_coverage['expected']
    assert all('规则确认零成本' in text for text in result.facts['source_note'])
    assert all(value == 0 for value in result.facts['contribution'])
    assert all('seller_flag_code' in text for text in result.facts['source_note'])


@pytest.mark.parametrize('change', [
    {'seller_flag_code': '1', 'seller_flag_name': '红色旗帜'},
    {'seller_flag_code': None, 'seller_flag_name': None},
    {'seller_flag_code': '4', 'seller_flag_name': '红色旗帜'},
    {'platform_order_identity_complete': False},
    {'platform_order_identity_scope': 'another_store'},
    {'remark': 'by陈慨发空包;正常发货'},
    {'remark': 'by陈慨买家秀'},
    {'remark': 'baby活动'},
])
def test_incomplete_or_conflicting_rules_never_exempt_missing_cost(change):
    result = calculate(evidence([header(**change)]), costs=[missing_cost()])
    sl = result.slices[(STORE.name, '2026-07')]
    assert sl.cost_coverage['uncovered'] == 3
    assert sl.coverage_gap_rows.height == 3
    assert sl.cost_coverage.get('brushing_zero_covered', 0) == 0


def test_mixed_header_sharing_a_platform_order_blocks_blanket_exemption():
    proof = evidence([header(), header(internal='other', parents=('A',), remark='普通发货',
        seller_flag_code='1', seller_flag_name='红色旗帜')])
    result = calculate(proof, scope='internal_order_header')
    assert result.slices[(STORE.name, '2026-07')].coverage_gap_rows['order_id'].unique().to_list() == ['A']


def test_same_order_number_in_another_store_cannot_inherit_the_rule():
    orders = [dict(order_id='A', sub_order_id='A1', product_id='P1', store_name='二号店',
                   order_type='销售订单', tracking_no='T', order_time=datetime(2026, 7, 2))]
    result = calculate(evidence(), orders=orders, costs=[missing_cost('二号店')])
    assert result.slices[('二号店', '2026-07')].cost_coverage['uncovered'] == 1


def test_unknown_member_stays_pending_when_only_another_member_is_proven():
    # The merged A/B header's mark cannot prove B. A has a separate single-order
    # record; B has no independent original flag/remark evidence.
    proof = evidence([header(), header(internal='original-A', parents=('A',), remark='by陈慨')])
    result = calculate(proof, costs=[missing_cost()])
    sl = result.slices[(STORE.name, '2026-07')]
    assert sl.coverage_gap_rows['order_id'].to_list() == ['B']
    assert set(result.facts['order_id'].drop_nulls()) == {'A'}


def test_unproven_members_are_not_exempt_under_the_default_rule():
    result = calculate(evidence(), costs=[missing_cost()])
    assert result.slices[(STORE.name, '2026-07')].cost_coverage['uncovered'] == 3


def test_independent_member_proof_exempts_only_its_verified_parent():
    result = calculate(evidence([member_header([member('A'), member('B', status='missing')])]), costs=[missing_cost()])
    assert result.slices[(STORE.name, '2026-07')].coverage_gap_rows['order_id'].to_list() == ['B']


@pytest.mark.parametrize('override', [{'source_is_merge': True}, {'order_store_id': '18'},
    {'scope': 'internal_order_header'}, {'source': 'copied_root_header'}, {'evidence_sha': 'bad'},
    {'captured_at': None}, {'order_remark': 'by买家秀'}, {'status': 'conflict'}])
def test_incomplete_member_records_never_authorize_zero(override):
    result = calculate(evidence([member_header([member('A', **override)])]), costs=[missing_cost()])
    assert result.slices[(STORE.name, '2026-07')].cost_coverage['uncovered'] == 3


def test_independent_blue_member_of_a_mixed_merge_is_preserved():
    proof = evidence([member_header([member('A'), member('B', seller_flag_code='1', seller_flag_name='红色旗帜',
                   order_remark='普通发货')], remark='by陈慨;普通发货')])
    result = calculate(proof, costs=[missing_cost()])
    assert result.slices[(STORE.name, '2026-07')].coverage_gap_rows['order_id'].to_list() == ['B']


def test_existing_cost_row_of_missing_member_is_not_exempted_by_root_flag():
    headers = pl.DataFrame([member_header([member('A'), member('B', status='missing',
        original_internal_order_id='15893416', seller_flag_code=None, seller_flag_name=None,
        order_remark=None, reason='original_seller_flag_missing')])])
    items = pl.DataFrame({'order_id': ['15617602'], 'order_flag': ['蓝色旗帜'],
                          'outer_sku': ['B1'], 'online_order_no': ['17310409:B']})
    protected = member_cost_flags(items, headers, STORE, {'B1': 'B'})
    assert protected['__brushing_member_pending'].item()
    from ledger.engine.cost_policy import is_brushing
    assert not protected.with_columns(pl.lit('by陈慨发空包').alias('order_remark')).select(is_brushing(
        protected.with_columns(pl.lit('by陈慨发空包').alias('order_remark')))).item()
    cost = {**missing_cost(), 'order_id':'B', 'original_order_id':'B', 'sub_order_id':'B1',
        'order_flag':'蓝色旗帜','order_remark':'by陈慨发空包','unit_cost':10.,'cost_source':'history',
        'cost_status':'priced','cost_as_of':'2026-07-02',
        'pricing_evidence':json.dumps({'order_date':'2026-07-02'}), '__brushing_member_pending':True}
    result = calculate(evidence(headers.to_dicts()), costs=[cost])
    assert result.facts['contribution'].sum() == -10


def test_existing_member_cost_uses_its_own_remark_not_root_by():
    headers = pl.DataFrame([member_header([member('B', order_remark='买家秀')])])
    items = pl.DataFrame({'order_id':['15617602'],'order_flag':['蓝色旗帜'],'outer_sku':['B1'],
                          'online_order_no':['17310409:B']})
    protected = member_cost_flags(items, headers, STORE, {'B1':'B'})
    assert protected['__member_order_remark'].item() == '买家秀'


def test_child_flag_conflict_cannot_be_overwritten_by_upload_fallback():
    items = pl.DataFrame({'order_id': ['15617602'], 'order_flag': [None],
                          'seller_flag_code': ['1'], 'seller_flag_name': ['红色旗帜']},
                         schema_overrides={'order_flag': pl.String})
    out = seller_flags(items, pl.DataFrame([header()]))
    out = fill_missing(out, 'order_id', {'15617602': '蓝色旗帜'})
    assert out['order_flag'].item() is None
    assert out['__seller_flag_conflict'].item()
    assert capture(pl.DataFrame([header()]), out, STORE).is_empty()


def test_pricing_context_does_not_override_independent_raw_seller_flag():
    items = pl.DataFrame({'order_id': ['15617602'], 'order_flag': ['红色旗帜']})
    out = seller_flags(items, pl.DataFrame([header()]))
    assert out['order_flag'].item() == '蓝色旗帜'
    assert not out['__seller_flag_conflict'].item()
    assert capture(pl.DataFrame([header()]), out, STORE).height == 1


def test_buyers_show_cost_remains_charged_and_ordinary_gap_remains():
    cost = dict(order_id='A', original_order_id='A', sub_order_id='A1', internal_order_id='15617602',
        sku='SKU', order_flag='蓝色旗帜', order_remark='by陈慨买家秀', order_type='销售订单',
        order_state='Sent', store_name=STORE.name, order_time=datetime(2026, 7, 2),
        quantity=1., unit_cost=10., cost_source='history', cost_status='priced', cost_as_of='2026-07-02',
        pricing_evidence=json.dumps({'order_date': '2026-07-02'}))
    result = calculate(evidence([header(remark='by陈慨买家秀')]), costs=[cost])
    sl = result.slices[(STORE.name, '2026-07')]
    assert result.facts['contribution'].sum() == -10
    assert sl.nodes['cost'].value is None  # Partial evidence must not look complete.
    assert set(sl.coverage_gap_rows['coverage_key']) == {'A2', 'B1'}


def test_snapshot_delta_preserves_new_fields_and_false_completeness():
    base = pl.DataFrame({'order_id': ['15617602'], 'online_order_no': ['A,B']})
    row = header(platform_order_identity_complete=False)
    delta = {'entity_type': 'order', 'entity_id': '15617602', 'operation': 'upsert',
             'payload_json': json.dumps({'order': row}), 'order_id': '15617602'}
    overlaid = OrderFeed._overlay(base, [delta], 'order', 'order_id', lambda p: [p['order']])
    assert overlaid['seller_flag_name'].item() == '蓝色旗帜'
    assert overlaid['source_evidence_version'].item() == 'jst-header-evidence.v1'
    assert capture(overlaid, seller_flags(pl.DataFrame({'order_id': ['15617602']}), overlaid), STORE).is_empty()
