"""Deterministic per-order allocation against an immutable relationship snapshot."""
from __future__ import annotations

import hashlib
import json
import warnings
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import polars as pl

from .commission import _order_base, _store_labels
from .commission_registry import Registry, json_text, now
from .money import money_float


def _legacy(model, store_id):
    groups = defaultdict(list)
    for rule in model.commission_for(store_id):
        groups[(rule.product_id, rule.effective_from)].append(rule)
    versions = []
    for (pid, day), rules in groups.items():
        body = [r.model_dump() for r in rules]
        versions.append({
            "id": "legacy:" + hashlib.sha256(json_text(body).encode()).hexdigest()[:24],
            "product_id": pid or "*", "body": {"product_name": rules[0].product_name,
            "segments": [{"valid_from": day + "T00:00:00", "valid_to": "", "mode": "distribute",
                          "total_rate": str(rules[0].total_rate), "allocations": [
                              {"person_id": "legacy:" + hashlib.sha256(r.person.encode()).hexdigest()[:24],
                               "person": r.person, "role": "历史配置", "rate": str(r.share)} for r in rules]}]},
        })
    return versions


def _frames(versions, people, wages="pending"):
    rules, allocations = [], []
    for version in versions:
        for index, segment in enumerate(version["body"]["segments"]):
            key = version["id"] + ":" + str(index)
            rules.append({"rule_key": key, "product_id": version["product_id"],
                          "valid_from": segment["valid_from"], "valid_to": segment.get("valid_to") or None,
                          "mode": segment.get("mode", "distribute"),
                          "managed": segment.get("managed", False),
                          "managed_team_id": segment.get("managed_team_id", ""),
                          "sales_unassigned": False,
                          "amount_hold": "" if wages == "skip_preview" else segment.get("amount_hold", ""),
                          "wage_preview": wages == "skip_preview" and segment.get("amount_hold") == "wage_pending",
                          "total_rate": segment["total_rate"], "rule_version": version["id"],
                          "rule_name": version["body"].get("product_name", ""),
                          "priority": 0 if version["id"].startswith("legacy:") else 1})
            for line in segment.get("allocations", []):
                pid = line["person_id"]
                allocations.append({"rule_key": key, "person_id": pid,
                                    "person": people.get(pid, {}).get("name") or line.get("person", pid),
                                    "role": line["role"], "share": line["rate"],
                                    "duty": line.get("duty", "")})
    schema = {k: pl.Utf8 for k in ["rule_key", "product_id", "valid_from", "valid_to", "mode",
                                  "total_rate", "rule_version", "rule_name", "amount_hold", "managed_team_id"]} | {"priority": pl.Int64, "wage_preview": pl.Boolean, "managed": pl.Boolean, "sales_unassigned": pl.Boolean}
    rf = pl.DataFrame(rules, schema=schema).with_columns(
        pl.col("valid_from").str.to_datetime("%Y-%m-%dT%H:%M:%S").alias("from_at"),
        pl.col("valid_to").str.to_datetime("%Y-%m-%dT%H:%M:%S", strict=False).alias("to_at"),
    ).sort("priority").unique(subset=["product_id", "valid_from"], keep="last")
    af = pl.DataFrame(allocations, schema={k: pl.Utf8 for k in ["rule_key", "person_id", "person", "role", "share", "duty"]})
    af = af.group_by("rule_key", "person_id", "person", maintain_order=True).agg(
        pl.col("role").str.join(" / "),
        pl.col("share").cast(pl.Decimal(16, 8)).sum(),
        pl.struct("role", "share").alias("allocation_parts"),
        pl.col("duty").first(),
    )
    return rf, af


