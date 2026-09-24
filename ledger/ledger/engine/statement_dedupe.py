"""Payment-event identity independent of workbook names and upload periods.

Only reviewed atomic IDs collapse within a sheet. Composite-only statements
retain the maximum multiplicity observed in any one sheet. Original money and
source anchors are never rewritten; normalization uses narrow shadow keys.
"""
from collections import defaultdict
from datetime import datetime

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
                owner.alias("_owner"), event.alias("_event"),
                (direction if item.template.event_directional else pl.lit("event")).alias("_leg"),
                _id(frame, "txn_id").alias("_txn"),
                _id(frame, "base_order_id").alias("_order"),
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
        economic = ["_order", "_time", "_subject", "_biz", "_remark", "_in", "_out"]
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
        strong_keep = safe.unique(subset=identity, keep="first", maintain_order=True).select("_item", "_row")
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
        keep = pl.concat([strong_keep, conflict_keep, weak_keep, unresolved_keep]).sort("_item", "_row")
        for index, item in items:
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
