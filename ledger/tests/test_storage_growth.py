import copy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import polars as pl
import pytest

from ledger import service
from ledger.commission_engine import persist
from ledger.commission_registry import Registry
from ledger.commission_reports import _fill_report_slices
from ledger.period_snapshot import fingerprint
from ledger.run_artifacts import prepare
from ledger.snapshot_store import PREFIX, SCHEMA, install_reader, put, resolve
from ledger.workspace import Workspace


def settings(root, archive=None):
    policy = {'compact_snapshots': True, 'period_snapshot_reuse': True}
    if archive:
        policy.update(archive_root=str(archive), archive_all_completed=True,
                      run_directory_bundles=True)
    (root / 'storage-policy.json').write_text(json.dumps(policy), encoding='utf-8')


def payload():
    return {'can_close': True, 'store': '示例店', 'period': '2026-06',
            'cost_review': {'requires_human': False},
            'statement': [{'id': 'profit', 'value': 12.34, 'available': True}],
            'commission': {'calculation_id': 'immutable-id', 'total': 1.23,
                           'people': [], 'products': [
                {'product_id': str(i), 'name': '商品原文保持不变', 'total_rate': .03,
                 'people': [{'person_id': 'p', 'duty': 'produce'}]}
                for i in range(300)]}}


def test_compressed_results_read_slices_sql_and_frozen_reference_are_lossless(tmp_path):
    ws = Workspace(tmp_path)
    settings(tmp_path)
    original = payload()
    raw = json.dumps(original, ensure_ascii=False)
    rid = ws.record('s', '2026-06', original, [])
    with sqlite3.connect(tmp_path / 'workspace.db') as disk:
        stored = disk.execute('select result from run where id=?', (rid,)).fetchone()[0]
        assert stored.startswith(PREFIX)
        assert resolve(disk, stored) == raw
        assert disk.execute('select sum(length(payload)) from evidence_blob').fetchone()[0] < len(raw.encode()) / 2
    assert ws.latest_run('s', '2026-06')['result'] == raw
    assert ws.period_headers('s')[0]['run_id'] == rid
    assert ws.previous_gap_basis('s', '2026-07')['statement'] == original['statement']
    before = ws.close_period('s', '2026-06', by='test', note='freeze')
    frozen_bytes = hashlib.sha256(ws.latest_run('s', '2026-06')['result'].encode()).hexdigest()
    ws.record('s', '2026-06', {**original, 'can_close': False}, [])
    assert ws.state('s', '2026-06').run_id == before.run_id == rid
    assert ws.state('s', '2026-06').result == original
    assert hashlib.sha256(ws.conn.execute('select result from run where id=?', (rid,)).fetchone()[0].encode()).hexdigest() == frozen_bytes
    # Historical backfill and its SQL JSON expressions must work with a compact run.
    with ws.conn as c:
        c.execute('delete from run_report_slice where run_id=?', (rid,))
    assert _fill_report_slices(ws, '2026-06', '2026-06') == 1
    row = ws.conn.execute('select overview_json,products_slim_json from run_report_slice where run_id=?', (rid,)).fetchone()
    shown = json.loads(row['overview_json'])
    assert shown['statement'] == original['statement']
    assert shown['commission']['total'] == 1.23
    assert len(json.loads(row['products_slim_json'])) == 300


def test_commission_summary_uses_verified_blob_and_history_sql_still_reads_it(tmp_path):
    ws = Workspace(tmp_path)
    settings(tmp_path)
    registry = Registry(tmp_path)
    raw = json.dumps(payload()['commission'], ensure_ascii=False)
    persist(registry, 7, pl.DataFrame({'amount': [1.23]}), {
        'id': 'proof', 'store_id': 's', 'period': '2026-06', 'registry_revision': 0,
        'model_json': '{ "space": 2.00 }', 'rules_json': '{}', 'summary_json': raw,
    })
    with sqlite3.connect(registry.path) as c:
        assert c.execute('select summary_json from calculation').fetchone()[0].startswith(PREFIX)
    assert registry.calculation_evidence('proof')['summary_json'] == raw
    with registry.connect() as c:
        result = c.execute("select json_remove(snapshot_json(summary_json),'$.products','$.people') as summary_json from calculation").fetchone()[0]
        assert json.loads(result)['total'] == 1.23