def _match(orders, rules):
    known = orders.filter(pl.col("order_at").is_not_null()).sort("order_at")
    unknown = orders.filter(pl.col("order_at").is_null())
    product = rules.filter(pl.col("product_id") != "*").sort("from_at")
    defaults = rules.filter(pl.col("product_id") == "*").drop("product_id").sort("from_at")
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Sortedness of columns")
        matched = known.join_asof(product, left_on="order_at", right_on="from_at", by="product_id",
                                 strategy="backward").with_columns(pl.lit(False).alias("fallback"))
        eligible = matched.filter(pl.col("rule_key").is_null() & (pl.col("product_id") != ""))
        if eligible.height and defaults.height:
            eligible = eligible.select(orders.columns).join_asof(defaults, left_on="order_at",
                                                                 right_on="from_at", strategy="backward")
            eligible = eligible.with_columns(pl.col("rule_key").is_not_null().alias("fallback"))
            matched = pl.concat([matched.filter(~(pl.col("rule_key").is_null() & (pl.col("product_id") != ""))),
                                 eligible], how="diagonal_relaxed")
    if unknown.height:
        matched = pl.concat([matched, unknown.with_columns(pl.lit(False).alias("fallback"))], how="diagonal_relaxed")
    # Expiry deliberately stops an old product rule; it does not resurrect a
    # previous owner or silently start paying the store default.
    return matched.with_columns(
        pl.when(pl.col('allocation_pending').fill_null(False) if 'allocation_pending' in matched.columns else pl.lit(False)).then(pl.lit('allocation_pending'))
        .when(pl.col("order_at").is_null()).then(pl.lit("missing_order_time"))
        .when(pl.col("product_id") == "").then(pl.lit("missing_product_id"))
        .when(pl.col("rule_key").is_null()).then(pl.lit("unassigned"))
        .when(pl.col("to_at").is_not_null() & (pl.col("order_at") >= pl.col("to_at"))).then(pl.lit("expired"))
        .when((pl.col("mode") == "distribute") & (pl.col("amount_hold") != "")).then(pl.col("amount_hold"))
        .otherwise(pl.col("mode")).alias("status")
    )


def _uniform_distribution_rule(rules, allocations, period):
    """Return one safe store-wide rule when every rule in the month has one signature.

    Missing product IDs and missing source timestamps cannot select a product/time
    rule.  They may still be attributed when every possible rule for this store
    month names the same people at the same rates.  A mixed owner, rate, exclusion,
    expiry, or amount hold remains unassigned instead of being guessed.
    """
    try:
        start = datetime.fromisoformat(period + "-01T00:00:00")
        year, month = map(int, period.split("-"))
        end = datetime(year + (month == 12), month % 12 + 1, 1)
    except (TypeError, ValueError):
        return None
    candidates = rules.filter(
        (pl.col("from_at") < end)
        & (pl.col("to_at").is_null() | (pl.col("to_at") > start))
    )
    if candidates.is_empty() or candidates.filter(
        (pl.col("mode") != "distribute")
        | (pl.col("amount_hold") != "")
    ).height:
        return None
    signatures = {}
    for rule in candidates.iter_rows(named=True):
        lines = allocations.filter(pl.col("rule_key") == rule["rule_key"])
        if lines.is_empty():
            return None
        shares = tuple(sorted(
            (str(person), Decimal(str(share)))
            for person, share in lines.select("person_id", "share").iter_rows()
        ))
        total = Decimal(str(rule["total_rate"]))
        if total <= 0 or any(share <= 0 for _, share in shares) or sum(
            (share for _, share in shares), Decimal(0)
        ) != total:
            return None
        signatures.setdefault((total, shares), rule)
        if len(signatures) > 1:
            return None
    result = dict(next(iter(signatures.values())))
    destinations = {(r.get('managed', False), r.get('managed_team_id', '')) for r in candidates.iter_rows(named=True)}
    if len(destinations) > 1:
        # Preserve the established payout fallback, but do not guess sales ownership.
        result.update(managed=False, managed_team_id='', sales_unassigned=True)
    return result


