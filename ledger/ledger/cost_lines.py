"""Audited per-order human cost amounts for uncovered order keys."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import csv
import io
import hashlib
import zipfile

import polars as pl

from .manual_cost import _cents
from .storage_integrity import verified
from .workspace import WorkspaceError


def _source_rows(ws, run_id: int) -> list[dict]:
    path = ws.coverage_gaps_path(run_id)
    if not path.exists() or not verified(path):
        raise WorkspaceError("这次计算尚无完整的未覆盖订单明细，请先重算本店")
    return pl.read_parquet(path).to_dicts()


def revision(ws, store_id: str, period: str) -> int:
    row = ws.conn.execute(
        'SELECT coalesce(max(id),0) FROM cost_line_log WHERE store_id=? AND period=?',
        (store_id, period),
    ).fetchone()
    return int(row[0])


def _latest(ws, store_id: str, period: str, run_id: int) -> dict[str, dict]:
    rows = ws.conn.execute('''
        SELECT l.* FROM cost_line_log l JOIN (
          SELECT coverage_key,max(id) id FROM cost_line_log
          WHERE store_id=? AND period=? AND source_run_id<=? GROUP BY coverage_key
        ) current ON current.id=l.id
        WHERE l.store_id=? AND l.period=?
    ''', (store_id, period, run_id, store_id, period)).fetchall()
    return {row['coverage_key']: dict(row) for row in rows}


def current(ws, run_id: int, store_id: str, period: str) -> dict:
    """Merge saved decisions only while their exact source order context is still missing."""
    items = _source_rows(ws, run_id)
    latest = _latest(ws, store_id, period, run_id)
    frozen = ws.conn.execute('SELECT decision_json FROM manual_finance WHERE run_id=?',
                             (run_id,)).fetchone()
    if frozen:
        decision = json.loads(frozen['decision_json'])
        latest = {row['coverage_key']: {
            'id': row['revision'], 'context_sha': row['context_sha'],
            'action': 'save', 'amount': str(row['amount']),
            'reason': row['reason'], 'by': '结账时的人工确认', 'at': '',
        } for row in decision.get('line_reviews') or []}
    total = Decimal(0)
    reviewed = []
    for item in items:
        item['editable'] = item['order_count'] == 1
        saved = latest.get(item['coverage_key'])
        item['line_revision'] = saved['id'] if saved else 0
        item['manual_amount'] = None
        item['manual_reason'] = ''
        item['stale_decision'] = bool(saved and saved['context_sha'] != item['context_sha'])
        if (saved and not item['stale_decision'] and item['order_count'] == 1
                and saved['action'] == 'save'):
            amount = _cents(saved['amount'])
            item['manual_amount'] = float(amount)
            item['manual_reason'] = saved['reason']
            item['manual_by'] = saved['by']
            item['manual_at'] = saved['at']
            total += amount
            reviewed.append({
                'coverage_key': item['coverage_key'], 'context_sha': item['context_sha'],
                'order_id': item['order_id'], 'amount': float(amount),
                'reason': saved['reason'], 'revision': saved['id'],
            })
    return {'items': items, 'total': len(items), 'reviewed_count': len(reviewed),
            'supplement_total': float(total),
            'line_revision': decision.get('line_revision', 0) if frozen else revision(ws, store_id, period),
            'reviewed': reviewed}


def page(ws, run_id: int, store_id: str, period: str, *, q: str = '',
         offset: int = 0, limit: int = 50) -> dict:
    result = current(ws, run_id, store_id, period)
    needle = q.strip().casefold()
    visible = [row for row in result['items']
               if not needle or any(needle in str(row.get(key) or '').casefold()
                                    for key in ('order_id','sub_order_id','coverage_key','product_ids'))]
    return {key: value for key, value in result.items() if key not in ('items','reviewed')} | {
        'matching': len(visible), 'items': visible[max(0,offset):max(0,offset)+max(1,min(limit,200))],
        'offset': max(0,offset),
    }


def export_csv(ws, run_id: int, store_id: str, period: str) -> str:
    def identifier(value):
        text = '' if value is None else str(value).strip()
        return f'="{text}"' if text.isdigit() and len(text) > 15 else text
    fields = [('order_id','平台订单号'),('sub_order_id','子订单号'),
              ('product_ids','商品链接'),('quantities','数量'),('order_date','下单日期'),
              ('manual_amount','人工补录总成本'),('manual_reason','确认依据'),
              ('editable','可直接补录'),
              ('coverage_key','清单键'),('context_sha','核对版本'),
              ('line_revision','修改版本')]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([label for _, label in fields])
    for row in current(ws, run_id, store_id, period)['items']:
        writer.writerow([identifier(row.get(key)) if key in {
                            'order_id','sub_order_id','coverage_key','context_sha'}
                         else row.get(key) if row.get(key) is not None else ''
                         for key, _ in fields])
    return output.getvalue()


def export_xlsx(ws, run_id: int, store_id: str, period: str) -> bytes:
    """Excel-native batch template; every identifier is a real text cell."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    fields = [('order_id','平台订单号'),('sub_order_id','子订单号'),
              ('product_ids','商品链接'),('quantities','数量'),('order_date','下单日期'),
              ('manual_amount','人工补录总成本'),('manual_reason','确认依据'),
              ('editable','可直接补录'),('coverage_key','清单键'),
              ('context_sha','核对版本'),('line_revision','修改版本')]
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = '未覆盖订单成本'
    sheet.append([label for _, label in fields])
    header_fill = PatternFill('solid', fgColor='DCE6F2')
    input_fill = PatternFill('solid', fgColor='FFF2CC')
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
    text_fields = {'order_id','sub_order_id','product_ids','coverage_key','context_sha'}
    for source in current(ws, run_id, store_id, period)['items']:
        values = []
        for key, _ in fields:
            value = source.get(key)
            values.append(str(value) if key in text_fields and value is not None else value)
        sheet.append(values)
        row = sheet.max_row
        for index, (key, _) in enumerate(fields, 1):
            cell = sheet.cell(row, index)
            if key in text_fields:
                cell.number_format = '@'
            elif key == 'manual_amount':
                cell.number_format = '0.00'
                cell.fill = input_fill
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    widths = [24,24,20,12,13,16,28,12,24,68,12]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[openpyxl.utils.get_column_letter(index)].width = width
    for column in ('I','J','K'):
        sheet.column_dimensions[column].hidden = True
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _batch_table(content: bytes):
    """Read the native Excel template and retain CSV compatibility."""
    if content.startswith(b'PK'):
        import openpyxl
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 100_000_000:
                    raise WorkspaceError('批量成本表解压后过大，请按店铺月份拆分')
            workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            rows = workbook.active.iter_rows(values_only=True)
            fieldnames = [str(value or '').strip() for value in next(rows)]
            return fieldnames, [dict(zip(fieldnames, row)) for row in rows]
        except (StopIteration, OSError, ValueError, zipfile.BadZipFile) as exc:
            raise WorkspaceError('Excel成本表无法读取，请重新下载模板') from exc
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            body = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise WorkspaceError('成本表编码无法读取，请上传系统导出的Excel模板或CSV')
    reader = csv.DictReader(io.StringIO(body))
    return reader.fieldnames, list(reader)


