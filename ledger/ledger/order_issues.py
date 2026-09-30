"""Order lists for saved closing checks, bound to the exact calculation run."""
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import polars as pl

from .storage_integrity import digest, verified
from .workspace import WorkspaceError

TITLES = {
    'dropship_scope_evidence': '代发商品待确认',
    'dropship_cost_evidence': '代发支出待核对',
}
FIELDS = ['issue_id', 'order_id', 'internal_order_id', 'sku', 'order_remark',
          'quantity', 'reason', 'file_sha', 'file_name', 'sheet', 'row_no']


def path_for(ws, run_id):
    return ws.facts_path(run_id).with_suffix('.order-issues.parquet')


def _rows(facts, issue_id):
    return facts.select([
        (pl.lit(issue_id) if name == 'issue_id' else
         pl.col('source_note') if name == 'reason' else
         pl.col(name) if name in facts.columns else pl.lit(None, dtype=pl.String)
         ).cast(pl.String).alias(name) for name in FIELDS
    ]).unique(maintain_order=True)


def build(facts, uncertain, supplier_gaps, sources=()):
    """Use the same selected records as the check; preserve every product line."""
    blocks = [_rows(uncertain, 'dropship_scope_evidence')]
    if not supplier_gaps.is_empty():
        keys = ['store', 'period', 'order_id', 'sku']
        required = facts.filter(pl.col('counted') & pl.col('metric_id').is_in(
            ['goods_cost', 'reshipment_cost']) & pl.col('source_note').str.contains(
                '代发商品：聚水潭成本计 0', literal=True).fill_null(False))
        selected = required.join(supplier_gaps, on=keys, how='semi', nulls_equal=True)
        blocks.append(_rows(selected, 'dropship_cost_evidence'))
    rows = pl.concat(blocks)
    if rows.is_empty():
        return rows
    # Capture operational context while the historical source frame is available.
    # A later read must not fetch today's changed order remarks.
    for item in sources:
        frame = getattr(item, 'frame', None)
        if frame is None or frame.is_empty():
            continue
        from .engine.types import ANCHOR_SHA, ANCHOR_FILE, ANCHOR_SHEET, ANCHOR_ROW
        anchors = [ANCHOR_SHA, ANCHOR_FILE, ANCHOR_SHEET, ANCHOR_ROW]
        if not set(anchors) <= set(frame.columns):
            continue
        extra = [c for c in ['order_remark', 'quantity'] if c in frame.columns]
        if not extra:
            continue
        keys = ['file_sha', 'file_name', 'sheet', 'row_no']
        mapping = dict(zip(anchors, keys))
        source_anchors = rows.select(keys).unique()
        context = frame.select(
            *[pl.col(a).cast(pl.String).alias(mapping[a]) for a in anchors],
            *[pl.col(c).cast(pl.String).alias('__' + c) for c in extra],
        ).join(source_anchors, on=keys, how='semi', nulls_equal=True).unique()
        # Ambiguous source anchors cannot supply a guessed remark/quantity.
        context = context.filter(pl.len().over(keys) == 1)
        rows = rows.join(context, on=keys, how='left',
                         nulls_equal=True, maintain_order='left').with_columns(
            [pl.coalesce(c, '__' + c).alias(c) for c in extra]
        ).drop(['__' + c for c in extra])
    return rows


def counts(rows):
    return {issue_id: {'rows': block.height, 'orders': block['order_id'].n_unique()}
            for issue_id in TITLES
            if not (block := rows.filter(pl.col('issue_id') == issue_id)).is_empty()}


def _expected(result, issue_id):
    saved = (result.get('order_issue_counts') or {}).get(issue_id)
    if saved is not None:
        return int(saved['orders'])
    finding = next((f for f in result.get('findings', []) if f.get('id') == issue_id), None)
    if not finding or finding.get('passed'):
        raise WorkspaceError('这次核算没有该项待处理检查，请返回账期页面核对')
    match = re.match(r'\s*(\d+)\s*笔', finding.get('message') or finding.get('head') or '')
    if not match:
        raise WorkspaceError('原留档缺少待处理订单笔数，无法校验订单清单')
    return int(match[1])


