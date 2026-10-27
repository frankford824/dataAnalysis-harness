"""Management-only income semantics over sealed, already booked evidence.

The 1688 receipt metric includes labelled refund events. Its accounting result
stays immutable; this module can expose a proven gross/refund split for the
management report, or preserve the known net when that split is unavailable.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import zlib

import polars as pl

CONTRACT = 'operations-income-1688.v1'
PLATFORM = 'alibaba1688'
METRIC = 'trade_receipt_1688'
SOURCE = 'settlement'
TEMPLATE = 'alibaba1688_settlement_in_v1'
SUBJECTS = {'订单收入': 'receipts', '订单售后退款': 'refunds'}
REFUND_METRICS = frozenset({'trade_refund', 'trade_refund_douyin', 'trade_refund_jd', 'trade_refund_pdd'})
FACT_FIELDS = frozenset({'metric_id', 'source_id', 'template_id', 'major', 'subject',
    'counted', 'amount', 'contribution', 'store', 'period', 'link_key',
    'file_sha', 'sheet', 'row_no'})
PROJECTION_FIELDS = frozenset({'metric_id', 'source_id', 'store', 'period', 'link_key',
    'amount', 'factor', 'spine_row'})
PRECISION = Decimal('.000001')


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _records(frame):
    if frame is None:
        return set(), []
    if isinstance(frame, pl.DataFrame):
        return set(frame.columns), frame.to_dicts()
    rows = list(frame)
    if any(not isinstance(row, dict) for row in rows):
        return set(), []
    return set.intersection(*(set(row) for row in rows)) if rows else set(), rows


def _equal_amount(first, second):
    try:
        return first is not None and second is not None and first.quantize(PRECISION) == second.quantize(PRECISION)
    except InvalidOperation:
        return False


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        default=str, separators=(',', ':')).encode()).hexdigest()


def _finish(value):
    # Optional order detail never changes the identity of the month summary.
    value['input_sha'] = _fingerprint({key: item for key, item in value.items()
        if key not in ('by_spine', 'detail_proof', 'input_sha')})
    return value


def saved_net(payload, platform):
    """Accept a known saved net only with an explicit non-applicable refund basis."""
    if not isinstance(payload, dict):
        return None
    if platform != PLATFORM or payload.get('platform', platform) != PLATFORM:
        return None
    statement = payload.get('statement') or []
    if not isinstance(statement, list) or any(not isinstance(row, dict) for row in statement):
        return None
    if any(sum(row.get('id') == key for row in statement) > 1 for key in ('n_receipt', 'n_refund')):
        return None
    nodes = {row.get('id'): row for row in statement}
    receipt = nodes.get('n_receipt') or {}
    if receipt.get('available') is not True:
        return None
    net = _decimal(receipt.get('value'))
    inputs = payload.get('calculation_inputs') or {}
    if not isinstance(inputs, dict) or not isinstance(inputs.get('metric_totals'), dict):
        return None
    if any(not isinstance(inputs.get(key, []), list) or any(not isinstance(item, str) for item in inputs.get(key, []))
            for key in ('unavailable_metrics', 'inapplicable_metrics')):
        return None
    metric_net = _decimal((inputs.get('metric_totals') or {}).get(METRIC))
    if (not _equal_amount(net, metric_net) or METRIC in set(inputs.get('unavailable_metrics') or [])
            or METRIC in set(inputs.get('inapplicable_metrics') or [])):
        return None
    refund = nodes.get('n_refund')
    if refund is not None:
        # A nonzero/unknown refund contradicts the reviewed 1688 net contract.
        if refund.get('available', True) is not True or _decimal(refund.get('value')) != 0:
            return None
        if refund.get('applicable') is False:
            return net
    inapplicable = set((payload.get('calculation_inputs') or {}).get('inapplicable_metrics') or [])
    return net if REFUND_METRICS <= inapplicable else None


def _details(result, projected, *, store, period, verified, groups=None):
    fields, rows = _records(projected)
    reasons = result['detail_proof']['reason_codes']
    if not verified or not PROJECTION_FIELDS <= fields:
        reasons.append('projection_unverified_or_missing_fields')
        return
    rows = [row for row in rows if row.get('metric_id') == METRIC]
    for row in rows:
        amount, factor = _decimal(row.get('amount')), _decimal(row.get('factor'))
        spine = row.get('spine_row')
        if (row.get('source_id') != SOURCE or row.get('store') != store or row.get('period') != period
                or amount is None or factor is None or factor < 0
                or (spine is not None and (not isinstance(spine, int) or isinstance(spine, bool) or spine < 0))):
            reasons.append('projection_scope_or_value_invalid')
            return
    if not _equal_amount(sum((_decimal(row['amount']) for row in rows), Decimal(0)), result['net_income']):
        reasons.append('projection_net_not_closed')
        return
    for row in rows:
        atom = result['by_spine'].setdefault(row['spine_row'], {'receipts': None, 'refunds': None, 'net_income': Decimal(0)})
        atom['net_income'] += _decimal(row['amount'])
    if groups is None:
        return
    by_key = defaultdict(list)
    for row in rows:
        by_key[row.get('link_key')].append(row)
    if set(by_key) != set(groups):
        reasons.append('source_projection_keys_differ')
        return
    splits = defaultdict(lambda: {'receipts': Decimal(0), 'refunds': Decimal(0), 'net_income': Decimal(0)})
    for key, group in groups.items():
        target = by_key[key]
        weight = sum((_decimal(row['factor']) for row in target), Decimal(0))
        projected_net = sum((_decimal(row['amount']) for row in target), Decimal(0))
        if weight <= 0 or not _equal_amount(group['receipts'] - group['refunds'], projected_net):
            reasons.append('source_projection_not_closed')
            return
        # A partial store/month may have an effective factor below one. Prove
        # that exact factor from each already-booked source contribution before
        # using relative factors; arbitrary 1+1 factors cannot be normalised away.
        if any(not _equal_amount(amount * weight, contribution) for amount, contribution in group['source_amounts']):
            reasons.append('source_projection_factor_not_closed')
            return
        if any(row['spine_row'] is not None for row in target) and not group['all_linked']:
            reasons.append('source_order_link_unverified')
            return
        for row in target:
            factor = _decimal(row['factor']) / weight
            receipt, refund = group['receipts'] * factor, group['refunds'] * factor
            row_net = receipt - refund
            if not _equal_amount(row_net, _decimal(row['amount'])):
                reasons.append('projection_factor_conflict')
                return
            atom = splits[row['spine_row']]
            atom['receipts'] += receipt
            atom['refunds'] += refund
            atom['net_income'] += row_net
    result['by_spine'] = dict(splits)


def analyze(facts, projected=None, *, saved_net, store, period,
            facts_verified=False, projection_verified=False):
    """Pure proof check; label semantics and six-place closure precede splitting.

    The overall proof is independent of optional detail loading. Decimal results
    are internal; by_spine uses the actual integer identity (None is unallocated).
    """
    net = _decimal(saved_net)
    result = {'contract': CONTRACT, 'status': 'net_only' if net is not None else 'unavailable',
        'receipts': None, 'refunds': None, 'net_income': net, 'by_spine': {},
        'detail_proof': {'reason_codes': []},
        'proof': {'method': 'labelled_booked_source_contribution', 'facts_sha': None,
            'projection_sha': None, 'source_rows': 0, 'reason_codes': []}}
    reasons = result['proof']['reason_codes']
    fields, rows = _records(facts)
    groups = None
    if net is None:
        reasons.append('saved_net_unavailable')
    elif not facts_verified:
        reasons.append('source_facts_unverified')
    elif not FACT_FIELDS <= fields:
        reasons.append('source_fields_missing')
    else:
        candidates = [row for row in rows if row.get('metric_id') == METRIC]
        booked = [row for row in candidates if row.get('counted') is True]
        result['proof']['source_rows'] = len(booked)
        groups = defaultdict(lambda: {'receipts': Decimal(0), 'refunds': Decimal(0),
            'source_amounts': [], 'all_linked': True})
        if any(not isinstance(row.get('counted'), bool) or
                (not row['counted'] and _decimal(row.get('contribution')) != 0) for row in candidates):
            reasons.append('source_booking_invalid')
        for row in booked:
            subject = str(row.get('subject') or '').strip()
            name = SUBJECTS.get(subject)
            contribution, amount = _decimal(row.get('contribution')), _decimal(row.get('amount'))
            if (row.get('source_id') != SOURCE or row.get('template_id') != TEMPLATE
                    or row.get('major') != METRIC or name is None):
                reasons.append('source_semantics_unknown')
                break
            if (row.get('store') != store or row.get('period') != period
                    or not isinstance(row.get('file_sha'), str) or not re.fullmatch(r'[0-9a-f]{64}', row['file_sha'])
                    or not isinstance(row.get('sheet'), str) or not row['sheet']
                    or not isinstance(row.get('row_no'), int) or isinstance(row.get('row_no'), bool) or row['row_no'] < 1):
                reasons.append('source_scope_or_anchor_invalid')
                break
            if (contribution is None or amount is None or
                    (name == 'receipts' and (contribution < 0 or amount < 0)) or
                    (name == 'refunds' and (contribution > 0 or amount > 0))):
                reasons.append('source_direction_conflict')
                break
            # Preserve the engine's existing deduplication and multiplicity.
            group = groups[row.get('link_key')]
            group[name] += contribution if name == 'receipts' else -contribution
            group['source_amounts'].append((amount, contribution))
            group['all_linked'] = group['all_linked'] and row.get('linked') is True
        gross = sum((group['receipts'] for group in groups.values()), Decimal(0))
        refunds = sum((group['refunds'] for group in groups.values()), Decimal(0))
        if not reasons and not _equal_amount(gross - refunds, net):
            reasons.append('source_net_not_closed')
        if not reasons:
            result.update(status='split_verified', receipts=gross, refunds=refunds)
        else:
            groups = None
    if projected is not None or projection_verified:
        _details(result, projected, store=store, period=period, verified=projection_verified, groups=groups)
    return _finish(result)


def _artifact_metadata(ws, run_id, path):
    """One pure readback/archive policy, bound to the original M3 artifact."""
    from .run_artifacts import verified_path
    suffix=path.name[len(str(run_id)):]
    expected=None
    row=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(run_id,)).fetchone()
    if row:
        try:
            manifest=json.loads(row['manifest_json'])
            if not isinstance(manifest,dict):return None
            artifacts=manifest.get('artifacts')
            if not isinstance(artifacts,list) or any(not isinstance(item,dict) for item in artifacts):return None
            matches=[item.get('sha') for item in artifacts if item.get('path')==path.relative_to(ws.root).as_posix()]
            if len(matches)!=1 or not isinstance(matches[0],str) or not re.fullmatch('[0-9a-f]{64}',matches[0]):return None
            expected=matches[0]
        except (ValueError,TypeError,KeyError):return None
    verified=verified_path(ws.root,run_id,suffix,expected_sha=expected)
    return verified.with_name(verified.name+'.sha256').read_text(encoding='ascii').strip() if verified is not None else None


def _read_artifact(path, sha):
    if sha is None:
        return None
    from .storage_integrity import digest
    try:
        frame = pl.read_parquet(path)
        return frame if digest(path) == sha else None
    except (OSError, pl.exceptions.PolarsError):
        return None


def load(ws, run_id, payload, platform, *, details=True):
    """Read the selected run only. Never replay sources or change a ledger."""
    if platform != PLATFORM:
        return None
    # The caller chooses a run, never its money. Prefer the persisted compact
    # projection of that exact run/manual result, with the full saved result as
    # a legacy fallback. This SQL never follows a newer background run.
    row = ws.conn.execute('''SELECT r.store_id,r.period,r.evidence_ready,r.evidence_error,
        CASE WHEN mf.run_id IS NOT NULL THEN 'manual' ELSE 'run' END payload_kind,
        CASE WHEN s.overview_json IS NOT NULL THEN 'run_report_slice.overview_json' ELSE 'full_result' END result_projection,
        coalesce(s.overview_json,mf.result_json,r.result) saved_result
        FROM run r LEFT JOIN period p ON p.store_id=r.store_id AND p.period=r.period
        LEFT JOIN manual_finance mf ON mf.run_id=r.id AND p.state='closed' AND p.run_id=r.id
        LEFT JOIN run_report_slice s ON s.run_id=r.id AND s.payload_kind=CASE WHEN mf.run_id IS NOT NULL THEN 'manual' ELSE 'run' END
        WHERE r.id=?''', (run_id,)).fetchone() if isinstance(run_id, int) and not isinstance(run_id, bool) and run_id > 0 else None
    try:
        from .snapshot_store import resolve
        stored_text=resolve(ws.conn,row['saved_result']) if row else ''
        original = json.loads(stored_text) if row else {}
    except (ValueError, TypeError, zlib.error):
        stored_text='';original = {}
    if not isinstance(original, dict):
        original = {}
    valid_run = bool(row and row['evidence_ready'] and not row['evidence_error'])
    facts, facts_sha, projected, projection_sha = None, None, None, None
    source_contract = None
    if valid_run:
        # Workspace.facts_path also mkdirs the evidence directory. This reader
        # must remain read-only even when an old run has lost that directory.
        from .run_artifacts import path as artifact_path
        path = artifact_path(ws.root,run_id)
        facts_sha = _artifact_metadata(ws, run_id, path)
        facts = _read_artifact(path, facts_sha)
        projection_path = path.with_suffix('.operations.parquet')
        projection_sha = _artifact_metadata(ws, run_id, projection_path)
        if details:
            projected = _read_artifact(projection_path, projection_sha)
        # Captured template identity can prove the known net even when the raw
        # facts are missing. The known gross/refund split still needs every fact.
        proof_row = ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?', (run_id,)).fetchone()
        try:
            captured = json.loads(proof_row['manifest_json']) if proof_row else {}
        except (ValueError, TypeError):
            captured = {}
        if not isinstance(captured, dict):
            captured = {}
        templates = captured.get('template_ids', [])
        if (isinstance(templates, list) and all(isinstance(template, str) for template in templates)
                and TEMPLATE in templates and isinstance(captured.get('model_sha'), str)
                and re.fullmatch(r'[0-9a-f]{64}', captured['model_sha'])):
            source_contract = {'method': 'captured_template_allowlist', 'template_id': TEMPLATE,
                'source_id': SOURCE, 'metric_id': METRIC, 'model_sha': captured.get('model_sha')}
        elif facts is not None and {'metric_id', 'source_id', 'template_id'} <= set(facts.columns):
            candidates = facts.filter(pl.col('metric_id') == METRIC)
            if candidates.height and all(r['source_id'] == SOURCE and r['template_id'] == TEMPLATE for r in candidates.to_dicts()):
                source_contract = {'method': 'sealed_source_template_allowlist', 'template_id': TEMPLATE,
                    'source_id': SOURCE, 'metric_id': METRIC, 'facts_sha': facts_sha}
    net = saved_net(original, platform) if valid_run and source_contract else None
    result = analyze(facts, projected, saved_net=net, store=original.get('accounting_store') or original.get('store'),
        period=row['period'] if row else None, facts_verified=facts is not None,
        projection_verified=details and projected is not None)
    net_source = None
    if net is not None:
        nodes = {item.get('id'): item for item in original.get('statement') or []}
        refund = nodes.get('n_refund') or {}
        method = 'explicit_not_applicable' if refund.get('applicable') is False else 'inapplicable_metrics'
        identity = {'run_id': run_id,
            'store_id': row['store_id'], 'period': row['period'], 'platform': PLATFORM,
            'statement_receipt': {key: nodes['n_receipt'].get(key) for key in ('id', 'value', 'available')}}
        net_source = {'kind': 'saved_1688_net_statement', **identity,
            'metric_id': METRIC, 'metric_total': (original['calculation_inputs'].get('metric_totals') or {})[METRIC],
            'refund_not_applicable': {'method': method, 'metric_ids': sorted(REFUND_METRICS)},
            'source_contract': source_contract, 'payload_kind': row['payload_kind'],
            'result_projection': row['result_projection'],
            'result_sha': _fingerprint(identity),
            'stored_text_sha': hashlib.sha256(stored_text.encode()).hexdigest()}
        if method == 'explicit_not_applicable':
            net_source['refund_not_applicable']['statement_refund'] = {
                key: refund.get(key) for key in ('id', 'value', 'available', 'applicable')}
    result['proof'].update(run_id=run_id, facts_sha=facts_sha, projection_sha=projection_sha, net_source=net_source)
    if not valid_run:
        result['proof']['reason_codes'].append('run_evidence_unavailable')
    elif source_contract is None:
        result['proof']['reason_codes'].append('income_source_contract_unverified')
    return _finish(result)