def _attribute_uniform_unknowns(matched, rules, allocations, period):
    """Apply an unambiguous store-month distribution to rows lacking match inputs."""
    matched = matched.with_columns(
        pl.when(pl.col("fallback")).then(pl.lit("store_default"))
        .otherwise(pl.lit("")).alias("fallback_reason")
    )
    rule = _uniform_distribution_rule(rules, allocations, period)
    if rule is None:
        return matched, 0
    unknown = pl.col("status").is_in(["missing_order_time", "missing_product_id"])
    count = int(matched.select(unknown.sum()).item() or 0)
    if not count:
        return matched, 0
    columns = [
        "rule_key", "valid_from", "valid_to", "mode", "total_rate",
        "rule_version", "rule_name", "amount_hold", "priority", "wage_preview",
        "from_at", "to_at", "managed", "managed_team_id", "sales_unassigned",
    ]
    matched = matched.with_columns(*[
        pl.when(unknown).then(pl.lit(rule[name], dtype=matched.schema[name]))
        .otherwise(pl.col(name)).alias(name)
        for name in columns
    ], pl.when(unknown).then(pl.lit(True)).otherwise(pl.col("fallback")).alias("fallback"),
        pl.when(unknown).then(pl.lit("store_uniform_distribution"))
        .otherwise(pl.col("fallback_reason")).alias("fallback_reason"),
        pl.when(unknown).then(pl.lit("distribute")).otherwise(pl.col("status")).alias("status"))
    return matched, count


def pending_pricing(count: int, coverage: dict | None = None) -> dict:
    coverage = coverage or {}
    if coverage.get("expected"):
        note = (f"商品成本覆盖率{coverage['coverage']:.1%}；还有"
                f"{coverage['uncovered']}笔订单未覆盖。已算现有资料，提成金额交人工逐人确认。")
    else:
        note = f"还有 {count} 条商品成本未覆盖；提成金额交人工逐人确认。"
    return {"configured": False, "amount_complete": False, "total": None,
            "base_total": None, "people": [], "products": [],
            "pricing_pending_count": count, "pricing_threshold_met": False,
            "cost_coverage": coverage,
            "notes": [note]}


def _participation_facts(result, model, store_id: str, period: str) -> tuple[pl.DataFrame, bool, bool, bool]:
    """Per-order posted sales, gross and operating profit for assigned links.

    The relationship only establishes participation. A shared link gives each
    assigned person its complete posted output; these figures are not additive
    across people. The store statement remains the single financial total.
    """
    sales_node = next((n for n in model.statement if n.name == "销售收入" and n.level == 2), None)
    gross_node = next((n for n in model.statement if n.name == "毛利" and n.is_total), None)
    profit_node = next((n for n in model.statement if n.headline == "profit" and n.is_total), None)
    sales_metrics = model.commission_base_metrics(sales_node.id) if sales_node else ()
    gross_metrics = model.commission_base_metrics(gross_node.id) if gross_node else ()
    profit_metrics = model.commission_base_metrics(profit_node.id) if profit_node else ()
    schema = {"spine_row": pl.UInt32, "participation_sales": pl.Float64,
              "participation_gross": pl.Float64, "participation_profit": pl.Float64}
    facts = result.spine_facts
    if facts.is_empty():
        return pl.DataFrame(schema=schema), bool(sales_metrics), bool(gross_metrics), bool(profit_metrics)
    facts = facts.filter(
        pl.col("store").is_in(_store_labels(model, store_id))
        & (pl.col("period") == period)
    )
    if facts.is_empty():
        return pl.DataFrame(schema=schema), bool(sales_metrics), bool(gross_metrics), bool(profit_metrics)

    def metric_rows(metrics, name):
        if not metrics:
            return pl.DataFrame(schema={"spine_row": pl.UInt32, name: pl.Float64})
        return (facts.filter(pl.col("metric_id").is_in(list(metrics)))
                .group_by("spine_row")
                .agg(pl.col("amount").sum().alias(name)))

    sales = metric_rows(sales_metrics, "participation_sales")
    gross = metric_rows(gross_metrics, "participation_gross")
    profit = metric_rows(profit_metrics, "participation_profit")
    combined = sales.join(gross, on="spine_row", how="full", coalesce=True, nulls_equal=True)
    combined = combined.join(profit, on="spine_row", how="full", coalesce=True, nulls_equal=True)
    return combined, bool(sales_metrics), bool(gross_metrics), bool(profit_metrics)


