"""Human confirmation of store-period cost totals, without rewriting source rows."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from copy import deepcopy
from types import SimpleNamespace

from .engine import calculate
from .money import money_float
from .workspace import WorkspaceError

COST_METRICS = frozenset({"goods_cost", "goods_return_cost", "dropship_cost", "reshipment_cost"})
COST_KEYS = ("goods", "dropship", "reshipment")


def _cents(value) -> Decimal:
    try:
        amount = Decimal(str(value))
        if (not amount.is_finite() or abs(amount) > Decimal("999999999999.99")
                or amount != amount.quantize(Decimal("0.01"))):
            raise ValueError()
        return amount
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise WorkspaceError("人工确认金额必须是有效的两位小数") from exc


def observed(result: dict) -> dict[str, float]:
    inputs = result.get("calculation_inputs") or {}
    totals = inputs.get("metric_totals") or {}
    return {
        "goods": money_float(-Decimal(str(totals.get("goods_cost", 0)))
                             - Decimal(str(totals.get("goods_return_cost", 0)))),
        "dropship": money_float(-Decimal(str(totals.get("dropship_cost", 0)))),
        "reshipment": money_float(-Decimal(str(totals.get("reshipment_cost", 0)))),
    }


def preview(model, result: dict, confirmed: dict) -> dict:
    """Re-evaluate the model tree from the original frozen metric totals."""
    inputs = result.get("calculation_inputs") or {}
    if not {"metric_totals", "unavailable_metrics", "inapplicable_metrics"} <= set(inputs):
        raise WorkspaceError("这次计算尚无人工预览依据，请先重算本店")
    if set(confirmed) != set(COST_KEYS):
        raise WorkspaceError("请分别确认商品、代发和补发成本")
    amounts = {key: _cents(confirmed[key]) for key in COST_KEYS}
    totals = dict(inputs["metric_totals"])
    totals.update({
        "goods_cost": float(-amounts["goods"]), "goods_return_cost": 0.0,
        "dropship_cost": float(-amounts["dropship"]),
        "reshipment_cost": float(-amounts["reshipment"]),
    })
    unavailable = set(inputs["unavailable_metrics"]) - COST_METRICS
    nodes = calculate.evaluate_statement(
        model, totals, unavailable, set(inputs["inapplicable_metrics"]),
    )
    from . import view
    statement = view._statement(SimpleNamespace(nodes=nodes), model)
    profit = nodes["net_profit"]
    gross = nodes["gross_profit"]
    return {
        "confirmed": {key: money_float(amount) for key, amount in amounts.items()},
        "observed": observed(result), "statement": statement,
        "profit": profit.value if profit.available else None,
        "gross": gross.value if gross.available else None,
        "other_missing": sorted(set(profit.missing_sources) - {"order_cost"}),
    }


def certified(model, result: dict, source_run_id: int, confirmed: dict,
              payouts: list[dict], no_payout: bool, reason: str) -> tuple[dict, dict]:
    """Prepare a separate immutable human result for the close transaction."""
    if not reason.strip():
        raise WorkspaceError("人工确认成本和提成必须填写原因")
    trial = preview(model, result, confirmed)
    if trial["profit"] is None:
        raise WorkspaceError("除成本外仍有资料缺口，当前无法确认利润；请先补齐其他资料")
    original_people = list((result.get("commission") or {}).get("people") or [])
    identities = [str(person.get("person_id") or "") for person in original_people]
    if len(identities) != len(set(identities)) or any(not pid for pid in identities):
        raise WorkspaceError("提成人员身份不完整，不能人工确认金额")
    if no_payout and (payouts or identities):
        raise WorkspaceError("已有提成人员时，请分别确认其提成金额")
    if not identities and not no_payout:
        raise WorkspaceError("本期没有提成人员，请明确确认无需提成")
    entered = [str(item.get("person_id") or "") for item in payouts]
    if not no_payout and (len(entered) != len(set(entered)) or set(entered) != set(identities)):
        raise WorkspaceError("请分别填写全部提成人员的确认金额")
    amounts = {str(item["person_id"]): _cents(item.get("amount")) for item in payouts}
    decided = deepcopy(result)
    decided["statement"] = trial["statement"]
    decided["manual_cost"] = {
        "source_run_id": source_run_id, "observed": trial["observed"],
        "confirmed": trial["confirmed"], "reason": reason.strip(),
        "gross": trial["gross"], "profit": trial["profit"],
        "method": "人工确认店期成本和提成，不改原始行",
    }
    decided["cost_review"] = {"observed": trial["observed"],
                              "confirmed": trial["confirmed"], "requires_human": False}
    from . import view
    cost_source = view.source_name(model, "order_cost")
    decided["missing_sources"] = [name for name in result.get("missing_sources") or []
                                  if name != cost_source]
    for finding in decided.get("findings") or []:
        if finding.get("id") in {"chk_goods_coverage", "historical_cost_evidence"}:
            finding.update(passed=True, blocking=False,
                           message="商品成本已按人工确认金额结账；原覆盖缺口与源明细保留")
    commission = deepcopy(result.get("commission") or {})
    for person in commission.get("people") or []:
        person["amount"] = money_float(amounts[person["person_id"]])
    basis_id = commission.get("base_node")
    basis = model.node(basis_id) if basis_id else model.commission_base_node(result.get("store_id"))
    if basis:
        row = next((item for item in trial["statement"] if item["id"] == basis.id), None)
        commission["base_node"] = basis.id
        commission["base_name"] = basis.name
        commission["base_total"] = row["value"] if row and row["available"] else None
    commission.update(
        engine="commission-v2", configured=bool(identities),
        total=money_float(sum(amounts.values(), Decimal(0))),
        amount_complete=True, manual_confirmed=True,
        manual_amounts_after_labor=True,
        pricing_threshold_met=True,
    )
    commission.setdefault("notes", []).append("提成金额由人工逐人确认，已含兼职分摊；源订单提成试算未替代人工确认")
    decided["commission"] = commission
    decision = {"source_run_id": source_run_id, "observed": trial["observed"],
                "confirmed": trial["confirmed"], "payouts": [
                    {"person_id": pid, "amount": money_float(amount)}
                    for pid, amount in sorted(amounts.items())],
                "no_payout": no_payout, "reason": reason.strip(),
                "original_findings": [finding for finding in result.get("findings") or []
                                      if finding.get("id") in {"chk_goods_coverage", "historical_cost_evidence"}]}
    return decided, decision


def payout_only(result: dict, source_run_id: int, payouts: list[dict],
                no_payout: bool, reason: str) -> tuple[dict, dict]:
    """Confirm payouts while keeping the computed store statement unchanged."""
    if not reason.strip():
        raise WorkspaceError("人工确认提成必须填写原因")
    if not isinstance(result.get("commission"), dict):
        raise WorkspaceError("本期尚无提成计算记录，请先重算")
    people = list((result.get("commission") or {}).get("people") or [])
    identities = [str(person.get("person_id") or "") for person in people]
    if len(identities) != len(set(identities)) or any(not pid for pid in identities):
        raise WorkspaceError("提成人员身份不完整，不能人工确认金额")
    if no_payout and (payouts or identities):
        raise WorkspaceError("已有提成人员时，请分别确认其提成金额")
    if not identities and not no_payout:
        raise WorkspaceError("本期没有提成人员，请明确确认无需提成")
    entered = [str(item.get("person_id") or "") for item in payouts]
    if not no_payout and (len(entered) != len(set(entered)) or set(entered) != set(identities)):
        raise WorkspaceError("请分别填写全部提成人员的确认金额")
    amounts = {str(item["person_id"]): _cents(item.get("amount")) for item in payouts}
    decided = deepcopy(result)
    for person in decided["commission"].get("people") or []:
        person["amount"] = money_float(amounts[person["person_id"]])
    decided["commission"].update(total=money_float(sum(amounts.values(), Decimal(0))),
                                   amount_complete=True, manual_confirmed=True,
                                   manual_amounts_after_labor=True)
    decided["commission"].setdefault("notes", []).append(
        "提成由人工逐人确认，已含兼职分摊；原订单试算与未分配订单保留")
    decided["manual_payout"] = {"source_run_id": source_run_id, "reason": reason.strip(),
                                 "method": "人工确认提成，不调整成本或经营利润"}
    decision = {"source_run_id": source_run_id, "reason": reason.strip(),
                "payouts": [{"person_id": pid, "amount": money_float(amount)}
                            for pid, amount in sorted(amounts.items())],
                "no_payout": no_payout}
    return decided, decision
