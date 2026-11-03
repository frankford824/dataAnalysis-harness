"""Adversarial release-gate checks, independent of the finance engine."""
import math
from types import SimpleNamespace as N

import pytest

from tools import accept
from ledger import replay


def node(value=100, available=True):
    return N(value=value, available=available)


def case(**changes):
    return accept.Case(**({'store': 'audit', 'platform': 'taobao', 'period': '2026-06',
        'detail': 'unused.xlsx', 'sheet': 's', 'key': 'id',
        'columns': {'sales': 'sales', 'profit': 'net_profit'}} | changes))


def verdict(nodes, human=None):
    return accept.compare_case(case(), N(nodes=nodes),
                               human or {'sales': 100, 'profit': 100}, N(statement=[]))


def test_derived_only_change_is_rejected():
    assert verdict({'sales': node(), 'net_profit': node(999)}) > 0


@pytest.mark.parametrize('nodes', [{}, {'sales': node(available=False)},
                                  {'sales': node(), 'net_profit': node(float('nan'))}])
def test_required_or_nonfinite_metric_is_rejected(nodes):
    assert not math.isfinite(verdict(nodes))


def test_missing_human_column_is_rejected():
    assert not math.isfinite(verdict({'sales': node(), 'net_profit': node()}, {'sales': 100}))


def test_formula_is_independent_of_human_expected_value():
    model = N(statement=[N(id='net_profit', formula=N(op='add', of=('sales', 'cost')))])
    result = N(nodes={'sales': node(), 'cost': node(-20), 'net_profit': node()})
    assert not math.isfinite(accept.compare_case(case(), result, {'sales': 100, 'profit': 100}, model))


def test_explicit_later_period_is_compared(tmp_path, monkeypatch):
    (tmp_path / 'audit.xlsx').touch()
    monkeypatch.setattr(accept, 'DATA', tmp_path)
    monkeypatch.setattr(accept, 'load_model', lambda _: N(name='m', templates=[], metrics=[], statement=[],
        stores=[N(id='audit-id',name='audit',aliases=['legacy-audit'])]))
    monkeypatch.setattr(accept, 'ingest', lambda *a, **k: N(summary=lambda: '', known=[], unknown=[]))
    sl = lambda v: N(nodes={'sales': node(v), 'net_profit': node(v)}, can_close=True, audit=N(findings=[]))
    monkeypatch.setattr(accept, 'run', lambda *a, **k: N(spine_rows=0, facts=N(height=0),
        spine_facts=N(height=0), notes=[], projections={},
        slices={('audit', '2026-05'): sl(100), ('audit', '2026-06'): sl(999)}))
    monkeypatch.setattr(accept, 'human_totals', lambda *a: {'sales': 100, 'profit': 100})
    assert accept.run_case(case()) > 0
    assert not math.isfinite(accept.run_case(case(period='2026-07')))
    assert not math.isfinite(accept.run_case(case(period='')))


@pytest.mark.parametrize('gap', [1, float('nan'), float('inf')])
def test_cli_returns_failure_for_unexplained_or_nonfinite_gap(monkeypatch, gap):
    monkeypatch.setattr('sys.argv', ['accept.py'])
    monkeypatch.setattr(accept, 'run_case', lambda _: gap)
    assert accept.main() == 1


def test_cli_rejects_empty_selection(monkeypatch):
    monkeypatch.setattr('sys.argv', ['accept.py'])
    monkeypatch.setattr(accept, 'CASES', {})
    assert accept.main() == 1


def manifest(scope):
    entry = {'path': 'source.xlsx', 'sha256': 'a' * 64}
    return {'scope': scope, 'inputs': [entry], 'model': [entry]}


@pytest.mark.parametrize('version', ['abc1234-dirty', 'unknown', 'invalid'])
def test_baseline_rejects_unreproducible_version(tmp_path, monkeypatch, version):
    monkeypatch.setattr(replay, 'engine_version', lambda: version)
    path = tmp_path / 'baseline.json'
    with pytest.raises(ValueError):
        replay.write_baseline({'s': {'2026-06': {}}}, path)
    assert not path.exists()


def test_baseline_requires_manifest_and_explicit_scope_reduction(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, 'engine_version', lambda: 'a' * 40)
    path = tmp_path / 'baseline.json'
    original = {'s': {'2026-05': {}, '2026-06': {}}}
    replay.write_baseline(original, path, manifest=manifest({'s': ['2026-05', '2026-06']}))
    before = path.read_bytes()
    subset = {'s': {'2026-06': {}}}
    with pytest.raises(ValueError, match='范围缩减'):
        replay.write_baseline(subset, path, manifest=manifest({'s': ['2026-06']}))
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match='完整输入'):
        replay.write_baseline(original, path)
    replay.write_baseline(subset, path, manifest=manifest({'s': ['2026-06']}), scope_change_reason='retired scope')