def production_weights(details: pl.DataFrame) -> pl.DataFrame:
    """Output ownership per order/product, independent of commission entitlement.

    Legacy evidence without duties retains its original share basis. Explicit
    cut rows receive zero output, even if nobody produced that order.
    """
    if '__output_share' in details.columns:
        return details
    if 'duty' not in details.columns or 'spine_row' not in details.columns:
        return details.with_columns(pl.col('share').alias('__output_share'), pl.col('total_rate').alias('__output_rate'))
    keys = ['spine_row'] + (['product_id'] if 'product_id' in details.columns else [])
    weighted = details.with_columns(
        pl.when((pl.col('duty') != 'cut').fill_null(True))
        .then(pl.col('share').cast(pl.Decimal(16, 8))).otherwise(pl.lit(0).cast(pl.Decimal(16, 8)))
        .alias('__output_share'))
    return weighted.with_columns(pl.col('__output_share').sum().over(keys).alias('__output_rate')).with_columns(
        pl.when(pl.col('__output_rate') > 0).then(pl.col('__output_rate')).otherwise(1).alias('__output_rate'))


def allocated_outputs(details: pl.DataFrame, *, net_profit_basis: bool = False, production: bool = False) -> dict[str, dict[str, float]] | None:
    """Split order output by each participant's share of the link's total rate."""
    required = {'status', 'person_id', 'share', 'total_rate'}
    if not required <= set(details.columns):
        return None
    assigned = details.filter(pl.col('status') == 'distribute')
    if production:
        assigned = production_weights(assigned)
    if assigned.is_empty():
        return {}
    fields = {name: name for name in ('participation_sales','participation_gross',
                                      'participation_profit') if name in assigned.columns}
    if 'participation_profit' not in fields and net_profit_basis and 'original_base' in assigned.columns:
        fields['participation_profit'] = 'original_base'
    if not fields:
        return None
    assigned = assigned.with_columns(
        pl.col('__output_share' if production else 'share').cast(pl.Decimal(16, 8)).alias('__share'),
        pl.col('__output_rate' if production else 'total_rate').cast(pl.Decimal(16, 8), strict=False).alias('__rate'),
        *[pl.col(source).cast(pl.Decimal(28, 10), strict=False).alias('__'+name)
          for name, source in fields.items()],
    )
    if assigned.filter(pl.col('__rate').is_null() | (pl.col('__rate') <= 0)
                       | pl.any_horizontal(*[pl.col('__'+name).is_null() for name in fields])).height:
        return None
    totals = (assigned.with_columns(*[
        (pl.col('__'+name) * pl.col('__share') / pl.col('__rate')
         * (pl.when(pl.col('managed').fill_null(False) | (pl.col('sales_unassigned').fill_null(False) if 'sales_unassigned' in assigned.columns else pl.lit(False))).then(0).otherwise(1)
            if production and name == 'participation_sales' and 'managed' in assigned.columns else 1)).alias('__part_'+name)
        for name in fields
    ], (pl.col('__participation_sales') * pl.col('__share') / pl.col('__rate')).alias('__sales_basis')
        if 'participation_sales' in fields else pl.lit(0).alias('__sales_basis')).group_by('person_id').agg(*[
        pl.col('__part_'+name).sum() for name in fields
    ], pl.col('__sales_basis').sum()).iter_rows(named=True))
    return {row['person_id']:{**{name.replace('participation_',''):money_float(row['__part_'+name])
                              for name in fields},
                              **({'sales_basis':money_float(row['__sales_basis'])} if production and 'managed' in assigned.columns else {})} for row in totals}


def managed_sales(details: pl.DataFrame) -> dict[str, float]:
    """One source sales fact per order, never per commission recipient.

    Team IDs come from the effective immutable rule, not today's organization.
    Expired/unmatched rules must never divert sales into a former team.
    """
    required = {'managed', 'managed_team_id', 'spine_row', 'product_id', 'participation_sales', 'status'}
    if not required <= set(details.columns):
        return {}
    rows = details.filter(pl.col('managed').fill_null(False)
                          & pl.col('managed_team_id').is_not_null()
                          & (pl.col('managed_team_id') != '')
                          & pl.col('status').is_in(['distribute', 'exclude', 'hold', 'wage_pending']))
    rows = rows.unique(subset=['spine_row', 'product_id', 'managed_team_id'])
    return {r['managed_team_id']: money_float(r['sales']) for r in rows.group_by('managed_team_id').agg(
        pl.col('participation_sales').cast(pl.Decimal(28, 10)).sum().alias('sales')).iter_rows(named=True)}


