"""Manual close and payout confirmation must agree about allocation risk."""
import json

from test_commission_reports import fixture


def _pending_run(ws, people, *, blocking=True):
    return ws.record('s1', '2026-06', {
        'can_close': not blocking,
        'findings': ([{'id': 'allocation_basis', 'name': '分配依据待核对',
                      'message': '2 项待核对', 'blocking': True, 'passed': False}]
                     if blocking else []),
        'missing_sources': [],
        'allocation_pending': [{'metric_id': 'freight_cost', 'order_id': 'order-1',
                                'amount': -1.2, 'reason': 'zero_net_payment'}],
        'allocation_pending_count': 2,
        'commission': {'engine': 'commission-v2',
                       'people': [{'person_id': people[0]['id'], 'person': '甲',
                                   'amount': 10, 'base': 100}],
                       'total': 10, 'amount_complete': False,
                       'unassigned_orders': 0},
        'statement': [],
    }, [])


def _decision(context, people):
    return {'store_id': 's1', 'period': '2026-06', 'run_id': context['run_id'],
            'source_sha': context['source_sha'], 'reason': '逐人核对并确认人工实发',
            'payouts': [{'person_id': people[0]['id'], 'amount': '12.00'}]}


def test_payout_requires_separate_ack_for_same_run_manual_close(tmp_path):
    ws, registry, people, client = fixture(tmp_path)
    run = _pending_run(ws, people)
    ws.close_period('s1', '2026-06', by='财务', note='核对后承担分配风险',
                    ignored_blockers=('allocation_basis',), expected_run_id=run)
    context = client.get('/api/commission-v2/payout-confirmations/context', params={
        'store_id': 's1', 'period': '2026-06', 'run_id': run}).json()
    risk = context['allocation_risk']
    assert risk['can_confirm'] is True and risk['pending_count'] == 2
    assert risk['close_reason'] == '核对后承担分配风险'
    assert risk['close_override_id'] > 0

    body = _decision(context, people)
    url = '/api/commission-v2/payout-confirmations'
    assert client.post(url, json=body).status_code == 400
    assert client.post(url, json={**body, 'allocation_risk_ack': True,
                                  'allocation_override_id': risk['close_override_id'] + 1}).status_code == 409
    with registry.connect() as conn:
        assert conn.execute('SELECT count(*) FROM payout_confirmation').fetchone()[0] == 0
    saved = client.post(url, json={**body, 'allocation_risk_ack': True,
                                   'allocation_override_id': risk['close_override_id']})
    assert saved.status_code == 200, saved.text
    with registry.connect() as conn:
        decision = conn.execute('SELECT trial_json,confirmed_total FROM payout_confirmation').fetchone()
    evidence = json.loads(decision['trial_json'])['allocation_risk']
    assert evidence['acknowledged'] is True
    assert evidence['close_override_id'] == risk['close_override_id']
    assert decision['confirmed_total'] == '12.00'
    assert ws.state('s1', '2026-06').closed is True
    assert ws.state('s1', '2026-06').result['allocation_pending_count'] == 2
    history = client.get('/api/commission-v2/payout-confirmations/context', params={
        'store_id': 's1', 'period': '2026-06', 'run_id': run}).json()['history']
    assert history[0]['trial']['allocation_risk']['acknowledged'] is True
    report = client.post('/api/commission-v2/reports/query', json={
        'start': '2026-06', 'end': '2026-06', 'store_ids': ['s1'],
        'view': 'store_people'}).json()
    assert report['items'][0]['status'] == '已人工确认（分配待复核）'


def test_closed_period_without_recorded_override_cannot_ack_missing_basis(tmp_path):
    ws, registry, people, client = fixture(tmp_path)
    run = _pending_run(ws, people, blocking=False)
    ws.close_period('s1', '2026-06', by='财务', note='普通结账', expected_run_id=run)
    context = client.get('/api/commission-v2/payout-confirmations/context', params={
        'store_id': 's1', 'period': '2026-06', 'run_id': run}).json()
    assert context['allocation_risk']['can_confirm'] is False
    result = client.post('/api/commission-v2/payout-confirmations', json={
        **_decision(context, people), 'allocation_risk_ack': True,
        'allocation_override_id': 1})
    assert result.status_code == 400
    with registry.connect() as conn:
        assert conn.execute('SELECT count(*) FROM payout_confirmation').fetchone()[0] == 0
