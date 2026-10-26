"""One priced component must not certify all components sharing an order key."""
import csv
import io
from datetime import datetime

import polars as pl
import pytest

from ledger import cost_lines, cost_line_posting, service, view
from ledger.workspace import Workspace, WorkspaceError
from test_brushing_coverage import calculate, missing_cost, STORE


def scenario(*, resolved=False, known=True, pending_child='A1', pending_store=None):
    costs = []
    if known:
        costs.append({**missing_cost(), 'sku': 'priced-component', 'quantity': 2.,
                      'unit_cost': 2.5, 'cost_as_of': '2026-07-02',
                      'cost_source': 'history', 'cost_status': 'priced'})
    costs.append({**missing_cost(), 'sku': 'missing-component', 'sub_order_id': pending_child,
                  'store_name': pending_store or STORE.name, 'unit_cost': 12.584,
                  'cost_as_of': '2026-07-02', 'cost_source': 'history' if resolved else 'register',
                  'cost_status': 'priced'})
    costs.append({**missing_cost(), 'sku': 'sibling', 'sub_order_id': 'A2',
                  'unit_cost': 3., 'cost_as_of': '2026-07-02',
                  'cost_source': 'history', 'cost_status': 'priced'})
    return calculate(pl.DataFrame(), costs=costs)


def test_partial_order_keeps_known_money_but_is_incomplete_and_listed():
    result = scenario(); sl = result.slices[(STORE.name, '2026-07')]
    assert sl.cost_coverage['expected'] == 3
    assert sl.cost_coverage['covered'] == 1  # only the fully priced sibling A2
    assert sl.facts.filter(pl.col('link_key') == 'A1')['contribution'].sum() == -5
    gap = sl.coverage_gap_rows.filter(pl.col('coverage_key') == 'A1').row(0, named=True)
    assert gap['partial_cost'] and not gap['editable']
    assert gap['posted_goods_cost'] == 5 and gap['pending_cost_rows'] == 1
    assert gap['pending_cost_skus'] == 'missing-component'
    assert '勿重复补录整单' in gap['coverage_reason']
    assert result.pricing_gaps['coverage_key'].to_list() == ['A1']


def test_resolving_last_component_restores_coverage_without_changing_denominator():
    sl = scenario(resolved=True).slices[(STORE.name, '2026-07')]
    assert sl.cost_coverage['expected'] == 3 and sl.cost_coverage['covered'] == 2
    assert sl.coverage_gap_rows['coverage_key'].to_list() == ['B1']
    assert sl.facts.filter(pl.col('link_key') == 'A1')['contribution'].sum() == pytest.approx(-17.584)


@pytest.mark.parametrize('zero_sku', ['HQT00365', 'HQT01773'])
def test_zero_cost_accessory_cannot_exempt_an_unpriced_physical_component(zero_sku):
    costs = [{**missing_cost(), 'sku':zero_sku, 'unit_cost':.0001},
             {**missing_cost(), 'sku':'physical-component'}]
    sl = calculate(pl.DataFrame(), costs=costs).slices[(STORE.name,'2026-07')]
    assert sl.cost_coverage['covered'] == 0
    gap = sl.coverage_gap_rows.filter(pl.col('coverage_key')=='A1').row(0,named=True)
    assert gap['partial_cost'] and gap['posted_goods_cost'] == 0
    assert gap['pending_cost_skus'] == 'physical-component'


def test_unlinked_component_does_not_quarantine_another_child_of_same_master():
    sl = scenario(pending_child='not-an-existing-child').slices[(STORE.name, '2026-07')]
    # Master fallback may split to its actual children; the resolved link keys,
    # rather than a guess that the source child string is an eligible key, govern.
    assert set(sl.coverage_gap_rows['coverage_key']) == {'A1', 'A2', 'B1'}
    assert sl.cost_coverage['covered'] == 0
    assert sl.cost_coverage['expected'] == 3


