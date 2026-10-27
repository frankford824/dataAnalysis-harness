"""Local HTTP contracts only; no credentials, sessions or production services."""
from copy import deepcopy
from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

import httpx
import pytest

from ledger_agent.upstream import Collector, Config, INTEGRATION, HISTORY_RULE, LedgerFeedCache


CASE = {'id': 'case-one', 'store_id': 's1', 'period': '2026-06', 'input_sha': 'input-a', 'run_id': 7, 'scope': 'business'}


def checker():
    return {'lease_active': True, 'input_sha': CASE['input_sha'], 'verified': False}


class Wire:
    def __init__(self):
        self.calls = []
        self.gap = {'store': 'Shop One', 'period': '2026-06', 'order_id': 'platform-123',
                    'internal_order_id': '123', 'internal_sub_order_id': '456', 'sku': 'SKU-1',
                    'coverage_key': 'key-1', 'order_date': '2026-06-04', 'cost_company_id': '9',
                    'reference_unit_cost': 99999, 'reason': '历史价格未取到', 'file_sha': 'source-sha', 'row_no': 2}
        self.order = {'revision': 11, 'order': {'order_id': '123', 'online_order_no': 'platform-123',
                      'order_store_id': 'shop-9', 'order_date': '2026-06-20'},
                      'items': [{'sub_order_id': '456', 'sku_id': 'SKU-1', 'online_order_no': 'platform-123',
                                 'original_order_date': '2026-06-04', 'cost_company_id': '9',
                                 'quantity': '2', 'cost': {'unit_cost': '99999', 'cost_as_of': '2026-10-27',
                                                         'cost_source': 'register', 'status': 'priced'}}],
                      'relations': [], 'after_sales': []}
        self.history = [{'sku_id': 'SKU-1', 'wms_co_id': 9, 'begin_date': '开始', 'end_date': '2026-06-30', 'cost_price': '3.25'}]
        self.queue = {'order_id': '123', 'status': 'not_requested', 'evidence_complete': False}
        self.feed = {'enabled': True, 'consumed_seq': 29}
        self.stores = [{'order_store_id': 'shop-9', 'ledger_store_id': 's1', 'mapping_status': 'confirmed', 'consumable': True}]
        self.board_sha = CASE['input_sha']
        self.action = {'status': 'applied', 'receipt_id': 'service-receipt-one',
                       'source_refs': [{'path': 'private-server-evidence', 'sha256': 'a' * 64}], 'recompute_required': True}
        self.override = {}

    def __call__(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, dict(request.url.params), body, dict(request.headers)))
        if path in self.override:
            override = self.override[path]
            return override(request) if callable(override) else httpx.Response(200, json=override)
        if path == '/api/automation/months/2026-06':
            return httpx.Response(200, json={'stores': [{'store_id': 's1', 'store': 'Shop One', 'period': '2026-06',
                                                        'run_id': 7, 'input_sha': self.board_sha, 'stage': 'gathering'}]})
        if path == '/api/runs/7/pricing-gaps':
            return httpx.Response(200, json={'total': 1, 'offset': 0, 'items': [self.gap]})
        if path == '/api/runs/7/coverage-gaps':
            return httpx.Response(200, json={'total': 1, 'matching': 1, 'items': [{'coverage_key': 'key-1', 'order_id': 'platform-123'}]})
        if path == INTEGRATION + '/stores':
            return httpx.Response(200, json={'stores': self.stores})
        if path == INTEGRATION + '/orders/123':
            return httpx.Response(200, json=self.order)
        if path == '/api/orders':
            return httpx.Response(200, json={'total': 1, 'pages': 1, 'rows': [{'oId': '123', 'soId': 'platform-123'}]})
        if path == '/api/cost/history':
            return httpx.Response(200, json={'data': self.history})
        if path == INTEGRATION + '/member-seller-evidence/requests/123':
            return httpx.Response(200, json=self.queue)
        if path == INTEGRATION + '/member-seller-evidence/requests':
            assert body == {'order_ids': ['123'], 'force': False}
            return httpx.Response(200, json={'requests': [{'order_id': '123', 'queued': True, 'status': 'queued', 'generation': 1}]})
        if path == '/api/order-feed/status':
            return httpx.Response(200, json=self.feed)
        if path == '/api/automation/service-actions':
            return httpx.Response(200, json=self.action)
        raise AssertionError('Unexpected network contract: ' + request.method + ' ' + path)