@pytest.mark.parametrize('before,after', [(0.949, 0.952), (True, 1), (float('nan'), float('nan')),
                                         (float('inf'), float('inf'))])
def test_ratio_bool_and_nonfinite_are_not_amount_noise(before, after):
    assert not replay.compare({'s': {'p': {'coverage': after}}},
                              {'stores': {'s': {'p': {'coverage': before}}}}).clean


def declared_scope(monkeypatch,periods):
    import test_acceptance as acceptance
    store=N(id='audit-id',name='audit',aliases=['legacy-audit'])
    model=N(stores=[store],sources=[N(id='orders',is_spine=True)])
    monkeypatch.setattr(acceptance,'CASES',{period:case(period=period) for period in periods})
    return acceptance,model,store


def test_acceptance_helpers_ignore_only_undeclared_tail_periods(monkeypatch):
    acceptance,model,store=declared_scope(monkeypatch,['2026-05'])
    selected=object();tail=object()
    result=N(slices={('audit','2026-05'):selected,('audit','2026-06'):tail})
    assert acceptance._declared_slices(model,store,result)=={('audit','2026-05'):selected}


@pytest.mark.parametrize('available',[{}, {('audit','2026-05'):object()}, {('another-store','2026-06'):object()}])
def test_acceptance_helpers_refuse_any_missing_declared_period(monkeypatch,available):
    acceptance,model,store=declared_scope(monkeypatch,['2026-05','2026-06'])
    with pytest.raises(ValueError,match='缺少唯一'):
        acceptance._declared_slices(model,store,N(slices=available))


@pytest.mark.parametrize('family',['test_store_can_close','test_coverage_denominator_is_scoped','test_spine_source_not_reported_missing'])
def test_acceptance_helpers_check_every_declared_period_with_original_assertions(monkeypatch,family):
    acceptance,model,store=declared_scope(monkeypatch,['2026-05','2026-06'])
    def slice(complete):
        return N(can_close=complete,audit=N(findings=[]),
            link_reports={'goods_cost':N(spine_keys=1,spine_keys_total=2,expect_label='已发货',coverage=.99 if complete else .97)},
            completeness=N(missing=[] if complete else ['orders'],reasons={'orders':'fixture missing'}))
    result=N(slices={('audit','2026-05'):slice(True),('audit','2026-06'):slice(False),('audit','2026-07'):slice(True)})
    selected=acceptance._declared_slices(model,store,result)
    assert set(selected)=={('audit','2026-05'),('audit','2026-06')}
    monkeypatch.setattr(acceptance,'_run_store',lambda _: (model,store,selected))
    with pytest.raises(AssertionError):getattr(acceptance,family).__wrapped__(store.id)


@pytest.mark.parametrize('delta',[0.005,0.006,0.01,0.015])
def test_half_cent_boundary_and_one_cent_errors_are_not_silently_discarded(delta):
    assert verdict({'sales':node(100+delta),'net_profit':node()})>=accept.ACCEPT_TOLERANCE


def test_each_tolerated_float_noise_is_not_added_to_a_false_amount_error():
    assert verdict({'sales':node(100.004),'net_profit':node(100.004)})==0


@pytest.mark.parametrize('part',['human','actual','explanation'])
def test_boolean_is_not_monetary_authority(part):
    checked=case(explained={'sales':(True,'invalid synthetic explanation')} if part=='explanation' else {})
    human={'sales':True if part=='human' else 100,'profit':100}
    nodes={'sales':node(True if part=='actual' else 100),'net_profit':node()}
    assert not math.isfinite(accept.compare_case(checked,N(nodes=nodes),human,N(statement=[])))


@pytest.mark.parametrize('gap',[0.005,0.006,0.01,0.015])
def test_cli_uses_the_same_half_cent_verdict_as_the_declared_pytest_gate(monkeypatch,gap):
    monkeypatch.setattr('sys.argv',['accept.py'])
    monkeypatch.setattr(accept,'run_case',lambda _:gap)
    assert accept.main()==1


def absent():
    return node(None, available=False)


def unavailable_case(**changes):
    return case(expected_unavailable={'profit': 'synthetic rule withholds profit'}, **changes)


