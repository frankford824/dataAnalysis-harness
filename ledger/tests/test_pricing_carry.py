from datetime import date

import pytest

from ledger import pricing_carry
from ledger.pricing_carry import CarryError, after_sale_block, carry_candidates, carry_reason


def test_picked_carry_price_keeps_default_reason():
    assert carry_reason('2026-08-01', '') == '下单日成本依据不足，按 2026-08-01 沿用价人工确认补录'
    assert carry_reason('2026-08-01', '按采购单 P1') == '按采购单 P1'


@pytest.mark.parametrize('basis', ['', '   ', '原表'])
def test_manual_price_requires_basis(basis):
    with pytest.raises(CarryError, match='手填单价必须写明成本依据'):
        carry_reason('', basis)


def test_manual_price_records_basis():
    reason = carry_reason('', '供应商 8 月报价单 Q0819')
    assert '人工手填单价' in reason and reason.endswith('依据：供应商 8 月报价单 Q0819')


def test_no_cost_history_gives_no_candidates(monkeypatch):
    monkeypatch.setattr(pricing_carry, '_get', lambda *a, **k: {'skuId': 'X', 'current': None, 'history': []})
    assert carry_candidates('X', date(2026, 8, 31)) == []


def _after_sales(monkeypatch, *records):
    monkeypatch.setattr(pricing_carry, '_get', lambda *a, **k: {'after_sales': list(records)})


def test_after_sale_without_goods_blocks_every_line_of_the_order(monkeypatch):
    _after_sales(monkeypatch, {'after_sale_id': 'A1', 'goods_status_raw': '卖家已收到退货', 'returned_items': None})
    assert after_sale_block('O1', 'HQT22732')[2] == 'clear'


def test_candidates_two_each_side_nearest_first(monkeypatch):
    history = [{'effectiveOn': f'2026-0{m}-01', 'newCost': m} for m in range(3, 10)]
    monkeypatch.setattr(pricing_carry, '_get', lambda *a, **k: {'history': history})
    found = carry_candidates('X', date(2026, 6, 15))
    assert [(c['side'], c['effective_on']) for c in found] == [
        ('before', '2026-06-01'), ('before', '2026-05-01'), ('after', '2026-07-01'), ('after', '2026-08-01')]
