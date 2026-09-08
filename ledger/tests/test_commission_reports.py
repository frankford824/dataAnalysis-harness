"""Stored payout reporting must not change money or substitute closed snapshots."""
import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.commission_api import install
from ledger.commission_registry import Registry
from ledger.model.schema import Store
from ledger.workspace import Workspace
from test_commission import _model


def fixture(tmp_path):
    ws = Workspace(tmp_path)
    registry = Registry(tmp_path)
    model = _model(stores=tuple(Store(id=f's{i}', name=f'店铺{i}', platform='taobao') for i in range(1, 4)))
    people = [registry.person_save({'name': name, 'employee_no': f'000{i}'}, 'test', '登记')
              for i, name in enumerate(['甲', '乙', '无记录人员'])]
    app = FastAPI()
    install(app, lambda: ws, lambda: model)
    return ws, registry, people, TestClient(app)


def record(ws, people, store, period, amounts, *, complete=True, legacy=False):
    rows = [{'person_id': p['id'], 'person': p['name'], 'amount': amount, 'base': 100}
            for p, amount in zip(people, amounts)]
    if legacy:
        for row in rows:
            row.pop('person_id')
    from decimal import Decimal
    return ws.record(store, period, {'can_close': True, 'findings': [], 'missing_sources': [],
        'commission': {'engine': 'legacy' if legacy else 'commission-v2', 'people': rows,
                       'total': float(sum((Decimal(str(a)) for a in amounts), Decimal(0))),
                       'amount_complete': complete, 'base_name': '利润', 'unassigned_orders': 0 if complete else 2}}, [])


@pytest.mark.parametrize('stores,indices,expected', [
    ([], [0], 13.32), ([], [0, 1], 36.65), (['s1'], [], 33.32),
    (['s1', 's2'], [], 36.65), (['s2'], [0], 3.33), (['s2'], [1], 0),
])
def test_people_and_store_filters_intersect_without_duplicate_runs(tmp_path, stores, indices, expected):
    ws, _, people, client = fixture(tmp_path)
    record(ws, people, 's1', '2026-06', [999, 888])  # superseded
    record(ws, people, 's1', '2026-06', [10.01, 20.02])
    record(ws, people, 's1', '2026-07', [-.02, 3.31])
    record(ws, people, 's2', '2026-06', [3.33], complete=False)
    result = client.post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-07', 'store_ids': stores,
        'person_ids': [people[i]['id'] for i in indices]})
    assert result.status_code == 200, result.text
    report = result.json()
    assert report['total'] == expected
    assert len(report['run_ids']) == (1 if stores == ['s2'] else 2 if stores == ['s1'] else 3)
    if stores == ['s2'] and indices == [1]:
        assert report['people'][0]['amount'] is None


def test_export_pins_viewed_runs_and_never_replaces_closed_result(tmp_path):
    ws, _, people, c = fixture(tmp_path)
    closed = record(ws, people, 's1', '2026-06', [12.34])
    ws.close_period('s1', '2026-06', by='test', note='结账')
    record(ws, people, 's1', '2026-06', [999])
    opened = record(ws, people, 's2', '2026-06', [-1.23])
    scope = {'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1', 's2']}
    report = c.post('/api/commission-v2/reports/query', json=scope).json()
    assert report['total'] == 11.11
    assert report['run_ids'] == [closed, opened]
    record(ws, people, 's2', '2026-06', [888])
    export_scope = {**scope, 'run_ids': report['run_ids'], 'fingerprint': report['fingerprint']}
    for kind, amount_column in [('people', '提成金额'), ('stores', '提成金额'),
                                ('breakdown', '提成金额'), ('coverage', '筛选范围提成金额')]:
        response = c.post('/api/commission-v2/export/reports/'+kind, json=export_scope)
        assert response.status_code == 200, response.text
        assert response.content.startswith(b'\xef\xbb\xbf')
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
        from decimal import Decimal
        assert sum(Decimal(r[amount_column]) for r in rows) == Decimal('11.11')
        if kind == 'people':
            assert rows[0]['工号'] == "'0000"
    assert ws.state('s1', '2026-06').run_id == closed
    ws.close_period('s2', '2026-06', by='test', note='状态变化')
    # The displayed open record remains historical; it must not silently become a closed payout.
    # Closing to a newer run doesn't change its open status or amounts, so export is still safe.
    assert c.post('/api/commission-v2/export/reports/people', json=export_scope).status_code == 200


def test_missing_zero_legacy_names_and_changed_state(tmp_path):
    ws, registry, people, c = fixture(tmp_path)
    zero = record(ws, people, 's1', '2026-06', [])
    scope = {'start': '2026-06', 'end': '2026-06'}
    report = c.post('/api/commission-v2/reports/query', json=scope).json()
    assert report['total'] == 0
    assert report['stores'][1]['amount'] is None
    assert report['missing_periods'] == 2
    ws.close_period('s1', '2026-06', by='test', note='结账')
    assert c.post('/api/commission-v2/export/reports/people', json={
        **scope, 'run_ids': [zero], 'fingerprint': report['fingerprint']}).status_code == 409
    record(ws, people, 's1', '2026-07', [1], legacy=True)
    record(ws, people, 's2', '2026-07', [2], legacy=True)
    scope = {'start': '2026-07', 'end': '2026-07'}
    report = c.post('/api/commission-v2/reports/query', json=scope).json()
    assert len(report['people']) == 2  # same name is not proof of identity
    assert len({p['person_id'] for p in report['people']}) == 2
    scope['person_ids'] = [report['people'][0]['person_id']]
    assert len(c.post('/api/commission-v2/reports/query', json=scope).json()['rows']) == 1


@pytest.mark.parametrize('patch', [
    {'start': '2026-13'}, {'end': '2026-05'}, {'end': '2037-01'},
    {'store_ids': ['unknown']}, {'person_ids': ['unknown']}, {'run_ids': [999]},
    {'run_ids': [1, 1]},
])
def test_invalid_scopes_fail_without_exporting_misleading_amounts(tmp_path, patch):
    _, _, _, c = fixture(tmp_path)
    scope = {'start': '2026-06', 'end': '2026-06', **patch}
    assert c.post('/api/commission-v2/reports/query', json=scope).status_code == 400
    assert c.post('/api/commission-v2/export/reports/people', json=scope).status_code == 400


def test_empty_commission_and_unknown_export_kind(tmp_path):
    ws, _, _, c = fixture(tmp_path)
    ws.record('s1', '2026-06', {'commission': {'notes': ['未算']}}, [])
    scope = {'start': '2026-06', 'end': '2026-06'}
    report = c.post('/api/commission-v2/reports/query', json=scope).json()
    assert report['total'] is None and report['missing_periods'] == 3
    assert c.post('/api/commission-v2/export/reports/unknown', json=scope).status_code == 400


def test_v2_legacy_name_ids_do_not_merge_across_stores(tmp_path):
    ws, _, _, c = fixture(tmp_path)
    historical = [{'id': 'legacy:same-name-hash', 'name': '同名人员'}]
    record(ws, historical, 's1', '2026-06', [10])
    record(ws, historical, 's2', '2026-06', [20])
    report = c.post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06'}).json()
    assert len(report['people']) == 2
    assert sorted(p['amount'] for p in report['people']) == [10, 20]
