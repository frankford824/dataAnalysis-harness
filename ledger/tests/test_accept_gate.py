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
