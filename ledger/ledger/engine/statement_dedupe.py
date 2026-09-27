"""Payment-event identity independent of workbook names and upload periods.

Only reviewed atomic IDs collapse within a sheet. Composite-only statements
retain the maximum multiplicity observed in any one sheet. Original money and
source anchors are never rewritten; normalization uses narrow shadow keys.
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
import json
import hashlib

import polars as pl

from .types import ANCHOR_ROW


def _time(value: str) -> str:
    value = value.strip().replace("/", "-")
    try:
        return datetime.fromisoformat(value).isoformat(sep=" ")
    except ValueError:
        return value  # never truncate time or guess an invalid date


def _text(frame, name):
    return (pl.col(name).cast(pl.String).fill_null("").str.strip_chars()
            if name in frame.columns else pl.lit(""))


def _id(frame, name):
    # Spreadsheet display wrappers are not part of identifiers. Never expand
    # scientific notation: the missing digits cannot be recovered from a float.
    return _text(frame, name).str.replace(r'^="(.*)"$', '$1').str.strip_chars("'`\" ")


_SCIENTIFIC = re.compile(r"^\d+(?:\.\d+)?e[+-]?\d+$", re.I)


def _shipping_reference(row):
    """Only a verbatim waybill in the remark certifies a lost carrier prefix."""
    original = row["_order"]
    if row['_subject'] == '在线寄件费' and (not row['_child'] or re.fullmatch(r'SCP-R\d+',row['_child'])):
        match = re.fullmatch(r'配送费（预扣）_(\d+)', row['_remark'])
        if match and original in ('', match[1]):
            return match[1]
    if row["_subject"] != "上门取件-支付快递费" or not row["_child"].isdigit():
        return original
    match = re.search(r"运单([A-Za-z]*\d+)(?!\w)", row["_remark"])
    missing_corroborated = original == "" and f"订单{row['_child']}" in row["_remark"]
    if match and (missing_corroborated or original in (match[1], re.sub(r"^[A-Za-z]+", "", match[1]))):
        return match[1]
    return original


def _compatible_precision(damaged, exact):
    """Compatibility only, never reconstruct an ID from an Excel float."""
    if not _SCIENTIFIC.fullmatch(damaged) or not exact.isdigit():
        return False
    try:
        value = Decimal(damaged)
        digits = value.as_tuple()
        # Fixed 15-digit Excel precision, not a tolerance widened by missing
        # mantissa digits. Written trailing zeroes may disappear (e.g. ...085).
        return (len(digits.digits) >= 8 and 0 < digits.exponent <= 20 and len(exact) > 15
                and abs(value - Decimal(exact)) < Decimal(10) ** (len(exact) - 15))
    except InvalidOperation:
        return False


def _corroborate_douyin(keys, ingestion, source, namespace):
    """Replace shadow identity only with a unique full-precision source event.

    Same store, parent, child, exact timestamp, subject and signed money are
    mandatory. Two full event IDs with identical business fields are ambiguous.
    Contradictory full-ID copies cannot certify a damaged copy either.
    """
    fields = ["_owner", "_order", "_child", "_time", "_subject", "_in", "_out", "_leg"]
    if not keys['_event'].str.contains(r'(?i)^\d+(?:\.\d+)?e[+-]?\d+$').any():
        return keys
    records = keys.to_dicts()
    index = defaultdict(dict)
    versions = defaultdict(set)
    for row in records:
        event = row["_event"]
        if event.isdigit() and row["_valid_money"]:
            key = tuple(row[k] for k in fields)
            index[key].setdefault(event, row)
            versions[(row["_owner"], event, row["_leg"])].add(
                (*key, row["_biz"], row["_remark"]))
    matches = []
    for row in records:
        if not _SCIENTIFIC.fullmatch(row["_event"]) or not row["_valid_money"]:
            continue
        charge_reference = (not row['_child'] and row['_order'].isdigit() and row['_remark']
                            and row['_subject'] in ['偏远地区物流服务','在线寄件费'])
        if not ((re.fullmatch(r'\d{15,}(?:[A-Za-z]\w*)?', row["_child"]) or charge_reference)
                and row["_order"] and row["_subject"] and row["_time"]):
            continue
        try:
            datetime.fromisoformat(row["_time"])
        except ValueError:
            continue
        candidates = index.get(tuple(row[k] for k in fields), {})
        if len(candidates) != 1:
            continue
        event, full = next(iter(candidates.items()))
        if charge_reference and row['_remark'] != full['_remark']:
            continue
        if len(versions[(row["_owner"], event, row["_leg"])]) != 1:
            continue
        distinct_export_versions = {row['_template'], full['_template']} == {'douyin_settlement_v1', 'douyin_settlement_v2'}
        if not distinct_export_versions and any(row[k] and full[k] and row[k] != full[k] for k in ("_biz", "_remark")):
            continue
        if not _compatible_precision(row["_event"], event):
            continue
        matches.append({"item": row["_item"], "row": row["_row"],
                        "verified_item": full["_item"], "verified_row": full["_row"], "event": event})
        # Leave the original frame untouched; only deduplication shadow keys
        # inherit the corroborating original's identity and optional wording.
        row.update(_event=event, _biz=full["_biz"], _remark=full["_remark"], _precision=1)
    if matches:
        samples = []
        for match in matches[:12]:
            old, full = ingestion.items[match['item']], ingestion.items[match['verified_item']]
            samples.append(dict(file_sha=old.ref.sha256, sheet=old.ref.sheet,
                row_no=int(old.frame[ANCHOR_ROW][match['row']]),
                verified_file_sha=full.ref.sha256, verified_sheet=full.ref.sheet,
                verified_row_no=int(full.frame[ANCHOR_ROW][match['verified_row']]), event_id=match['event']))
        ingestion.deduplication.append(dict(source=source.id, namespace=namespace,
            status="corroborated", rows=len(matches), removed_rows=0, samples=samples,
            message=f"{len(matches)} 行精度受损流水已由完整原账单逐笔唯一互证，优先保留完整流水，未猜补原始编号。"))
    return pl.from_dicts(records, schema=keys.schema)


def _compatible_export_metadata(keys):
    """A full event ID can reconcile optional metadata across reviewed layouts.

    Never resolve contradictory parent/child IDs, time, subject or money. The
    two official layouts use different descriptive remark vocabularies and one
    omits certain charge-reference child IDs. Missing is not contradictory.
    """
    reviewed = ['douyin_settlement_v1', 'douyin_settlement_v2']
    identity = ['_owner', '_event', '_leg']
    economic = ['_order', '_time', '_subject', '_in', '_out']
    candidates = keys.filter(pl.col('_template').is_in(reviewed) &
        pl.col('_event').str.contains(r'^\d+$') & pl.col('_valid_money') & (pl.col('_time') != ''))
    groups = candidates.group_by(identity).agg(
        pl.col('_template').n_unique().alias('_layouts'),
        pl.struct(economic).n_unique().alias('_economics'),
        pl.col('_child').filter(pl.col('_child') != '').n_unique().alias('_children'),
        pl.col('_child').filter(pl.col('_child') != '').first().fill_null('').alias('_canonical_child'),
        pl.col('_biz').filter(pl.col('_biz') != '').n_unique().alias('_businesses'),
        pl.col('_biz').filter(pl.col('_biz') != '').first().fill_null('').alias('_canonical_biz'),
    ).filter((pl.col('_layouts') == 2) & (pl.col('_economics') == 1)
             & (pl.col('_children') <= 1) & (pl.col('_businesses') <= 1))
    # A third/unreviewed layout or an incomplete copy cannot piggyback on the
    # compatible pair to erase a genuine conflict.
    blockers = keys.filter(~pl.col('_template').is_in(reviewed) | ~pl.col('_valid_money') | (pl.col('_time') == '')).select(identity)
    within_layout = candidates.group_by([*identity, '_template']).agg(
        pl.col('_remark').filter(pl.col('_remark') != '').n_unique().alias('_remarks')
    ).filter(pl.col('_remarks') > 1).select(identity)
    groups = groups.join(pl.concat([blockers, within_layout]), on=identity, how='anti')
    if groups.is_empty():
        return keys
    keys = keys.join(groups.select(*identity, '_canonical_child', '_canonical_biz'), on=identity,
                     how='left', maintain_order='left')
    return keys.with_columns(
        pl.coalesce('_canonical_child','_child').alias('_child'),
        pl.coalesce('_canonical_biz','_biz').alias('_biz'),
        pl.when(pl.col('_canonical_child').is_not_null()).then(pl.lit('已核对的跨版本描述'))
          .otherwise(pl.col('_remark')).alias('_remark'),
    ).drop('_canonical_child','_canonical_biz')


def dedupe_statements(ingestion, source):
    groups = defaultdict(list)
    for index, item in enumerate(ingestion.items):
        if item.ok and item.recognition.source_id == source.id and item.template:
            # Unreviewed templates cannot weaken another template's identity.
            namespace = item.template.event_namespace or f"template:{item.template.id}"
            groups[namespace].append((index, item))

    for namespace, items in groups.items():
        parts = []
        for index, item in items:
            frame = item.frame
            if frame.is_empty():
                continue
            income = pl.col("income").cast(pl.Float64, strict=False) if "income" in frame.columns else pl.lit(None, dtype=pl.Float64)
            outgo = pl.col("outgo").cast(pl.Float64, strict=False) if "outgo" in frame.columns else pl.lit(None, dtype=pl.Float64)
            valid_money = ((income.is_finite().fill_null(False) | outgo.is_finite().fill_null(False))
                           & (income.is_null() | income.is_finite()) & (outgo.is_null() | outgo.is_finite()))
            owner = pl.coalesce([
                _text(frame, "__hint_store__").replace("", None),
                _text(frame, "store_name").replace("", None),
                pl.lit(f"unscoped:{item.ref.sha256}"),
            ])
            event_role = item.template.event_id_role
            event = _id(frame, event_role) if event_role else pl.lit("")
            direction = (pl.when((income.fill_null(0) != 0) & (outgo.fill_null(0) != 0)).then(pl.lit("both"))
                         .when(income.fill_null(0) != 0).then(pl.lit("income"))
                         .when(outgo.fill_null(0) != 0).then(pl.lit("outgo")).otherwise(pl.lit("zero")))
            part = frame.select(
                pl.int_range(0, pl.len(), dtype=pl.UInt32).alias("_row"),
                pl.lit(index, dtype=pl.UInt32).alias("_item"),
                pl.lit(item.template.id).alias('_template'),
                owner.alias("_owner"), event.alias("_event"),
                (direction if item.template.event_directional else pl.lit("event")).alias("_leg"),
                _id(frame, "txn_id").alias("_txn"),
                _id(frame, "base_order_id").alias("_order"),
                _id(frame, "sub_order_id").alias("_child"),
                pl.lit(0, dtype=pl.Int64).alias("_precision"),
                _text(frame, "settle_time").alias("_time"),
                _text(frame, "subject").alias("_subject"),
                _text(frame, "remark").alias("_remark"),
                _text(frame, "biz_type").alias("_biz"),
                income.fill_null(0).alias("_in"), outgo.fill_null(0).alias("_out"),
                valid_money.alias("_valid_money"),
                (income.is_not_null() | outgo.is_not_null()).alias("_has_money"),
            )
            dates = part.select("_time").unique().with_columns(
                pl.col("_time").map_elements(_time, return_dtype=pl.String).alias("_canonical_time"))
            part = part.join(dates, on="_time", how="left", maintain_order="left").drop("_time").rename({"_canonical_time": "_time"})
            parts.append(part)
        if not parts:
            continue
        keys = pl.concat(parts, how="vertical_relaxed")
        if namespace == "douyin_settlement":
            keys = keys.with_columns(pl.when((pl.col('_subject') == '货款结算入账') &
                pl.col('_remark').is_in(['订单结算', '商家货款入账'])).then(pl.lit('货款结算入账'))
                .otherwise(pl.col('_remark')).alias('_remark'))
            keys = keys.with_columns(pl.struct("_order", "_subject", "_child", "_remark").map_elements(
                _shipping_reference, return_dtype=pl.String).alias("_order"))
            # These layouts place charge references, not order children, in
            # the child column. Preserve them in raw frames; the comparable
            # economic reference is the unchanged parent/remark reference.
            keys = keys.with_columns(pl.when(
                ((pl.col('_subject') == '偏远地区物流服务') & pl.col('_child').str.contains(r'^AWE\d+$'))
                | ((pl.col('_subject') == '在线寄件费') & pl.col('_child').str.contains(r'^SCP-R\d+$'))
            ).then(pl.lit('')).otherwise(pl.col('_child')).alias('_child'))
            keys = _compatible_export_metadata(keys)
            keys = _corroborate_douyin(keys, ingestion, source, namespace)
        economic = ["_order", "_child", "_time", "_subject", "_biz", "_remark", "_in", "_out"]
        identity = ["_owner", "_event", "_leg"]
        complete = pl.col("_valid_money") & (pl.col("_time") != "")
        atomic = keys.filter(pl.col("_event") != "")
        exact_id = ~pl.col("_event").str.contains(r"(?i)^\d+(?:\.\d+)?e[+-]?\d+$")
        conflicts = atomic.group_by(identity).agg(
            pl.struct(economic).n_unique().alias("_versions"),
            (complete & exact_id).all().alias("_complete"),
        ).filter((pl.col("_versions") > 1) | ~pl.col("_complete"))
        if conflicts.height:
            detail = atomic.join(conflicts.select(identity), on=identity, how="inner")
            samples = []
            lookup = dict(items)
            for row in detail.head(8).iter_rows(named=True):
                item = lookup[row["_item"]]
                anchor = item.frame[ANCHOR_ROW][row["_row"]] if ANCHOR_ROW in item.frame.columns else row["_row"] + 2
                samples.append(f"{item.ref.label()} 第{anchor}行")
            message = (f"{namespace} 有{conflicts.height}笔流水标识对应冲突、精度丢失或不完整的金额/订单/时间，"
                       "不可自动选一笔或相加，请核对原账单：" + "；".join(samples))
            ingestion.validation_errors.setdefault(source.id, []).append(message)
            ingestion.deduplication.append(dict(source=source.id, namespace=namespace,
                status="conflict", events=conflicts.height, removed_rows=0, message=message))

        # Conflicts remain traceable; completeness blocks authoritative totals.
        safe = atomic.join(conflicts.select(identity), on=identity, how="anti")
        strong_keep = safe.sort("_precision", maintain_order=True).unique(subset=identity, keep="first", maintain_order=True).select("_item", "_row")
        conflict_keep = atomic.join(conflicts.select(identity), on=identity, how="inner").select("_item", "_row")
        fallback = keys.filter(pl.col("_event") == "")
        composite = ["_owner", "_txn", *economic]
        complete = complete & ((pl.col("_subject") != "") | (pl.col("_biz") != "") | (pl.col("_remark") != ""))
        eligible = fallback.filter(complete).with_columns(
            pl.col("_row").cum_count().over(["_item", *composite]).alias("_occurrence"))
        weak_keep = eligible.unique(subset=[*composite, "_occurrence"], keep="first", maintain_order=True).select("_item", "_row")
        unresolved_keep = fallback.filter(~complete).select("_item", "_row")
        # Empty spacer cells and known export footer labels are not payment
        # events. Keep their raw anchors, but do not turn them into a risk.
        non_event = (~pl.col("_has_money") & (pl.col("_txn") == "") & (pl.col("_time") == "")
                     & (pl.col("_subject") == "") & (pl.col("_biz") == "") & (pl.col("_remark") == "")
                     & ((pl.col("_order") == "") | pl.col("_order").str.contains(r"^#(?:支出合计|收入合计|总计|导出时间)[:：]")))
        unresolved_count = fallback.filter(~complete & ~non_event).height
        if unresolved_count:
            message = (f"{namespace} 有{unresolved_count}行缺少可验证的流水标识或完整的时间/金额/业务描述，"
                       "无法证明重复关系；原行保留待核对，请补充原始对账单后重算。")
            ingestion.validation_errors.setdefault(source.id, []).append(message)
            ingestion.deduplication.append(dict(source=source.id, namespace=namespace,
                status="conflict", events=unresolved_count, removed_rows=0, message=message))
        pending = pl.concat([conflict_keep, fallback.filter(~complete & ~non_event).select("_item", "_row")])
        evidence_by_row = {}
        pending_keys = keys.join(pending,on=['_item','_row'],how='inner')
        pending_keys=pending_keys.with_columns(pl.when(pl.col('_event')=='').then(
            pl.concat_str(pl.col('_item').cast(pl.String),pl.lit(':'),pl.col('_row').cast(pl.String)))
            .otherwise(pl.col('_event')).alias('_pending_identity'))
        for group in pending_keys.partition_by(['_owner','_pending_identity','_leg'],maintain_order=True):
            members=[]
            for r in group.iter_rows(named=True):
                original=ingestion.items[r['_item']]
                members.append(dict(file_sha=original.ref.sha256,file_name=original.ref.filename,
                    template_id=original.template.id,namespace=namespace,
                    order_label=('订单号' if original.template.id=='douyin_settlement_v1' else
                                 '关联订单号' if original.template.id=='douyin_settlement_v2' else '订单引用'),
                    sheet=original.ref.sheet or '',row_no=int(original.frame[ANCHOR_ROW][r['_row']]),
                    event_id=r['_event'],order=r['_order'],child=r['_child'],time=r['_time'],
                    income=r['_in'] if r['_valid_money'] else None,outgo=r['_out'] if r['_valid_money'] else None,
                    subject=r['_subject'],valid=bool(r['_valid_money'])))
            members.sort(key=lambda r:(r['file_sha'],r['sheet'],r['row_no']))
            fingerprint=hashlib.sha256(json.dumps(members,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            doc=json.dumps(dict(group_id=fingerprint,namespace=namespace,leg=group['_leg'][0],members=members[:50],
                                truncated=len(members)>50),ensure_ascii=False)
            for r in group.iter_rows(named=True):evidence_by_row[(r['_item'],r['_row'])]=doc
        keep = pl.concat([strong_keep, conflict_keep, weak_keep, unresolved_keep]).sort("_item", "_row")
        for index, item in items:
            pending_ids = pending.filter(pl.col("_item") == index)["_row"].to_list()
            if pending_ids:
                old_note = pl.col("source_note") if "source_note" in item.frame.columns else pl.lit(None, dtype=pl.String)
                item.frame = item.frame.with_columns(pl.when(pl.int_range(0, pl.len()).is_in(pending_ids)).then(
                    pl.concat_str([pl.lit("对账流水待核对：身份冲突或证据不足，未计入账目"), old_note], separator="；", ignore_nulls=True)
                ).otherwise(old_note).alias("source_note"))
                item.frame=item.frame.with_columns(pl.Series('statement_evidence',
                    [evidence_by_row.get((index,pos)) for pos in range(item.frame.height)],dtype=pl.String))
            row_ids = keep.filter(pl.col("_item") == index)["_row"]
            removed = item.frame.height - len(row_ids)
            if not removed:
                continue
            before = item.frame.height
            item.frame = item.frame[row_ids]
            message = f"对账流水去重：{item.ref.label()} 的 {before:,} 行保留 {item.frame.height:,} 行，排除 {removed:,} 行重复流水；原文件完整保留。"
            item.notes.append(message)
            ingestion.deduplication.append(dict(source=source.id, namespace=namespace,
                status="deduplicated", file=item.ref.filename, sheet=item.ref.sheet,
                sha256=item.ref.sha256, removed_rows=removed, message=message))
