"""Carry explicit ERP order flags across the live-cost overlay."""
from collections import defaultdict

import polars as pl

from .engine.link import normalize_key
from .engine.rules import norm_expr
from .model.schema import ColumnBinding


FLAG_NAMES = {'0': '无旗帜', '1': '红色旗帜', '2': '黄色旗帜', '3': '绿色旗帜',
              '4': '蓝色旗帜', '5': '紫色旗帜'}


def seller_flag_value(row):
    """Read the independent seller flag; the old field remains a pricing context."""
    code = str(row.get('seller_flag_code') or '').strip()
    name = str(row.get('seller_flag_name') or '').strip()
    decoded = FLAG_NAMES.get(code)
    if name and decoded and name != decoded:
        return None, True
    if code and decoded is None:
        return None, True
    if name and name not in FLAG_NAMES.values():
        return None, True
    return name or decoded, False


def seller_flags(items, orders):
    """Combine exact header/item identities and retain disagreements as unknown."""
    if 'seller_flag_code' not in orders.columns and 'seller_flag_name' not in orders.columns:
        return items
    headers = {}
    for row in orders.select([c for c in ('order_id', 'seller_flag_code', 'seller_flag_name') if c in orders.columns]).iter_rows(named=True):
        key = str(row['order_id'])
        value = seller_flag_value(row)
        if key in headers and headers[key] != value:
            headers[key] = (None, True)
        else:
            headers[key] = value
    values, conflicts = [], []
    for row in items.select([c for c in ('order_id', 'seller_flag_code', 'seller_flag_name', 'order_flag') if c in items.columns]).iter_rows(named=True):
        incoming, bad = seller_flag_value(row)
        header, header_bad = headers.get(str(row['order_id']), (None, False))
        # A populated child flag must agree with its header before a whole
        # merged group may inherit brushing status.
        live = str(row.get('order_flag') or '').strip() or None
        # oi.order_flag is a historical-pricing context. Once independent raw
        # seller fields are available it is not a competing seller observation.
        candidates = {v for v in (incoming, header) if v}
        if not candidates and not bad and not header_bad and live:
            candidates.add(live)
        bad = bad or header_bad or len(candidates) > 1
        values.append(None if bad else next(iter(candidates), None))
        conflicts.append(bad)
    return items.with_columns(pl.Series('order_flag', values, dtype=pl.String),
                              pl.Series('__seller_flag_conflict', conflicts, dtype=pl.Boolean))


def member_cost_flags(items, orders, store, exported_orders=None, *, scope='platform_order'):
    """Protect the actual-cost path as well as the absent-cost supplement path."""
    import json
    import re
    from .brushing_zero import _ids, _member_evidence
    from .engine.cost_policy import BRUSHING_REMARK_PATTERN
    fields = [c for c in ('order_id', 'order_store_id', 'platform_order_ids', 'order_remark',
        'platform_seller_evidence_json', 'platform_seller_evidence_version') if c in orders.columns]
    headers = {str(row['order_id']): row for row in orders.select(fields).iter_rows(named=True)}
    members = {}
    for internal, header in headers.items():
        for record in _member_evidence(header, store):
            parent = json.loads(record['parents'])[0]
            proof = json.loads(record['proof'])
            key = (internal, parent)
            if key in members and members[key] != proof:
                members[key] = {'status': 'conflict'}
            else: members[key] = proof
    flags, remarks, pending, notes = [], [], [], []
    for row in items.select([c for c in ('order_id', 'order_flag', 'outer_sku', 'online_order_no') if c in items.columns]).iter_rows(named=True):
        internal = str(row['order_id']); header = headers.get(internal, {})
        parents = _ids(header.get('platform_order_ids'))
        flag = row.get('order_flag'); remark = None; wait = False; note = None
        if scope != 'internal_order_header' and len(parents) > 1:
            parent = (exported_orders or {}).get(normalize_key(row.get('outer_sku')))
            if parent not in parents:
                written = str(row.get('online_order_no') or '').strip()
                prefix, sep, value = written.partition(':')
                candidate = value if sep and prefix == str(header.get('order_store_id')) else written if not sep else ''
                parent = candidate if candidate in parents else None
            proof = members.get((internal, parent))
            if proof and proof.get('status') == 'verified' and proof.get('source_is_merge') is False:
                flag, bad = seller_flag_value(proof)
                remark = proof.get('order_remark')
                if '买家秀' in str(header.get('order_remark') or ''):
                    remark = str(remark or '') + '；当前合并头备注含买家秀'
                wait = bad
                note = f"按原平台主单 {parent} 的独立卖家标记核对；原内部订单 {proof.get('original_internal_order_id')}；依据 {proof.get('evidence_sha')}"
            elif flag == '蓝色旗帜' and re.search(BRUSHING_REMARK_PATTERN, str(header.get('order_remark') or '')):
                wait = True
                note = f"合并刷单成员依据待核对：原平台主单 {parent or '身份待确认'} 未有独立完整标记，不能凭当前头部蓝旗/by自动免成本"
        flags.append(flag); remarks.append(remark); pending.append(wait); notes.append(note)
    return items.with_columns(pl.Series('order_flag', flags, dtype=pl.String),
        pl.Series('__member_order_remark', remarks, dtype=pl.String),
        pl.Series('__brushing_member_pending', pending, dtype=pl.Boolean),
        pl.Series('__member_brushing_note', notes, dtype=pl.String))


