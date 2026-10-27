"""Trusted fixed-source history query, with local HTTP only and no real secrets."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import threading
import urllib.parse

import pytest

from ledger.upstream_history import HistoryClient, HistoryError, HistoryUnavailable


@contextmanager
def source(payload, *, status=200, headers=None):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({'path': self.path, 'headers': dict(self.headers)})
            self.send_response(status)
            for key, value in (headers or {}).items(): self.send_header(key, value)
            self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode())
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield 'http://127.0.0.1:' + str(server.server_port), requests
    finally: server.shutdown(); server.server_close(); thread.join(timeout=2)


def row(**values):
    return {'sku_id': 'sku-one', 'wms_co_id': 9, 'begin_date': '2026-06-01',
            'end_date': '2026-06-04', 'cost_price': '3.2500', **values}


def test_fixed_history_endpoint_original_day_inclusive_end_and_immutable_source(tmp_path):
    raw = json.dumps({'data': [row()]}, ensure_ascii=False).encode()
    with source(raw) as (origin, requests):
        client = HistoryClient(tmp_path, base_url=origin, token='trusted-private-key')
        assert requests == []  # Construction does not probe production or login.
        quote = client.quote('sku-one', '9', '2026-06-04')
    assert quote['unit_cost'] == '3.2500' and quote['financial_applied'] is False
    path = urllib.parse.urlsplit(requests[0]['path'])
    assert path.path == '/api/cost/history'
    assert urllib.parse.parse_qs(path.query) == {'sku_ids': ['sku-one'], 'as_of': ['2026-06-04'], 'wms_co_ids': ['9']}
    assert requests[0]['headers']['X-Erp-Bridge-Cost-Token'] == 'trusted-private-key'
    assert len(quote['quote_sha']) == 64 and quote['pricing_day'] == '2026-06-04'
    assert quote['artifact_refs'][0]['sha256'] == hashlib.sha256(raw).hexdigest()
    for item in quote['artifact_refs']:
        assert Path(item['path']).is_relative_to(tmp_path)
        assert hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() == item['sha256']
        assert Path(item['path']).stat().st_mode & 0o077 == 0
        assert b'trusted-private-key' not in Path(item['path']).read_bytes()
    assert 'trusted-private-key' not in repr(client)


def test_explicit_open_begin_is_not_a_missing_business_date(tmp_path):
    with source({'data': [row(begin_date='开始', end_date=None)]}) as (origin, _):
        quote = HistoryClient(tmp_path, base_url=origin, token='key').quote('sku-one', '9', '2026-05-01')
    interval = quote['intervals'][0]
    assert interval['begin_date'] is None and interval['begin_unbounded'] is True
    assert interval['begin_date_raw'] == '开始'


@pytest.mark.parametrize('values,reason', [
    ({'begin_date': None}, 'history_interval_boundary_unknown'),
    ({'begin_date': None, 'begin_unbounded': True}, 'history_interval_boundary_conflict'),
    ({'begin_date': '2026-06-05'}, 'history_interval_reversed'),
    ({'end_date': 'unknown'}, 'history_interval_boundary_unknown'),
    ({'end_date': '2026-06-03'}, 'history_price_not_covering_original_day'),
    ({'cost_price': '0'}, 'history_price_requires_independent_zero_or_invalid_basis'),
    ({'cost_price': '-2'}, 'history_price_requires_independent_zero_or_invalid_basis'),
    ({'cost_price': 'NaN'}, 'history_price_requires_independent_zero_or_invalid_basis'),
    ({'sku_id': 'foreign'}, 'history_price_not_covering_original_day'),
    ({'wms_co_id': 10}, 'history_price_not_covering_original_day'),
])
def test_no_zero_fallback_or_unproven_interval_identity(tmp_path, values, reason):
    with source({'data': [row(**values)]}) as (origin, _):
        with pytest.raises(HistoryError) as error:
            HistoryClient(tmp_path, base_url=origin, token='key').quote('sku-one', '9', '2026-06-04')
    assert error.value.reason_code == reason
    assert all(Path(r['path']).is_file() for r in error.value.artifact_refs)
    assert error.value.artifact_refs
    assert list((tmp_path / 'upstream-history').glob('*.json'))  # Failed source retained as evidence.


def test_conflicting_overlaps_do_not_choose_latest_or_average(tmp_path):
    with source({'data': [row(), row(begin_date='2026-06-02', cost_price='4.0000')]}) as (origin, _):
        with pytest.raises(HistoryError) as error:
            HistoryClient(tmp_path, base_url=origin, token='key').quote('sku-one', '9', '2026-06-04')
    assert error.value.status == 'conflict' and error.value.reason_code == 'history_price_overlapping_conflict'


@pytest.mark.parametrize('status,payload,reason', [
    (401, {'error': 'not logged in'}, 'history_source_login_required'),
    (403, {'error': 'forbidden'}, 'history_source_login_required'),
    (302, {}, 'history_source_login_required'),
    (503, {}, 'history_source_http_503'),
    (200, {'code': -1, 'msg': '未登录'}, 'history_source_login_required'),
    (200, {'error': 'failed'}, 'history_source_invalid_contract'),
    (200, b'<html>login</html>', 'history_source_invalid_json'),
    (200, b'{"data": [NaN]}', 'history_source_invalid_json'),
])
def test_expired_login_invalid_json_and_network_failure_are_explicit_pending(tmp_path, status, payload, reason):
    with source(payload, status=status, headers={'Location': 'https://must-not-follow.invalid/'}) as (origin, requests):
        with pytest.raises(HistoryError) as error:
            HistoryClient(tmp_path, base_url=origin, token='key').quote('sku-one', '9', '2026-06-04')
    assert error.value.reason_code == reason and len(requests) == 1
    assert error.value.status == ('login_required' if 'login_required' in reason else 'pending')


def test_no_request_with_missing_credentials_invalid_identity_or_arbitrary_source(tmp_path):
    with source({'data': [row()]}) as (origin, requests):
        with pytest.raises(HistoryUnavailable, match='credentials_unconfigured'):
            HistoryClient(tmp_path, base_url=origin).quote('sku-one', '9', '2026-06-04')
        client = HistoryClient(tmp_path, base_url=origin, token='key')
        for args in [('sku-one,foreign', '9', '2026-06-04'), ('sku-one', 'name:shop', '2026-06-04'), ('sku-one', '9', 'today')]:
            with pytest.raises(HistoryUnavailable, match='query_identity_invalid'): client.quote(*args)
        assert requests == []
    for origin in ('file:///etc/passwd', 'http://key@example.test', 'http://example.test/api/any', 'http://example.test?token=key'):
        with pytest.raises(HistoryUnavailable, match='configuration_invalid'):
            HistoryClient(tmp_path, base_url=origin, token='key')


def test_corrupt_existing_source_or_credential_echo_is_not_admitted(tmp_path):
    raw = json.dumps({'data': [row()]}).encode()
    folder = tmp_path / 'upstream-history'; folder.mkdir()
    (folder / (hashlib.sha256(raw).hexdigest() + '.json')).write_text('corrupted')
    with source(raw) as (origin, _):
        with pytest.raises(HistoryUnavailable, match='evidence_integrity_failed'):
            HistoryClient(tmp_path, base_url=origin, token='key').quote('sku-one', '9', '2026-06-04')
    with source({'data': [row()], 'message': 'trusted-secret-echo'}) as (origin, _):
        with pytest.raises(HistoryUnavailable, match='credential_echo'):
            HistoryClient(tmp_path, base_url=origin, token='trusted-secret-echo').quote('sku-one', '9', '2026-06-04')


def test_environment_contract_and_response_quota(tmp_path, monkeypatch):
    with source({'data': [row()]}) as (origin, _):
        monkeypatch.setenv('LEDGER_HISTORY_COST_API_BASE', origin)
        monkeypatch.setenv('LEDGER_HISTORY_COST_API_TOKEN', 'env-key')
        assert HistoryClient.from_env(tmp_path).quote('sku-one', '9', '2026-06-04')['unit_cost'] == '3.2500'
    with source(b' ' * 1025) as (origin, _):
        with pytest.raises(HistoryUnavailable, match='response_too_large'):
            HistoryClient(tmp_path, base_url=origin, token='key', max_bytes=1024).quote('sku-one', '9', '2026-06-04')


@pytest.mark.parametrize('literal,expected',[
    ('3.335','3.335'),
    ('3.340000000000000000000000000000000000000000000000000000000001',
     '3.340000000000000000000000000000000000000000000000000000000001'),
    ('1e-1000050','1E-1000050'),
    ('3.335e+2','333.5'),
])
def test_json_numeric_history_price_preserves_source_decimal_lexeme(tmp_path,literal,expected):
    raw=('{"data":[{"sku_id":"sku-one","wms_co_id":9,"begin_date":"2026-06-01",'
         '"end_date":null,"cost_price":'+literal+'}]}').encode()
    with source(raw) as (origin,_):
        quote=HistoryClient(tmp_path,base_url=origin,token='key').quote('sku-one','9','2026-06-04')
    assert quote['unit_cost']==expected and quote['intervals'][0]['unit_cost']==expected
    assert quote['source_response_sha']==hashlib.sha256(raw).hexdigest()
    assert Path(quote['artifact_refs'][0]['path']).read_bytes()==raw


def test_json_number_outside_decimal_representation_is_pending_not_unhandled(tmp_path):
    raw=b'{"data":[{"sku_id":"sku-one","wms_co_id":9,"begin_date":"2026-06-01","cost_price":1e99999999999999999999999999999999999999}]}'
    with source(raw) as (origin,_):
        with pytest.raises(HistoryError,match='history_source_invalid_json'):
            HistoryClient(tmp_path,base_url=origin,token='key').quote('sku-one','9','2026-06-04')