def _legacy_supplier_gaps(ws, state, facts):
    """Recover relationships from the sealed commission spine, never live feeds."""
    from .engine.cost_policy import missing_supplier_costs
    from .engine.link import target_role
    from .engine.rules import norm_expr
    from .model.schema import Model
    from .snapshot_store import resolve
    cid = (state.result.get('commission') or {}).get('calculation_id')
    db = ws.root / 'commission' / 'registry.db'
    if not cid or not db.is_file():
        raise WorkspaceError('旧留档缺少该次订单关联依据，暂不能准确列出代发支出缺口；请核对原核算留档')
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        row = conn.execute('SELECT finance_run,store_id,period,path,sha,model_json '
                           'FROM calculation WHERE id=?', (cid,)).fetchone()
        if not row or row[:3] != (state.run_id, state.store_id, state.period):
            raise WorkspaceError('订单关联留档与核算版本不一致，不能混用订单清单')
        model = Model.model_validate_json(resolve(conn, row[5]))
    archive = db.parent / 'calculations' / Path(row[3]).name
    if not archive.is_file() or digest(archive) != row[4]:
        raise WorkspaceError('历史订单关联留档缺失或校验不符，代发缺口清单暂不可用')
    spine = pl.read_parquet(archive)
    needed = {'spine_row', 'order_id', 'sub_order_id'}
    if not needed <= set(spine.columns):
        raise WorkspaceError('旧订单关联留档缺少主子订单号，无法定位代发缺口')
    spine = spine.select(sorted(needed)).unique()
    if spine['spine_row'].n_unique() != spine.height:
        raise WorkspaceError('同一历史订单行存在冲突的订单号，不能选择其中一个')
    projections = []
    platform = state.result.get('platform') or model.store(state.store_id).platform
    for mid in ('goods_cost', 'reshipment_cost', 'dropship_cost'):
        source = facts.filter(pl.col('counted') & (pl.col('metric_id') == mid))
        if source.is_empty():
            continue
        metric = model.metric(mid).for_platform(platform)
        role = target_role(metric.link.to) if metric and metric.link else ''
        if role not in ('order_id', 'sub_order_id') or (
                metric.allocate and metric.allocate.mode != 'even'):
            raise WorkspaceError('旧留档的代发关联口径无法恢复，请核对该次完整订单依据')
        keyed = spine.with_columns(norm_expr(pl.col(role)).alias('link_key'))
        source = source.group_by('store', 'period', 'link_key').agg(pl.col('amount').sum())
        selected = source.join(keyed, on='link_key', how='inner')
        if mid == 'dropship_cost':
            selected = selected.filter(pl.col('amount') != 0)
        projections.append(selected.select(pl.lit(mid).alias('metric_id'),
            'store', 'period', 'link_key', 'spine_row',
            pl.lit(1. if mid == 'dropship_cost' else 0.).alias('amount')))
    if not projections:
        raise WorkspaceError('旧留档没有可验证的代发订单关联')
    return missing_supplier_costs(facts, pl.concat(projections, how='vertical_relaxed'))


def for_run(ws, run_id, issue_id):
    if issue_id not in TITLES:
        raise WorkspaceError('不支持的待处理订单事项')
    state = ws.state_by_run(run_id)
    if not state or not state.result:
        raise WorkspaceError('找不到该次核算留档')
    expected = _expected(state.result, issue_id)
    path = path_for(ws, run_id)
    if path.exists():
        if not verified(path):
            raise WorkspaceError('待处理订单明细留档校验未通过，请核对该次留档')
        rows = pl.read_parquet(path).filter(pl.col('issue_id') == issue_id)
        source = 'saved'
        expected_rows = (state.result.get('order_issue_counts') or {}).get(issue_id, {}).get('rows')
        if expected_rows is not None and rows.height != expected_rows:
            raise WorkspaceError('待处理商品明细数量与检查留档不一致，不能展示不完整清单')
    else:
        if state.result.get('order_issue_counts') is not None:
            raise WorkspaceError('该次待处理订单明细文件缺失，不能显示为零笔')
        facts_path = ws.facts_path(run_id)
        if not verified(facts_path):
            raise WorkspaceError('原始核算明细留档缺失或校验不符，订单清单暂不可用')
        facts = pl.read_parquet(facts_path)
        required = {'counted', 'source_note', 'order_id', 'metric_id', 'sku', 'store', 'period'}
        if not required <= set(facts.columns):
            raise WorkspaceError('旧留档缺少订单检查依据，不能推测订单清单')
        uncertain = facts.filter(pl.col('counted') & pl.col('source_note').str.contains(
            '代发范围待确认：', literal=True).fill_null(False))
        gaps = (_legacy_supplier_gaps(ws, state, facts) if issue_id == 'dropship_cost_evidence'
                else pl.DataFrame())
        rows = build(facts, uncertain, gaps).filter(pl.col('issue_id') == issue_id)
        source = 'historical_recovery'
    if rows['order_id'].n_unique() != expected:
        raise WorkspaceError('订单清单与原检查笔数不一致，不能展示猜测或截断后的清单；请核对该次留档')
    rows = rows.sort(['order_id', 'internal_order_id', 'sku', 'file_name', 'sheet', 'row_no'])
    return rows, source, expected


