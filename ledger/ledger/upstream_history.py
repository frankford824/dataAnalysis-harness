"""Server-owned dated cost quotes; no model JSON or current-price fallback.

This module validates a single SKU/company/day quote and retains exact source
bytes. Order identity, immutable quantity/components, grants and posting belong
to the caller's existing financial transaction. A quote alone authorizes none.
Uses stdlib HTTP so production needs no new optional test/runtime dependency.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid


class HistoryError(RuntimeError):
    def __init__(self, reason_code: str, *, status='pending'):
        super().__init__(reason_code)
        self.reason_code, self.status = reason_code, status
        self.reason_codes = [reason_code]
        self.artifact_refs = []


class HistoryUnavailable(HistoryError):
    pass


class HistoryConflict(HistoryError):
    def __init__(self, reason_code):
        super().__init__(reason_code, status='conflict')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


class HistoryClient:
    """Fixed-source GET only. Constructing the client makes no HTTP request."""
    def __init__(self, root: Path, *, base_url='https://yongbo.cloud', token='', timeout=15,
                 opener=None, max_bytes=2 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.base_url = base_url.rstrip('/')
        self._token = token
        parsed = urllib.parse.urlsplit(self.base_url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            raise HistoryUnavailable('history_source_configuration_invalid')
        if not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 120 or not 1024 <= max_bytes <= 20 * 1024 * 1024:
            raise HistoryUnavailable('history_source_limits_invalid')
        self.timeout, self.max_bytes = float(timeout), max_bytes
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_env(cls, root: Path):
        """Only trusted service environment; never read credential/session files."""
        try:
            timeout = float(os.environ.get('LEDGER_HISTORY_COST_API_TIMEOUT', '15'))
        except (TypeError, ValueError):
            raise HistoryUnavailable('history_source_limits_invalid') from None
        return cls(root, base_url=os.environ.get('LEDGER_HISTORY_COST_API_BASE', 'https://yongbo.cloud'),
                   token=os.environ.get('LEDGER_HISTORY_COST_API_TOKEN', ''), timeout=timeout)

    def __repr__(self):
        return f'HistoryClient(base_url={self.base_url!r}, token_configured={bool(self._token)!r})'

    def _fetch(self, sku, company, pricing_day):
        if not self._token:
            raise HistoryUnavailable('history_source_credentials_unconfigured')
        query = urllib.parse.urlencode({'sku_ids': sku, 'as_of': pricing_day, 'wms_co_ids': company})
        request = urllib.request.Request(self.base_url + '/api/cost/history?' + query,
            headers={'Accept': 'application/json', 'X-ERP-Bridge-Cost-Token': self._token}, method='GET')
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read(self.max_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403} or 300 <= exc.code < 400:
                raise HistoryUnavailable('history_source_login_required', status='login_required') from None
            raise HistoryUnavailable('history_source_http_' + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise HistoryUnavailable('history_source_unavailable') from None
        if not 200 <= status < 300:
            raise HistoryUnavailable('history_source_http_' + str(status))
        if len(raw) > self.max_bytes:
            raise HistoryUnavailable('history_source_response_too_large')
        if self._token.encode() in raw:
            raise HistoryUnavailable('history_source_credential_echo')
        try:
            # Preserve JSON number lexemes too: binary float must not erase a
            # fractional-cent tail before the server's exact-amount check.
            value = json.loads(raw, parse_float=Decimal,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, DecimalException):
            raise HistoryUnavailable('history_source_invalid_json') from None
        if not isinstance(value, dict):
            raise HistoryUnavailable('history_source_invalid_contract')
        message = str(value.get('msg') or value.get('message') or '')
        if value.get('code') in {-1, '-1', 401, '401'} and any(x in message.casefold() for x in ('未登录', 'login', '登录')):
            raise HistoryUnavailable('history_source_login_required', status='login_required')
        if value.get('error') or value.get('ok') is False or value.get('success') is False or not isinstance(value.get('data'), list):
            raise HistoryUnavailable('history_source_invalid_contract')
        return raw, value['data']

    def _retain(self, raw, query):
        folder = self.root / 'upstream-history'
        if folder.is_symlink():
            raise HistoryUnavailable('history_evidence_path_invalid')
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)
        if not folder.resolve().is_relative_to(self.root):
            raise HistoryUnavailable('history_evidence_path_invalid')
        source_sha = _digest(raw)
        path = folder / (source_sha + '.json')
        if path.is_symlink():
            raise HistoryUnavailable('history_evidence_path_invalid')
        if path.exists():
            if _digest(path.read_bytes()) != source_sha:
                raise HistoryUnavailable('history_evidence_integrity_failed')
        else:
            # Exclusive creation, no replacement of an existing financial source.
            try:
                with path.open('xb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                path.chmod(0o600)
            except FileExistsError:
                if _digest(path.read_bytes()) != source_sha:
                    raise HistoryUnavailable('history_evidence_integrity_failed')
        provenance = {'schema': 'ledger-history-source.v1', 'source': self.base_url + '/api/cost/history',
                      'query': query, 'captured_at': datetime.now(timezone.utc).isoformat(),
                      'artifact': {'path': str(path), 'sha256': source_sha}}
        manifest_raw = _encoded(provenance)
        manifest = folder / ('capture-' + uuid.uuid4().hex + '.json')
        with manifest.open('xb') as stream:
            stream.write(manifest_raw)
        manifest.chmod(0o600)
        return [provenance['artifact'], {'path': str(manifest), 'sha256': _digest(manifest_raw)}]

    def quote(self, sku: str, company: str, pricing_day: str) -> dict:
        """Retain source even on an invalid/non-covering/ambiguous quote.

        Raises HistoryError with a machine reason; never returns zero or swallows
        a failed query. The caller derives amounts from sealed quantity itself.
        """
        sku, company = str(sku).strip(), str(company).strip()
        if not sku or len(sku) > 256 or any(c in sku for c in ',\r\n\x00') or not re.fullmatch(r'[1-9][0-9]{0,18}', company) or not _date(pricing_day):
            raise HistoryUnavailable('history_query_identity_invalid')
        raw, rows = self._fetch(sku, company, pricing_day)
        refs = self._retain(raw, {'sku_ids': sku, 'as_of': pricing_day, 'wms_co_ids': company})
        def bound(error):
            error.artifact_refs = refs
            return error
        intervals = []
        for row in rows:
            if not isinstance(row, dict):
                raise bound(HistoryUnavailable('history_source_row_invalid'))
            if str(row.get('sku_id') or '') != sku or str(row.get('wms_co_id') or '') != company:
                continue
            if row.get('begin_unbounded') is True and row.get('begin_date') not in {'开始', '0001-01-01'}:
                raise bound(HistoryConflict('history_interval_boundary_conflict'))
            unbounded = row.get('begin_date') == '开始' or row.get('begin_unbounded') is True
            start = '0001-01-01' if unbounded else _date(row.get('begin_date'))
            end_raw = row.get('end_date')
            end = None if end_raw in (None, '') else _date(end_raw)
            if start is None or end_raw not in (None, '') and end is None:
                raise bound(HistoryUnavailable('history_interval_boundary_unknown'))
            if end is not None and end < start:
                raise bound(HistoryConflict('history_interval_reversed'))
            if not start <= pricing_day or end is not None and pricing_day > end:
                continue
            try:
                price = Decimal(str(row.get('cost_price')))
            except InvalidOperation:
                raise bound(HistoryUnavailable('history_price_invalid')) from None
            if not price.is_finite() or price <= 0:
                raise bound(HistoryUnavailable('history_price_requires_independent_zero_or_invalid_basis'))
            intervals.append({'begin_date': None if unbounded else start, 'begin_unbounded': unbounded,
                              'begin_date_raw': row.get('begin_date'), 'end_date': end, 'unit_cost': str(price)})
        if not intervals:
            raise bound(HistoryUnavailable('history_price_not_covering_original_day'))
        prices = {Decimal(row['unit_cost']) for row in intervals}
        if len(prices) != 1:
            raise bound(HistoryConflict('history_price_overlapping_conflict'))
        quote = {'schema': 'ledger-history-quote.v1', 'sku': sku, 'company': company,
                 'pricing_day': pricing_day, 'unit_cost': str(next(iter(prices))), 'intervals': intervals,
                 'artifact_refs': refs, 'source_response_sha': _digest(raw), 'financial_applied': False}
        return {**quote, 'quote_sha': _digest(_encoded(quote))}
