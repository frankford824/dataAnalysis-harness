from pathlib import Path

import polars as pl

from ledger.engine.controls import verify
from ledger.engine.types import ControlTotal, FileRef, RawTable
from ledger.model import load_model


MODEL = Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"


def pdd_table(outgo_amount=-5.0):
    return RawTable(
        ref=FileRef("a" * 64, "对账-PDD.csv"),
        headers=[],
        controls=[
            ControlTotal("支出合计", count=2, amount=outgo_amount, direction="outgo"),
            ControlTotal("收入合计", count=2, amount=10.0, direction="income"),
        ],
    )


def test_pdd_declared_counts_may_include_zero_amount_events():
    frame = pl.DataFrame({"income": [5.0, 5.0, 0.0, 0.0], "outgo": [0.0, 0.0, -5.0, 0.0]})
    template = load_model(MODEL).template("pdd_settlement_v1")
    results = verify(pdd_table(), frame, template)
    assert all(result.passed for result in results)
    assert "含零金额事件" in results[0].message


def test_pdd_count_exception_never_relaxes_amount_check():
    frame = pl.DataFrame({"income": [5.0, 5.0, 0.0, 0.0], "outgo": [0.0, 0.0, -5.0, 0.0]})
    template = load_model(MODEL).template("pdd_settlement_v1")
    results = verify(pdd_table(outgo_amount=-6.0), frame, template)
    assert not results[0].passed
    assert "金额" in results[0].message