def filtered(rows, q=''):
    if q.strip():
        rows = rows.filter(pl.any_horizontal([
            pl.col(c).str.contains(q.strip(), literal=True).fill_null(False)
            for c in ('order_id', 'internal_order_id', 'sku', 'order_remark', 'file_name')
        ]))
    return rows


def platform_ids(value):
    """Unpack explicit ERP header IDs without extracting numbers from prose."""
    result = []
    for token in re.split(r'[,，;；]', str(value or '')):
        token = token.strip().lstrip("'@")
        prefixed = re.fullmatch(r'\d+:([0-9][0-9-]*)', token)
        if prefixed:
            token = prefixed[1]
        if re.fullmatch(r'[0-9][0-9-]*', token) and token not in result:
            result.append(token)
    # Preserve ordinary non-numeric platform IDs verbatim; composite IDs whose
    # syntax is unknown remain visible as the raw source, not a guessed number.
    if not result and value and not re.search(r'[,，;；:]', str(value)):
        result = [str(value).strip()]
    return result


def page(ws, run_id, issue_id, *, q='', offset=0, limit=50):
    rows, source, expected = for_run(ws, run_id, issue_id)
    selected = filtered(rows, q)
    order_ids = sorted({value for raw in selected['order_id'].drop_nulls() for value in platform_ids(raw)})
    offset, limit = max(0, offset), max(1, min(200, limit))
    return {'title': TITLES[issue_id], 'run_id': run_id, 'issue_id': issue_id,
            'total': selected.height, 'order_count': selected['order_id'].n_unique(),
            'copyable_order_count': len(order_ids),
            'expected_order_count': expected, 'source': source,
            'offset': offset, 'items': [{**row, 'platform_order_ids': platform_ids(row['order_id'])}
                                       for row in selected.slice(offset, limit).to_dicts()],
            'order_ids': order_ids,
            'missing_order_rows': selected.filter(pl.col('order_id').is_null() |
                (pl.col('order_id').str.strip_chars() == '')).height}


def export_csv(ws, run_id, issue_id, q=''):
    from .model.config import csv_cell
    from .view import _excel_identifier_cell
    rows, _, _ = for_run(ws, run_id, issue_id)
    fields = [('platform_order_ids', '平台订单号（可查询）'), ('order_id', '平台订单号（原记录）'), ('internal_order_id', '聚水潭订单号'),
              ('sku', '商品编码'), ('quantity', '原记录数量'), ('order_remark', '订单备注（核算时）'),
              ('reason', '待核对原因'), ('file_name', '来源文件'), ('sheet', '工作表'),
              ('row_no', '原表行号')]
    lines = [','.join(label for _, label in fields)]
    for row in filtered(rows, q).iter_rows(named=True):
        row['platform_order_ids'] = '；'.join(platform_ids(row['order_id']))
        lines.append(','.join(_excel_identifier_cell(row.get(key))
            if key in ('platform_order_ids', 'order_id', 'internal_order_id', 'sku') else csv_cell(row.get(key))
            for key, _ in fields))
    return '\n'.join(lines) + '\n'