def allocated_profit(details: pl.DataFrame, *, net_profit_basis: bool = False) -> dict[str, float] | None:
    outputs = allocated_outputs(details, net_profit_basis=net_profit_basis)
    if outputs is None or any('profit' not in value for value in outputs.values()):
        return None
    return {pid:value['profit'] for pid,value in outputs.items()}


def participation_only(result, model, store_id: str, period: str, registry: Registry) -> list[dict]:
    """Show linked sales output even when goods cost prevents a payout."""
    _, versions, people, policy = registry.active(store_id, period)
    versions = _legacy(model, store_id) + versions
    if not versions:
        return []
    sales_node = next((n for n in model.statement if n.name == "销售收入" and n.level == 2), None)
    if not sales_node:
        return []
    sales_metrics = model.commission_base_metrics(sales_node.id)
    if not sales_metrics:
        return []
    orders = _order_base(result, sales_metrics, _store_labels(model, store_id), period)
    if orders.is_empty():
        return []
    rf, af = _frames(versions, people, (policy or {}).get("body", {}).get("wages", "pending"))
    matched = _match(orders, rf)
    assigned = matched.filter(pl.col("status") == "distribute").join(af, on="rule_key", how="inner")
    if assigned.is_empty():
        return []
    facts, has_sales, has_gross, has_profit = _participation_facts(result, model, store_id, period)
    assigned = assigned.join(facts, on="spine_row", how="left", nulls_equal=True).with_columns(
        pl.col("participation_sales").fill_null(0.0),
        pl.col("participation_gross").fill_null(0.0),
        pl.col("participation_profit").fill_null(0.0),
    )
    return [
        {"person_id": row["person_id"], "person": row["person"], "amount": None,
         "base": None, "sales": money_float(row["participation_sales"]) if has_sales else None,
         "gross": money_float(row["participation_gross"]) if has_gross else None,
         "profit": money_float(row["participation_profit"]) if has_profit else None}
        for row in assigned.group_by("person_id", "person").agg(
            pl.col("participation_sales").sum(), pl.col("participation_gross").sum(),
            pl.col("participation_profit").sum()
        ).iter_rows(named=True)
    ]


