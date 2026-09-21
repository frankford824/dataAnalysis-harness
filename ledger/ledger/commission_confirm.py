"""Run-bound human payout decisions for employee reports.

The original store calculation and store close remain separate. A later run
cannot inherit an old decision, while the audit and settlement stay readable.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation

from . import commission_profit, commission_reports
from .commission_registry import Registry, RegistryError, RevisionConflict, json_text, now
from .money import money_float


def _money(value):
    try:
        amount = Decimal(str(value))
        if (not amount.is_finite() or abs(amount) > Decimal('999999999999.99')
                or amount != amount.quantize(Decimal('.01'))):
            raise ValueError()
        return amount
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RegistryError('确认提成金额须为有效的两位小数') from exc


def _shown(ws, store_id, period):
    state = ws.state(store_id, period)
    if state and state.closed and state.run_id:
        return state.run_id, state.result or {}, True
    row = ws.latest_run(store_id, period)
    if not row:
        raise RegistryError('本店本月还没有核算记录')
    if not row.get('evidence_ready'):
        raise RegistryError('本次核算原始记录尚未留档，请稍后重试')
    return row['id'], json.loads(row['result']), False


def _last(conn, store_id, period, run_id):
    row = conn.execute('''SELECT * FROM payout_confirmation
      WHERE store_id=? AND period=? AND finance_run=?
      ORDER BY at DESC,id DESC LIMIT 1''', (store_id, period, run_id)).fetchone()
    if not row:
        return None
    result = dict(row)
    result['payouts'] = json.loads(result.pop('payouts_json'))
    result['trial'] = json.loads(result.pop('trial_json'))
    return result


def context(ws, registry, model, store_id, period, *, expected_run=None):
    commission_reports.months(period, period)
    if store_id not in {store.id for store in model.stores}:
        raise RegistryError('请选择已登记店铺')
    run_id, source, closed = _shown(ws, store_id, period)
    if expected_run is not None and run_id != expected_run:
        raise RevisionConflict('本店核算记录已更新，请刷新后重新确认')
    commission = source.get('commission') or {}
    if not isinstance(commission, dict):
        raise RegistryError('本期尚无提成计算记录')
    original = commission.get('people') or []
    identities = {str(row.get('person_id') or ''): row for row in original}
    if '' in identities or len(identities) != len(original):
        raise RegistryError('人员身份不完整，请先在店铺页核对')
    configured = commission_reports.configured_people(registry, period, period,
                                                      [store_id]).get(store_id, set())
    known = {row['id']: row for row in registry.people()}
    for pid in configured:
        if pid not in identities and pid in known:
            identities[pid] = {'person_id': pid, 'person': known[pid]['name'],
                               'amount': None}
    report = commission_reports.build(ws, registry, model, period, period,
                                      [store_id])
    labor = next((row['labor_cost'] for row in report['coverage']
                  if row['store_id'] == store_id), 0)
    base = commission.get('base_total')
    keep = (Decimal(1) if commission.get('manual_amounts_after_labor')
            else commission_reports.labor_keep(base, labor))
    with registry.connect() as conn:
        latest = _last(conn, store_id, period, run_id)
        history = [dict(row) for row in conn.execute('''SELECT id,finance_run,at,actor,
          reason,confirmed_total,payouts_json FROM payout_confirmation
          WHERE store_id=? AND period=? ORDER BY at DESC,id DESC LIMIT 10''',
          (store_id, period))]
    suggestions = {row['person_id']: row['amount'] for row in report['rows']
                   if row['store_id'] == store_id and row['period'] == period}
    if latest:
        suggestions = {row['person_id']: row['suggested']
                       for row in latest['trial'].get('people') or []}
    people = [{'person_id': pid,
               'person': row.get('person') or known.get(pid, {}).get('name') or pid,
               'suggested': suggestions.get(pid) if pid in suggestions else
               money_float(Decimal(str(row['amount'])) * keep)
               if row.get('amount') is not None else None}
              for pid, row in identities.items()]
    original_order = {row['person_id']: index for index, row in enumerate(original)}
    people.sort(key=lambda row: (original_order.get(row['person_id'], len(original)),
                                 row['person'], row['person_id']))
    roster_ids = set(known)
    exclusions = commission_profit.latest_for_run(registry, store_id, period, run_id)
    for person in people:
        display = commission_profit.display_person_id(
            store_id, person['person_id'], person['person'], roster_ids)
        extra = exclusions.get(display) or exclusions.get(person['person_id'])
        if extra:
            person['included_profit'] = extra['included_profit']
            person['excluded_count'] = extra['excluded_count']
    for item in history:
        item['payouts'] = json.loads(item.pop('payouts_json'))
    from .store_display import names
    return {'store_id': store_id, 'store': names(ws.root,model)[store_id],
            'period': period, 'run_id': run_id, 'store_closed': closed,
            'source_sha': hashlib.sha256(json_text(commission).encode()).hexdigest(),
            'people': people, 'latest': latest, 'history': history,
            'unassigned_orders': commission.get('unassigned_orders') or 0}


def confirm(ws, registry: Registry, model, *, store_id, period, run_id,
            source_sha, expected_confirmation_id, payouts, no_payout,
            reason, actor):
    from .finance_guard import guard
    with guard(ws.root,store_id,period):
        return _confirm(ws,registry,model,store_id=store_id,period=period,run_id=run_id,
            source_sha=source_sha,expected_confirmation_id=expected_confirmation_id,payouts=payouts,
            no_payout=no_payout,reason=reason,actor=actor)


def _confirm(ws, registry: Registry, model, *, store_id, period, run_id,
             source_sha, expected_confirmation_id, payouts, no_payout, reason, actor):
    if not reason.strip():
        raise RegistryError('请写明人工确认依据')
    current = context(ws, registry, model, store_id, period,
                      expected_run=run_id)
    if source_sha != current['source_sha']:
        raise RevisionConflict('本店计算数据已更新，请重新打开确认窗口')
    expected = current['latest']['id'] if current['latest'] else ''
    if expected_confirmation_id != expected:
        raise RevisionConflict('提成已经由其他操作更新，请刷新后重看')
    ids = {row['person_id'] for row in current['people']}
    entered = [str(row.get('person_id') or '') for row in payouts]
    if no_payout and (ids or payouts):
        raise RegistryError('本期已有提成人员，请逐人确认金额')
    if not ids and not no_payout:
        raise RegistryError('请明确确认本期无需提成')
    if not no_payout and (len(entered) != len(set(entered)) or set(entered) != ids):
        raise RegistryError('请填写全部提成人员的确认金额')
    amounts = {pid: _money(row.get('amount')) for pid, row in
               zip(entered, payouts)}
    snapshot = [{'person_id': row['person_id'], 'person': row['person'],
                 'amount': money_float(amounts[row['person_id']])}
                for row in current['people']]
    total = sum(amounts.values(), Decimal(0))
    confirmation_id = str(uuid.uuid4())
    record = {'id': confirmation_id, 'store_id': store_id, 'period': period,
              'finance_run': run_id, 'at': now(), 'actor': actor,
              'reason': reason.strip(), 'source_sha': source_sha,
              'payouts_json': json_text(snapshot),
              'trial_json': json_text({'people': current['people'],
                                      'unassigned_orders': current['unassigned_orders']}),
              'confirmed_total': str(total)}
    with registry.transaction() as conn:
        previous = _last(conn, store_id, period, run_id)
        if (previous['id'] if previous else '') != expected_confirmation_id:
            raise RevisionConflict('提成已经由其他操作更新，请刷新后重看')
        shown, source, _ = _shown(ws, store_id, period)
        if source.get('allocation_pending') or (source.get('allocation_correction') or {}).get('pending_orders'):
            raise RegistryError('主子订单分配依据待核对，不能核定最终实发；请先补齐分配依据')
        if (shown != run_id or hashlib.sha256(json_text(source.get('commission') or {})
                                           .encode()).hexdigest() != source_sha):
            raise RevisionConflict('本店计算数据已更新，请重新打开确认窗口')
        conn.execute('''INSERT INTO payout_confirmation VALUES(
          :id,:store_id,:period,:finance_run,:at,:actor,:reason,:source_sha,
          :payouts_json,:trial_json,:confirmed_total)''', record)
        registry.audit(conn, actor, 'payout.confirm', confirmation_id,
                       reason.strip(), previous, record)
    return {'id': confirmation_id, 'run_id': run_id,
            'confirmed_total': money_float(total), 'at': record['at']}


def active_for_runs(registry, records):
    """Select the latest immutable confirmation for each viewed run."""
    if not records:
        return {}
    ids = [row['id'] for row in records]
    result = {}
    with registry.connect() as conn:
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            for row in conn.execute('SELECT * FROM payout_confirmation WHERE finance_run IN ('
                                    + ','.join('?' for _ in chunk)
                                    + ') ORDER BY at DESC,id DESC', chunk):
                key = (row['store_id'], row['period'], row['finance_run'])
                if key not in result:
                    result[key] = dict(row)
    return result
