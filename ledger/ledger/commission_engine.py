"""Deterministic per-order allocation against an immutable relationship snapshot."""
from __future__ import annotations

import hashlib
import json
import warnings
from collections import defaultdict
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
                          "amount_hold": "" if wages == "skip_preview" else segment.get("amount_hold", ""),
                          "wage_preview": wages == "skip_preview" and segment.get("amount_hold") == "wage_pending",
                          "total_rate": segment["total_rate"], "rule_version": version["id"],
                          "rule_name": version["body"].get("product_name", ""),
                          "priority": 0 if version["id"].startswith("legacy:") else 1})
            for line in segment.get("allocations", []):
                pid = line["person_id"]
                allocations.append({"rule_key": key, "person_id": pid,
                                    "person": people.get(pid, {}).get("name") or line.get("person", pid),
                                    "role": line["role"], "share": line["rate"]})
    schema = {k: pl.Utf8 for k in ["rule_key", "product_id", "valid_from", "valid_to", "mode",
                                  "total_rate", "rule_version", "rule_name", "amount_hold"]} | {"priority": pl.Int64, "wage_preview": pl.Boolean}
    rf = pl.DataFrame(rules, schema=schema).with_columns(
        pl.col("valid_from").str.to_datetime("%Y-%m-%dT%H:%M:%S").alias("from_at"),
        pl.col("valid_to").str.to_datetime("%Y-%m-%dT%H:%M:%S", strict=False).alias("to_at"),
    ).sort("priority").unique(subset=["product_id", "valid_from"], keep="last")
    af = pl.DataFrame(allocations, schema={k: pl.Utf8 for k in ["rule_key", "person_id", "person", "role", "share"]})
    af = af.group_by("rule_key", "person_id", "person", maintain_order=True).agg(
        pl.col("role").str.join(" / "),
        pl.col("share").cast(pl.Decimal(16, 8)).sum(),
        pl.struct("role", "share").alias("allocation_parts"),
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
        pl.when(pl.col("order_at").is_null()).then(pl.lit("missing_order_time"))
        .when(pl.col("product_id") == "").then(pl.lit("missing_product_id"))
        .when(pl.col("rule_key").is_null()).then(pl.lit("unassigned"))
        .when(pl.col("to_at").is_not_null() & (pl.col("order_at") >= pl.col("to_at"))).then(pl.lit("expired"))
        .when((pl.col("mode") == "distribute") & (pl.col("amount_hold") != "")).then(pl.col("amount_hold"))
        .otherwise(pl.col("mode")).alias("status")
    )


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
        & pl.col("spine_row").is_not_null()
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
    combined = sales.join(gross, on="spine_row", how="full", coalesce=True)
    combined = combined.join(profit, on="spine_row", how="full", coalesce=True)
    return combined, bool(sales_metrics), bool(gross_metrics), bool(profit_metrics)


def allocated_profit(details: pl.DataFrame, *, net_profit_basis: bool = False) -> dict[str, float] | None:
    """Split assigned order profit by each participant's share of that link's rate."""
    if 'participation_profit' in details.columns:
        profit = pl.col('participation_profit')
    elif net_profit_basis and 'original_base' in details.columns:
        profit = pl.col('original_base')
    else:
        return None
    required = {'status', 'person_id', 'share', 'total_rate'}
    if not required <= set(details.columns):
        return None
    assigned = details.filter(pl.col('status') == 'distribute')
    if assigned.is_empty():
        return {}
    assigned = assigned.with_columns(
        pl.col('share').cast(pl.Decimal(16, 8)).alias('__share'),
        pl.col('total_rate').cast(pl.Decimal(16, 8), strict=False).alias('__rate'),
        profit.cast(pl.Decimal(28, 10), strict=False).alias('__profit'),
    )
    if assigned.filter(pl.col('__rate').is_null() | (pl.col('__rate') <= 0)
                       | pl.col('__profit').is_null()).height:
        return None
    totals = (assigned.with_columns(
        (pl.col('__profit') * pl.col('__share') / pl.col('__rate')).alias('__part')
    ).group_by('person_id').agg(pl.col('__part').sum()).iter_rows(named=True))
    return {row['person_id']: money_float(row['__part']) for row in totals}


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
    assigned = assigned.join(facts, on="spine_row", how="left").with_columns(
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
    paid = matched.filter(pl.col("status") == "distribute").join(af, on="rule_key", how="inner")
    paid = paid.with_columns(pl.col("share").cast(pl.Decimal(16, 8)))
    paid = paid.with_columns((pl.col("base") * pl.col("share")).round(2, mode="half_away_from_zero").alias("amount"))
    output_facts, has_sales, has_gross, has_profit = _participation_facts(result, model, store_id, period)
    paid = paid.join(output_facts, on="spine_row", how="left").with_columns(
        pl.col("participation_sales").fill_null(0.0),
        pl.col("participation_gross").fill_null(0.0),
        pl.col("participation_profit").fill_null(0.0),
    )
    unpaid = matched.filter(pl.col("status") != "distribute").with_columns(
        pl.lit("", dtype=pl.Utf8).alias("person_id"), pl.lit("", dtype=pl.Utf8).alias("person"),
        pl.lit("", dtype=pl.Utf8).alias("role"), pl.lit(Decimal(0), dtype=pl.Decimal(16, 8)).alias("share"),
        pl.when(pl.col("status") == "exclude").then(pl.lit(Decimal(0), dtype=paid.schema["amount"]))
        .otherwise(pl.lit(None, dtype=paid.schema["amount"])).alias("amount"),
        pl.lit(None, dtype=pl.Float64).alias("participation_sales"),
        pl.lit(None, dtype=pl.Float64).alias("participation_gross"),
        pl.lit(None, dtype=pl.Float64).alias("participation_profit"),
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
    ).sort("amount", descending=True)
    split_profit = allocated_profit(paid) if has_profit else None
    person_rows = [
        {**r, "amount": money_float(r["amount"]), "base": money_float(r["base"]),
         "sales": money_float(r["participation_sales"]) if has_sales else None,
         "gross": money_float(r["participation_gross"]) if has_gross else None,
         "profit": money_float(r["participation_profit"]) if has_profit else None,
         "allocated_profit": split_profit.get(r['person_id']) if split_profit is not None else None}
        for r in people_lines.iter_rows(named=True)
    ]
    for row in person_rows:
        row.pop("participation_sales")
        row.pop("participation_gross")
        row.pop("participation_profit")
    by_product = defaultdict(list)
    for r in paid.group_by("product_id", "person_id", "person").agg(pl.col("amount").sum()).iter_rows(named=True):
        by_product[r["product_id"]].append({"person_id": r["person_id"], "person": r["person"], "amount": money_float(r["amount"])})
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
    calc_id = str(uuid4())
    payload = {
        "store": store_id, "period": period, "base_node": node.id, "base_name": node.name,
        "base_total": money_float(orders["original_base"].sum()),
        "total": money_float(paid["amount"].sum()), "people": person_rows,
        "products": sorted(products, key=lambda r: -r["base"]), "configured": bool(person_rows),
        "unassigned_base": money_float(missing["base"].sum()),
        "fallback_base": money_float(matched.filter(pl.col("fallback"))["base"].sum()),
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
        "participation_basis": "complete_link_output_per_assigned_person",
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
