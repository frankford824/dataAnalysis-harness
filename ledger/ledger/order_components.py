"""Verified original-order expansion, independent of today's mutable SKU catalogue."""
from collections import defaultdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import polars as pl


def fingerprint(home: Path) -> str:
    digest = hashlib.sha256()
    files = sorted((home / "order-components").glob("*/*.json"))
    for path in files:
        digest.update(path.relative_to(home).as_posix().encode())
        digest.update(path.read_bytes())
    return ":components:" + digest.hexdigest() if files else ""


def load(home: Path, store_id: str, order_store_ids: list[str], costs: pl.DataFrame) -> pl.DataFrame | None:
    required = {"internal_order_id", "internal_sub_order_id", "sub_order_id", "sku", "quantity"}
    if not required <= set(costs.columns):
        return None
    live = defaultdict(list)
    for row in costs.iter_rows(named=True):
        live[(str(row["internal_order_id"]), str(row["internal_sub_order_id"]))].append(row)
    output = []
    for path in sorted((home / "order-components" / store_id).glob("*.json")):
        envelope = json.loads(path.read_text(encoding="utf-8-sig"))
        raw = envelope["payload_json"]
        sha = hashlib.sha256(raw.encode()).hexdigest()
        if sha != envelope["sha256"]:
            raise ValueError(f"原订单组成明细校验失败：{path.name}")
        payload = json.loads(raw)
        if payload["source"] != "jst_order_expanded_v1" or str(payload["order_store_id"]) not in order_store_ids:
            raise ValueError(f"原订单组成明细的来源或店铺不匹配：{path.name}")
        internal = str(payload["order_id"])
        parents = {str(row["oi_id"]): row for row in payload["bundles"]}
        grouped = defaultdict(list)
        for row in payload["components"]:
            grouped[str(row["oi_id"])].append(row)
        for item_id, rows in grouped.items():
            candidates = live.get((internal, item_id), [])
            parent = parents.get(item_id)
            if len(candidates) != 1 or parent is None:
                continue
            current = candidates[0]
            q = Decimal(str(current["quantity"]))
            if (parent["sku_id"] != current["sku"] or str(parent["outer_oi_id"]) != current["sub_order_id"]
                    or Decimal(str(parent["qty"])) != q or q <= 0):
                continue
            parts = defaultdict(Decimal)
            for row in rows:
                amount = Decimal(str(row["qty"]))
                if (str(row["o_id"]) != internal or row["src_combine_sku_id"] != current["sku"]
                        or Decimal(str(row["src_combine_sku_qty"])) != q or not amount.is_finite() or amount <= 0):
                    raise ValueError(f"原订单组成明细的商品或数量不匹配：{path.name}")
                parts[row["sku_id"]] += amount
            for sku, amount in parts.items():
                output.append({"internal_order_id": internal, "internal_sub_order_id": item_id,
                               "sub_order_id": current["sub_order_id"], "sku": sku, "quantity": float(amount),
                               "component_evidence": f"原订单展开明细：{internal}/{item_id}；查询时间：{payload['captured_at']}；凭据：{sha}"})
    return pl.DataFrame(output) if output else None