def _batch_rows(ws, store_id: str, period: str, run_id: int,
                content: bytes, default_reason: str = '') -> dict:
    """Match edited export rows to the sealed current order gap list."""
    if len(content) > 20_000_000:
        raise WorkspaceError('批量成本表过大，请按店铺月份拆分后导入')
    fieldnames, rows = _batch_table(content)
    required = {'平台订单号','子订单号','人工补录总成本','确认依据',
                '清单键','核对版本','修改版本'}
    if not fieldnames or not required <= set(fieldnames):
        raise WorkspaceError('请使用本页导出的完整订单缺口Excel或CSV，保留原表头和核对列')
    current_rows = current(ws, run_id, store_id, period)
    by_key = {row['coverage_key']: row for row in current_rows['items']}
    by_context = {}
    duplicate_contexts = set()
    for row in current_rows['items']:
        context = str(row['context_sha'])
        if context in by_context:
            duplicate_contexts.add(context)
        else:
            by_context[context] = row
    for context in duplicate_contexts:
        by_context.pop(context, None)
    chosen = []
    used = set()
    issues = []
    skipped = 0
    for line, edited in enumerate(rows, start=2):
        if line > 200_002:
            raise WorkspaceError('一次最多导入20万笔，请按店铺月份拆分')
        raw_amount = str(edited.get('人工补录总成本') or '').strip()
        if not raw_amount:
            skipped += 1
            continue
        def identifier(value):
            text = str(value or '').strip().lstrip("'")
            if len(text) >= 2 and text[0] == text[-1] == '"':
                text = text[1:-1].replace('""', '"')
            return text[2:-1] if text.startswith('="') and text.endswith('"') else text
        key = identifier(edited.get('清单键'))
        context = identifier(edited.get('核对版本'))
        source = by_context.get(context) or by_key.get(key)
        canonical = source['coverage_key'] if source else key
        if not source or canonical in used:
            issues.append(f'第{line}行订单键不存在或重复')
            continue
        used.add(canonical)
        # Excel/WPS can rewrite 18+ digit order numbers even when the export uses
        # formula text.  Those columns are for people to read, not row identity.
        # The opaque key plus the hash of its exact order context are the stable,
        # tamper-evident authority; checking the rendered IDs again only creates
        # false stale-file errors after an ordinary CSV save.
        if (source['order_count'] != 1 or source['context_sha'] !=
                context):
            issues.append(f'第{line}行清单键或核对版本已变化，请重新导出')
            continue
        try:
            expected = int(str(edited.get('修改版本') or '0').strip())
        except ValueError:
            issues.append(f'第{line}行修改版本无效')
            continue
        if expected != source['line_revision']:
            issues.append(f'第{line}行金额已有新修改，请刷新清单')
            continue
        reason = str(edited.get('确认依据') or '').strip() or default_reason.strip()
        if not reason or len(reason) > 500:
            issues.append(f'第{line}行请填写不超过500字的确认依据')
            continue
        try:
            amount = _cents(raw_amount)
        except WorkspaceError:
            issues.append(f'第{line}行成本金额须为两位小数')
            continue
        old = source.get('manual_amount')
        if old is not None and _cents(old) == amount and source.get('manual_reason') == reason:
            skipped += 1
            continue
        chosen.append({'coverage_key': canonical, 'context_sha': source['context_sha'],
                       'order_id': source['order_id'], 'amount': amount,
                       'reason': reason, 'expected_line_revision': expected})
    if not chosen and not issues:
        issues.append('没有填写新的人工成本金额')
    return {'run_id': run_id, 'file_sha': hashlib.sha256(content).hexdigest(),
            'line_revision': current_rows['line_revision'],
            'changes': chosen, 'skipped': skipped, 'issues': issues,
            'total': len(current_rows['items'])}


