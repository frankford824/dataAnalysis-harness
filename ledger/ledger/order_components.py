"""Verified original-order expansion, independent of today's mutable SKU catalogue."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from datetime import datetime
import hashlib
import json
from pathlib import Path

import polars as pl


def quantity(value):
    try:
        result=Decimal(str(value))
        if not result.is_finite():raise ValueError('原订单组成明细数量无效')
        return result
    except (InvalidOperation,TypeError) as exc:
        raise ValueError('原订单组成明细数量无效') from exc


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
    resolved = {}
    for (internal,item_id),candidates in live.items():
        if len(candidates)!=1:
            continue
        current=candidates[0]
        if current.get('pricing_suspect') or current.get('failure_reason') in {'invalid_original_quantity','invalid_component_quantity'}:
            continue
        raw=current.get('pricing_evidence')
        if not raw:
            continue
        evidence=json.loads(raw) if isinstance(raw,str) else raw
        components=evidence.get('original_components') or []
        captured=evidence.get('original_captured_at')
        if not components or not captured or not evidence.get('source_order_hash'):
            continue
        try:datetime.fromisoformat(str(captured).replace('Z','+00:00'))
        except ValueError:continue
        if all(p.get('sku_id')==current['sku'] for p in components):
            continue  # Scalar rows do not need a bundle crosswalk.
        q=quantity(current['quantity']);parts=defaultdict(Decimal)
        if not q.is_finite() or q<=0:
            continue
        for part in components:
            amount=quantity(part.get('qty'))
            if (str(part.get('oi_id'))!=item_id or part.get('src_combine_sku_id')!=current['sku']
                or quantity(part.get('src_combine_sku_qty'))!=q or not amount.is_finite() or amount<0
                or not part.get('sku_id')):
                raise ValueError(f'订单台原组件与商品行不匹配：{internal}/{item_id}')
            if amount:parts[part['sku_id']]+=amount
        if parts:
            digest=hashlib.sha256(json.dumps(evidence,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            resolved[internal,item_id]=(current,dict(parts),f'订单台原订单展开明细：{internal}/{item_id}；查询时间：{captured}；凭据：{digest}')
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
            q = quantity(current["quantity"])
            if (parent["sku_id"] != current["sku"] or str(parent["outer_oi_id"]) != current["sub_order_id"]
                    or quantity(parent["qty"]) != q or q <= 0):
                continue
            parts = defaultdict(Decimal)
            for row in rows:
                amount = quantity(row["qty"])
                if (str(row["o_id"]) != internal or row["src_combine_sku_id"] != current["sku"]
                        or quantity(row["src_combine_sku_qty"]) != q or not amount.is_finite() or amount < 0):
                    raise ValueError(f"原订单组成明细的商品或数量不匹配：{path.name}")
                if amount:parts[row["sku_id"]] += amount
            existing=resolved.get((internal,item_id))
            if existing and existing[1]!=dict(parts):
                raise ValueError(f'原订单组成证据不一致：{internal}/{item_id}')
            if not existing:
                resolved[internal,item_id]=(current,dict(parts),f"原订单展开明细：{internal}/{item_id}；查询时间：{payload['captured_at']}；凭据：{sha}")
    output=[]
    for (internal,item_id),(current,parts,evidence) in resolved.items():
        for sku,amount in parts.items():
            output.append({'internal_order_id':internal,'internal_sub_order_id':item_id,'sub_order_id':current['sub_order_id'],
                           'sku':sku,'quantity':float(amount),'component_evidence':evidence})
    return pl.DataFrame(output) if output else None