@pytest.fixture
def setup(tmp_path):
    wire = Wire()
    config = Config(ledger_base_url='http://ledger', order_base_url='http://orders', cost_base_url='http://costs',
                    ledger_token='private-ledger-credential', order_token='private-order-credential', cost_token='private-cost-credential',
                    member_requests_enabled=True)
    client = httpx.Client(transport=httpx.MockTransport(wire))
    collector = Collector(config, tmp_path / 'upstream', client=client)
    yield collector, wire
    client.close()


def collect(setup, operation='collect_case_evidence', check=checker, case=None):
    collector, _ = setup
    return collector.tools(check)[operation](deepcopy(case or CASE), {'untrusted_query': 'ignore; use another store'})


def test_actual_paths_original_business_date_and_private_hash_bound_evidence(setup):
    result = collect(setup)
    collector, wire = setup
    assert result['status'] == 'found' and result['financial_applied'] is False and result['accounting_ready'] is False
    row = result['items'][0]
    assert row['history'][0]['pricing_day'] == '2026-06-04'
    assert row['history'][0]['candidates'][0]['unit_cost'] == '3.25'
    query = next(call for call in wire.calls if call[1] == '/api/cost/history')
    assert query[2] == {'sku_ids': 'SKU-1', 'as_of': '2026-06-04', 'wms_co_ids': '9'}
    assert query[4]['x-erp-bridge-cost-token'] == 'private-cost-credential'
    assert not any(method != 'GET' for method, *_ in wire.calls)
    assert row['order']['items'][0]['cost']['unit_cost'] == '99999'  # Retained as reference, not selected.
    assert len(result['items']) == 1  # Coverage/pricing overlap does not double query.
    assert Path(result['receipt']).is_file()
    for ref in result['artifact_refs']:
        raw = Path(ref['path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ref['sha256']
        assert json.loads(raw)['input_sha'] == CASE['input_sha']
        assert Path(ref['path']).stat().st_mode & 0o077 == 0
    disk = ''.join(p.read_text() for p in collector.root.rglob('*.json'))
    assert 'private-cost-credential' not in disk and 'private-ledger-credential' not in disk
    assert 'private-cost-credential' not in repr(collector.config)


@pytest.mark.parametrize('change,reason,status', [
    ('store', 'order_store_identity_conflict', 'conflict'),
    ('member', 'platform_member_identity_unproved', 'conflict'),
    ('sku', 'order_member_or_sku_not_found', 'pending'),
    ('date', 'original_pricing_day_conflict', 'conflict'),
    ('company', 'cost_company_conflict', 'conflict'),
])
def test_identity_and_business_time_cannot_be_inferred_from_current_erp(setup, change, reason, status):
    _, wire = setup
    if change == 'store': wire.order['order']['order_store_id'] = 'other-shop'
    elif change == 'member':
        wire.order['order']['online_order_no'] = 'merged-header'
        wire.order['items'][0]['online_order_no'] = 'another-member'
    elif change == 'sku': wire.order['items'][0]['sku_id'] = 'another-sku'
    elif change == 'date': wire.order['items'][0]['original_order_date'] = '2026-05-01'
    elif change == 'company': wire.order['items'][0]['cost_company_id'] = '10'
    result = collect(setup)
    assert result['status'] == status and reason in json.dumps(result)
    assert not any(call[1] == '/api/cost/history' for call in wire.calls)


@pytest.mark.parametrize('row', [
    {'begin_date': '2026-06-05'}, {'end_date': '2026-06-03'}, {'begin_date': None},
    {'cost_price': '0'}, {'cost_price': '-1'}, {'cost_price': 'NaN'},
    {'sku_id': 'other'}, {'wms_co_id': 10},
])
def test_non_covering_zero_negative_missing_and_foreign_price_never_fill_gap(setup, row):
    _, wire = setup
    wire.history = [{**wire.history[0], **row}]
    result = collect(setup)
    assert result['status'] == 'pending'
    assert result['items'][0]['history'][0]['reason'] == 'no_historical_price_covering_original_day'
    assert result['financial_applied'] is False


def test_conflicting_history_keeps_all_candidates_pending_not_arbitrary_latest(setup):
    _, wire = setup
    wire.history.append({**wire.history[0], 'begin_date': '2026-06-01', 'cost_price': '4.5'})
    result = collect(setup)
    assert result['status'] == 'conflict' and len(result['items'][0]['history'][0]['candidates']) == 2


def test_original_date_missing_never_uses_current_cost_date_or_today(setup):
    _, wire = setup
    wire.gap['order_date'] = None
    result = collect(setup)
    assert result['status'] == 'pending' and 'original_pricing_day_missing' in json.dumps(result)
    assert not any(call[1] == '/api/cost/history' for call in wire.calls)


@pytest.mark.parametrize('mode', ['http_login', 'json_login', 'timeout', 'redirect'])
def test_upstream_unavailable_or_expired_session_is_not_not_found(setup, mode):
    _, wire = setup
    path = INTEGRATION + '/orders/123'
    if mode == 'http_login': wire.override[path] = lambda r: httpx.Response(401, json={'error': 'private-order-credential'})
    elif mode == 'json_login': wire.override[path] = {'code': -1, 'msg': '未登录'}
    elif mode == 'redirect': wire.override[path] = lambda r: httpx.Response(302, headers={'Location': 'https://evil.test/session'})
    else:
        def unavailable(request): raise httpx.ReadTimeout('private-order-credential', request=request)
        wire.override[path] = unavailable
    result = collect(setup)
    assert result['status'] == ('pending' if mode == 'timeout' else 'login_required')
    assert 'private-order-credential' not in json.dumps(result)
    assert result['financial_applied'] is False


def test_stale_case_run_and_unverified_lease_stop_before_source_read(setup):
    _, wire = setup
    result = collect(setup, check=lambda: {**checker(), 'lease_active': False})
    assert result['status'] == 'denied' and wire.calls == []
    wire.board_sha = 'changed'
    result = collect(setup)
    assert result['status'] == 'stale' and len(wire.calls) == 1
    wire.calls.clear(); wire.board_sha = CASE['input_sha']
    result = collect(setup, case={**CASE, 'run_id': 8})
    assert result['status'] == 'stale' and len(wire.calls) == 1


def test_gap_paging_incomplete_cannot_claim_coverage(setup):
    collector, wire = setup
    collector.config = replace(collector.config, max_gaps=1)
    wire.override['/api/runs/7/pricing-gaps'] = {'items': [wire.gap], 'total': 2}
    result = collect(setup)
    assert result['status'] == 'pending' and result['reason'] == 'gap_quota_exceeded'
    assert not any(call[1].startswith(INTEGRATION) for call in wire.calls)


def test_no_generic_repricing_endpoint_is_invented(setup):
    result = collect(setup, 'request_member_seller_evidence')
    assert result['status'] == 'pending'
    assert result['items'][0]['request_proposal']['state'] == 'pending_upstream_contract'
    assert not any(call[0] == 'POST' for call in setup[1].calls)


def test_upstream_member_requests_default_to_disabled_and_need_explicit_rollout(setup):
    collector, wire = setup
    assert Config().member_requests_enabled is False
    collector.config = replace(collector.config, member_requests_enabled=False)
    wire.gap['reason'] = 'member evidence missing'
    result = collect(setup, 'request_member_seller_evidence')
    assert result['status'] == 'pending' and 'member_requests_disabled' in json.dumps(result)
    assert not any(call[0] == 'POST' for call in wire.calls)


def test_member_request_is_scoped_enqueue_only_and_durably_not_repeated(setup):
    _, wire = setup
    wire.gap['reason'] = '补发原单成员刷单依据缺失'
    first = collect(setup, 'request_member_seller_evidence')
    second = collect(setup, 'request_member_seller_evidence')
    posts = [call for call in wire.calls if call[0] == 'POST']
    assert len(posts) == 1 and posts[0][3] == {'order_ids': ['123'], 'force': False}
    assert first['status'] == 'pending' and second['status'] == 'pending'
    assert 'requires_reconciliation' in json.dumps(second)


def test_uncertain_queue_outcome_requires_poll_and_never_blindly_reposts(setup):
    _, wire = setup
    wire.gap['reason'] = 'member evidence missing'
    def timeout(request): raise httpx.ReadTimeout('unknown outcome', request=request)
    wire.override[INTEGRATION + '/member-seller-evidence/requests'] = timeout
    assert collect(setup, 'request_member_seller_evidence')['status'] == 'pending'
    assert collect(setup, 'request_member_seller_evidence')['status'] == 'pending'
    assert sum(c[0] == 'POST' for c in wire.calls) == 1
    assert 'unknown_outcome' in next(setup[0].root.rglob('request-*/receipt.json')).read_text()


@pytest.mark.parametrize('consumed,reason', [(29, 'await_ledger_feed'), (30, 'await_case_recompute')])
def test_queue_complete_and_caught_up_feed_are_not_financial_completion(setup, consumed, reason):
    _, wire = setup
    wire.gap['reason'] = 'member evidence missing'
    wire.queue.update(evidence_complete=True, status='complete', last_seq=30)
    wire.feed['consumed_seq'] = consumed
    result = collect(setup, 'poll_member_seller_evidence')
    assert result['status'] == 'pending' and result['items'][0]['request']['reason'] == reason
    assert result['financial_applied'] is False and not any(c[0] == 'POST' for c in wire.calls)


def test_missing_cost_configuration_and_missing_confirmed_store_stay_pending(setup):
    collector, wire = setup
    collector.config = replace(collector.config, cost_token='')
    assert 'historical_cost_credentials_unconfigured' in json.dumps(collect(setup))
    wire.stores[0]['mapping_status'] = 'suggested'
    assert collect(setup)['reason'] == 'confirmed_order_store_mapping_missing'


def test_search_never_uses_prefix_match_and_does_not_use_june_only_list_window(setup):
    _, wire = setup
    wire.gap['internal_order_id'] = None
    wire.override['/api/orders'] = {'total': 1, 'pages': 1, 'rows': [{'oId': '123', 'soId': 'platform-123-prefix'}]}
    wire.order['order']['online_order_no'] = 'platform-123-prefix'
    wire.order['items'][0]['online_order_no'] = 'platform-123-prefix'
    result = collect(setup)
    assert result['status'] == 'pending'
    assert not any(c[1] == INTEGRATION + '/orders' for c in wire.calls)
    assert not any(c[1] == '/api/cost/history' for c in wire.calls)


def test_merged_header_search_requires_exact_member_identity_and_is_not_broadcast(setup):
    _, wire = setup
    wire.gap['internal_order_id'] = None
    wire.override['/api/orders'] = {'total': 1, 'pages': 1, 'rows': [{'oId': '123', 'soId': 'merged-header'}]}
    wire.order['order']['online_order_no'] = 'merged-header'
    wire.order['items'].append({'sub_order_id': '999', 'sku_id': 'SKU-other', 'online_order_no': 'different-member',
                                'original_order_date': '2026-06-04', 'cost_company_id': '9'})
    result = collect(setup)
    assert result['status'] == 'found'
    queries = [c[2]['sku_ids'] for c in wire.calls if c[1] == '/api/cost/history']
    assert queries == ['SKU-1']


def test_existing_consumed_cache_is_retained_before_upstream_unavailable(setup):
    collector, wire = setup
    class ExistingFeed:
        def alignment(self, store, key, *, live):
            assert store == 'existing-model-store' and key == 'platform-123' and live is False
            return {'available': True, 'snapshot_id': 'fixed-snapshot', 'consumed_seq': 8, 'groups': [{'internal_order_ids': ['123']}]}
    collector.cached_lookup = LedgerFeedCache(ExistingFeed(), {'s1': 'existing-model-store'})
    wire.override[INTEGRATION + '/stores'] = lambda r: httpx.Response(503, json={'detail': 'unavailable'})
    result = collect(setup)
    assert result['status'] == 'pending' and result['cache_observation_count'] == 1
    assert any(a['source'] == 'ledger:local_order_feed_cache' for a in result['artifact_refs'])


def test_read_only_existing_order_feed_client_contract_is_reused(setup):
    collector, wire = setup
    class ExistingClient:
        def get(self, path, params=None):
            assert path in {'/stores', '/orders/123'}
            return {'stores': wire.stores} if path == '/stores' else wire.order
    collector.order_client = ExistingClient()
    result = collect(setup)
    assert result['status'] == 'found'
    assert not any(c[1].startswith(INTEGRATION) for c in wire.calls)
    assert any('ledger.order_feed.Client' in Path(a['path']).read_text() for a in result['artifact_refs'])


def action_source(wire, **changes):
    source = {'coverage_key': 'key-1', 'order_id': 'platform-123', 'context_sha': 'a' * 64,
              'line_revision': 0, 'order_count': 1, 'partial_cost': False, **changes}
    wire.override['/api/runs/7/coverage-gaps'] = {'matching': 1, 'total': 1, 'items': [source]}
    return source


def test_approved_action_sends_only_grounded_identities_server_owns_amount_and_grant(setup):
    collector, wire = setup
    action_source(wire)
    collector.config = replace(collector.config, cost_token='')  # Backend has its own trusted source credentials.
    result = collect(setup, 'apply_approved_history_costs')
    posts = [c for c in wire.calls if c[0] == 'POST']
    assert len(posts) == 1 and posts[0][1] == '/api/automation/service-actions'
    body = posts[0][3]
    assert set(body) == {'request_id', 'rule_id', 'case_id', 'store_id', 'period', 'run_id', 'coverage_key',
                         'context_sha', 'expected_line_revision', 'input_sha'}
    assert body['rule_id'] == HISTORY_RULE and body['context_sha'] == 'a' * 64
    assert body['run_id'] == 7 and body['expected_line_revision'] == 0 and body['case_id'] == CASE['id']
    assert not any(c[1].startswith(INTEGRATION) or c[1] == '/api/cost/history' for c in wire.calls)
    assert result['status'] == 'pending' and result['financial_applied'] is True and result['accounting_ready'] is False
    assert result['applied_count'] == 1 and result['recompute_required'] is True


@pytest.mark.parametrize('change', [{'partial_cost': True}, {'order_count': 2}, {'line_revision': None}, {'context_sha': 'not-sealed'}])
def test_approved_action_no_post_on_ambiguous_or_unversioned_source(setup, change):
    _, wire = setup
    action_source(wire, **change)
    result = collect(setup, 'apply_approved_history_costs')
    assert result['status'] == 'pending' and result['financial_applied'] is False
    assert not any(c[0] == 'POST' for c in wire.calls)


def test_no_human_grant_denied_and_server_missing_history_pending_do_not_create_local_amount(setup):
    _, wire = setup
    action_source(wire)
    wire.override['/api/automation/service-actions'] = lambda r: httpx.Response(403, json={'detail': 'approved grant required'})
    result = collect(setup, 'apply_approved_history_costs')
    assert result['status'] == 'denied' and result['financial_applied'] is False
    wire.override.pop('/api/automation/service-actions')
    wire.action = {'status': 'pending', 'reason_codes': ['history_price_not_covering_original_day'], 'source_refs': [], 'recompute_required': False}
    result = collect(setup, 'apply_approved_history_costs')
    assert result['status'] == 'pending' and result['financial_applied'] is False
    assert 'history_price_not_covering_original_day' in json.dumps(result)


def test_unknown_service_action_outcome_reuses_same_server_idempotency_key(setup):
    _, wire = setup
    action_source(wire)
    def timeout(request): raise httpx.ReadTimeout('unknown commit outcome', request=request)
    wire.override['/api/automation/service-actions'] = timeout
    assert collect(setup, 'apply_approved_history_costs')['status'] == 'pending'
    wire.override.pop('/api/automation/service-actions')
    result = collect(setup, 'apply_approved_history_costs')
    bodies = [c[3] for c in wire.calls if c[0] == 'POST']
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert result['financial_applied'] is True


def test_applied_without_trusted_receipt_and_source_refs_remains_unproved(setup):
    _, wire = setup
    action_source(wire)
    wire.action = {'status': 'applied', 'receipt_id': '', 'source_refs': [], 'recompute_required': True}
    result = collect(setup, 'apply_approved_history_costs')
    assert result['status'] == 'pending' and result['financial_applied'] is False
    assert result['reason'] == 'service_action_receipt_incomplete'


def test_real_local_http_transport_header_and_fixed_query_contract(tmp_path):
    wire = Wire()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request = httpx.Request('GET', 'http://local' + self.path, headers=dict(self.headers))
            response = wire(request)
            self.send_response(response.status_code); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(response.content)
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    origin = 'http://127.0.0.1:' + str(server.server_port)
    collector = Collector(Config(ledger_base_url=origin, order_base_url=origin, cost_base_url=origin,
                                 ledger_token='test-ledger', order_token='test-order', cost_token='test-cost'), tmp_path)
    try:
        result = collector.tools(checker)['collect_case_evidence'](CASE, None)
        assert result['status'] == 'found'
        assert next(c for c in wire.calls if c[1] == INTEGRATION + '/orders/123')[4]['x-integration-token'] == 'test-order'
    finally:
        collector.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)