def test_reported_seven_component_bundle_is_not_complete_when_only_four_are_priced():
    order = '5127395798395028536'
    orders = [dict(order_id=order, sub_order_id=order, product_id='bundle', store_name=STORE.name,
                   order_type='销售订单', tracking_no='tracking', order_time=datetime(2026, 8, 31))]
    entries = [('CGK001033',1.,12.584,'register'), ('CGK001035',1.,2.178,'register'),
               ('CGK001858',1.,3.71,'register'), ('HQT00963',1.,.2766,'history'),
               ('HQT02063',2.,1.045,'history'), ('HQT02065',6.,.308,'history'),
               ('HZS09320',4.,.4145,'history')]
    costs = [{**missing_cost(), 'order_id':order, 'original_order_id':order, 'sub_order_id':order,
              'internal_order_id':'16323658', 'order_time':datetime(2026,8,31),
              'sku':sku,'quantity':qty,'unit_cost':price,'cost_source':source,
              'cost_status':'priced','cost_as_of':'2026-08-31'} for sku,qty,price,source in entries]
    result = calculate(pl.DataFrame(),orders=orders,costs=costs)
    sl = result.slices[(STORE.name,'2026-08')]
    assert sl.facts['contribution'].sum() == pytest.approx(-5.8726)
    assert sl.cost_coverage['expected'] == 1 and sl.cost_coverage['covered'] == 0
    gap = sl.coverage_gap_rows.row(0,named=True)
    assert gap['coverage_key'] == order and gap['pending_cost_rows'] == 3
    assert gap['posted_goods_cost'] == pytest.approx(5.8726)
    assert set(gap['pending_cost_skus'].split('、')) == {'CGK001033','CGK001035','CGK001858'}


def test_partial_orders_reject_whole_order_manual_and_batch_supplements(tmp_path):
    result = scenario(); sl = result.slices[(STORE.name, '2026-07')]
    ws = Workspace(tmp_path); store = result.model.store('s')
    rid = ws.record('s', '2026-07', view.slice_dict(sl, store, result.model), [])
    service._keep_facts(ws, rid, sl)
    gap = next(r for r in cost_lines.current(ws, rid, 's', '2026-07')['items'] if r['coverage_key'] == 'A1')
    assert not gap['editable']
    with pytest.raises(WorkspaceError, match='部分商品成本'):
        cost_lines.save(ws, 's', '2026-07', rid, coverage_key='A1', context_sha=gap['context_sha'],
                        amount='17.58', reason='would double count the already posted part')
    exported = list(csv.DictReader(io.StringIO(cost_lines.export_csv(ws, rid, 's', '2026-07'))))
    for row in exported:
        row['人工补录总成本'] = '17.58' if row['清单键'] == 'A1' else ''
    buf = io.StringIO(); writer = csv.DictWriter(buf, fieldnames=exported[0].keys())
    writer.writeheader(); writer.writerows(exported)
    preview = cost_lines.batch_preview(ws, 's', '2026-07', rid, buf.getvalue().encode(), 'QA')
    assert preview['valid'] == 0 and preview['issue_count'] == 1
    assert '部分商品成本' in preview['issues'][0]
    assert ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0] == 0


def test_old_whole_order_supplement_cannot_repost_on_partial_source_cost(tmp_path):
    old = scenario(known=False); store = old.model.store('s'); ws = Workspace(tmp_path)
    sl = old.slices[(STORE.name, '2026-07')]
    rid = ws.record('s', '2026-07', view.slice_dict(sl, store, old.model), [])
    service._keep_facts(ws, rid, sl)
    gap = sl.coverage_gap_rows.filter(pl.col('coverage_key') == 'A1').row(0, named=True)
    cost_lines.save(ws, 's', '2026-07', rid, coverage_key='A1', context_sha=gap['context_sha'],
                    amount='17.58', reason='old whole-order amount')
    current = scenario(); cost_line_posting.apply(ws, current, store)
    newer = current.slices[(STORE.name, '2026-07')]
    assert newer.cost_line_posting['count'] == 0
    assert newer.facts.filter(pl.col('link_key') == 'A1')['contribution'].sum() == -5
    assert newer.coverage_gap_rows.filter(pl.col('coverage_key') == 'A1')['context_sha'][0] != gap['context_sha']
    assert ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0] == 1