def collect(ingestion, store):
    choices = defaultdict(set)
    for item in ingestion.frames_of("order_cost"):
        frame = item.frame
        if frame is None or not {"internal_order_id", "order_flag"} <= set(frame.columns):
            continue
        owners=[pl.col(c).cast(pl.Utf8).str.strip_chars().replace('',None)
                for c in ('store_name','__hint_store__') if c in frame.columns]
        if not owners:
            continue  # An internal order number alone does not prove ownership.
        frame=frame.filter(pl.coalesce(owners).is_in([store.name,*store.aliases]))
        for internal, flag in frame.select("internal_order_id", "order_flag").unique().iter_rows():
            internal, flag = normalize_key(internal), normalize_key(flag)
            if internal and flag:
                choices[internal].add(flag)
    flags = {key: next(iter(values)) for key, values in choices.items() if len(values) == 1}
    conflicts = sum(len(values) > 1 for values in choices.values())
    return flags, conflicts


def fill_missing(frame, key, flags):
    """Fill absent/blank flags, never overwrite a live flag or mixed-order evidence."""
    live=(pl.col('order_flag').cast(pl.Utf8).str.strip_chars().replace('',None)
          if 'order_flag' in frame.columns else pl.lit(None,dtype=pl.Utf8))
    frame=frame.with_columns(live.alias('order_flag'))
    if key not in frame.columns or not flags or frame.is_empty():
        return frame
    identity=norm_expr(pl.col(key).cast(pl.Utf8))
    proposed=identity.replace_strict(flags,default=None,return_dtype=pl.Utf8)
    conflict=((pl.col('order_flag').is_not_null()) & proposed.is_not_null()
              & (pl.col('order_flag')!=proposed)).fill_null(False).any().over(identity)
    bad = (pl.col('__seller_flag_conflict').fill_null(False)
           if '__seller_flag_conflict' in frame.columns else pl.lit(False))
    fill=pl.col('order_flag').is_null() & proposed.is_not_null() & ~conflict & ~bad
    prior=pl.col('__flag_from_upload').fill_null(False) if '__flag_from_upload' in frame.columns else pl.lit(False)
    return frame.with_columns(pl.when(fill).then(proposed).otherwise(pl.col('order_flag')).alias('order_flag'),
                              (prior|fill).alias('__flag_from_upload'))


def apply(ingestion, store, cost, orders):
    flags, conflicts = collect(ingestion, store)
    if conflicts:
        cost.notes.append(f"有 {conflicts} 张内部订单的旗帜记录不一致，未据此排除成本")
    cost.frame=fill_missing(cost.frame,'internal_order_id',flags)
    if '__flag_from_upload' in cost.frame.columns:
        amount=int(cost.frame['__flag_from_upload'].sum() or 0)
        if amount:cost.notes.append(f'订单台旗帜缺失：按同店且无冲突的内部订单原表证据补全 {amount} 行，仍须同时满足卖家备注 by 规则才不计成本')
    # A merged group can contain items with different flags.  Only propagate a
    # flag when every line agrees; the cost rule combines it with seller remarks.
    coverage = cost.frame.group_by("order_id").agg(
        (pl.col("order_flag") == "蓝色旗帜").fill_null(False).all().alias("__all_blue"))
    all_blue={str(key):'蓝色旗帜' for key in coverage.filter(pl.col('__all_blue'))['order_id'].drop_nulls().to_list()}
    orders.frame=fill_missing(orders.frame,'order_id',all_blue)
    for item in (cost, orders):
        template = getattr(item, "template", None)
        if template is not None and not any(binding.role == "order_flag" for binding in template.bindings):
            item.template = template.model_copy(update={"bindings": template.bindings + (
                ColumnBinding(role="order_flag", columns=("order_flag",), required=False),)})