def calculate(result, model, store_id: str, period: str, registry: Registry,
              *, allow_partial_pricing: bool = False):
    gaps = getattr(result, "pricing_gaps", pl.DataFrame())
    if not allow_partial_pricing and not gaps.is_empty() and not gaps.filter(
        pl.col("store").is_in(_store_labels(model, store_id))
        & pl.col("period").is_in([period, "(未知账期)"])
    ).is_empty():
        raise ValueError("未覆盖商品成本需要人工确认，提成金额请逐人确认")
    revision, versions, people, policy = registry.active(store_id, period)
    if not versions and not policy:
        return None
    policy_body = policy["body"] if policy else {}
    node = model.node(policy_body["base_node"]) if policy_body.get("base_node") else model.commission_base_node(store_id)
    if node is None:
        raise ValueError("没有可用于提成的基数")
    store = model.store(store_id)
    on_loss = policy_body.get("on_loss", "inherit")
    on_loss = policy_body.get("loss_by_store", {}).get(store_id, store.commission_on_loss) if on_loss == "inherit" else on_loss
    versions = _legacy(model, store_id) + versions
    orders = _order_base(result, model.commission_base_metrics(node.id), _store_labels(model, store_id), period)
    if orders.is_empty():
        return None
    for col in ("order_id", "sub_order_id", "product_name"):
        if col not in orders.columns:
            orders = orders.with_columns(pl.lit("", dtype=pl.Utf8).alias(col))
    with registry.connect() as conn:
        names = {r[0]: r[1] for r in conn.execute("SELECT product_id,product_name FROM catalog WHERE store_id=?", (store_id,))}
    if names:
        name_frame = pl.DataFrame({"product_id": list(names), "catalog_name": list(names.values())})
        orders = orders.join(name_frame, on="product_id", how="left").with_columns(
            pl.coalesce(pl.col("catalog_name"), pl.col("product_name")).alias("product_name")
        ).drop("catalog_name")
    # Allocated source facts can carry sub-cent fractions. Keep them until the
    # per-person cent rounding rather than truncating every order to 4 places.
    orders = orders.with_columns(pl.col("base").cast(pl.Decimal(28, 10)).alias("original_base"))
    if on_loss == "skip":
        orders = orders.with_columns(pl.max_horizontal(pl.col("base"), pl.lit(0)).alias("base"))
    orders = orders.with_columns(pl.col("base").cast(pl.Decimal(28, 10)))
    rf, af = _frames(versions, people, policy_body.get("wages", "pending"))
    matched = _match(orders, rf)
    matched, uniform_fallback_orders = _attribute_uniform_unknowns(
        matched, rf, af, period,
    )
    paid = matched.filter(pl.col("status") == "distribute").join(af, on="rule_key", how="inner")
    paid = paid.with_columns(pl.col("share").cast(pl.Decimal(16, 8)))
    paid = paid.with_columns((pl.col("base") * pl.col("share")).round(2, mode="half_away_from_zero").alias("amount"))
    output_facts, has_sales, has_gross, has_profit = _participation_facts(result, model, store_id, period)
    paid = paid.join(output_facts, on="spine_row", how="left", nulls_equal=True).with_columns(
        pl.col("participation_sales").fill_null(0.0),
        pl.col("participation_gross").fill_null(0.0),
        pl.col("participation_profit").fill_null(0.0),
    )
    unpaid = matched.filter(pl.col("status") != "distribute").join(output_facts, on="spine_row", how="left", nulls_equal=True).with_columns(
        pl.lit("", dtype=pl.Utf8).alias("person_id"), pl.lit("", dtype=pl.Utf8).alias("person"),
        pl.lit("", dtype=pl.Utf8).alias("role"), pl.lit(Decimal(0), dtype=pl.Decimal(16, 8)).alias("share"),
        pl.when(pl.col("status") == "exclude").then(pl.lit(Decimal(0), dtype=paid.schema["amount"]))
        .otherwise(pl.lit(None, dtype=paid.schema["amount"])).alias("amount"),
    )
    details = pl.concat([paid, unpaid], how="diagonal_relaxed").with_columns(
        pl.lit(store_id).alias("store_id"), pl.lit(period).alias("period"),
        pl.lit(node.id).alias("base_node"), pl.lit(revision).alias("registry_revision"),
        pl.when(pl.col("original_base") < 0).then(pl.lit(on_loss)).otherwise(pl.lit("")).alias("loss_treatment"),
    )
    people_lines = paid.group_by("person_id", "person").agg(
        pl.col("amount").sum(), pl.col("base").sum(),
        pl.col("participation_sales").sum(), pl.col("participation_gross").sum(),
        pl.col("participation_profit").sum(),
        pl.col("product_id").n_unique().alias("products"),
        pl.col("duty").drop_nulls().unique().alias("_duties"),
    ).sort("amount", descending=True)
    split_output = allocated_outputs(paid) if (has_sales or has_gross or has_profit) else None
    person_rows = []
    for r in people_lines.iter_rows(named=True):
        duties_set = [d for d in (r.pop("_duties") or []) if d]
        entry = {**r, "amount": money_float(r["amount"]), "base": money_float(r["base"]),
         "sales": money_float(r["participation_sales"]) if has_sales else None,
         "gross": money_float(r["participation_gross"]) if has_gross else None,
         "profit": money_float(r["participation_profit"]) if has_profit else None,
         "allocated_sales": split_output.get(r['person_id'],{}).get('sales') if split_output is not None else None,
         "allocated_gross": split_output.get(r['person_id'],{}).get('gross') if split_output is not None else None,
         "allocated_profit": split_output.get(r['person_id'],{}).get('profit') if split_output is not None else None}
        if len(duties_set) == 1:
            entry["duty"] = duties_set[0]
        entry.pop("participation_sales")
        entry.pop("participation_gross")
        entry.pop("participation_profit")
        person_rows.append(entry)
    by_product = defaultdict(list)
    for r in paid.group_by("product_id", "person_id", "person").agg(pl.col("amount").sum(), pl.col("duty").first()).iter_rows(named=True):
        entry = {"person_id": r["person_id"], "person": r["person"], "amount": money_float(r["amount"])}
        if r.get("duty"):
            entry["duty"] = r["duty"]
        by_product[r["product_id"]].append(entry)
    products = []
    for r in matched.group_by("product_id").agg(
        pl.col("product_name").drop_nulls().first(), pl.col("base").sum(), pl.len().alias("sub_orders"),
        pl.col("fallback").any(), (pl.col("status").is_in(["distribute", "exclude"])).all().alias("covered"),
        pl.col("valid_from").drop_nulls().max().alias("effective_from"),
        pl.col("total_rate").cast(pl.Float64).max(), pl.col("status").unique().alias("statuses"),
    ).iter_rows(named=True):
        crew = by_product[r["product_id"]]
        products.append({**r, "base": money_float(r["base"]), "unassigned": not r.pop("covered"),
                         "amount": money_float(sum(Decimal(str(x["amount"])) for x in crew)),
                         "people": crew, "total_rate": r["total_rate"] or 0.0})
    missing = matched.filter(~pl.col("status").is_in(["distribute", "exclude"]))
    negative = matched.filter(pl.col("original_base") < 0)
    wage_pending = matched.filter(pl.col("status") == "wage_pending").height
    wage_preview = matched.filter(pl.col("wage_preview").fill_null(False)).height
    notes = []
    if wage_pending:
        notes.append(f"{wage_pending}笔订单的工资扣减口径待确认，已保留关系，金额未计算")
    other_missing = missing.height - wage_pending
    if other_missing:
        notes.append(f"{other_missing}笔订单的提成关系待确认或缺少有效身份/时间")
    if wage_preview:
        notes.append(f"按指定口径先不扣工资，{wage_preview}笔相关订单仅作试算")
    if uniform_fallback_orders:
        notes.append(
            f"{uniform_fallback_orders}笔订单缺少商品链接或原始下单时间；"
            "本店本月所有有效配置的人员和点数完全一致，已按该唯一配置分配"
        )
    calc_id = str(uuid4())
    payload = {
        "store": store_id, "period": period, "base_node": node.id, "base_name": node.name,
        "base_total": money_float(orders["original_base"].sum()),
        "total": money_float(paid["amount"].sum()), "people": person_rows,
        "managed_sales": managed_sales(details),
        "production_outputs": allocated_outputs(paid, production=True),
        "products": sorted(products, key=lambda r: -r["base"]), "configured": bool(person_rows),
        "unassigned_base": money_float(missing["base"].sum()),
        "fallback_base": money_float(matched.filter(pl.col("fallback"))["base"].sum()),
        "uniform_fallback_orders": uniform_fallback_orders,
        "negative_orders": negative.height, "negative_base": money_float(negative["original_base"].sum()),
        "on_loss": on_loss,
        "skipped_loss_base": money_float(negative["original_base"].sum()) if on_loss == "skip" else 0.0,
        "notes": notes,
        "engine": "commission-v2", "calculation_id": calc_id, "registry_revision": revision,
        "unassigned_orders": missing.height,
        "amount_complete": missing.height == 0 and wage_preview == 0,
        "wage_pending_orders": wage_pending,
        "wage_preview_orders": wage_preview,
        "policy_version": policy["id"] if policy else None,
        "rounding": "per_order_person_cent_half_up",
        "participation_basis": "complete_link_output_with_rate_allocated_report",
    }
    metadata = {"id": calc_id, "store_id": store_id, "period": period, "registry_revision": revision,
                "model_json": model.model_dump_json(), "rules_json": json_text({"relationships": versions, "policy": policy}),
                "summary_json": json_text(payload)}
    return payload, details, metadata


def persist(registry: Registry, finance_run: int, details: pl.DataFrame, metadata: dict):
    folder = registry.root / "calculations"
    folder.mkdir(exist_ok=True)
    path = folder / (metadata["id"] + ".parquet")
    temporary = path.with_suffix(".tmp")
    details.with_columns(pl.lit(finance_run).alias("finance_run")).write_parquet(temporary)
    temporary.replace(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    from .snapshot_store import put
    with registry.transaction() as conn:
        model_ref=put(conn,metadata["model_json"])
        rules_ref=put(conn,metadata["rules_json"])
        conn.execute("INSERT INTO calculation VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (metadata["id"], finance_run, metadata["store_id"], metadata["period"], now(),
                      metadata["registry_revision"], path.name, digest, model_ref,
                      rules_ref, metadata["summary_json"]))
