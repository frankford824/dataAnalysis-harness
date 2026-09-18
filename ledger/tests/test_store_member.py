"""Tests for store_member CRUD and duty-based attribution."""
import json
from decimal import Decimal

import pytest

from ledger.commission_registry import Registry, RegistryError
from ledger.commission_reports import (
    attributed_outputs,
    attributed_profit,
    store_member_duties,
)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_store_member_crud(tmp_path):
    reg = Registry(tmp_path)
    alice = reg.person_save({'name': '甲'}, 'test', '登记')
    bob = reg.person_save({'name': '乙'}, 'test', '登记')

    assert reg.store_members('s1') == []

    r = reg.save_store_member('s1', alice['id'], 'produce', '', 'test', '设置身份')
    assert r['duty'] == 'produce'
    assert r['revision'] == 1

    r2 = reg.save_store_member('s1', bob['id'], 'cut', alice['id'], 'test', '设置身份')
    assert r2['duty'] == 'cut'
    assert r2['leader_id'] == alice['id']

    members = reg.store_members('s1')
    assert len(members) == 2

    r3 = reg.save_store_member('s1', bob['id'], 'produce', '', 'test', '改为做货')
    assert r3['revision'] == 2
    assert r3['duty'] == 'produce'


def test_save_store_member_validates_duty(tmp_path):
    reg = Registry(tmp_path)
    alice = reg.person_save({'name': '甲'}, 'test', '登记')
    with pytest.raises(RegistryError, match='身份应为做货或抽点'):
        reg.save_store_member('s1', alice['id'], 'invalid', '', 'test', '测试')


def test_save_store_member_validates_person(tmp_path):
    reg = Registry(tmp_path)
    with pytest.raises(RegistryError, match='人员不存在'):
        reg.save_store_member('s1', 'nonexistent', 'produce', '', 'test', '测试')


def test_save_store_member_validates_leader(tmp_path):
    reg = Registry(tmp_path)
    alice = reg.person_save({'name': '甲'}, 'test', '登记')
    with pytest.raises(RegistryError, match='所属组长不存在'):
        reg.save_store_member('s1', alice['id'], 'produce', 'nonexistent', 'test', '测试')


# ---------------------------------------------------------------------------
# attributed_outputs with duties
# ---------------------------------------------------------------------------

def _commission(people_data):
    """Build a minimal commission dict."""
    return {'people': people_data, 'engine': 'commission-v2'}


def test_attributed_outputs_gives_all_to_producers(tmp_path):
    """With duties, producers get 100% of sales/gross; cut members get 0."""
    reg = Registry(tmp_path)
    alice = reg.person_save({'name': '甲'}, 'test', '登记')
    bob = reg.person_save({'name': '乙'}, 'test', '登记')

    reg.save_store_member('s1', alice['id'], 'produce', '', 'test', '做货')
    reg.save_store_member('s1', bob['id'], 'cut', '', 'test', '抽点')
    duties = store_member_duties(reg, 's1')

    commission = _commission([
        {'person_id': alice['id'], 'person': '甲',
         'allocated_sales': 800, 'allocated_gross': 400, 'amount': 10},
        {'person_id': bob['id'], 'person': '乙',
         'allocated_sales': 200, 'allocated_gross': 100, 'amount': 5},
    ])
    result = attributed_outputs(commission, 1000, 500, reg, duties=duties)

    assert result[alice['id']]['sales'] == 1000
    assert result[alice['id']]['gross'] == 500
    assert result[bob['id']]['sales'] == 0.0
    assert result[bob['id']]['gross'] == 0.0


def test_attributed_profit_gives_all_to_producers(tmp_path):
    """With duties, producers get 100% profit; cut members get 0."""
    reg = Registry(tmp_path)
    alice = reg.person_save({'name': '甲'}, 'test', '登记')
    bob = reg.person_save({'name': '乙'}, 'test', '登记')

    reg.save_store_member('s1', alice['id'], 'produce', '', 'test', '做货')
    reg.save_store_member('s1', bob['id'], 'cut', '', 'test', '抽点')
    duties = store_member_duties(reg, 's1')

    commission = _commission([
        {'person_id': alice['id'], 'person': '甲',
         'allocated_sales': 800, 'allocated_gross': 400,
         'allocated_profit': 300, 'amount': 10},
        {'person_id': bob['id'], 'person': '乙',
         'allocated_sales': 200, 'allocated_gross': 100,
         'allocated_profit': 100, 'amount': 5},
    ])
    result = attributed_profit(commission, 400, 0, reg, duties=duties)

    assert result[alice['id']] == 400
    assert result[bob['id']] == 0.0