def test_reader_does_not_hide_rolled_back_or_corrupt_evidence():
    c = sqlite3.connect(':memory:')
    c.executescript(SCHEMA)
    install_reader(c)
    ref = put(c, '{"original":"' + '甲' * 2000 + '"}')
    assert c.execute('select ? as result', (ref,)).fetchone()[0].startswith('{"original"')
    assert c.execute('select ? as name', (ref,)).fetchone()[0] == ref
    c.rollback()
    with pytest.raises(ValueError, match='missing'):
        c.execute('select ? as result', (ref,)).fetchone()
    ref = put(c, 'same bytes')
    c.commit()
    c.execute('drop trigger evidence_blob_no_update')
    c.execute("update evidence_blob set payload=x'00'")
    with pytest.raises(sqlite3.OperationalError, match='function'):
        c.execute('select snapshot_json(?)', (ref,)).fetchone()


def test_simultaneous_jobs_can_share_the_same_new_blob(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    database = tmp_path / 'shared.db'
    with sqlite3.connect(database) as c:
        c.execute('pragma journal_mode=wal')
        c.executescript(SCHEMA)
    seen = Barrier(2)
    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if sql.startswith('SELECT 1 FROM evidence_blob'):
                seen.wait(timeout=5)
            return cursor
    def save():
        c = sqlite3.connect(database, factory=Connection, timeout=5)
        try:
            with c:
                return put(c, 'identical valid evidence' * 100)
        finally:
            c.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(save); b = pool.submit(save)
        assert a.result(timeout=10) == b.result(timeout=10)
    with sqlite3.connect(database) as c:
        assert c.execute('select count(*) from evidence_blob').fetchone()[0] == 1


def slice_rows():
    return SimpleNamespace(facts=pl.DataFrame({'order_id': ['a', 'b'], 'amount': [10., -10.],
                                              'quantity': [1, 1], 'file_sha': ['source', 'source']}),
                           pricing_gaps=pl.DataFrame(), coverage_gap_rows=pl.DataFrame(),
                           allocation_evidence=pl.DataFrame({'order_id': ['a'], 'weight': [.5]}),
                           order_issue_rows=pl.DataFrame())


def test_period_identity_ignores_only_random_calculation_id():
    sl = slice_rows(); p = payload()
    identity = fingerprint('clean-release', 'model', '2026-06', p, sl)
    same = copy.deepcopy(p); same['commission']['calculation_id'] = 'another-id'
    assert fingerprint('clean-release', 'model', '2026-06', same, sl) == identity
    assert fingerprint('release-dirty', 'model', '2026-06', same, sl) is None
    assert fingerprint('unknown', 'model', '2026-06', same, sl) is None
    for field in ('facts', 'allocation_evidence', 'pricing_gaps', 'coverage_gap_rows', 'order_issue_rows'):
        changed = copy.copy(sl)
        setattr(changed, field, pl.DataFrame({'order_id': ['a'], 'changed': [True]}))
        assert fingerprint('clean-release', 'model', '2026-06', same, changed) != identity
    changed = copy.copy(sl)
    changed.facts = sl.facts.with_columns(pl.Series('amount', [11., -11.]))
    assert changed.facts['amount'].sum() == sl.facts['amount'].sum()
    assert fingerprint('clean-release', 'model', '2026-06', same, changed) != identity
    changed = copy.deepcopy(same); changed['commission']['total'] += .01
    assert fingerprint('clean-release', 'model', '2026-06', changed, sl) != identity


def test_new_run_bundle_keeps_one_link_and_all_sealed_evidence(tmp_path):
    ws = Workspace(tmp_path / 'home'); archive = tmp_path / 'cold'
    settings(ws.root, archive)
    rid = ws.record('s', '2026-06', {'can_close': True}, [], evidence_ready=False)
    sl = slice_rows(); sl.pricing_gaps = pl.DataFrame({'order_id': ['pending']})
    service._keep_facts(ws, rid, sl)
    folder = ws.root / 'runs' / str(rid)
    assert folder.is_symlink()
    assert len([p for p in (ws.root / 'runs').iterdir() if p.is_symlink()]) == 1
    from ledger.storage_integrity import verified
    assert verified(ws.facts_path(rid))
    assert verified(ws.pricing_gaps_path(rid))
    assert verified(ws.facts_path(rid).with_suffix('.allocation.parquet'))
    assert pl.read_parquet(ws.facts_path(rid)).equals(sl.facts)
    assert ws.latest_run('s', '2026-06')['evidence_ready'] == 1
    with sqlite3.connect(ws.root / 'storage.db') as c:
        assert c.execute('select source,state from artifact_archive').fetchall() == [(f'runs/{rid}', 'archived')]
    # A broken bundle retains the original identity instead of looking like an empty flat run.
    folder.resolve().rename(tmp_path / 'offline')
    assert ws.facts_path(rid).parent == folder
    assert not ws.facts_path(rid).exists()


def test_bundle_link_setup_failure_retains_local_evidence_and_never_moves_old_runs(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / 'home'); settings(ws.root, tmp_path / 'cold')
    rid = ws.record('s', '2026-06', {'can_close': True}, [], evidence_ready=False)
    monkeypatch.setattr('pathlib.Path.symlink_to', lambda *a, **k: (_ for _ in ()).throw(OSError('offline')))
    service._keep_facts(ws, rid, slice_rows())
    assert ws.facts_path(rid) == ws.root / 'runs' / f'{rid}.parquet'
    assert pl.read_parquet(ws.facts_path(rid)).height == 2
    prepare(ws, rid)
    assert ws.facts_path(rid).exists()


def test_unavailable_archive_metadata_falls_back_before_any_bundle_write(tmp_path, monkeypatch):
    from pathlib import Path
    ws = Workspace(tmp_path / 'home'); archive = tmp_path / 'cold'
    settings(ws.root, archive)
    rid = ws.record('s', '2026-06', {'can_close': True}, [], evidence_ready=False)
    original = Path.resolve
    def offline(path, *args, **kwargs):
        if path == archive:
            raise PermissionError('NAS credentials unavailable')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', offline)
    service._keep_facts(ws, rid, slice_rows())
    assert ws.latest_run('s', '2026-06')['evidence_ready'] == 1
    assert ws.facts_path(rid) == ws.root / 'runs' / f'{rid}.parquet'
    assert pl.read_parquet(ws.facts_path(rid)).height == 2


def test_unrelated_store_input_changes_reuse_identical_period_evidence(tmp_path, monkeypatch):
    from dataclasses import replace
    from test_recompute_reuse import setup_calculation
    from ledger import workspace as workspace_module
    ws, model, result, registry, calls = setup_calculation(tmp_path, monkeypatch)
    settings(ws.root)
    monkeypatch.setattr(service, 'engine_version', lambda: 'verified-release')
    monkeypatch.setattr(workspace_module, 'engine_version', lambda: 'verified-release')
    first = service.recompute(ws, model, model.store('s'))
    assert first.failure is None
    item = result.ingestion.items[0]
    result.ingestion.items[0] = replace(item, ref=replace(item.ref, sha256='f' * 64))
    second = service.recompute(ws, model, model.store('s'))
    assert second.failure is None and len(calls) == 2
    assert [p['run_id'] for p in second.periods] == [p['run_id'] for p in first.periods]
