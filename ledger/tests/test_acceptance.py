"""历史原件回归：核对 CASES 明确绑定的店铺和账期。

人工表差异、结账门禁、覆盖率与订单骨架检查共同提供有限的回归证据，
不能代替当前期间的原始业务凭据与审批。explained 保留已有取证记录，
当前材料是否仍支持这些解释须另行核实，未解释差异继续按原阈值拒绝。

输入可能产生其它月份的尾片；只有显式 Case.period 属于该案例的目标。
任何已声明目标缺失都失败。缺少本机原件默认失败，显式禁用语料才跳过。

旧语料 2026-05 按已发布规则登记了预期（docs/legacy-corpus-20261103.md）：
expected_unavailable 的列必须确实不出数，expected_blockers 的拦截集合与计数、
expected_goods_coverage 的覆盖计数必须逐项一致，任何变化都失败。
"""

from __future__ import annotations

import math

import pytest
from conftest import needs_real_data

from tools.accept import ACCEPT_TOLERANCE, CASES, run_case, select_case_slice

#: 与命令行共用半分边界；一分钱差异必须拒绝。
TOLERANCE = ACCEPT_TOLERANCE


@needs_real_data
@pytest.mark.slow
@pytest.mark.parametrize("key", sorted(CASES))
def test_store_reconciles_exactly(key: str) -> None:
    case = CASES[key]
    gap = run_case(case)
    assert not math.isnan(gap), (
        f"{case.store} 没算出结果。多半是数据文件没找齐，"
        f"或者模板认不出某张表——看上面的输出。"
    )
    assert gap < TOLERANCE, (
        f"{case.store} 出现 {gap:,.2f} 的未解释差异。"
        f"要么是刚才的改动算错了，要么是发现了新情况需要取证后记进 explained。"
        f"上面的分项对照里标着「未解释」的就是。"
    )


@needs_real_data
@pytest.mark.slow
def test_all_three_stores_covered() -> None:
    """三家店都得在验收里。少一家就等于那家的口径没人看着。"""
    platforms = {c.platform for c in CASES.values()}
    assert platforms == {"taobao", "alibaba1688", "douyin"}


#: 对这三家店执行全部显式案例账期的检查。
ALL_STORES = ["taobao_xibishun", "alibaba1688_xingze", "douyin_qianhuajian"]


def _declared_slices(model, store, result):
    cases = [case for case in CASES.values()
             if case.store in (store.id, store.name, *store.aliases)]
    assert cases, f'{store.name} 没有显式验收案例'
    return {(store.name, case.period): select_case_slice(model, result, case) for case in cases}


def _case_of(store, period):
    matches = [case for case in CASES.values()
               if case.store in (store.id, store.name, *store.aliases) and case.period == period]
    assert len(matches) == 1, f'{store.name} {period} 没有唯一的验收案例'
    return matches[0]


def _run_store(store_id: str):
    from conftest import MODELS, PLATFORM_DATA

    from ledger.engine.runtime import ingest, run
    from ledger.model.loader import load_model

    model = load_model(MODELS / "cn-ecommerce")
    store = model.store(store_id)
    found = list(PLATFORM_DATA.rglob("*.xlsx"))
    mine = set(model.files_of(store_id, (p.name for p in found)))
    files = [p for p in found if p.name in mine]
    assert files, f"{store.name} 一个文件都没找到"
    result = run(ingest(files, model, [store.name, *store.aliases]), store.platform)
    assert result.slices, f"{store.name} 没算出结果"
    return model, store, _declared_slices(model, store, result)


@needs_real_data
@pytest.mark.slow
@pytest.mark.parametrize("store_id", ALL_STORES)
def test_store_can_close(store_id: str) -> None:
    """每个已声明店月均须满足原有结账门禁；有阻塞就保持失败。

    案例登记了 expected_blockers 时，拦截集合与逐项计数必须和登记完全一致：
    多一项、少一项、计数变了都失败，店期也必须仍然不能结账。
    """
    _, store, selected = _run_store(store_id)
    for (_, period), sl in selected.items():
        case = _case_of(store, period)
        blockers = {f.check_id: f for f in sl.audit.findings if f.blocking}
        if not case.expected_blockers:
            assert not blockers, (
                f"{store.name} {period} 结不出账，拦截项："
                + "；".join(f"{f.name}（{f.message}）" for f in blockers.values())
            )
            assert sl.can_close
            continue
        assert all(why.strip() for _, why in case.expected_blockers.values())
        expected = {cid: want for cid, (want, _) in case.expected_blockers.items()}
        observed = {cid: ({k: (f.detail or {}).get(k) for k in expected[cid]}
                          if cid in expected else f"{f.name}（{f.message}）")
                    for cid, f in blockers.items()}
        assert observed == expected, (
            f"{store.name} {period} 的结账拦截与现行规则下的登记不符：\n"
            f"  登记 {expected}\n  实际 {observed}"
        )
        assert not sl.can_close


@needs_real_data
@pytest.mark.slow
@pytest.mark.parametrize("store_id", ALL_STORES)
def test_coverage_denominator_is_scoped(store_id: str) -> None:
    """商品成本的覆盖率只对已发货订单算，而且分母确实比全部订单小。

    要是哪天运单号这一列的绑定失效了，分母会悄悄退回全部订单，覆盖率跟着掉，
    结账被拦——但拦截理由会指向"聚水潭没同步"这个错方向。这里钉住分母被收窄
    这件事本身。
    """
    _, store, selected = _run_store(store_id)
    for (_, period), sl in selected.items():
        case = _case_of(store, period)
        report = sl.link_reports.get("goods_cost")
        if report is None or not report.spine_keys:
            assert case.expected_goods_coverage is None, f"{store.name} {period} 登记了覆盖计数却没有可比对的订单"
            continue
        assert report.expect_label in ("已发货", "已发货且未全额退款", "已出库")
        assert report.spine_keys < report.spine_keys_total
        if case.expected_goods_coverage is None:
            assert report.coverage >= 0.98
            continue
        covered, expected, why = case.expected_goods_coverage
        assert why.strip()
        assert (report.spine_keys_covered, report.spine_keys) == (covered, expected), (
            f"{store.name} {period} 商品成本覆盖 {report.spine_keys_covered}/{report.spine_keys}，"
            f"登记的是 {covered}/{expected}（{why}）"
        )


@needs_real_data
@pytest.mark.slow
@pytest.mark.parametrize("store_id", ALL_STORES)
def test_spine_source_not_reported_missing(store_id: str) -> None:
    """订单明细交了就不能报它缺。

    脊柱源不产出金额事实，它交付的是订单骨架本身，所以在事实表里永远查不到它。
    早先据此判断完整度，结果三家店都被报「订单明细没有本月数据」——而正是它撑起了
    整张损益表。催人补一份已经交了的文件，是最快让人不信这套提示的办法。
    """
    model, store, selected = _run_store(store_id)
    for sl in selected.values():
        spine_sources = [s.id for s in model.sources if s.is_spine]
        for sid in spine_sources:
            assert sid not in sl.completeness.missing, (
                f"{store.name} 的 {sid} 交了却被报缺：{sl.completeness.reasons.get(sid)}"
            )
