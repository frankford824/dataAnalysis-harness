"""中文导出和订单台 Cancelled 状态都不得贡献成本；保留同单有效组件。"""
from datetime import datetime

import polars as pl
import pytest

from conftest import MODELS
from ledger.engine.runtime import Ingestion, run
from ledger.model.loader import load_model
from ledger.model.schema import Model, SourceContract, StatementNode, Store
from test_reship_period_and_cost_drill import item


@pytest.mark.parametrize("platform", ["taobao", "pdd", "douyin", "jd", "alibaba1688"])
@pytest.mark.parametrize("cancelled", ["取消", "已取消", "Cancelled"])
@pytest.mark.parametrize("reship", [False, True])
def test_cancelled_component_contributes_zero_without_erasing_valid_same_order(platform, cancelled, reship):
    builtin = load_model(MODELS / "cn-ecommerce")
    metrics = tuple(builtin.metric(mid) for mid in ["goods_cost", "reshipment_cost"])
    model = Model(id="test", name="test", stores=(Store(id="s", name="shop", platform=platform),),
                  sources=(SourceContract(id="order_detail", name="订单", is_spine=True, owner_role="shop_owner", cadence="monthly"),
                           SourceContract(id="order_cost", name="成本", owner_role="shop_owner", cadence="monthly")),
                  metrics=metrics, statement=(StatementNode(id="cost", name="成本", formula={"op": "add", "of": [m.id for m in metrics]}),))
    orders = [dict(order_id="parent", sub_order_id="child", order_type="销售订单", order_state="Sent",
                   store_name="shop", order_time=datetime(2026, 6, 1))]
    costs = [dict(order_id="parent", original_order_id="parent", sub_order_id="child", internal_order_id=internal,
                  sku=internal, order_type="补发订单" if reship else "销售订单", order_state=state,
                  store_name="shop", order_time=datetime(2026, 6, 2), quantity=2.0, unit_cost=price)
             for internal, state, price in [("valid", "Sent", 5.0), ("cancelled", cancelled, 7.0)]]
    inputs = [item("order_detail", orders, orders[0].keys()), item("order_cost", costs, costs[0].keys())]
    result = run(Ingestion(model=model, items=inputs), platform)
    assert not result.eval_errors
    assert inputs[1].frame.height == 2  # 原始记录、单价保持可追溯。
    cancelled_rows = result.facts.filter(pl.col("internal_order_id") == "cancelled")
    assert cancelled_rows.height == 1
    assert cancelled_rows["contribution"].sum() == 0
    assert result.facts.filter(pl.col("internal_order_id") == "valid")["contribution"].sum() == pytest.approx(-10)
    assert result.spine_facts["amount"].sum() == pytest.approx(-10)


@pytest.mark.parametrize("platform", ["taobao", "pdd", "douyin", "jd", "alibaba1688"])
@pytest.mark.parametrize("state", [None, "", "Sent", "已发货", "Split", "Merged", "Question", "WaitPay"])
def test_non_cancelled_and_unknown_states_are_not_treated_as_cancelled(platform, state):
    from ledger.engine.predicate import compile_where
    model = load_model(MODELS / "cn-ecommerce")
    for mid, kind in [("goods_cost", "销售订单"), ("reshipment_cost", "补发订单")]:
        metric = model.metric(mid).for_platform(platform)
        if metric is None:
            continue
        frame = pl.DataFrame({"order_type": [kind], "order_state": pl.Series([state], dtype=pl.Utf8)})
        assert frame.filter(compile_where(metric.where, frame)).height == 1
