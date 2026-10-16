"""Read-only freight review: compare saved records, never certify or post changes."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from decimal import Decimal

from .money import decimal_amount, money_float


TASKS = {
    'changes': ('试算有变化', '需要复核', '先看哪些店铺有差异，再按需查看依据。试算差额不是应补扣金额。'),
    'syncing': ('试算资料未齐', '暂不比较', '该次试算的来源或留档不完整，暂不判断差额。不需要逐单操作。更新结果只读取已有记录。'),
    'evidence': ('金额依据待核对', '需要资料', '部分记录尚无完整运费金额，不能按零元处理。'),
    'allocation': ('商品分摊待核对', '需要依据', '完整试算中已有待分摊费用，不代表店铺漏扣。'),
    'no_trial': ('尚无后续试算', '等待结果', '原记录保留；有完整的新试算后，才能比较变化。'),
}


def _expense(payload):
    node = next((n for n in payload.get('statement') or [] if n.get('id') == 'n_freight'), None)
    if node is None:
        return None, '本次记录没有发货运费金额'
    if node.get('available') is False or node.get('value') is None:
        return None, node.get('unavailable_reason') or '运费金额依据不完整'
    try:
        amount = -decimal_amount(node['value'])
        if not amount.is_finite():
            return None, '运费金额尚未形成'
        return money_float(amount), ''
    except (ValueError, ArithmeticError):
        return None, '运费金额尚未形成'


def _total(rows, key):
    if not rows or any(r.get(key) is None for r in rows):
        return None
    return money_float(sum((decimal_amount(r[key]) for r in rows), Decimal(0)))


def build(ws, model, period, *, store_id='', platform='', store_names=None):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period or ''):
        raise ValueError('请选择有效月份')
    stores = {s.id: s for s in model.stores}
    if store_id and store_id not in stores:
        raise ValueError('请选择已登记店铺')
    if platform and platform not in {p.id for p in model.platforms}:
        raise ValueError('请选择已登记平台')
    selected = {sid for sid, s in stores.items()
                if (not store_id or sid == store_id) and (not platform or s.platform == platform)}
    names = store_names or {sid: s.name for sid, s in stores.items()}
    # One SELECT fixes shown/latest pointers together. Nothing is recalculated or written.
    source = ws.conn.execute('''
      SELECT p.store_id,p.state,p.run_id frozen_run_id,
        r.id shown_id,r.at shown_at,r.result shown_result,
        mf.result_json manual_result,
        l.id latest_id,l.at latest_at,l.result latest_result,l.evidence_ready
      FROM period p
      LEFT JOIN run l ON l.id=(SELECT max(lr.id) FROM run lr
        WHERE lr.store_id=p.store_id AND lr.period=p.period)
      LEFT JOIN run r ON r.id=CASE WHEN p.state='closed' AND p.run_id IS NOT NULL
        THEN p.run_id ELSE l.id END
      LEFT JOIN manual_finance mf ON mf.run_id=r.id AND p.state='closed'
      WHERE p.period=? ORDER BY p.store_id
    ''', (period,)).fetchall()
    rows = []
    evidence_version = []
    for record in source:
        sid = record['store_id']
        if sid not in selected:
            continue
        shown_raw = record['manual_result'] or record['shown_result'] or '{}'
        latest_raw = record['latest_result'] or '{}'
        shown, latest = json.loads(shown_raw), json.loads(latest_raw)
        current_amount, current_reason = _expense(shown)
        new_amount, new_reason = _expense(latest)
        closed = record['state'] == 'closed'
        issues = []
        if current_amount is None:
            issues.append('evidence')
        same = record['shown_id'] == record['latest_id']
        if record['latest_id'] is None or (closed and same):
            trial_status, trial_reason = 'no_trial', '尚无可对照的后续试算'
            issues.append('no_trial')
        elif not record['evidence_ready'] or latest.get('source_sync_pending'):
            trial_status, trial_reason = 'syncing', ('来源尚未同步完整' if latest.get('source_sync_pending') else '试算依据尚未留全')
            issues.append('syncing')
        elif new_amount is None:
            trial_status, trial_reason = 'evidence_missing', new_reason
            if 'evidence' not in issues:
                issues.append('evidence')
        else:
            trial_status, trial_reason = ('same' if same else 'ready'), ''
        complete = trial_status in {'ready', 'same'}
        delta = money_float(decimal_amount(new_amount) - decimal_amount(current_amount)) if complete and current_amount is not None else None
        if delta is not None and delta != 0:
            issues.append('changes')
        allocation = [item for item in latest.get('allocation_pending') or []
                      if item.get('metric_id') == 'freight_cost'] if complete else []
        if allocation:
            issues.append('allocation')
        from .engine.allocation import REASON_LABELS
        allocation_reasons = sorted({REASON_LABELS.get(item.get('reason'), '商品分摊依据待核对') for item in allocation})
        rows.append({
            'store_id': sid, 'store': names.get(sid) or stores[sid].name,
            'platform': stores[sid].platform, 'period': period,
            'closed': closed, 'shown_run_id': record['shown_id'], 'shown_at': record['shown_at'] or '',
            'shown_amount': current_amount, 'shown_reason': current_reason,
            'trial_run_id': record['latest_id'], 'trial_at': record['latest_at'] or '',
            'trial_amount': new_amount if complete else None,
            'trial_status': trial_status, 'trial_reason': trial_reason, 'change': delta,
            'allocation_count': len(allocation), 'allocation_reasons': allocation_reasons,
            'issues': issues,
        })
        evidence_version.append((sid, stores[sid].platform, record['state'], record['shown_id'], record['latest_id'],
            hashlib.sha256(shown_raw.encode()).hexdigest(), hashlib.sha256(latest_raw.encode()).hexdigest(),
            record['evidence_ready']))
    groups = []
    for key, (title, owner, hint) in TASKS.items():
        affected = [r for r in rows if key in r['issues']]
        if not affected:
            continue
        groups.append({'id': key, 'title': title, 'owner': owner, 'hint': hint,
                       'store_count': len(affected), 'store_ids': [r['store_id'] for r in affected],
                       'net_change': _total(affected, 'change') if key == 'changes' else None})
    summary = {
        'store_count': len(rows), 'closed_count': sum(r['closed'] for r in rows),
        'shown_amount': _total(rows, 'shown_amount'),
        'trial_amount': _total(rows, 'trial_amount'), 'change': _total(rows, 'change'),
        'shown_available_count': sum(r['shown_amount'] is not None for r in rows),
        'trial_available_count': sum(r['trial_amount'] is not None for r in rows),
        'changed_store_count': sum('changes' in r['issues'] for r in rows),
        'increased_store_count': sum((r['change'] or 0) > 0 for r in rows),
        'decreased_store_count': sum((r['change'] or 0) < 0 for r in rows),
    }
    scope = {'period': period, 'store_id': store_id, 'platform': platform}
    visible_names = {r['store_id']: r['store'] for r in rows}
    fingerprint = hashlib.sha256(json.dumps([scope, visible_names, evidence_version], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'scope': scope, 'summary': summary, 'groups': groups, 'stores': rows,
            'fingerprint': fingerprint, 'audit_status': 'comparison_only',
            'notice': '本页只比较账内记录与完整试算。差额尚未核实，原结账和提成不变。'}


def export_csv(data, group=''):
    if group and group not in TASKS:
        raise ValueError('请选择有效问题组')
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['店铺', '月份', '问题', '账内运费（元）', '后续试算运费（元）', '试算差额（元，未核实）',
                     '原记录状态', '试算状态', '金额说明', '商品分摊说明', '原记录编号', '试算编号', '原记录时间', '试算时间', '核对范围校验值'])
    labels = {'ready': '可对照', 'same': '同一份试算', 'no_trial': '尚无后续试算',
              'syncing': '试算资料未齐', 'evidence_missing': '金额依据待核对'}
    for row in data['stores']:
        if group and group not in row['issues']:
            continue
        name = row['store']
        if name.startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
            name = "'" + name
        cells = [name, row['period'], '；'.join(TASKS[k][0] for k in row['issues']),
            row['shown_amount'], row['trial_amount'], row['change'], '已结账' if row['closed'] else '未结账试算',
            labels[row['trial_status']], row['shown_reason'] or row['trial_reason'] or '试算差额尚未核实，不是应补扣金额',
            '；'.join(row['allocation_reasons']),
            row['shown_run_id'], row['trial_run_id'], row['shown_at'], row['trial_at'], data['fingerprint']]
        writer.writerow(["'" + v if isinstance(v, str) and
                         (v.startswith(('=', '+', '-', '@', '\t', '\r', '\n')) or (v.isdigit() and len(v) >= 15))
                         else v for v in cells])
    return '\ufeff' + output.getvalue()
