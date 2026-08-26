"""聚水潭子订单号和千牛对不上时，用原始线上订单号兜底。

天猫拆单、合单之后，聚水潭给新的线上子订单编号，千牛还是买家下单时的号。
主订单对得上、子订单对不上——天猫皇莉诗 2026-06 有 508 行成本卡在这里，
报表上商品成本少一截，覆盖率掉到 97.7% 结不了账。
"""

from __future__ import annotations

import polars as pl

from ledger.engine.calculate import evaluate_metric
from ledger.engine.link import LINK_KEY, LINK_SPLIT, LINKED, Spine, link
from ledger.model.schema import ColumnBinding, LinkRule, Metric, Template, ValueExpr


def _metric() -> Metric:
    return Metric(
        id="goods_cost",
        name="商品成本",
        source="order_cost",
        value=ValueExpr(op="sum_product", of=["unit_cost", "quantity"]),
        sign="negate",
        link=LinkRule(
            key="sub_order_id",
            to="order.sub_order_id",
            grain="order",
            fallback_key="original_order_id",
            fallback_to=("order.order_id",),
        ),
    )


def _tpl() -> Template:
    return Template(
        id="jst",
        source="order_cost",
        match_columns=["线上子订单编号"],
        bindings=(
            ColumnBinding(role="sub_order_id", columns=["线上子订单编号"]),
            ColumnBinding(role="original_order_id", columns=["原始线上订单号"]),
            ColumnBinding(role="unit_cost", columns=["成本价"], kind="number"),
            ColumnBinding(role="quantity", columns=["数量"], kind="number"),
        ),
    )


#: 一个主订单两个子订单。聚水潭给的子订单号和千牛的都对不上。
_SPINE = [
    {"order_id": "M1", "sub_order_id": "S11", "store": "店", "period": "2026-06"},
    {"order_id": "M1", "sub_order_id": "S12", "store": "店", "period": "2026-06"},
    {"order_id": "M2", "sub_order_id": "S21", "store": "店", "period": "2026-06"},
]


def _cost(**cols) -> pl.DataFrame:
    n = len(next(iter(cols.values())))
    base = {
        "__sha__": ["x"] * n,
        "__file__": ["jst.xlsx"] * n,
        "__sheet__": ["s"] * n,
        "__row__": list(range(2, 2 + n)),
    }
    return pl.DataFrame(base | cols)


class TestOriginalOrderIdFallback:
    def test_matching_sub_order_stays_one_to_one(self):
        """对得上的行不能被兜底改写，更不能被拆成两半。"""
        frame, report = link(
            _cost(sub_order_id=["S11"], original_order_id=["M1"], unit_cost=[10.0], quantity=[1.0]),
            _metric(),
            Spine(frame=pl.DataFrame(_SPINE)),
        )
        assert report.linked_rows == 1
        assert frame.get_column(LINK_KEY).to_list() == ["S11"]
        assert LINK_SPLIT not in frame.columns or frame.get_column(LINK_SPLIT).to_list() == [1.0]

    def test_mismatched_sub_order_spreads_across_the_main_order(self):
        """子订单对不上、主订单对得上：两个子订单各摊一半。"""
        src = _cost(
            sub_order_id=["JST-NEW"],
            original_order_id=["M1"],
            unit_cost=[20.0],
            quantity=[1.0],
        )
        frame, report = link(src, _metric(), Spine(frame=pl.DataFrame(_SPINE)))
        assert report.linked_rows == 2
        assert report.fallback_rows == 1
        assert sorted(frame.filter(pl.col(LINKED)).get_column(LINK_KEY).to_list()) == ["S11", "S12"]
        splits = frame.filter(pl.col(LINKED)).get_column(LINK_SPLIT).to_list()
        assert splits == [0.5, 0.5]

        facts, _ = evaluate_metric(frame, _metric(), _tpl(), "店", "2026-06")
        # 20 元成本取负之后均分成两行 -10。
        got = dict(zip(facts.get_column("link_key").to_list(), facts.get_column("amount").to_list()))
        assert got == {"S11": -10.0, "S12": -10.0}

    def test_neither_key_stays_unlinked(self):
        src = _cost(
            sub_order_id=["NOPE"],
            original_order_id=["NOPE"],
            unit_cost=[9.0],
            quantity=[1.0],
        )
        frame, report = link(src, _metric(), Spine(frame=pl.DataFrame(_SPINE)))
        assert report.linked_rows == 0
        assert frame.get_column(LINKED).to_list() == [False]

    def test_empty_sub_order_is_not_spread_as_a_gift(self):
        """空号行是赠品，就算原始订单号对得上主订单也不进账。

        挂不上的行仍然会产出一条未挂钩事实，进「取不出订单号」那一桶——这是故意的，
        业务确认暂不计入，但钱得看得见。不能铺到 S11/S12 上进商品成本。
        """
        src = _cost(
            sub_order_id=[None],
            original_order_id=["M1"],
            unit_cost=[5.0],
            quantity=[1.0],
        )
        frame, report = link(src, _metric(), Spine(frame=pl.DataFrame(_SPINE)))
        assert report.linked_rows == 0
        assert report.fallback_rows == 0
        assert frame.get_column(LINKED).to_list() == [False]

        facts, _ = evaluate_metric(frame, _metric(), _tpl(), "店", "2026-06")
        assert facts.get_column("linked").to_list() == [False]
        assert facts.get_column("link_key").to_list() == [None]
        assert facts.get_column("amount").to_list() == [-5.0]
