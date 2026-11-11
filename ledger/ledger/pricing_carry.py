"""「下单日成本依据不足」的沿用价补录。

规则（2026-11-10 业务确认）：
- 默认给出下单日前后各 ``SPAN`` 个沿用价，交人工确认，不自动采用；
- 确认时两个范围二选一：``store`` 只补本店这一类缺价行，``all`` 补全系统所有店铺
  里同一个商品的缺价行（都只动「下单日成本依据不足」这一类，不动已定价的行）；
- 不论哪种范围，先判这一行牵不牵涉售后清成本：该商品有会清成本的售后
  （货物状态是买家未收到货 / 买家已退货 / 卖家已收到退货）时不补录，明确提示无法补录。
  「仅退款 + 货物状态空」按既有口径不清成本，照常补录。
- 只扫未结账月份；已结账的账期是冻结的，不能改。
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any

CATEGORY = "historical_price"
CATEGORY_LABEL = "下单日成本依据不足"
RULE_NAME = "W20261110"  # 这条补录规则的业务编号，写进每笔成本调整备注，方便回查和撤销
SPAN = 2
#: 这三种货物状态按既有口径清成本，补录会把清掉的行又变成有成本，一律拦下。
CLEAR_STATES = ("买家未收到货", "买家已退货", "卖家已收到退货")
SCOPE_STORE = "store"
SCOPE_ALL = "all"
MANUAL_BASIS_MIN = 4


class CarryError(Exception):
    """补录前置条件不满足，消息可以直接给人看。"""


def _feed_origin() -> str:
    base = os.environ.get(
        "LEDGER_ORDER_FEED_URL", "http://127.0.0.1:8001/api/integration/ledger/v1"
    )
    return base.split("/api/integration/", 1)[0].rstrip("/")


def _get(path: str, params: dict[str, Any] | None = None, timeout: float = 25.0) -> Any:
    url = _feed_origin() + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _day(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _price(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


# --------------------------------------------------------------------------- #
# 沿用价候选
# --------------------------------------------------------------------------- #


def carry_candidates(sku: str, order_day: date | None) -> list[dict[str, Any]]:
    """下单日前后各 SPAN 个沿用价，近的排前面。

    候选只是给人挑的参考，挑中的价格照样要过售后拦截，写进去的仍是人工调整。
    """
    sku = (sku or "").strip()
    if not sku:
        return []
    try:
        payload = _get(f"/api/sku-cost/{urllib.parse.quote(sku)}/history")
    except Exception:
        return []
    events: dict[date, float] = {}
    for entry in payload.get("history") or []:
        day = _day(entry.get("effectiveOn"))
        price = _price(entry.get("newCost"))
        if price is None:
            price = _price(entry.get("oldCost"))
        if day and price is not None:
            events[day] = price
    current = payload.get("current") or {}
    day = _day(current.get("effectiveOn"))
    price = _price(current.get("cost"))
    if day and price is not None:
        events.setdefault(day, price)
    if not events or order_day is None:
        return []
    timeline = sorted(events.items())
    before = [(d, p) for d, p in timeline if d <= order_day][-SPAN:]
    after = [(d, p) for d, p in timeline if d > order_day][:SPAN]
    options = [
        {
            "side": "before",
            "effective_on": d.isoformat(),
            "unit_cost": round(p, 4),
            "label": f"下单日前最近沿用价 {d.isoformat()}：{p:.2f} 元",
            "on_order_day": d == order_day,
        }
        for d, p in reversed(before)
    ]
    options += [
        {
            "side": "after",
            "effective_on": d.isoformat(),
            "unit_cost": round(p, 4),
            "label": f"下单日后首个沿用价 {d.isoformat()}：{p:.2f} 元",
            "on_order_day": False,
        }
        for d, p in after
    ]
    return options


# --------------------------------------------------------------------------- #
# 售后清成本拦截
# --------------------------------------------------------------------------- #


def _states(record: dict[str, Any]) -> str:
    for key, value in record.items():
        if "goods_status" in key or "货物状态" in key:
            return str(value or "").strip()
    return ""


def _skus(record: dict[str, Any]) -> set[str]:
    returned = record.get("returned_items")
    if isinstance(returned, list) and returned:
        # 订单台读过这张售后的商品明细页，退了哪些货以它为准。
        return {str(x.get("sku_id") or "").strip().upper()
                for x in returned if isinstance(x, dict) and str(x.get("sku_id") or "").strip()}
    found = set()
    for key, value in record.items():
        if "sku" in key.lower() or "商品编码" in key:
            text = str(value or "").strip()
            if text:
                found.add(text.upper())
    return found


def after_sale_block(order_id: str, sku: str = "") -> tuple[bool, str, str]:
    """这行的商品有没有会清成本的售后。返回 (是否命中, 说明, 类型)。

    类型 ``clear`` = 命中清成本货物状态，这行成本按 0 计；
    类型 ``unknown`` = 售后情况查不到，宁可不写。
    """
    order_id = (order_id or "").strip()
    if not order_id:
        return False, "", ""
    try:
        payload = _get(f"/api/orders/{urllib.parse.quote(order_id)}/after-sales")
    except Exception as exc:  # 查不到不等于没有，宁可拦下
        return True, f"售后情况查不到（{exc}），先不补录", "unknown"
    records = payload.get("after_sales") or []
    if not records:
        return False, "", ""
    wanted = (sku or "").strip().upper()
    for record in records:
        if not isinstance(record, dict):
            continue
        state = _states(record)
        if not any(word in state for word in CLEAR_STATES):
            continue
        listed = _skus(record)
        if wanted and listed and wanted not in listed:
            continue
        number = record.get("after_sale_id") or record.get("平台售后单号") or record.get("platform_after_sale_id") or "未填单号"
        return True, f"售后单 {number} 货物状态「{state}」按规则清成本", "clear"
    return False, "", ""


# --------------------------------------------------------------------------- #
# 缺价行
# --------------------------------------------------------------------------- #


def _rows_of(ws, run_id: int, sku: str) -> list[dict[str, Any]]:
    path: Path = ws.pricing_gaps_path(run_id)
    if not path.exists():
        return []
    import polars as pl

    lazy = pl.scan_parquet(path)
    names = set(lazy.collect_schema().names())
    if not {"category", "sku"} <= names:
        return []
    keep = [c for c in ("store", "period", "order_id", "sku", "order_date", "quantity") if c in names]
    wanted = (sku or "").strip()
    frame = (
        lazy.select(keep + ["category"])
        .filter((pl.col("category") == CATEGORY)
                & (pl.col("sku").cast(pl.Utf8).str.strip_chars() == wanted))
        .collect()
    )
    rows = []
    for row in frame.iter_rows(named=True):
        # 留档里的 store 是店铺名、internal_sub_order_id 是聚水潭商品行ID，都不能当补录钥匙；
        # 店铺ID由调用方按未结账店月填，子订单号一律不填，按订单号+商品编码定位。
        rows.append({
            "store_id": None,
            "store_name": row.get("store"),
            "period": row.get("period"),
            "order_id": str(row.get("order_id") or ""),
            "sub_order_id": None,
            "sku": row.get("sku"),
            "order_date": row.get("order_date"),
            "quantity": row.get("quantity"),
            "run_id": run_id,
        })
    return rows


def _open_runs(ws) -> list[tuple[str, str, int]]:
    """未结账店月的最新一次核算。"""
    with ws.transaction() as conn:
        cursor = conn.execute(
            "SELECT store_id, period, (SELECT MAX(id) FROM run r WHERE r.store_id = p.store_id "
            "AND r.period = p.period) FROM period p WHERE p.state = 'open'"
        )
        found = [(store, period, run_id) for store, period, run_id in cursor.fetchall() if run_id]
    return found


def store_rows(ws, store_id: str, period: str, sku: str) -> list[dict[str, Any]]:
    with ws.transaction() as conn:
        cursor = conn.execute(
            "SELECT MAX(id) FROM run WHERE store_id = ? AND period = ?", (store_id, period)
        )
        run_id = cursor.fetchone()[0]
    if not run_id:
        return []
    rows = _rows_of(ws, run_id, sku)
    for row in rows:
        row["store_id"] = store_id
    return [row for row in rows if (row["period"] or "") == period]


def all_store_rows(ws, sku: str, limit: int = 4000) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for store_id, period, run_id in _open_runs(ws):
        for row in _rows_of(ws, run_id, sku):
            row["store_id"] = row["store_id"] or store_id
            row["period"] = row["period"] or period
            rows.append(row)
            if len(rows) >= limit:
                return rows
    return rows


def collect(ws, scope: str, store_id: str, period: str, sku: str) -> list[dict[str, Any]]:
    if scope == SCOPE_ALL:
        return all_store_rows(ws, sku)
    return store_rows(ws, store_id, period, sku)


# --------------------------------------------------------------------------- #
# 预览 / 写入
# --------------------------------------------------------------------------- #


def carry_reason(price_date: str, basis: str) -> str:
    """补录备注。没挑沿用价（price_date 为空）就是手填单价，必须写明成本依据。"""
    basis = (basis or "").strip()
    if (price_date or "").strip():
        return basis or f"下单日成本依据不足，按 {price_date.strip()} 沿用价人工确认补录"
    if len(basis) < MANUAL_BASIS_MIN:
        raise CarryError(f"手填单价必须写明成本依据（至少 {MANUAL_BASIS_MIN} 个字，如成本原表、采购单号）")
    return f"下单日成本依据不足，订单台无沿用价，人工手填单价；依据：{basis}"


def plan(ws, *, scope: str, store_id: str, period: str, sku: str,
         unit_cost: float, price_date: str, reason: str) -> dict[str, Any]:
    """按范围取行、逐行过售后拦截，得出可补录和不能补录的两份清单。"""
    sku = (sku or "").strip()
    if not sku:
        raise CarryError("没有商品编码，无法确定补录对象")
    if unit_cost is None or float(unit_cost) < 0:
        raise CarryError("成本单价无效")
    rows = collect(ws, scope, store_id, period, sku)
    if not rows:
        raise CarryError(
            "没有找到这个商品在未结账月份里「下单日成本依据不足」的缺价行"
            if scope == SCOPE_ALL else "本店这一类里没有这个商品的缺价行"
        )
    if RULE_NAME not in reason:
        reason = f"【{RULE_NAME}】{reason}" if reason else f"【{RULE_NAME}】下单日成本依据不足，按沿用价人工确认补录"
    items, blocked, cleared, seen = [], [], [], set()
    for row in rows:
        key = (row["store_id"], row["period"], row["order_id"], str(row["sub_order_id"] or ""))
        if key in seen:
            continue
        seen.add(key)
        stop, why, kind = after_sale_block(row["order_id"], sku)
        if stop and kind != "clear":
            blocked.append({**row, "blocked_reason": why})
            continue
        if stop:
            # 符合售后清成本条件：照样补录，但金额按 0 执行，缺口才会真正合上。
            cleared.append({**row, "cleared_reason": why})
            items.append({
                "kind": "line",
                "store_id": row["store_id"],
                "period": row["period"],
                "order_id": row["order_id"],
                "sub_order_id": row["sub_order_id"],
                "sku": sku,
                "unit_cost": "0",
                "reason": f"{why}，本行成本按 0 计",
            })
            continue
        items.append({
            "kind": "line",
            "store_id": row["store_id"],
            "period": row["period"],
            "order_id": row["order_id"],
            "sub_order_id": row["sub_order_id"],
            "sku": sku,
            "unit_cost": str(round(float(unit_cost), 4)),
            "reason": reason,
        })
    return {
        "scope": scope,
        "scope_label": "全系统所有店铺" if scope == SCOPE_ALL else f"本店 {period}",
        "sku": sku,
        "unit_cost": round(float(unit_cost), 4),
        "price_date": price_date,
        "manual": not (price_date or "").strip(),
        "items": items,
        "blocked": blocked,
        "cleared": cleared,
        "stores": sorted({item["store_id"] for item in items}),
        "note": "、".join(part for part in (
            f"{len(items) - len(cleared)} 行按{'沿用价' if (price_date or '').strip() else '手填单价'}补录"
            if len(items) - len(cleared) else "",
            f"{len(cleared)} 行符合售后清成本、按 0 计入" if cleared else "",
            f"{len(blocked)} 行售后情况查不到、不补录" if blocked else "",
        ) if part) or "没有可补录的行",
    }


def apply(ws, plan_result: dict[str, Any], store_ids: set[str], by: str) -> dict[str, Any]:
    from . import cost_adjustments

    items = plan_result.get("items") or []
    if not items:
        raise CarryError("没有可补录的行")
    saved = cost_adjustments.add(ws, items, store_ids, by=by)
    return {
        "saved": len(saved.get("saved") or []),
        "ids": saved.get("saved"),
        "stores": saved.get("stores"),
        "revision": saved.get("revision"),
        "blocked": plan_result.get("blocked") or [],
        "cleared": plan_result.get("cleared") or [],
        "note": plan_result.get("note"),
    }
