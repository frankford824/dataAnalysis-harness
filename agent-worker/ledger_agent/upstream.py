"""Case-scoped evidence tools and a server-verified, delegated cost action.

The model chooses a tool name and an opaque evidence reference. Query identities
come only from the current ledger run, not model arguments. All business payloads
stay in private, hash-bound local artifacts; Runtime exposes only their reference.
No tool accepts a financial amount. The fixed service action requires a human
rule grant and independent backend source validation. This is not a generic
HTTP/shell facility.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import uuid
from urllib.parse import quote, urlsplit

import httpx

from .commands import _lock, _write

INTEGRATION = '/api/integration/ledger/v1'
HISTORY_RULE = 'history_cost_single_component.v1'
_MEMBER_REASONS = ('member', 'seller', 'brushing', 'reship', 'original', '补发', '原单', '刷单', '合并')
_TOOL_NAMES = {'collect':'collect_case_evidence','request':'request_member_seller_evidence',
               'poll':'poll_member_seller_evidence','apply':'apply_approved_history_costs'}


@dataclass(frozen=True)
class Config:
    ledger_base_url: str = 'http://127.0.0.1:8000'
    order_base_url: str = 'http://127.0.0.1:8001'
    cost_base_url: str = 'https://yongbo.cloud'
    ledger_token: str = field(default='', repr=False)
    order_token: str = field(default='', repr=False)
    cost_token: str = field(default='', repr=False)
    timeout_seconds: float = 15
    max_gaps: int = 200
    max_requests: int = 80
    max_response_bytes: int = 5 * 1024 * 1024
    # Enqueue-only: the existing upstream worker owns ERP reads/outbox writes.
    member_requests_enabled: bool = False
    source_recovery_enabled: bool = False

    def __post_init__(self):
        for value in (self.ledger_base_url, self.order_base_url, self.cost_base_url):
            parts = urlsplit(value)
            if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
                raise ValueError('Configure trusted HTTP origins without embedded credentials')
            if parts.path not in {'', '/'}:
                raise ValueError('Base URL must be an origin; paths are fixed by this collector')
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError('Configure a finite timeout')
        if not 1 <= self.max_gaps <= 2000 or not 4 <= self.max_requests <= 1000 or not 1024 <= self.max_response_bytes <= 20 * 1024 * 1024:
            raise ValueError('Configure bounded evidence quotas')


class Pending(Exception):
    def __init__(self, reason, status='pending'):
        self.reason, self.status = reason, status


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _day(value):
    text = str(value or '').strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[ T].*)?', text):
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _positive_id(value):
    text = str(value or '')
    return text if re.fullmatch(r'[1-9][0-9]{0,18}', text) and int(text) <= 2**63-1 else None


class LedgerFeedCache:
    """Read an already configured OrderFeed; do not initialize/sync a feed here."""
    def __init__(self, feed, stores):
        self.feed, self.stores = feed, stores

    def __call__(self, case, gap):
        store = self.stores.get(case['store_id'])
        key = gap.get('order_id') or gap.get('internal_order_id')
        if store is None or not key:
            return None
        return self.feed.alignment(store, str(key), live=False)


class Collector:
    """Construct without network access; compatible with commands.Catalog.

    ``order_client`` may be an existing ``ledger.order_feed.Client``. Its get()
    contract is reused when a backend host has that client; the standalone worker
    uses identical fixed HTTP paths and needs no Polars/backend installation.
    ``cached_lookup`` is an optional trusted local read-only adapter returning an
    retained cache observation. It must not fetch or mutate when called.
    """
    def __init__(self, config: Config, root: Path, *, client=None, order_client=None, cached_lookup=None,capability_configuration=None):
        self.config = config
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.client = client or httpx.Client(timeout=config.timeout_seconds, follow_redirects=False, trust_env=False)
        self._owns_client = client is None
        self.order_client, self.cached_lookup = order_client, cached_lookup
        self.capability_configuration=capability_configuration

    def close(self):
        if self._owns_client:
            self.client.close()

    def tools(self, checker):
        return {
            'collect_case_evidence': lambda case, evidence: self._execute(case, checker, 'collect'),
            'request_member_seller_evidence': lambda case, evidence: self._execute(case, checker, 'request'),
            'poll_member_seller_evidence': lambda case, evidence: self._execute(case, checker, 'poll'),
            'apply_approved_history_costs': lambda case, evidence: self._execute(case, checker, 'apply'),
        }

    def configuration_sha(self):
        """Private configuration epoch; credential values never leave this hash."""
        return _hash({'schema':'ledger-source-configuration.v1','configuration':asdict(self.config),
            'capability_configuration':self.capability_configuration() if callable(self.capability_configuration) else None})

    def _case_scope(self,case):
        fields=('id','store_id','period','input_sha','source_context_sha')
        if not all(isinstance(case.get(key),str) and case[key] for key in fields):
            return None
        return {**{key:case[key] for key in fields},
                'run_id':str(case['run_id']) if case.get('run_id') is not None else None,
                'configuration_sha':self.configuration_sha()}

    def _remember_receipt(self, case, path):
        scope=self._case_scope(case)
        if scope is None:return
        folder=self.root/'trusted-outcomes'
        if folder.is_symlink():raise ValueError('Trusted outcome storage cannot be redirected')
        folder.mkdir(mode=0o700,exist_ok=True)
        old=self._trusted_outcomes(case,ignore_receipt=path)
        ref={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        current=self._read_receipt(scope,ref)
        if current is None:return
        result,_=current
        positive=self._positive_sources(result)
        held={(item['source_kind'],item['reason_code']):{'tool_id':item['tool_id'],
            'source_kind':item['source_kind'],'reason_code':item['reason_code'],'receipt':item['artifact_refs'][0]}
            for item in old if item['source_kind'] not in positive}
        for source,reason in self._blocked_sources(result):
            held[(source,reason)]={'tool_id':_TOOL_NAMES[result['operation']],
                'source_kind':source,'reason_code':reason,'receipt':ref}
        _write(folder/(_hash(scope)+'.json'),{'schema':'ledger-trusted-source-pointer.v2','case_scope':scope,
            'blocked':list(held.values()),'latest_receipt':ref,
            'seen_receipts':sorted(p.parent.name for p in path.parent.parent.glob('*/receipt.json'))})

    @staticmethod
    def _reason_outcome(reason, status, source):
        """Only deterministic provider codes enter the handoff vocabulary."""
        if not isinstance(reason,str):return None
        if reason.startswith(('cost_','historical_','history_')):source='history_cost'
        elif reason.startswith('jst_worker_'):source='seller_member'
        elif reason.startswith(('ledger_','checker_')):source='system'
        if status=='login_required' or reason in {'historical_cost_credentials_unconfigured'}:
            return source,'login_required'
        if reason in {'sku_type_contract_unknown','unknown_sku_contract'}:
            return source if source in {'order','history_cost'} else 'history_cost','sku_contract_unknown'
        if reason in {'case_has_no_original_run','original_order_identity_missing','order_not_found_upstream',
                      'order_not_found_or_merged_member_pending','order_member_or_sku_not_found','upstream_order_not_found'}:
            return source,'original_evidence_missing'
        if reason in {'no_historical_price_covering_original_day','history_price_not_covering_original_day',
                      'original_pricing_day_missing','historical_sku_or_company_missing'}:
            return 'history_cost','original_evidence_missing'
        if reason in {'confirmed_order_store_mapping_missing','member_requests_disabled'}:
            return source,'source_unavailable'
        if reason=='source_recovery_anchor_pending':return 'order','original_evidence_missing'
        if reason=='source_recovery_tool_pending':return 'order','source_unavailable'
        if reason.endswith('_unavailable') or re.fullmatch(r'(?:order|cost|ledger)_http_[45][0-9]{2}',reason):
            return source,'technical_failure' if source=='system' else 'source_unavailable'
        if reason in {'collector_contract_or_local_error','local_evidence_integrity_failed','invalid_json_contract',
                      'invalid_gap_page_contract','invalid_gap_row','incomplete_gap_page','coverage_context_not_retained',
                      'coverage_context_or_revision_missing','service_action_receipt_incomplete','invalid_service_action_contract'}:
            return 'system','technical_failure'
        return None

    @classmethod
    def _blocked_sources(cls, result):
        if result.get('financial_applied') is True or result.get('recompute_required') is True:
            return []
        if result.get('status') in {'stale','denied'}:return []
        sources=[]
        def add(reason,status,source):
            found=cls._reason_outcome(reason,status,source)
            if found is not None and found not in sources:sources.append(found)
        add(result.get('reason'),result.get('status'),'seller_member' if result.get('operation') in {'request','poll'} else 'order')
        for entry in result.get('items',[]):
            if not isinstance(entry,dict):continue
            add(entry.get('reason'),entry.get('status'),'order')
            request=entry.get('request')
            if isinstance(request,dict):add(request.get('reason'),request.get('status'),'seller_member')
            for history in entry.get('history',[]):
                if isinstance(history,dict) and history.get('invalid_price_observed') is not True:
                    add(history.get('reason'),history.get('status'),'history_cost')
            receipt=entry.get('service_receipt')
            if isinstance(receipt,dict):
                for reason in receipt.get('reason_codes',[]):add(reason,receipt.get('status'),'history_cost')
            # A generic application exclusion is not a SKU diagnosis. Inspect
            # only the exact raw item already grounded by this gap's identities.
            detail=entry.get('order');gap=entry.get('gap') or {}
            if isinstance(detail,dict):
                items=detail.get('items') or []
                if gap.get('internal_sub_order_id'):
                    items=[i for i in items if str(i.get('sub_order_id'))==str(gap['internal_sub_order_id'])]
                if gap.get('sku'):items=[i for i in items if str(i.get('sku_id'))==str(gap['sku'])]
                for item in items:
                    if item.get('is_gift') is True or item.get('is_virtual') is True:continue
                    if str(item.get('sku_type') or '').lower() not in {'normal','普通','普通商品'}:
                        if ('order','sku_contract_unknown') not in sources:sources.append(('order','sku_contract_unknown'))
        return sources

    @staticmethod
    def _positive_sources(result):
        if result.get('status') in {'stale','denied'}:return set()
        if result.get('financial_applied') is True or result.get('recompute_required') is True:
            return {'order','history_cost','seller_member','system','statement','freight'}
        positive=set()
        for entry in result.get('items',[]):
            if not isinstance(entry,dict):continue
            if isinstance(entry.get('order'),dict) and entry.get('observation_kind')=='current_erp_order':
                positive.update({'order','system'})
                members=entry['order'].get('items') or []
                if members and all(item.get('platform_seller_evidence_complete') is True for item in members):
                    positive.add('seller_member')
            if any(isinstance(h,dict) and (h.get('status')=='found' or h.get('invalid_price_observed') is True)
                   for h in entry.get('history',[])):
                positive.add('history_cost')
            request=entry.get('request')
            if isinstance(request,dict) and request.get('reason') in {'await_ledger_feed','await_case_recompute',
                    'upstream_request_queued','upstream_request_pending','upstream_request_already_recorded','upstream_request_in_progress'}:
                positive.add('seller_member')
            receipt=entry.get('service_receipt')
            if isinstance(receipt,dict) and receipt.get('source_refs') and any(code in {
                    'historical_amount_not_exact_cents','historical_amount_precision_unproved'} for code in receipt.get('reason_codes',[])):
                # The backend obtained the original identities and quote; its
                # precision/business rejection is not a missing-source event.
                positive.update({'order','history_cost','system'})
        if result.get('status')=='found':positive.add('system')
        return positive

    def _read_receipt(self,scope,ref):
        try:
            path=Path(ref['path'])
            if path.resolve()!=path or not path.resolve().is_relative_to(self.root) or path.name!='receipt.json':return None
            expected_parent=_hash({'case':scope['id'],'input_sha':scope['input_sha']})
            if path.parent.parent.name!=expected_parent or not re.fullmatch('[0-9a-f]{32}',path.parent.name):return None
            raw=path.read_bytes()
            if len(raw)>self.config.max_response_bytes*2 or hashlib.sha256(raw).hexdigest()!=ref['sha256']:return None
            result=json.loads(raw)
            if (result.get('schema')!='ledger-upstream-evidence.v1' or result.get('case_scope')!=scope
                    or result.get('input_sha')!=scope['input_sha'] or result.get('operation') not in _TOOL_NAMES):return None
            if result.get('run_id') is not None and str(result['run_id'])!=scope['run_id']:return None
            references=[{'path':str(path),'sha256':ref['sha256']}]
            for artifact in result.get('artifact_refs',[]):
                item=Path(artifact['path']);sha=artifact['sha256']
                if (item.resolve()!=item or not item.resolve().is_relative_to(path.parent)
                        or not re.fullmatch('[0-9a-f]{64}',sha) or hashlib.sha256(item.read_bytes()).hexdigest()!=sha):return None
                if len(references)<32:references.append({'path':str(item),'sha256':sha})
            return result,references
        except (OSError,ValueError,KeyError,TypeError,AttributeError):return None

    def trusted_outcomes(self, case):
        """Read only this Collector's exact local receipt, never model output.

        Only a positive observation of a source clears its prior diagnosis.
        A later denial or unrelated business rejection is not source recovery.
        """
        return self._trusted_outcomes(case)

    def _trusted_outcomes(self,case,ignore_receipt=None):
        scope=self._case_scope(case)
        if scope is None:return []
        pointer=self.root/'trusted-outcomes'/(_hash(scope)+'.json')
        try:
            if pointer.resolve()!=pointer or not pointer.is_file() or pointer.stat().st_size>32768:return []
            index=json.loads(pointer.read_text(encoding='utf-8'))
            if index.get('schema')!='ledger-trusted-source-pointer.v2' or index.get('case_scope')!=scope:return []
            # A crash after retaining new material but before committing its
            # pointer must not resurrect an older "missing" diagnosis. The
            # unindexed observation remains unknown until the next tool pass.
            receipt_root=self.root/_hash({'case':scope['id'],'input_sha':scope['input_sha']})
            seen=index.get('seen_receipts')
            if not isinstance(seen,list) or any(not isinstance(value,str) for value in seen):return []
            if any(path.parent.name not in seen for path in receipt_root.glob('*/receipt.json') if path!=ignore_receipt):return []
            if self._read_receipt(scope,index['latest_receipt']) is None:return []
            blocked=index.get('blocked')
            if not isinstance(blocked,list) or len(blocked)>32:return []
            outcomes=[]
            for observation in blocked:
                read=self._read_receipt(scope,observation['receipt'])
                if read is None:return []
                result,references=read
                source,reason=observation['source_kind'],observation['reason_code']
                if ((source,reason) not in self._blocked_sources(result)
                        or observation['tool_id']!=_TOOL_NAMES[result['operation']]):return []
                outcomes.append({'tool_id':observation['tool_id'],'source_kind':source,'reason_code':reason,
                    'artifact_refs':references})
            return outcomes
        except (OSError,ValueError,KeyError,TypeError,AttributeError):return []

    def _safe(self, value):
        if isinstance(value, dict):
            return {str(k): '[redacted]' if any(x in str(k).casefold() for x in ('token', 'password', 'cookie', 'authorization', 'secret'))
                    else self._safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._safe(v) for v in value]
        if isinstance(value, str):
            for token in (self.config.ledger_token, self.config.order_token, self.config.cost_token):
                if token:
                    value = value.replace(token, '[redacted]')
        return value

    @staticmethod
    def _fresh(ctx):
        try:
            current = ctx['checker']()
        except Exception:
            raise Pending('checker_unavailable') from None
        if current.get('lease_active') is not True:
            raise Pending('lease_unverified', 'denied')
        if current.get('input_sha') != ctx['case']['input_sha']:
            raise Pending('case_inputs_changed', 'stale')

    def _retain(self, ctx, source, query, payload, **extra):
        record = self._safe({'source': source, 'query': query, 'payload': payload,
            'captured_at': datetime.now(timezone.utc).isoformat(), 'input_sha': ctx['case']['input_sha'], **extra})
        digest = _hash(record)
        path = ctx['folder'] / (digest + '.json')
        if not path.exists():
            _write(path, record)
        elif _hash(json.loads(path.read_text(encoding='utf-8'))) != digest:
            raise Pending('local_evidence_integrity_failed', 'failed')
        ctx['artifacts'].append({'path': str(path), 'sha256': digest, 'source': source})
        return payload

    def _request(self, ctx, service, path, *, params=None, body=None, optional=False):
        self._fresh(ctx)
        ctx['requests'] += 1
        if ctx['requests'] > self.config.max_requests:
            raise Pending('request_quota_exceeded')
        method = 'POST' if body is not None else 'GET'
        source = service + ':' + path
        if service == 'order' and self.order_client is not None and body is None and path.startswith(INTEGRATION + '/'):
            try:
                value = self.order_client.get(path[len(INTEGRATION):], params)
            except Exception as exc:
                if type(exc).__name__ == 'OrderFeedNotFound' and optional:
                    self._retain(ctx, source, params, None, http_status=404)
                    return None
                # The legacy bridge suppresses HTTP details in its error type;
                # unavailable is unknown, never classified as no order/login.
                raise Pending('order_feed_client_unavailable') from None
            self._retain(ctx, source, params, value, via='ledger.order_feed.Client')
            return self._json_payload(value)
        origin = {'ledger': self.config.ledger_base_url, 'order': self.config.order_base_url,
                  'cost': self.config.cost_base_url}[service]
        token = {'ledger': self.config.ledger_token, 'order': self.config.order_token,
                 'cost': self.config.cost_token}[service]
        headers = {'Accept': 'application/json'}
        if token:
            headers[{'ledger': 'Authorization', 'order': 'X-Integration-Token', 'cost': 'X-ERP-Bridge-Cost-Token'}[service]] = ('Bearer ' if service == 'ledger' else '') + token
        try:
            with self.client.stream(method, origin.rstrip('/') + path, params=params, json=body,
                                    headers=headers, timeout=self.config.timeout_seconds, follow_redirects=False) as response:
                raw = bytearray()
                for block in response.iter_bytes():
                    raw.extend(block)
                    if len(raw) > self.config.max_response_bytes:
                        raise Pending('response_quota_exceeded')
                status = response.status_code
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeError):
                    value = None
                # Do not retain an HTML login page, cookies or credential-bearing
                # HTTP exception string. The structured result carries the status.
                self._retain(ctx, source, {'params': params, 'body': body}, value, http_status=status)
                if service == 'ledger' and path == '/api/automation/service-actions' and status in {403, 409}:
                    raise Pending('service_action_denied' if status == 403 else 'service_action_context_changed',
                                  'denied' if status == 403 else 'stale')
                if status in {401, 403}:
                    raise Pending(service + '_login_required', 'login_required')
                if 300 <= status < 400:
                    raise Pending(service + '_redirect_requires_login_or_configuration', 'login_required')
                if status == 404 and optional:
                    return None
                if status < 200 or status >= 300:
                    raise Pending(service + '_http_' + str(status))
                return self._json_payload(value)
        except (httpx.HTTPError, OSError):
            raise Pending(service + '_unavailable') from None

    @staticmethod
    def _json_payload(value):
        if not isinstance(value, dict):
            raise Pending('invalid_json_contract')
        message = str(value.get('msg') or value.get('message') or '')
        if value.get('code') in {-1, '-1', 401, '401'} and any(s in message.casefold() for s in ('未登录', 'login', '登录')):
            raise Pending('upstream_login_required', 'login_required')
        if value.get('success') is False or value.get('ok') is False or value.get('error'):
            raise Pending('upstream_reported_failure')
        return value

    def _bind(self, ctx):
        case = ctx['case']
        if case.get('scope', 'business') != 'business' or not all(case.get(k) for k in ('id', 'store_id', 'period', 'input_sha')):
            raise Pending('business_case_required', 'denied')
        if not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', str(case['period'])):
            raise Pending('invalid_period', 'denied')
        board = self._request(ctx, 'ledger', '/api/automation/months/' + case['period'])
        rows = [r for r in board.get('stores', []) if r.get('store_id') == case['store_id']]
        if len(rows) != 1 or rows[0].get('input_sha') != case['input_sha']:
            raise Pending('ledger_case_binding_changed', 'stale')
        row = rows[0]
        if row.get('case_id') is not None and row['case_id'] != case['id']:
            raise Pending('ledger_case_identity_changed', 'stale')
        if row.get('stage') in {'closed', 'excluded'}:
            raise Pending('case_is_closed_or_excluded', 'denied')
        run = _positive_id(row.get('run_id'))
        if not run:
            raise Pending('case_has_no_original_run')
        if case.get('run_id') is not None and str(case['run_id']) != run:
            raise Pending('case_run_changed', 'stale')
        ctx['run_id'], ctx['store_name'] = run, row.get('store')

    def _gaps(self, ctx):
        pricing, coverage = [], []
        for name, target in (('pricing-gaps', pricing), ('coverage-gaps', coverage)):
            offset, expected = 0, None
            while True:
                response = self._request(ctx, 'ledger', '/api/runs/' + ctx['run_id'] + '/' + name,
                    params={'offset': offset, 'limit': min(200, self.config.max_gaps)}, optional=True)
                if response is None:
                    # 404 means no retained file, not a proved empty gap list.
                    ctx['warnings'].append(name + '_not_retained')
                    break
                rows = response.get('items')
                total = response.get('matching', response.get('total'))
                if not isinstance(rows, list) or not isinstance(total, int) or isinstance(total, bool) or total < 0:
                    raise Pending('invalid_gap_page_contract')
                if expected is not None and total != expected:
                    raise Pending('gap_pages_changed', 'stale')
                expected = total
                if offset + len(rows) > total or not rows and offset < total:
                    raise Pending('incomplete_gap_page')
                for row in rows:
                    if not isinstance(row, dict):
                        raise Pending('invalid_gap_row')
                    if row.get('store') not in (None, '', ctx['store_name'], ctx['case']['store_id']):
                        raise Pending('foreign_store_in_run', 'conflict')
                    if row.get('period') not in (None, '', ctx['case']['period'], '(未知账期)'):
                        raise Pending('foreign_period_in_run', 'conflict')
                    if row.get('manual_amount') is None:
                        target.append({**row, '_gap_source': name})
                offset += len(rows)
                if offset >= total:
                    break
                if offset >= self.config.max_gaps:
                    raise Pending('gap_quota_exceeded')
        # Pricing and coverage are independent evidence layers. A coverage row
        # only substitutes when there is no detailed price row for that key.
        keys = {r.get('coverage_key') for r in pricing if r.get('coverage_key')}
        by_key = {}
        for row in coverage:
            if row.get('coverage_key') in by_key:
                raise Pending('duplicate_coverage_identity', 'conflict')
            by_key[row.get('coverage_key')] = row
        return [{**r, '_coverage_row': by_key.get(r.get('coverage_key'))} for r in pricing] + [
            r for r in coverage if not r.get('coverage_key') or r['coverage_key'] not in keys]

    def _approved_cost(self, ctx, gaps):
        """Send versioned identities only; the server re-fetches and prices.

        A human's scoped grant is checked by the service, never created here.
        After one accepted decision inputs change, so this captured case stops
        and the existing recompute/case pipeline resumes remaining keys.
        """
        seen = set()
        for gap in gaps:
            source = gap.get('_coverage_row') if gap.get('_gap_source') == 'pricing-gaps' else gap
            if not source:
                ctx['result']['items'].append({'status': 'pending', 'reason': 'coverage_context_not_retained'})
                continue
            key, context, revision = source.get('coverage_key'), source.get('context_sha'), source.get('line_revision')
            if key in seen:
                continue
            seen.add(key)
            if (not isinstance(key, str) or not key or not isinstance(context, str)
                    or not re.fullmatch(r'[0-9a-f]{64}', context) or not isinstance(revision, int)
                    or isinstance(revision, bool) or revision < 0):
                ctx['result']['items'].append({'status': 'pending', 'reason': 'coverage_context_or_revision_missing'})
                continue
            if source.get('partial_cost') is True or source.get('order_count') != 1:
                ctx['result']['items'].append({'status': 'pending', 'reason': 'history_rule_requires_single_complete_component'})
                continue
            case = ctx['case']
            fields = {'rule_id': HISTORY_RULE, 'case_id': case['id'], 'store_id': case['store_id'],
                      'period': case['period'], 'run_id': int(ctx['run_id']), 'coverage_key': key,
                      'context_sha': context, 'expected_line_revision': revision, 'input_sha': case['input_sha']}
            body = {'request_id': 'history-cost:' + _hash(fields), **fields}
            self._retain(ctx, 'ledger:approved_history_cost_intent', None, body)
            response = self._request(ctx, 'ledger', '/api/automation/service-actions', body=body)
            entry = {'coverage_key': key, 'status': 'pending', 'service_receipt': response}
            ctx['result']['items'].append(entry)
            if response.get('status') == 'applied':
                if (not response.get('receipt_id') or not isinstance(response.get('source_refs'), list)
                        or not response['source_refs'] or response.get('recompute_required') is not True):
                    raise Pending('service_action_receipt_incomplete')
                ctx['result'].update(financial_applied=True, applied_count=1, recompute_required=True,
                    reason='approved_source_decision_recorded_await_recompute')
                return
            if response.get('status') != 'pending' or not isinstance(response.get('reason_codes'), list):
                raise Pending('invalid_service_action_contract')
        ctx['result']['reason'] = 'history_action_pending_or_outside_approved_rule'

    def _stores(self, ctx):
        data = self._request(ctx, 'order', INTEGRATION + '/stores')
        rows = [r for r in data.get('stores', []) if r.get('ledger_store_id') == ctx['case']['store_id']
                and r.get('mapping_status') == 'confirmed' and r.get('consumable') is True]
        ids = {str(r['order_store_id']) for r in rows if r.get('order_store_id')}
        if not ids:
            raise Pending('confirmed_order_store_mapping_missing')
        return ids

    @staticmethod
    def _member_needed(gap, detail):
        text = ' '.join(str(gap.get(k) or '') for k in ('reason', 'upstream_failure_reason', 'category', 'required_evidence'))
        if any(s in text.casefold() for s in _MEMBER_REASONS):
            return True
        for item in detail.get('items', []):
            if item.get('platform_seller_evidence_complete') is False:
                return True
        return False

    def _order(self, ctx, gap, stores):
        internal = _positive_id(gap.get('internal_order_id') or gap.get('brushing_evidence_internal_id'))
        platform = str(gap.get('order_id') or '').strip()
        if not internal and not platform:
            raise Pending('original_order_identity_missing')
        detail = None
        if internal:
            detail = self._request(ctx, 'order', INTEGRATION + '/orders/' + internal, optional=True)
        else:
            # List endpoint is prefix/SKU search. Never accept the first hit.
            search = self._request(ctx, 'order', '/api/orders', params={'q': platform, 'page': 1, 'size': 100})
            rows = search.get('rows')
            if not isinstance(rows, list):
                raise Pending('invalid_order_search_contract')
            if search.get('pages', 1) > 1 or search.get('total', len(rows)) > len(rows):
                raise Pending('order_search_requires_more_evidence')
            candidates = {_positive_id(r.get('oId')) for r in rows}
            candidates.discard(None)
            details = [self._request(ctx, 'order', INTEGRATION + '/orders/' + oid, optional=True) for oid in sorted(candidates)]
            details = [d for d in details if d and str((d.get('order') or {}).get('order_store_id') or '') in stores]
            details = [d for d in details if str(d['order'].get('online_order_no') or '') == platform
                       or any(platform in {str(i.get('online_order_no') or ''), str(i.get('platform_sub_order_id') or '')}
                              for i in d.get('items', []))]
            if len(details) != 1:
                raise Pending('order_identity_ambiguous' if details else 'order_not_found_or_merged_member_pending',
                              'conflict' if details else 'pending')
            detail = details[0]
        if detail is None:
            raise Pending('order_not_found_upstream')
        order, items = detail.get('order'), detail.get('items')
        if not isinstance(order, dict) or not isinstance(items, list):
            raise Pending('invalid_order_detail_contract')
        if str(order.get('order_store_id') or '') not in stores:
            raise Pending('order_store_identity_conflict', 'conflict')
        if internal and str(order.get('order_id')) != internal:
            raise Pending('order_internal_identity_conflict', 'conflict')
        if platform and platform != str(order.get('online_order_no') or ''):
            # Member identity may be proved at item granularity. A merged header
            # or a relation alone does not authorize broadcasting its evidence.
            items = [i for i in items if str(i.get('online_order_no') or '') == platform
                     or str(i.get('platform_sub_order_id') or '') == platform]
            if not items:
                raise Pending('platform_member_identity_unproved', 'conflict')
        if gap.get('internal_sub_order_id'):
            items = [i for i in items if str(i.get('sub_order_id') or '') == str(gap['internal_sub_order_id'])]
        if gap.get('sku'):
            items = [i for i in items if str(i.get('sku_id') or '') == str(gap['sku'])]
        if not items:
            raise Pending('order_member_or_sku_not_found')
        return detail, items

    def _history(self, ctx, gap, item):
        sku = str(gap.get('sku') or item.get('sku_id') or '').strip()
        company = str(gap.get('cost_company_id') or item.get('cost_company_id') or '').strip()
        # Original pricing day is distinct from replacement order_date and the
        # current cost_as_of. Never use today's date or a reference-price date.
        original = _day(gap.get('source_pricing_day')) or _day(gap.get('order_date'))
        item_day = _day(item.get('original_order_date'))
        if not original:
            return {'status': 'pending', 'reason': 'original_pricing_day_missing'}
        if item_day and item_day != original:
            return {'status': 'conflict', 'reason': 'original_pricing_day_conflict', 'ledger_day': original, 'order_day': item_day}
        if not sku or not company:
            return {'status': 'pending', 'reason': 'historical_sku_or_company_missing'}
        if gap.get('cost_company_id') and item.get('cost_company_id') and str(gap['cost_company_id']) != str(item['cost_company_id']):
            return {'status': 'conflict', 'reason': 'cost_company_conflict'}
        if not self.config.cost_token:
            return {'status': 'pending', 'reason': 'historical_cost_credentials_unconfigured'}
        key = (sku, company, original)
        if key not in ctx['histories']:
            ctx['histories'][key] = self._request(ctx, 'cost', '/api/cost/history',
                params={'sku_ids': sku, 'as_of': original, 'wms_co_ids': company})
        rows = ctx['histories'][key].get('data')
        if not isinstance(rows, list):
            raise Pending('invalid_historical_cost_contract')
        candidates = [];invalid_price_observed=False
        for row in rows:
            if str(row.get('sku_id') or '') != sku or str(row.get('wms_co_id') or '') != company:
                continue
            if row.get('begin_unbounded') is True and row.get('begin_date') not in {'开始', '0001-01-01'}:
                return {'status': 'conflict', 'reason': 'historical_boundary_conflict'}
            begin = '0001-01-01' if row.get('begin_date') == '开始' or row.get('begin_unbounded') is True else _day(row.get('begin_date'))
            end = _day(row.get('end_date'))
            if not begin or row.get('end_date') and not end or begin > original or end and end < original:
                continue
            try:
                price = Decimal(str(row.get('cost_price')))
            except InvalidOperation:
                invalid_price_observed=invalid_price_observed or row.get('cost_price') not in (None,'')
                continue
            if not price.is_finite() or price <= 0:
                invalid_price_observed=True
                continue  # Zero needs independent policy evidence, never guessed.
            candidates.append({**row, 'begin_date': begin, 'unit_cost': str(price)})
        if not candidates:
            return {'status': 'pending', 'reason': 'no_historical_price_covering_original_day',
                    'sku': sku, 'company': company, 'pricing_day': original,'invalid_price_observed':invalid_price_observed}
        if len({Decimal(row['unit_cost']) for row in candidates}) != 1:
            return {'status': 'conflict', 'reason': 'overlapping_historical_prices', 'candidates': candidates}
        return {'status': 'found', 'reason': 'dated_price_evidence_retained', 'sku': sku,
                'company': company, 'pricing_day': original, 'candidates': candidates,
                'financial_applied': False}

    def _queue(self, ctx, order_id, *, request):
        status = self._request(ctx, 'order', INTEGRATION + '/member-seller-evidence/requests/' + order_id, optional=True)
        if not status:
            return {'status': 'pending', 'reason': 'upstream_order_not_found', 'order_id': order_id}
        if status.get('evidence_complete') is True:
            feed = self._request(ctx, 'ledger', '/api/order-feed/status')
            seq = status.get('last_seq')
            caught_up = isinstance(seq, int) and not isinstance(seq, bool) and seq > 0 and feed.get('enabled') is True and isinstance(feed.get('consumed_seq'), int) and feed['consumed_seq'] >= seq
            # Even caught-up feed is not proof that this run consumed it. The
            # independently recomputed case must receive a new input identity.
            return {'status': 'pending', 'reason': 'await_case_recompute' if caught_up else 'await_ledger_feed',
                    'order_id': order_id, 'source_seq': seq, 'feed_caught_up': caught_up,
                    'financial_applied': False}
        reason_text = json.dumps(status, ensure_ascii=False).casefold()
        if '未登录' in reason_text or 'login_required' in reason_text or 'not_logged_in' in reason_text:
            return {'status': 'login_required', 'reason': 'jst_worker_login_required', 'order_id': order_id}
        if not request or not self.config.member_requests_enabled:
            return {'status': 'pending', 'reason': 'upstream_request_pending' if request is False else 'member_requests_disabled', 'order_id': order_id, 'queue': status}
        if status.get('status') not in {'not_requested'}:
            return {'status': 'pending', 'reason': 'upstream_request_already_recorded', 'order_id': order_id, 'queue': status}
        key = _hash({'case': ctx['case']['id'], 'input_sha': ctx['case']['input_sha'], 'order_id': order_id, 'kind': 'member-seller-evidence'})
        folder = self.root / ('request-' + key)
        folder.mkdir(mode=0o700, exist_ok=True)
        receipt = folder / 'receipt.json'
        with _lock(folder / 'request.lock') as locked:
            if not locked:
                return {'status': 'pending', 'reason': 'upstream_request_in_progress'}
            if receipt.exists():
                return {'status': 'pending', 'reason': 'upstream_request_receipt_requires_reconciliation', 'receipt': str(receipt)}
            self._fresh(ctx)
            _write(receipt, {'state': 'unknown_outcome', 'input_sha': ctx['case']['input_sha'],
                             'order_id': order_id, 'operation_id': key})
            result = self._request(ctx, 'order', INTEGRATION + '/member-seller-evidence/requests',
                                   body={'order_ids': [order_id], 'force': False})
            rows = result.get('requests')
            if not isinstance(rows, list) or len(rows) != 1 or str(rows[0].get('order_id')) != order_id:
                raise Pending('unbound_upstream_request_receipt')
            _write(receipt, {'state': 'queued' if rows[0].get('queued') is True else 'reported',
                             'input_sha': ctx['case']['input_sha'], 'order_id': order_id,
                             'operation_id': key, 'result': self._safe(result)})
        return {'status': 'pending', 'reason': 'upstream_request_queued', 'receipt': str(receipt), 'queue': result}

    def _source_recovery(self,ctx,gap,*,request):
        if not self.config.source_recovery_enabled:
            return {'status':'pending','reason':'source_recovery_disabled','financial_applied':False}
        source=gap.get('_coverage_row') or gap
        if not source.get('coverage_key') or not source.get('context_sha'):
            return {'status':'pending','reason':'source_recovery_anchor_pending','financial_applied':False}
        body={'case_id':ctx['case']['id'],'run_id':int(ctx['run_id']),
            'input_sha':ctx['case']['input_sha'],'coverage_key':source['coverage_key'],
            'context_sha':source['context_sha']}
        # Server derives every lookup identity/anchor from the sealed run. No
        # model-supplied identity, path, URL, date, quantity or amount is sent.
        if request:
            value=self._request(ctx,'ledger','/api/automation/source-recovery/requests',body=body)
            task_id=value.get('task_id')
            if not isinstance(task_id,str) or not re.fullmatch(r'source-[0-9a-f]{40}',task_id):
                raise Pending('source_recovery_receipt_unbound')
            folder=self.root/'source-tasks';folder.mkdir(mode=0o700,exist_ok=True)
            from .source_recovery import durable as source_write
            source_write(folder/(_hash(body)+'.json'),{'body':body,'task_id':task_id})
        else:
            pointer=self.root/'source-tasks'/(_hash(body)+'.json')
            if not pointer.is_file():return {'status':'pending','reason':'source_recovery_not_requested'}
            stored=json.loads(pointer.read_text())
            if stored.get('body')!=body:raise Pending('source_recovery_receipt_unbound')
            task_id=stored['task_id']
            value=self._request(ctx,'ledger','/api/automation/source-recovery/requests/'+task_id)
        state=value.get('state')
        source_reason=value.get('reason')
        technical=source_reason in {'source_executor_not_qualified','source_node_runtime_unavailable',
            'source_jst_module_unavailable','source_jst_module_identity_changed','source_session_or_http_unavailable',
            'source_session_refresh_required','source_process_failed','source_runtime_unavailable',
            'source_receipt_missing','source_time_budget','source_lease_or_time_budget','source_tool_unavailable',
            'source_executor_identity_changed','source_rate_limited','source_transport_unavailable',
            'source_recovery_scope_not_enabled','source_execution_scope_changed','source_raw_bytes_unverified',
            'source_collector_identity_unverified','sealed_run_proof_missing','sealed_original_anchor_index_missing',
            'native_original_schema_pending','sealed_original_demand_index_missing'}
        return {'status':'pending','reason':'source_recovery_raw_ready' if state=='source_raw_ready' else
                'source_recovery_queued' if state=='source_queued' else 'source_recovery_tool_pending' if technical else 'source_recovery_anchor_pending',
                'task_id':task_id,'source_task_state':state,'business_complete':False,
                'financial_applied':False,'source_reason_code':source_reason,
                'independent_proof_sha256':value.get('proof_sha256')}

    def _execute(self, case, checker, mode):
        identifier = _hash({'case': case.get('id'), 'input_sha': case.get('input_sha')})
        folder = self.root / identifier / uuid.uuid4().hex
        folder.mkdir(mode=0o700, parents=True)
        ctx = {'case': case, 'checker': checker, 'folder': folder, 'artifacts': [], 'warnings': [],
               'requests': 0, 'histories': {}, 'cached_orders': []}
        result = {'schema': 'ledger-upstream-evidence.v1', 'input_sha': case.get('input_sha'),
                  'case_scope':self._case_scope(case),
                  'operation': mode, 'financial_applied': False, 'accounting_ready': False,
                  'status': 'pending', 'items': [], 'artifact_refs': ctx['artifacts']}
        ctx['result'] = result
        try:
            self._bind(ctx)
            gaps = self._gaps(ctx)
            result['run_id'] = ctx['run_id']
            if not gaps:
                result['reason'] = 'no_retained_actionable_gap'  # Not a completion assertion.
            elif mode == 'apply':
                self._approved_cost(ctx, gaps)
            else:
                if self.cached_lookup is not None:
                    for gap in gaps:
                        self._fresh(ctx)
                        try:
                            cached = self.cached_lookup(case, gap)
                        except Exception:
                            cached = None
                            ctx['warnings'].append('local_order_feed_cache_unavailable')
                        if cached:
                            self._retain(ctx, 'ledger:local_order_feed_cache',
                                {'order_id': gap.get('order_id'), 'internal_order_id': gap.get('internal_order_id')}, cached)
                            ctx['cached_orders'].append(cached)
                stores = self._stores(ctx)
                queued = set()
                for gap in gaps:
                    entry = {'gap': gap}
                    try:
                        detail, items = self._order(ctx, gap, stores)
                        entry.update(order=detail, status='found', observation_kind='current_erp_order')
                        entry['history'] = [self._history(ctx, gap, i) for i in items] if mode == 'collect' else []
                        if mode in {'request', 'poll'}:
                            if not self._member_needed(gap, detail):
                                entry.update(status='pending', reason='no_existing_recollection_endpoint_for_gap')
                                entry['request_proposal'] = {'kind': 'historical_order_or_cost_evidence',
                                    'state': 'pending_upstream_contract', 'financial_write_allowed': False}
                            else:
                                oid = _positive_id(detail['order'].get('order_id'))
                                if not oid:
                                    raise Pending('invalid_internal_order_identity')
                                if oid not in queued:
                                    entry['request'] = self._queue(ctx, oid, request=mode == 'request')
                                    queued.add(oid)
                                    entry['status'] = entry['request']['status']
                    except Pending as exc:
                        entry.update(status=exc.status, reason=exc.reason)
                        if exc.status in {'stale', 'denied'}:
                            raise
                        if self.config.source_recovery_enabled and exc.reason in {'order_not_found_upstream','order_not_found_or_merged_member_pending'}:
                            entry['request']=self._source_recovery(ctx,gap,request=mode!='poll')
                            entry.update(status='pending',reason=entry['request']['reason'])
                    result['items'].append(entry)
                self._fresh(ctx)
                statuses = {r.get('status') for r in result['items']}
                history_status = {h['status'] for r in result['items'] for h in r.get('history', [])}
                combined = statuses | history_status
                result['status'] = next((s for s in ('stale', 'denied', 'login_required', 'conflict', 'pending', 'failed') if s in combined), 'found')
                result['reason'] = 'source_evidence_retained_requires_existing_accounting_pipeline'
        except Pending as exc:
            result.update(status=exc.status, reason=exc.reason)
        except Exception as exc:
            # No raw URL/credentials/traceback escapes to the tool/model.
            result.update(status='failed', reason='collector_contract_or_local_error', error_code=type(exc).__name__)
        result['warnings'] = ctx['warnings']
        result['request_count'] = ctx['requests']
        if ctx['cached_orders']:
            result['cache_observation_count'] = len(ctx['cached_orders'])
        _write(folder / 'receipt.json', self._safe(result))
        self._remember_receipt(case,folder/'receipt.json')
        result['receipt'] = str(folder / 'receipt.json')
        return self._safe(result)
