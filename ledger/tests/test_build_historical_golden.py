"""Frozen closed months become hash-bound historical cases without touching the source."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from conftest import write_xlsx
from ledger import freight_experiments_inputs as frozen
from ledger import replay_service
from ledger.workspace import Workspace
from test_replay_service import all_expected, changed_bundle, seed, service_fixture_bundle

ROOT = Path(__file__).resolve().parents[2]
APPROVAL = 'owner-approval/2026-06..07 frozen close baseline'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load('build_historical_golden', 'build-historical-golden.py')
gate = load('service_cases_gate_for_builder', 'verify-ledger-service-cases.py')


def rename(body):
    body.update(id='cn-closed-history', name='历史月结模型')
    body['overheads'] = [{**row, 'name': '已确认零兼职'} for row in body['overheads']]


def checkpoint(path):
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    conn.close()


@pytest.fixture(scope='module')
def production(tmp_path_factory):
    """Real service runs closed through the original gate, then copied as a quiescent backup."""
    tmp = tmp_path_factory.mktemp('production')
    bundle = changed_bundle(service_fixture_bundle(tmp, fully_priced=True), tmp / 'production-bundle', rename)
    target = tmp / 'workspace'
    with replay_service.Session(bundle, seed(), publication_scopes=all_expected()['scopes']) as session:
        session.recompute('s1');session.recompute('s2');session.drain_pending()
        for period in ('2026-06', '2026-07'):
            for sid in ('s1', 's2'):
                state = session.ws.state(sid, period)
                closed = session.ws.close_period(sid, period, by='finance', note='月结',
                                                 expected_run_id=state.run_id, model=session.model)
                assert closed.closed
        session.confirm('s1', '2026-06', [{'person_id': 'p1', 'amount': '8.00'}], '财务核定实发')
        session.confirm('s2', '2026-07', [{'person_id': 'p1', 'amount': '7.50'}], '财务核定实发')
        session.ws.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        for name in ('commission/registry.db', 'order-feed.db'):
            checkpoint(session.root / name)
        shutil.copytree(session.root, target, ignore=shutil.ignore_patterns('*-wal', '*-shm'))
    model = tmp / 'candidate-model.json'
    shutil.copyfile(bundle / 'model.json', model)
    return {'workspace': target, 'feed': bundle / 'feed', 'model': model, 'tmp': tmp}


def source_copy(production, tmp_path):
    root = tmp_path / 'source'
    shutil.copytree(production['workspace'], root)
    return root


def tree(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


def execute(conn_path, sql, args=()):
    conn = sqlite3.connect(conn_path)
    with conn:
        conn.execute(sql, args)
    conn.close()


def query(conn_path, sql):
    conn = sqlite3.connect(conn_path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def run(production, root, output, *extra):
    return builder.main(['--workspace-root', str(root), '--feed-root', str(production['feed']),
        '--model', str(production['model']), '--target-period', '2026-07',
        '--approval-reference', APPROVAL, '--output', str(output), *extra])


def test_closed_months_build_gate_passing_cases_from_close_time_files_read_only(production, tmp_path):
    root = source_copy(production, tmp_path)
    ws = Workspace(root)
    before_slot = ws.conn.execute("SELECT sha FROM slot WHERE store_id='s1'").fetchone()[0]
    changed = write_xlsx(tmp_path / '甲店订单原表.xlsx', [['主订单号', '子订单号', '宝贝ID', '买家实付', '下单时间', '物流单号'],
        ['3319999999999999999', '3329999999999999999', '123456789001', 999, '2026-07-03 10:00:00', 'X1']])
    ws.keep(changed.name, changed, 's1')
    ws.close()
    execute(root / 'workspace.db', "INSERT INTO cost_line_log(store_id,period,coverage_key,context_sha,source_run_id,action,amount,reason,at,by) "
            "VALUES('s2','2026-06','k','c',1,'save','1.00','补录','2026-07-05T00:00:00+08:00','finance')")
    assert query(root / 'workspace.db', "SELECT sha FROM slot WHERE store_id='s1'") != before_slot
    checkpoint(root / 'workspace.db')
    assert not list(root.rglob('*-wal')) and not list(root.rglob('*-shm'))
    original = tree(root)
    output = tmp_path / 'golden'
    assert run(production, root, output) == 0
    assert tree(root) == original  # no byte, sidecar or migration written to the source

    selection = json.loads((output / 'selection.json').read_text())
    assert {(r['store_id'], r['period']) for r in selection['selected']} == {('s1', '2026-06'), ('s1', '2026-07'), ('s2', '2026-07')}
    assert {'store_id': 's2', 'period': '2026-06', 'reasons': ['manual_cost_lines']} in selection['excluded']
    assert selection['coverage']['missing_platforms'] == [] and selection['coverage']['cross_period_stores'] == ['s1']
    assert '999' not in json.dumps(selection['excluded'])
    manifest = json.loads((output / 'approved.json').read_text())
    assert manifest['target_scopes'] == ['s1/2026-06', 's1/2026-07', 's2/2026-07']
    decisions = json.loads((output / 'workspace-decisions.json').read_text())
    assert {(d['kind'], d['period'], d['values']['amount']) for d in decisions['decisions']} == {
        ('overhead_total', '2026-06', '0.00'), ('overhead_total', '2026-07', '0.00')}

    cases = {json.loads((output / row['path']).read_text())['case_id']: output / row['path'] for row in manifest['cases']}
    july = json.loads(cases['historical-s1-2026-07'].read_text())
    inputs, _fingerprint = frozen.verify((cases['historical-s1-2026-07'].parent / july['bundle']).resolve())
    assert {s['sha'] for s in inputs['slots'] if s['store_id'] == 's1'} == {before_slot}
    assert inputs['file_policy'] == 'close_time_slots' and july['source_evidence']['input_sha256'] == [before_slot]
    assert [s['operation'] for s in july['steps']] == ['recompute', 'drain_pending']
    june = json.loads(cases['historical-s1-2026-06'].read_text())
    assert june['steps'][-1]['operation'] == 'confirm' and june['steps'][-1]['payouts'] == [{'person_id': 'p1', 'amount': '8.00'}]
    scope = june['expected']['scopes']['s1/2026-06']
    assert scope['confirmed_total'] == 8.0 and scope['confirmed_people'] == {'p1': 8.0}
    assert scope['state'] == 'open' and scope['can_close'] is False and scope['cost_line_total'] == 0
    assert scope['statement']['net_profit'] == {'value': 160.0, 'available': True}
    assert json.loads(cases['historical-s2-2026-07'].read_text())['expected']['scopes']['s2/2026-07']['confirmed_people'] == {'p1': 7.5}

    attempt = gate.evaluate(production['model'], output / 'approved.json', target_period='2026-07')
    assert attempt['status'] == 'passed', json.dumps([attempt['issues'], [(c['case_id'], c['issues'], (c.get('result') or {}).get('differences'))
                                                     for c in attempt['cases']]], ensure_ascii=False, default=str)
    assert attempt['verified_scopes'] == manifest['target_scopes']
    corpus = tmp_path / 'corpus'
    corpus.mkdir()
    write_xlsx(corpus / 'sample.xlsx', [['a'], [1]])
    roots, rows = load('acceptance_inputs_for_builder', 'ledger_acceptance_inputs.py').capture(corpus, output / 'approved.json')
    assert {spec['kind'] for spec in roots.values()} == {'corpus_xlsx', 'frozen_bundle', 'explicit_files'}
    assert any(row['relative_path'] == 'workspace-decisions.json' for row in rows)


@pytest.mark.parametrize('mutation,reason', [
    ('manual_finance', 'manual_finance'),
    ('statement_review', 'statement_review_decisions'),
    ('feed_delta', 'feed_changes_after_close'),
    ('feed_snapshot', 'feed_snapshot_replaced'),
    ('file_bytes', 'close_time_file_changed'),
])
def test_unclean_store_months_are_excluded_with_reason_codes(production, tmp_path, mutation, reason):
    root = source_copy(production, tmp_path)
    db, feed = root / 'workspace.db', root / 'order-feed.db'
    if mutation == 'manual_finance':
        run_id = query(db, "SELECT run_id FROM period WHERE store_id='s1' AND period='2026-06'")
        execute(db, "INSERT INTO manual_finance VALUES(?,'s1','2026-06','{}','{}','x','2026-07-06T00:00:00+08:00','finance')", (run_id,))
    elif mutation == 'statement_review':
        execute(db, "INSERT INTO config_log(at,by,kind,summary,after_json) VALUES('2026-07-01','finance','statement-review','s1','{}')")
    elif mutation == 'feed_delta':
        execute(feed, "INSERT INTO feed_entity VALUES('order','late',11,1,'upsert','10','late',NULL,NULL,'{}')")
        execute(feed, 'UPDATE feed_state SET consumed_seq=11,source_latest_seq=11 WHERE id=1')
    elif mutation == 'feed_snapshot':
        execute(feed, "UPDATE feed_state SET snapshot_id='replaced' WHERE id=1")
    else:
        sha = query(db, "SELECT sha FROM slot WHERE store_id='s1'")
        (root / 'files' / sha[:2] / sha).write_bytes(b'changed after close')
    output = tmp_path / 'golden'
    code = run(production, root, output)
    selection = json.loads((output / 'selection.json').read_text())
    excluded = {(r['store_id'], r['period']): r['reasons'] for r in selection['excluded']}
    if mutation == 'manual_finance':
        assert excluded[('s1', '2026-06')] == ['manual_finance'] and ('s1', '2026-07') not in excluded
    else:
        assert all(reason in excluded[('s1', p)] for p in ('2026-06', '2026-07'))
    if mutation == 'feed_snapshot':
        assert code == 1 and not selection['selected'] and not (output / 'approved.json').exists()
    else:
        assert code == 0 and 's2' in {r['store_id'] for r in selection['selected']}


def test_explicit_store_selection_and_unsafe_requests_are_refused(production, tmp_path):
    root = source_copy(production, tmp_path)
    output = tmp_path / 'golden'
    assert run(production, root, output, '--stores', 's2') == 0
    selection = json.loads((output / 'selection.json').read_text())
    assert {r['store_id'] for r in selection['selected']} == {'s2'}
    assert {r['reasons'][0] for r in selection['clean_not_selected']} == {'not_requested'}
    assert run(production, root, root / 'golden') == 2
    assert builder.main(['--workspace-root', str(root), '--feed-root', str(production['feed']), '--model', str(production['model']),
        '--target-period', '2026-07', '--approval-reference', 'synthetic approval', '--output', str(tmp_path / 'other')]) == 2
    assert run(production, root, output) == 2  # never overwrite
    assert not (root / 'golden').exists() and not (tmp_path / 'other').exists()


LEGACY_ENGINE = 'a4bb7b2'


@pytest.fixture(scope='module')
def legacy_production(tmp_path_factory):
    """A source written entirely by the old engine: old schema, run.shas, run_labor."""
    import io
    import subprocess
    import sys
    import tarfile
    tmp = tmp_path_factory.mktemp('legacy')
    archive = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', LEGACY_ENGINE, 'ledger/ledger', 'models'],
                             capture_output=True)
    if archive.returncode:
        pytest.skip('legacy engine revision is not in this clone: ' + LEGACY_ENGINE)
    engine = tmp / 'engine'
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(engine, filter='data')
    (engine / 'VERSION').write_text(LEGACY_ENGINE)
    bundle, _missing = service_fixture_bundle(tmp, fully_priced=True)
    workspace = tmp / 'workspace'
    done = subprocess.run([sys.executable, str(Path(__file__).with_name('legacy_engine_fixture.py')), str(engine / 'ledger'),
                           str(bundle), str(workspace), '10'], capture_output=True, text=True, cwd=tmp)
    assert done.returncode == 0, done.stderr[-3000:]
    for path in list(workspace.rglob('*-wal')) + list(workspace.rglob('*-shm')):
        path.unlink()
    body = json.loads((bundle / 'model.json').read_text())
    body.update(id='cn-closed-history', name='历史月结模型', overheads=[])  # candidate has no 06/07 totals
    model = tmp / 'candidate-model.json'
    model.write_text(json.dumps(body, ensure_ascii=False))
    return {'workspace': workspace, 'feed': bundle / 'feed', 'model': model}


def test_legacy_schema_uses_version_history_original_engine_proof_and_run_labor(legacy_production, tmp_path):
    root = source_copy(legacy_production, tmp_path)
    db = root / 'workspace.db'
    assert query(db, "SELECT count(*) FROM sqlite_master WHERE name='automation_run_proof'") == 0
    old = query(db, "SELECT sha FROM slot WHERE store_id='s1'")
    raw = b'changed after close'
    sha = hashlib.sha256(raw).hexdigest()
    (root / 'files' / sha[:2]).mkdir(exist_ok=True)
    (root / 'files' / sha[:2] / sha).write_bytes(raw)
    execute(db, "INSERT INTO file VALUES(?,'甲店订单原表.xlsx',?,'2026-08-03T00:00:00+08:00')", (sha, len(raw)))
    execute(db, "INSERT INTO version(store_id,name,sha,at,by) VALUES('s1','甲店订单原表.xlsx',?,'2026-08-03T00:00:00+08:00','upload')", (sha,))
    execute(db, "UPDATE slot SET sha=? WHERE store_id='s1'", (sha,))
    original = tree(root)
    output = tmp_path / 'golden'
    assert run(legacy_production, root, output) == 0
    assert tree(root) == original

    selection = json.loads((output / 'selection.json').read_text())
    assert len(selection['selected']) == 4 and not selection['excluded']
    assert {r['slots_from'] for r in selection['selected']} == {'version_history_at_close+original_engine_run_shas'}
    fallbacks = selection['legacy_fallbacks']
    assert {'automation_run_proof', 'automation_overhead_close', 'automation_decision', 'period_cost_input'} <= set(
        fallbacks['missing_candidate_tables_treated_as_empty']['workspace'])
    assert set(fallbacks['company_basis_sources'].values()) == {'legacy_model_overheads_verified_by_run_labor'}
    decisions = json.loads((output / 'workspace-decisions.json').read_text())['decisions']
    assert sorted((d['period'], d['values']['amount']) for d in decisions if d['kind'] == 'overhead_total') == [
        ('2026-06', '10.00'), ('2026-07', '10.00')]
    manifest = json.loads((output / 'approved.json').read_text())
    case_path = next(output / row['path'] for row in manifest['cases'] if 's1' in row['path'] and '2026-07' in row['path'])
    case = json.loads(case_path.read_text())
    inputs, _fingerprint = frozen.verify((case_path.parent / case['bundle']).resolve())
    assert [s['sha'] for s in inputs['slots']] == [old] and inputs['close_time']['slots_from'].startswith('version_history_at_close')
    attempt = gate.evaluate(legacy_production['model'], output / 'approved.json', target_period='2026-07')
    assert attempt['status'] == 'passed', json.dumps([attempt['issues'], [(c['case_id'], c['issues'], (c.get('result') or {}).get('differences'))
                                                     for c in attempt['cases']]], ensure_ascii=False, default=str)


@pytest.mark.parametrize('mutation', ['version_gap', 'feed_changed', 'dirty_engine', 'labor_changed'])
def test_legacy_fallbacks_exclude_or_report_what_cannot_be_proved(legacy_production, tmp_path, mutation):
    root = source_copy(legacy_production, tmp_path)
    db, feed = root / 'workspace.db', root / 'order-feed.db'
    if mutation == 'version_gap':
        execute(db, "DELETE FROM version WHERE store_id='s2'")
    elif mutation == 'feed_changed':
        execute(feed, "INSERT INTO feed_entity VALUES('order','41',11,2,'delete','20','41',NULL,NULL,NULL)")
        execute(feed, 'UPDATE feed_state SET consumed_seq=11,source_latest_seq=11 WHERE id=1')
    elif mutation == 'dirty_engine':
        execute(db, "UPDATE run SET engine='a4bb7b2-dirty' WHERE store_id='s1' AND period='2026-06'")
    else:
        execute(db, 'DROP TRIGGER run_labor_no_update')
        execute(db, "UPDATE run_labor SET amount='4.99' WHERE store_id='s1' AND period='2026-06'")
    output = tmp_path / 'golden'
    run(legacy_production, root, output)
    selection = json.loads((output / 'selection.json').read_text())
    excluded = {(r['store_id'], r['period']): r['reasons'] for r in selection['excluded']}
    if mutation == 'version_gap':
        assert excluded == {('s2', p): ['close_time_slots_unproven'] for p in ('2026-06', '2026-07')}
    elif mutation == 'feed_changed':
        assert excluded == {('s2', p): ['legacy_inputs_not_reproduced'] for p in ('2026-06', '2026-07')}
    elif mutation == 'dirty_engine':
        assert excluded == {('s1', '2026-06'): ['legacy_engine_unavailable']}
    else:
        assert not excluded and len(selection['selected']) == 4
        assert selection['company_basis']['2026-06']['unproven_code'] == 'run_labor_not_reproduced'
        assert selection['workspace_decisions']['overhead_total_periods'] == ['2026-07']