def batch_preview(ws, store_id: str, period: str, run_id: int,
                  content: bytes, default_reason: str = '') -> dict:
    result = _batch_rows(ws, store_id, period, run_id, content, default_reason)
    return {key: value for key, value in result.items() if key != 'changes'} | {
        'valid': len(result['changes']),
        'amount_total': float(sum((row['amount'] for row in result['changes']), Decimal(0))),
        'issues': result['issues'][:20], 'issue_count': len(result['issues']),
    }


def batch_apply(ws, store_id: str, period: str, run_id: int, content: bytes,
                *, expected_file_sha: str, expected_line_revision: int,
                default_reason: str = '', by: str = '人工批量补录') -> dict:
    if hashlib.sha256(content).hexdigest() != expected_file_sha:
        raise WorkspaceError('批量成本文件已变化，请重新预览')
    result = _batch_rows(ws, store_id, period, run_id, content, default_reason)
    if result['issues']:
        raise WorkspaceError('批量成本表不能提交：' + result['issues'][0])
    if result['line_revision'] != expected_line_revision:
        raise WorkspaceError('已有人工成本发生变化，请重新预览')
    with ws.conn as conn:
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute('SELECT id FROM run WHERE store_id=? AND period=? '
                              'ORDER BY id DESC LIMIT 1', (store_id, period)).fetchone()
        state = conn.execute('SELECT state FROM period WHERE store_id=? AND period=?',
                             (store_id, period)).fetchone()
        if not active or active['id'] != run_id or not state or state['state'] != 'open':
            raise WorkspaceError('核算结果或账期状态已更新，请重新导出成本表')
        if revision(ws, store_id, period) != expected_line_revision:
            raise WorkspaceError('已有人工成本发生变化，请重新预览')
        for item in result['changes']:
            previous = conn.execute('SELECT coalesce(max(id),0) FROM cost_line_log '
                                    'WHERE store_id=? AND period=? AND coverage_key=?',
                                    (store_id, period, item['coverage_key'])).fetchone()[0]
            if previous != item['expected_line_revision']:
                raise WorkspaceError('订单金额已被修改，请重新预览')
            conn.execute('''INSERT INTO cost_line_log(
              store_id,period,coverage_key,context_sha,source_run_id,action,
              amount,reason,at,by) VALUES(?,?,?,?,?,?,?,?,?,?)''',
              (store_id, period, item['coverage_key'], item['context_sha'], run_id,
               'save', str(item['amount']), item['reason'],
               datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'), by))
    return {'saved': len(result['changes']), 'amount_total': float(sum(
        (row['amount'] for row in result['changes']), Decimal(0))),
        'line_revision': revision(ws, store_id, period), 'run_id': run_id}


def save(ws, store_id: str, period: str, run_id: int, *, coverage_key: str,
         context_sha: str, amount, reason: str, action: str = 'save',
         expected_line_revision: int = 0, by: str = '人工操作') -> dict:
    if action not in {'save','remove'} or not reason.strip() or len(reason.strip()) > 500:
        raise WorkspaceError("补录或撤销成本必须填写不超过 500 字的依据")
    if action == 'save':
        amount = _cents(amount)
    else:
        amount = None
    source = next((row for row in _source_rows(ws, run_id)
                   if row['coverage_key'] == coverage_key), None)
    if source is None or source['order_count'] != 1 or source['context_sha'] != context_sha:
        raise WorkspaceError("订单对应关系已变化或存在歧义，请刷新明细后再修改")
    conn = ws.conn
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute('SELECT id FROM run WHERE store_id=? AND period=? ORDER BY id DESC LIMIT 1',
                              (store_id, period)).fetchone()
        state = conn.execute('SELECT state FROM period WHERE store_id=? AND period=?',
                             (store_id, period)).fetchone()
        if not active or active['id'] != run_id or not state or state['state'] != 'open':
            raise WorkspaceError("计算结果或账期状态已更新，请刷新后重试")
        previous = conn.execute('SELECT id FROM cost_line_log WHERE store_id=? AND period=? '
                                'AND coverage_key=? ORDER BY id DESC LIMIT 1',
                                (store_id, period, coverage_key)).fetchone()
        if (previous['id'] if previous else 0) != expected_line_revision:
            raise WorkspaceError("这笔人工成本已被修改，请刷新后再确认")
        if action == 'remove' and not previous:
            raise WorkspaceError("这笔订单尚无人工补录金额")
        conn.execute('''INSERT INTO cost_line_log(
            store_id,period,coverage_key,context_sha,source_run_id,action,amount,reason,at,by
        ) VALUES(?,?,?,?,?,?,?,?,?,?)''',
          (store_id, period, coverage_key, context_sha, run_id, action,
           str(amount) if amount is not None else None, reason.strip(),
           datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'), by))
    return page(ws, run_id, store_id, period)
