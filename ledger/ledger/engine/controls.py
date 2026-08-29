"""用文件自带的控制总数自证解析正确。

很多平台导出的文件在尾部写了自己的合计，比如支付宝账务明细：

    #支出合计：75171笔，共-540182.61元
    #收入合计：18003笔，共536923.62元

这是文件自己声明的正确答案。解析完拿它对一遍，笔数对不上说明漏读或多读了行，
金额对不上说明数值解析有问题（科学计数法、千分位、全角负号都可能出岔子）。

这比"相信解析库够强"靠谱得多：它不依赖任何库的质量承诺，是逐文件的实证。
拿不到控制总数的文件不代表有问题，只是这一层保障用不上，要靠别的校验兜。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import polars as pl

from ..model.schema import Template
from ..money import decimal_amount, sum_amounts
from .types import ControlTotal, RawTable

#: 金额允许的误差。逐笔累加的浮点误差远小于此。
_TOLERANCE = Decimal("0.01")


@dataclass
class ControlResult:
    """一条控制总数的核对结果。"""

    label: str
    passed: bool
    message: str

    @property
    def icon(self) -> str:
        return "对上了" if self.passed else "对不上"


def verify(table: RawTable, frame: pl.DataFrame, template: Template) -> list[ControlResult]:
    """核对一张表的控制总数。

    frame 是归一后的数据帧，按角色取金额列——控制总数说的是"收入合计""支出合计"，
    对应模板里绑到 income / outgo 角色的那两列。
    """
    if not table.controls:
        return []
    roles = {b.role for b in template.bindings}
    directional = [
        control for control in table.controls
        if control.direction in {"income", "outgo"} and control.count is not None
    ]
    # PDD's footer splits every business-event row into "income count" or "outgo count", even
    # when the corresponding amount is zero. The two declared counts cover the entire data table;
    # counting only non-zero monetary cells therefore understates outgo rows while the amount is
    # still exact. This exception is narrow, evidence-backed, and never relaxes the amount check.
    pdd_zero_amount_counts = (
        template.id == "pdd_settlement_v1"
        and len(directional) == 2
        and sum(control.count or 0 for control in directional) == frame.height
    )
    out: list[ControlResult] = []
    for control in table.controls:
        role = {"income": "income", "outgo": "outgo"}.get(control.direction)
        if role is None or role not in roles or role not in frame.columns:
            continue
        out.append(_check_one(control, frame, role, check_count=not pdd_zero_amount_counts))
    return out


def _check_one(
    control: ControlTotal, frame: pl.DataFrame, role: str, *, check_count: bool = True,
) -> ControlResult:
    values = frame.get_column(role).cast(pl.Float64, strict=False).fill_null(0.0).to_list()
    got_count = sum(1 for v in values if v != 0.0)
    got_amount = sum_amounts(values)

    problems = []
    if check_count and control.count is not None and got_count != control.count:
        problems.append(
            f"笔数 文件说 {control.count:,}、解析出 {got_count:,}，差 {got_count - control.count:+,}"
        )
    expected = decimal_amount(control.amount) if control.amount is not None else None
    if expected is not None and abs(got_amount - expected) > _TOLERANCE:
        problems.append(
            f"金额 文件说 {expected:,.2f}、解析出 {got_amount:,.2f}，"
            f"差 {got_amount - expected:+,.2f}"
        )

    if not problems:
        count_text = (
            f"{got_count:,} 笔"
            if check_count or control.count is None
            else f"金额非零 {got_count:,} 笔；平台声明 {control.count:,} 笔含零金额事件"
        )
        return ControlResult(
            control.label, True,
            f"{control.label} 与文件自带总数一致（{count_text} / {got_amount:,.2f} 元）",
        )
    return ControlResult(
        control.label, False,
        f"{control.label} 与文件自带总数对不上：{'；'.join(problems)}。"
        f"这说明解析漏了行或数值没解对，不能直接用来结账。",
    )


def summarize(results: list[ControlResult]) -> str:
    if not results:
        return ""
    bad = [r for r in results if not r.passed]
    if not bad:
        return f"文件自带的 {len(results)} 项控制总数全部对上"
    return f"{len(bad)}/{len(results)} 项控制总数对不上：" + "；".join(r.message for r in bad)