def test_single_person_link_still_gets_output(tmp_path):
    """A 1-person link (even if person is 'cut') gives them everything."""
    reg = Registry(tmp_path)
    alice = reg.person_save({'name': '甲'}, 'test', '登记')

    reg.save_store_member('s1', alice['id'], 'cut', '', 'test', '抽点')
    duties = store_member_duties(reg, 's1')

    commission = _commission([
        {'person_id': alice['id'], 'person': '甲',
         'allocated_sales': 1000, 'allocated_gross': 500, 'amount': 20},
    ])
    result = attributed_outputs(commission, 1000, 500, reg, duties=duties)

    assert result[alice['id']]['sales'] == 1000
    assert result[alice['id']]['gross'] == 500


def test_no_duties_preserves_original_behavior(tmp_path):
    """Without duties, the original split behavior is unchanged."""
    reg = Registry(tmp_path)
    alice = reg.person_save({'name': '甲'}, 'test', '登记')
    bob = reg.person_save({'name': '乙'}, 'test', '登记')

    commission = _commission([
        {'person_id': alice['id'], 'person': '甲',
         'allocated_sales': 800, 'allocated_gross': 400, 'amount': 10},
        {'person_id': bob['id'], 'person': '乙',
         'allocated_sales': 200, 'allocated_gross': 100, 'amount': 5},
    ])
    result = attributed_outputs(commission, 1000, 500, reg)
    assert result[alice['id']]['sales'] == 800
    assert result[bob['id']]['sales'] == 200


def test_all_producers_no_change(tmp_path):
    """When all members are producers, behavior is unchanged."""
    reg = Registry(tmp_path)
    alice = reg.person_save({'name': '甲'}, 'test', '登记')
    bob = reg.person_save({'name': '乙'}, 'test', '登记')

    reg.save_store_member('s1', alice['id'], 'produce', '', 'test', '做货')
    reg.save_store_member('s1', bob['id'], 'produce', '', 'test', '做货')
    duties = store_member_duties(reg, 's1')

    commission = _commission([
        {'person_id': alice['id'], 'person': '甲',
         'allocated_sales': 800, 'allocated_gross': 400, 'amount': 10},
        {'person_id': bob['id'], 'person': '乙',
         'allocated_sales': 200, 'allocated_gross': 100, 'amount': 5},
    ])
    result = attributed_outputs(commission, 1000, 500, reg, duties=duties)
    assert result[alice['id']]['sales'] == 800
    assert result[bob['id']]['sales'] == 200


def test_store_members_for_stores(tmp_path):
    reg = Registry(tmp_path)
    alice = reg.person_save({'name': '甲'}, 'test', '登记')
    reg.save_store_member('s1', alice['id'], 'produce', '', 'test', '店1')
    reg.save_store_member('s2', alice['id'], 'cut', '', 'test', '店2')
    rows = reg.store_members_for_stores(['s1', 's2'])
    assert {(r['store_id'], r['duty']) for r in rows} == {('s1', 'produce'), ('s2', 'cut')}


def test_duty_time_segments_keep_earlier_period(tmp_path):
    reg = Registry(tmp_path)
    song = reg.person_save({'name': '宋永康'}, 'test', '登记')
    zong = reg.person_save({'name': '宗玲'}, 'test', '登记')
    reg.save_store_member('s1', song['id'], 'produce', '', 'test', '6月做货',
                          valid_from='2026-06-01T00:00:00', valid_to='2026-08-01T00:00:00')
    reg.save_store_member('s1', zong['id'], 'produce', '', 'test', '6月做货',
                          valid_from='2026-06-01T00:00:00', valid_to='2026-08-01T00:00:00')
    reg.save_store_member('s1', song['id'], 'produce', '', 'test', '8月后只宋做货',
                          valid_from='2026-08-01T00:00:00')
    june = store_member_duties(reg, 's1', '2026-06')
    august = store_member_duties(reg, 's1', '2026-08')
    assert june[song['id']]['duty'] == 'produce'
    assert june[zong['id']]['duty'] == 'produce'
    assert august[song['id']]['duty'] == 'produce'
    assert zong['id'] not in august
    reg.save_store_member('s1', song['id'], 'produce', '', 'test', '再改6月不应盖掉8月',
                          valid_from='2026-06-01T00:00:00')
    june = store_member_duties(reg, 's1', '2026-06')
    august = store_member_duties(reg, 's1', '2026-08')
    assert june[song['id']]['duty'] == 'produce'
    assert june[zong['id']]['duty'] == 'produce'
    assert august[song['id']]['duty'] == 'produce'
    assert zong['id'] not in august