def test_expected_unavailable_must_actually_be_unavailable():
    checked = unavailable_case()
    assert accept.compare_case(checked, N(nodes={'sales': node(), 'net_profit': absent()}),
                               {'sales': 100, 'profit': 100}, N(statement=[])) == 0
    for wrong in (node(), node(0), node(0, available=False), node(None)):
        assert not math.isfinite(accept.compare_case(checked, N(nodes={'sales': node(), 'net_profit': wrong}),
                                                     {'sales': 100, 'profit': 100}, N(statement=[])))


def test_expected_unavailable_does_not_hide_other_differences_or_missing_columns():
    checked = unavailable_case()
    assert accept.compare_case(checked, N(nodes={'sales': node(101), 'net_profit': absent()}),
                               {'sales': 100, 'profit': 100}, N(statement=[])) > 0
    assert not math.isfinite(accept.compare_case(checked, N(nodes={'sales': node()}),
                                                 {'sales': 100, 'profit': 100}, N(statement=[])))
    assert not math.isfinite(accept.compare_case(checked, N(nodes={'sales': node(), 'net_profit': absent()}),
                                                 {'sales': 100}, N(statement=[])))


@pytest.mark.parametrize('changes', [
    {'expected_unavailable': {'unknown-column': 'reason'}},
    {'expected_unavailable': {'profit': ' '}},
    {'expected_unavailable': {'profit': 'reason'}, 'explained': {'profit': (1.0, 'reason')}},
])
def test_expected_unavailable_registration_is_validated(changes):
    assert not math.isfinite(accept.compare_case(case(**changes), N(nodes={'sales': node(), 'net_profit': absent()}),
                                                 {'sales': 100, 'profit': 100}, N(statement=[])))


def test_expected_unavailable_skips_only_its_own_formula():
    model = N(statement=[N(id='net_profit', formula=N(op='add', of=('sales', 'cost')))])
    result = N(nodes={'sales': node(), 'cost': absent(), 'net_profit': absent()})
    assert accept.compare_case(unavailable_case(), result, {'sales': 100, 'profit': 100}, model) == 0
    assert not math.isfinite(accept.compare_case(case(), result, {'sales': 100, 'profit': 100}, model))


def legacy_scope(monkeypatch, **changes):
    import test_acceptance as acceptance
    store = N(id='audit-id', name='audit', aliases=[])
    model = N(stores=[store], sources=[])
    monkeypatch.setattr(acceptance, 'CASES', {'2026-05': case(period='2026-05', **changes)})
    return acceptance, model, store


def blocker(check_id, **detail):
    return N(check_id=check_id, name=check_id, message='synthetic', blocking=True, detail=detail)


@pytest.mark.parametrize('findings,closes,ok', [
    ([blocker('coverage', uncovered=3), blocker('shared', count=2)], False, True),
    ([blocker('coverage', uncovered=3)], False, False),
    ([blocker('coverage', uncovered=3), blocker('shared', count=2), blocker('new')], False, False),
    ([blocker('coverage', uncovered=4), blocker('shared', count=2)], False, False),
    ([blocker('coverage', uncovered=3), blocker('shared', count=2)], True, False),
])
def test_registered_blockers_must_match_exactly(monkeypatch, findings, closes, ok):
    expected = {'coverage': ({'uncovered': 3}, 'synthetic coverage rule'),
                'shared': ({'count': 2}, 'synthetic shared rule')}
    acceptance, model, store = legacy_scope(monkeypatch, expected_blockers=expected)
    selected = {('audit', '2026-05'): N(can_close=closes, audit=N(findings=findings))}
    monkeypatch.setattr(acceptance, '_run_store', lambda _: (model, store, selected))
    if ok:
        acceptance.test_store_can_close.__wrapped__(store.id)
    else:
        with pytest.raises(AssertionError):
            acceptance.test_store_can_close.__wrapped__(store.id)


@pytest.mark.parametrize('covered,denominator,total,ok', [
    (5, 10, 20, True), (6, 10, 20, False), (5, 11, 20, False), (5, 10, 10, False)])
def test_registered_coverage_counts_must_match_exactly(monkeypatch, covered, denominator, total, ok):
    acceptance, model, store = legacy_scope(monkeypatch, expected_goods_coverage=(5, 10, 'synthetic rule'))
    report = N(spine_keys=denominator, spine_keys_total=total, expect_label='已发货',
               spine_keys_covered=covered, coverage=covered / denominator)
    selected = {('audit', '2026-05'): N(link_reports={'goods_cost': report})}
    monkeypatch.setattr(acceptance, '_run_store', lambda _: (model, store, selected))
    if ok:
        acceptance.test_coverage_denominator_is_scoped.__wrapped__(store.id)
    else:
        with pytest.raises(AssertionError):
            acceptance.test_coverage_denominator_is_scoped.__wrapped__(store.id)
