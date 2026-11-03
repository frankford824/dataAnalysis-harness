"""Build approved historical_golden cases from frozen CLOSED store-months.

Usage (read-only against a backup copy of the production workspace)::

    ledger/.venv/bin/python tools/build-historical-golden.py \
      --workspace-root /backup/ledger --feed-root /backup/ledger-feed \
      --model models/cn-ecommerce --target-period 2026-07 \
      --approval-reference "<owner approval text>" --output /evidence/historical-202607 \
      [--per-platform 2] [--stores store_a,store_b]

The output directory is new and receives approved.json, cases/*.json,
bundles/<case>/, workspace-decisions.json (when frozen company bases exist) and
selection.json (selected store-months and reason codes for every exclusion; no
amounts). Verify it with tools/verify-ledger-service-cases.py.

Source access: every SQLite file is opened through a mode=ro URI (immutable=1
when no -wal sidecar exists, so no sidecar is created), compressed JSON cells
expand through snapshot_store.install_reader, and no Workspace/Registry object
is constructed on the source. Output may not be inside a source root.

One case per store-month, because a store's close-time slot set differs between
months. Each bundle is freight_experiments_inputs.capture() over a private
staging root that exposes the slot set recorded in the frozen run's proof (or a
later equivalence validation) instead of today's slots, plus the current order
feed, admitted only when the store-scoped feed state is provably the close-time
state (same rule as feed_freshness.evaluate, without the code identity check).

Older engines (e.g. a4bb7b2) lack the candidate journal tables; each missing
table is an empty TEMP table on the read-only connection and selection.json
lists them. Their close-time slots are replayed from the append-only version log
at period.at_version (then run.at), restricted to and checked against run.shas.
Because old feed items enter run.shas as content digests, the run's own engine
revision (run.engine, extracted from git history) re-ingests those slots plus
today's feed in a subprocess (tools/ledger_legacy_inputs.py); every hash must
equal run.shas, which also proves the store-scoped feed state. Company totals
come from the frozen runs' own models (by run.model_revision) and labor-overhead
edits, accepted only when overhead.allocate reproduces every run_labor share.

Expected values are copied from frozen production data only: statement,
commission, allocation_pending_count, source_sync_pending from the frozen
run.result; confirmed payouts from the latest registry payout_confirmation of
the frozen run (re-entered through a confirm step); cost_line_total through
replay_service._posted_cost_line_total on the frozen source. state is 'open'
(no close step: closing needs every operating store in one case) and can_close
is False only where the code guarantees it in an isolated store-month replay
(another operating store has no revenue, or the overhead total is unknown);
otherwise the store-month is excluded rather than guessed.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'ledger'))
from ledger import freight_experiments_inputs as frozen
from ledger import replay_service, run_artifacts, snapshot_store, store_classification
from ledger.commission_registry import local_time
from ledger.feed_freshness import ACCOUNTING_ENTITIES, BOUNDARY_SCHEMA
from ledger.freight_trial import _write_json
from ledger.overhead import allocate
from ledger.snapshot_store import install_reader

_spec = importlib.util.spec_from_file_location('service_cases_gate', REPO / 'tools/verify-ledger-service-cases.py')
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

SHARED = '__shared__'
REQUIRED_NODES = ('n_receipt', 'gross_profit', 'net_profit')
EXTERNAL_VERSION_NAMES = {'__order_console__', '__commission_rules__'}
HEX = re.compile('[0-9a-f]{64}')
BEIJING = timezone(timedelta(hours=8))
LIMITATIONS = [
    'feed_current_state_admitted_only_when_store_scoped_equivalent_to_close_time',
    'feed_mapping_payload_and_other_store_deltas_are_current_state',
    'order_components_support_files_are_current_state_not_versioned',
    'registry_people_members_org_stores_are_current_state_scheme_versions_and_policy_as_of_frozen_calculation',
    'expected_values_reflect_production_engine_and_model_at_close_candidate_changes_show_as_differences',
    'can_close_false_is_isolated_store_month_replay_semantics_not_production_readiness',
    'legacy_feed_equivalence_is_proved_on_the_original_engine_projection_candidate_may_read_more_feed_columns',
    'legacy_slot_names_come_from_version_history_only_file_identities_are_proved_by_run_shas',
    'legacy_store_scope_is_period_rows_first_computed_before_the_last_close_of_the_month',
    'profit_exclusion_rows_are_display_only_and_not_replayed_confirmed_payouts_are_restored_verbatim',
]


class BuildError(RuntimeError):
    pass


def _sha_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def candidate_schema():
    """Candidate DDL, created only in a disposable directory."""
    from ledger.commission_registry import Registry
    from ledger.order_feed import OrderFeed
    from ledger.workspace import Workspace
    with tempfile.TemporaryDirectory(prefix='historical-golden-schema-') as temporary:
        root = Path(temporary)
        Workspace(root).close();Registry(root);OrderFeed(root, feed_root=root)
        schema = {}
        for key, path in (('workspace', root / 'workspace.db'), ('registry', root / 'commission' / 'registry.db'),
                          ('order_feed', root / 'order-feed.db')):
            with closing(sqlite3.connect(path)) as conn:
                schema[key] = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' "
                                                "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"))
    return schema


def connect(path, *, begin=True, schema=None, missing=None):
    """Read-only, never creating WAL/SHM sidecars on a quiescent backup copy.

    Candidate tables absent from an older engine's file become empty TEMP tables:
    the temp schema is private to this connection and never touches the source.
    """
    path = Path(path).resolve()
    if not path.is_file():
        raise BuildError('缺少只读来源库：' + str(path))
    wal = Path(str(path) + '-wal').exists()
    conn = sqlite3.connect(path.as_uri() + '?mode=ro' + ('' if wal else '&immutable=1'), uri=True, timeout=5)
    install_reader(conn)
    present = _tables(conn)
    for name, sql in sorted((schema or {}).items()):
        if name not in present:
            conn.execute(re.sub(r'^\s*CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?', 'CREATE TEMP TABLE ', sql, flags=re.I))
            if missing is not None:
                missing.append(name)
    conn.execute('PRAGMA query_only=1')
    if begin:
        conn.execute('BEGIN')
    return conn


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]


def _instant(value):
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=BEIJING)


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float, Decimal))


def _slug(text):
    return re.sub(r'[^A-Za-z0-9_.-]', '_', text)[:60] + '-' + hashlib.sha256(text.encode()).hexdigest()[:8]


def _money(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount != amount.quantize(Decimal('.01')):
        raise BuildError('冻结金额不是精确到分的有限数值，不能四舍五入改写')
    return format(amount.quantize(Decimal('.01')), 'f')


class Source:
    """One consistent read transaction per source database."""

    def __init__(self, root, feed_root, model, scratch):
        self.root, self.feed_root, self.model, self.scratch = Path(root).resolve(), Path(feed_root).resolve(), model, Path(scratch)
        schema = candidate_schema()
        self.missing = {key: [] for key in schema}
        self.db = connect(self.root / 'workspace.db', schema=schema['workspace'], missing=self.missing['workspace'])
        self.registry = connect(self.root / 'commission' / 'registry.db', schema=schema['registry'], missing=self.missing['registry'])
        self.feed = connect(self.root / 'order-feed.db', schema=schema['order_feed'], missing=self.missing['order_feed'])
        self.file_checks, self.models, self.engines = {}, {}, {}
        self.file_shas = {r[0] for r in self.db.execute('SELECT sha FROM file')}
        self._feed_copy = None

    def close(self):
        for conn in (self.db, self.registry, self.feed):
            conn.close()

    def production_model(self, revision, run_id=None):
        """The exact model JSON of a frozen run: content-addressed by run.model_revision."""
        if revision not in self.models or (self.models[revision] is None and run_id):
            text = None
            if isinstance(revision, str) and HEX.fullmatch(revision):
                # Aliased column: the row factory leaves a compressed reference unexpanded.
                raws = [r[0] for r in self.registry.execute('SELECT model_json AS raw FROM calculation WHERE finance_run=?', (run_id,))] if run_id else []
                raws.append(json.dumps({'$ledger_snapshot_v1': revision}, separators=(',', ':')))
                for raw, conn in ((raw, conn) for raw in raws for conn in (self.registry, self.db)):
                    try:
                        candidate = snapshot_store.resolve(conn, raw)
                    except ValueError:
                        continue
                    if hashlib.sha256(candidate.encode('utf-8')).hexdigest() == revision:
                        text = candidate;break
            self.models[revision] = text
        return self.models[revision]

    def engine(self, revision):
        """The run's own engine revision, extracted from this repository's git history."""
        if revision not in self.engines:
            self.engines[revision] = None
            if isinstance(revision, str) and re.fullmatch('[0-9a-f]{7,40}', revision):
                found = subprocess.run(['git', '-C', str(REPO), 'rev-parse', '--verify', '--quiet', revision + '^{commit}'],
                                       capture_output=True, text=True)
                if found.returncode == 0:
                    target = self.scratch / 'engines' / found.stdout.strip()
                    archive = subprocess.run(['git', '-C', str(REPO), 'archive', '--format=tar', found.stdout.strip(),
                                              'ledger/ledger', 'models'], capture_output=True)
                    if archive.returncode == 0:
                        target.mkdir(parents=True)
                        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
                            tar.extractall(target, filter='data')
                        (target / 'VERSION').write_text(revision, encoding='utf-8')
                        self.engines[revision] = target / 'ledger'
        return self.engines[revision]

    def feed_copy(self):
        """Consistent private copy of order-feed.db; the source file is only read."""
        if self._feed_copy is None:
            target = self.scratch / 'order-feed.db'
            source, copy = connect(self.root / 'order-feed.db', begin=False), sqlite3.connect(target)
            try:
                source.backup(copy)
                staged = copy.execute('SELECT snapshot_id,consumed_seq FROM feed_state WHERE id=1').fetchone()
            finally:
                source.close();copy.close()
            current = self.feed.execute('SELECT snapshot_id,consumed_seq FROM feed_state WHERE id=1').fetchone()
            if not current or tuple(staged) != tuple(current):
                raise BuildError('订单台来源在读取期间发生变化；请使用静止的备份副本')
            self._feed_copy = target
        return self._feed_copy

    def file_state(self, sha):
        """Original bytes must still hash to the slot identity."""
        if sha not in self.file_checks:
            path = self.root / 'files' / sha[:2] / sha
            self.file_checks[sha] = (None if path.is_file() and _sha_file(path) == sha
                                     else 'close_time_file_changed' if path.is_file() else 'close_time_file_missing')
        return self.file_checks[sha]


def frozen_basis(src, periods):
    """Company scope/total per month: the candidate journal, else the legacy derivation."""
    result, codes = {}, {}
    for period in periods:
        row = src.db.execute('SELECT * FROM automation_overhead_close WHERE period=? ORDER BY rowid DESC LIMIT 1',
                             (period,)).fetchone()
        if row:
            basis = json.loads(row['basis_json'])
            result[period] = {'total': row['total'], 'store_ids': [str(r['store_id']) for r in basis],
                              'unverified_revenue': any(r.get('revenue') is None for r in basis),
                              'source': 'automation_overhead_close'}
            continue
        basis, codes[period] = legacy_basis(src, period)
        if basis:
            result[period] = basis
    return result, codes


def legacy_basis(src, period):
    """Old engines froze only per-store run_labor; the total must re-derive every share.

    Candidate totals are the frozen runs' own production models (resolved by
    run.model_revision) and the latest labor-overhead edit before the last close.
    They must agree and, through overhead.allocate over the same store basis as the
    old labor_api.share_at_close, reproduce every closed store's run_labor to the cent.
    """
    rows = _rows(src.db, '''SELECT p.store_id,p.state,p.run_id,p.changed_at,r.model_revision,
        coalesce(mf.result_json,r.result) AS result,
        (SELECT min(x.at) FROM run x WHERE x.store_id=p.store_id AND x.period=p.period) AS first_at
        FROM period p LEFT JOIN run r ON r.id=CASE WHEN p.state='closed' AND p.run_id IS NOT NULL THEN p.run_id
        ELSE (SELECT max(id) FROM run rr WHERE rr.store_id=p.store_id AND rr.period=p.period) END
        LEFT JOIN manual_finance mf ON mf.run_id=r.id AND p.state='closed'
        WHERE p.period=? ORDER BY p.store_id''', (period,))
    closed = [r for r in rows if r['state'] == 'closed' and r['run_id']]
    if not closed:
        return None, 'no_closed_store_month'
    last_close = max(_instant(r['changed_at']) for r in closed)
    scope = [r for r in rows if r['first_at'] and _instant(r['first_at']) <= last_close]
    base = {'total': None, 'store_ids': [r['store_id'] for r in scope], 'unverified_revenue': False,
            'source': 'legacy_period_rows'}
    values, revenue_nodes = [], set()
    for r in closed:
        text = src.production_model(r['model_revision'], r['run_id'])
        if text is None:
            return base, 'legacy_model_unavailable'
        body = json.loads(text)
        entries = [o for o in body.get('overheads') or [] if o.get('period') == period]
        values.append(Decimal(str(entries[0]['amount'])) if len(entries) == 1 else None)
        revenue_nodes.add(next((n['id'] for n in body.get('statement') or [] if n.get('headline') == 'revenue'), ''))
    for log in src.db.execute("SELECT at,after_json FROM config_log WHERE kind='labor-overhead' ORDER BY id DESC"):
        after = json.loads(log['after_json'] or '{}')
        if after.get('period') == period and _instant(log['at']) <= last_close:
            values.append(Decimal(str(after.get('amount'))));break
    if set(values) == {None}:
        return base, 'overhead_total_unconfigured'
    if len(set(values)) != 1:
        return base, 'overhead_sources_disagree'
    total = values[0]
    if not total.is_finite() or total != total.quantize(Decimal('.01')):
        return base, 'overhead_total_not_cent_exact'
    if len(revenue_nodes) != 1 or not next(iter(revenue_nodes)):
        return base, 'revenue_node_ambiguous'
    node = next(iter(revenue_nodes))
    basis = []
    for r in scope:
        row = next((n for n in json.loads(r['result'] or '{}').get('statement') or [] if n.get('id') == node), None)
        basis.append((r['store_id'], (row.get('value') if row and row.get('available', True) else None) or 0))
    spread = allocate(period, float(total), basis)
    labor = {r['run_id']: r['amount'] for r in src.db.execute('SELECT run_id,amount FROM run_labor WHERE period=?', (period,))}
    for r in closed:
        frozen_share = labor.get(r['run_id'])
        if frozen_share is None or Decimal(str(frozen_share)) != Decimal(str(spread.of(r['store_id']))).quantize(Decimal('.01')):
            return base, 'run_labor_not_reproduced'
    return {**base, 'total': format(total.quantize(Decimal('.01')), 'f'),
            'source': 'legacy_model_overheads_verified_by_run_labor'}, None


def workspace_decisions(model, basis, approval):
    decisions = []
    for period, frozen_row in sorted(basis.items()):
        if frozen_row['total'] is not None:
            decisions.append({'period': period, 'store_id': '', 'kind': 'overhead_total',
                'values': {'amount': _money(frozen_row['total'])},
                'reason': '恢复已冻结公司兼职总额（' + frozen_row['source'] + '）' + period + '；依据：' + approval})
        for store in model.stores:
            if store.id not in frozen_row['store_ids']:
                decisions.append({'period': period, 'store_id': store.id, 'kind': 'store_scope',
                    'values': {'included': False, 'valid_from': period, 'valid_until': period},
                    'reason': '不在 ' + period + ' 已冻结公司营业基准内；依据：' + approval})
    return decisions


def replay_scope(model, decisions, period):
    """Mirror automation_checker.operating_stores inside a fresh replay workspace."""
    excluded = {d['store_id'] for d in decisions if d['kind'] == 'store_scope' and d['values']['included'] is False
                and d['values']['valid_from'] <= period <= d['values']['valid_until']}
    current_month = datetime.now(BEIJING).strftime('%Y-%m')
    return [s.id for s in model.stores if s.id not in excluded and not (s.archived and period >= current_month)]


def gate_scope(model, decisions, period):
    return replay_scope(model, decisions, period) if decisions else [s.id for s in model.stores if not s.archived]


def feed_reason(src, store_id, manifest):
    """Data half of feed_freshness.evaluate: is today's store view the close-time one?"""
    boundary = manifest.get('feed_boundary')
    if not isinstance(boundary, dict) or boundary.get('schema') != BOUNDARY_SCHEMA or boundary.get('store_id') != store_id:
        return 'feed_boundary_missing'
    ids, captured, required = boundary.get('order_store_ids'), boundary.get('captured_through_seq'), boundary.get('required_through_seq')
    if (not isinstance(ids, list) or not ids or type(captured) is not int or type(required) is not int
            or captured < required or not isinstance(boundary.get('snapshot_id'), str)):
        return 'feed_boundary_missing'
    state = src.feed.execute('SELECT * FROM feed_state WHERE id=1').fetchone()
    if not state or state['last_error'] or not state['snapshot_id']:
        return 'feed_unavailable'
    if state['snapshot_id'] != boundary['snapshot_id']:
        return 'feed_snapshot_replaced'
    if type(state['consumed_seq']) is not int or state['consumed_seq'] < captured:
        return 'feed_behind_close'
    mapping = sorted(str(r[0]) for r in src.feed.execute(
        "SELECT order_store_id FROM feed_store WHERE ledger_store_id=? AND mapping_status='confirmed'", (store_id,)))
    if mapping != sorted(ids):
        return 'feed_mapping_changed'
    later = src.feed.execute('SELECT 1 FROM feed_entity WHERE seq>? AND entity_type IN (%s) AND (order_store_id IN (%s) '
        "OR order_store_id IS NULL OR order_store_id='') LIMIT 1" % (','.join('?' * len(ACCOUNTING_ENTITIES)), ','.join('?' * len(mapping))),
        [captured, *sorted(ACCOUNTING_ENTITIES), *mapping]).fetchone()
    pending = src.feed.execute('SELECT 1 FROM feed_pending_store WHERE ledger_store_id=? AND seq>?',
                               (store_id, captured)).fetchone()
    return 'feed_changes_after_close' if later or pending else None


def legacy_slots(src, store_id, row):
    """Slot assignment replayed from the append-only version log, proved by run.shas.

    keep() appends a row only when a (store, name) gets a different sha; forget()
    appends the removed slot's own sha. A row repeating the live sha is therefore a
    removal. Exclusive shared replacement deletes without a row, so the result is
    accepted only when the live slots holding the run's files are exactly run.shas.
    Anchors: period.at_version (close), then run.at (calculation time).
    """
    read = {sha for sha in json.loads(row['shas'] or '[]') if sha in src.file_shas}
    if not read:
        return None, None, 'no_close_time_files'
    history = _rows(src.db, 'SELECT id,store_id,name,sha,at FROM version WHERE store_id IN (?,?) ORDER BY id', (store_id, SHARED))
    computed = _instant(row['at'])
    anchors = [('version_history_at_close', lambda r: r['id'] <= (row['at_version'] or 0)),
               ('version_history_at_run', lambda r: _instant(r['at']) <= computed)]
    code = 'close_time_slots_unproven'
    for origin, included in anchors:
        live = {}
        for r in history:
            if r['name'] in EXTERNAL_VERSION_NAMES or not included(r):
                continue
            key = (r['store_id'], r['name'])
            if live.get(key) == r['sha']:
                del live[key]
            else:
                live[key] = r['sha']
        slots = [{'store_id': s, 'name': n, 'sha': h} for (s, n), h in sorted(live.items()) if h in read]
        if {s['sha'] for s in slots} != read:
            continue
        names = defaultdict(set)
        for s in slots:
            names[s['sha']].add((s['store_id'], s['name']))
        if any(len({n for _s, n in owners}) > 1 and any(s == SHARED for s, _n in owners) for owners in names.values()):
            code = 'close_time_slot_names_ambiguous'
            continue
        return slots, origin, None
    return None, None, code


def reproduce_inputs(src, record, engine, model_text):
    """Run the frozen run's own engine over the reconstructed slots and today's feed."""
    work = Path(tempfile.mkdtemp(dir=src.scratch, prefix='legacy-inputs-'))
    (work / 'model.json').write_text(model_text, encoding='utf-8')
    request = {'engine': str(engine), 'scratch': str(work / 'root'), 'model': str(work / 'model.json'),
        'store_id': record['store_id'], 'feed_db': str(src.feed_copy()), 'feed_root': str(src.feed_root),
        'cache': str(src.scratch / 'engines' / (engine.parent.name + '-parse-cache')),
        'support': [str(src.root / f) for f in ('order-components',) if (src.root / f).is_dir()],
        'slots': [{**s, 'path': str(src.root / 'files' / s['sha'][:2] / s['sha'])} for s in record['slots']]}
    (work / 'request.json').write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if not k.startswith('LEDGER_')}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    done = subprocess.run([sys.executable, str(REPO / 'tools' / 'ledger_legacy_inputs.py'), str(work / 'request.json')],
                          capture_output=True, text=True, cwd=work, env=env)
    shutil.rmtree(work / 'root', ignore_errors=True)
    if done.returncode != 0:
        return 'legacy_engine_failed'
    try:
        computed = json.loads(done.stdout.strip().splitlines()[-1])['shas']
    except (ValueError, IndexError, KeyError, TypeError):
        return 'legacy_engine_failed'
    frozen_shas = {s for s in json.loads(record['run_shas']) if HEX.fullmatch(s)}
    return None if {s for s in computed if HEX.fullmatch(s)} == frozen_shas else 'legacy_inputs_not_reproduced'


def frozen_refund_reason(src, run_id, manifest):
    """Closed earlier months can exclude refunds; an isolated replay has no such month."""
    if manifest.get('frozen_refund_artifacts'):
        return 'frozen_refund_dependency'
    path = run_artifacts.path(src.root, run_id)
    try:
        if not path.is_file():
            return 'frozen_facts_unavailable'
        import polars as pl
        scan = pl.scan_parquet(path)
        if 'statement_evidence' not in scan.collect_schema():
            return None
        values = scan.filter(pl.col('statement_evidence').str.contains('frozen_refund', literal=True)).select(
            'statement_evidence').collect()['statement_evidence'].to_list()
    except Exception:
        return 'frozen_facts_unavailable'
    for raw in values:
        try:
            status = (json.loads(raw).get('frozen_refund') or {}).get('status')
        except (ValueError, TypeError, AttributeError):
            return 'frozen_facts_unavailable'
        if status in {'excluded', 'pending'}:
            return 'frozen_refund_dependency'
    return None


def registry_snapshot(src, store_id, period, calculation, as_of):
    """As-of-calculation configuration: used scheme versions and policies recorded by then."""
    reg = src.registry
    meta = _rows(reg, 'SELECT * FROM meta ORDER BY id')
    people = _rows(reg, 'SELECT * FROM person ORDER BY id')
    members = _rows(reg, 'SELECT * FROM store_member WHERE store_id=? ORDER BY person_id,valid_from,id', (store_id,))
    org_stores = _rows(reg, 'SELECT * FROM org_store WHERE store_id=? ORDER BY person_id', (store_id,))
    schemes = _rows(reg, 'SELECT * FROM scheme WHERE store_id=? ORDER BY product_id,id', (store_id,))
    rules = json.loads(calculation['rules_json']) if calculation else {'relationships': [], 'policy': None}
    used = {v['scheme_id']: v['id'] for v in rules.get('relationships') or [] if v.get('scheme_id')}
    versions = []
    for version_id in sorted(set(used.values())):
        row = reg.execute('SELECT * FROM scheme_version WHERE id=?', (version_id,)).fetchone()
        if row:
            versions.append(dict(row))
    known = {s['id'] for s in schemes}
    if len(versions) != len(used) or set(used) - known or any(v['scheme_id'] not in known for v in versions):
        return None, 'commission_versions_unresolved'
    for scheme in schemes:
        scheme['active_version'] = used.get(scheme['id'])
    policies = [p for p in _rows(reg, "SELECT * FROM policy_version WHERE store_id IN ('',?) ORDER BY id", (store_id,))
                if _instant(p['recorded_at']) <= as_of]
    eligible = [p for p in policies if local_time(p['effective_from']) <= period + '-01T00:00:00']
    eligible.sort(key=lambda p: (p['store_id'] != '', local_time(p['effective_from']), p['id']), reverse=True)
    if (eligible[0]['id'] if eligible else None) != ((rules.get('policy') or {}).get('id')):
        return None, 'commission_policy_unresolved'
    after = _rows(reg, 'SELECT * FROM meta ORDER BY id')
    return {'schema': 1, 'kind': 'readonly_registry_configuration_snapshot', 'read_only_sqlite': True,
        'transaction': 'BEGIN read snapshot', 'meta': meta, 'meta_after': after,
        'captured_at': datetime.now(BEIJING).isoformat(timespec='seconds'), 'stores': [store_id],
        'people': people, 'members': members, 'org_stores': org_stores, 'schemes': schemes,
        'scheme_versions': versions, 'policies': policies,
        'as_of': {'kind': 'frozen_calculation_relationships', 'calculation_id': calculation['id'] if calculation else None,
                  'at': as_of.isoformat(), 'active_versions_from': 'calculation.rules_json' if calculation else 'none'}}, None


def _seed(snapshot, directory):
    path = Path(directory) / 'registry-snapshot.json'
    path.write_text(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=1), encoding='utf-8')
    reference = {'path': str(path.resolve()), 'sha256': _sha_file(path)}
    return path, reference, replay_service.registry_seed_from_snapshot(reference)


def expected_scope(payload, confirmation, cost_line_total):
    commission = payload.get('commission') or {}
    return {'state': 'open',
        'statement': {r['id']: {'value': r.get('value'), 'available': r.get('available')} for r in payload.get('statement') or []},
        'commission_total': commission.get('total'),
        'commission_complete': commission.get('amount_complete'),
        'can_close': False,
        'source_sync_pending': payload.get('source_sync_pending'),
        'commission_people': {p['person_id']: {'base': p.get('base'), 'amount': p.get('amount')} for p in commission['people']}
            if isinstance(commission.get('people'), list) else None,
        'confirmed_total': float(confirmation['confirmed_total']) if confirmation else None,
        'confirmed_people': {p['person_id']: p['amount'] for p in confirmation['payouts']} if confirmation else {},
        'cost_line_total': cost_line_total,
        'allocation_pending_count': payload.get('allocation_pending_count')}


def assess(src, row, decisions):
    """Every applicable reason code; an empty list means the store-month is clean."""
    model, db = src.model, src.db
    sid, period, run_id = row['store_id'], row['period'], row['rid']
    record = {'store_id': sid, 'period': period, 'run_id': run_id, 'reasons': []}
    reasons = record['reasons']
    stores = {s.id: s for s in model.stores}
    if sid not in stores:
        return {**record, 'reasons': ['store_not_in_candidate_model']}
    if gate.FIXTURE.search(sid):
        reasons.append('identity_matches_fixture_pattern')
    if not run_id:
        return {**record, 'reasons': reasons + ['frozen_run_missing']}
    if not row['evidence_ready'] or row['evidence_error']:
        reasons.append('frozen_evidence_not_ready')
    payload = json.loads(row['result'])
    if db.execute('SELECT 1 FROM manual_finance WHERE run_id=?', (run_id,)).fetchone():
        reasons.append('manual_finance')
    for log in db.execute("SELECT before_json FROM config_log WHERE kind='manual-close' AND summary=?", (f'人工结账 {sid} {period}',)):
        if json.loads(log['before_json'] or '{}').get('run_id') == run_id:
            reasons.append('manual_close_ignored_blockers');break
    if db.execute("SELECT 1 FROM config_log WHERE kind='statement-review' AND summary=? LIMIT 1", (sid,)).fetchone():
        reasons.append('statement_review_decisions')
    if db.execute('SELECT 1 FROM cost_line_log WHERE store_id=? AND period=? LIMIT 1', (sid, period)).fetchone():
        reasons.append('manual_cost_lines')
    kinds = {r[0] for r in db.execute('SELECT DISTINCT kind FROM automation_decision WHERE store_id=? AND period=?', (sid, period))}
    if 'period_cost_input' in kinds or payload.get('period_cost_input'):
        reasons.append('period_cost_input')
    if kinds & {'history_cost_single_component.v1', 'history_cost_source_facts.v2', 'jst_physical_source_cost_facts.v3'}:
        reasons.append('workspace_source_facts')
    proof = db.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?', (run_id,)).fetchone()
    validations = [json.loads(r[0]) for r in db.execute(
        'SELECT manifest_json FROM automation_run_validation WHERE run_id=? ORDER BY rowid DESC', (run_id,))]
    proof = json.loads(proof[0]) if proof else None
    if proof is not None:
        if proof.get('approved_cost_sources'):
            reasons.append('workspace_source_facts')
        if proof.get('verified_original_date_sources'):
            reasons.append('saved_source_dates')
    refund = frozen_refund_reason(src, run_id, proof or {})
    if refund:
        reasons.append(refund)
    legacy = None
    if proof is None and not validations:
        slots, origin, code = legacy_slots(src, sid, row)
        if code:
            reasons.append(code)
        else:
            record.update(slots=slots, manifest_origin=origin, feed_boundary=None)
        legacy = {'engine': src.engine(row['engine']), 'model': src.production_model(row['model_revision'], run_id)}
        if legacy['engine'] is None:
            reasons.append('legacy_engine_unavailable')
        if legacy['model'] is None:
            reasons.append('legacy_model_unavailable')
    else:
        chosen, feed_codes = None, []
        for manifest, origin in [*((v, 'automation_run_validation') for v in validations), *([(proof, 'automation_run_proof')] if proof else [])]:
            code = feed_reason(src, sid, manifest) if isinstance(manifest.get('slots'), list) else 'close_time_slots_unproven'
            if code is None:
                chosen = (manifest, origin);break
            feed_codes.append(code)
        if chosen is None:
            reasons.append(feed_codes[-1])
        else:
            manifest, origin = chosen
            slots = [{'store_id': s, 'name': n, 'sha': h} for s, n, h in manifest['slots']]
            if not slots or any(s['store_id'] not in (sid, SHARED) for s in slots):
                reasons.append('no_close_time_files' if not slots else 'close_time_slots_unproven')
            if {sha for sha in manifest.get('source_refs') or [] if sha in src.file_shas} - {s['sha'] for s in slots}:
                reasons.append('run_inputs_outside_close_time_slots')
            record.update(slots=slots, manifest_origin=origin, feed_boundary=manifest.get('feed_boundary'))
    for code in sorted({src.file_state(s['sha']) for s in record.get('slots', [])} - {None}):
        reasons.append(code)
    if payload.get('source_sync_pending') is not False:
        reasons.append('frozen_source_sync_pending')
    statement = {r.get('id'): r for r in payload.get('statement') or []}
    if any(not (statement.get(n) or {}).get('available') is True or not _number((statement.get(n) or {}).get('value')) for n in REQUIRED_NODES):
        reasons.append('required_statement_unavailable')
    commission = payload.get('commission') or {}
    calculation = None
    if commission.get('calculation_id'):
        calculation = src.registry.execute('SELECT * FROM calculation WHERE id=? AND finance_run=?',
                                           (commission['calculation_id'], run_id)).fetchone()
        if calculation is None:
            reasons.append('commission_calculation_missing')
    as_of = _instant(calculation['at'] if calculation else row['at'])
    snapshot, code = registry_snapshot(src, sid, period, dict(calculation) if calculation else None, as_of)
    if code:
        reasons.append(code)
    confirmation = src.registry.execute('SELECT * FROM payout_confirmation WHERE store_id=? AND period=? AND finance_run=? '
                                        'ORDER BY at DESC,id DESC LIMIT 1', (sid, period, run_id)).fetchone()
    if confirmation:
        confirmation = {**dict(confirmation), 'payouts': json.loads(confirmation['payouts_json']),
                        'trial': json.loads(confirmation['trial_json'])}
        if confirmation['trial'].get('allocation_risk'):
            reasons.append('confirmation_allocation_risk')
    if snapshot:
        persons = {p['id'] for p in snapshot['people']}
        named = {str(p.get('person_id')) for p in commission.get('people') or []} | {
            str(p.get('person_id')) for p in (confirmation or {}).get('payouts', [])}
        if {p for p in named if not p.startswith('legacy:')} - persons:
            reasons.append('commission_person_unknown')
        try:
            folder = Path(tempfile.mkdtemp(dir=src.scratch))
            _seed(snapshot, folder)
        except replay_service.ReplayError:
            reasons.append('registry_snapshot_invalid')
    cost_line_total = replay_service._posted_cost_line_total(SimpleNamespace(conn=db), sid, period, run_id, payload)
    if cost_line_total is None:
        reasons.append('cost_line_total_unknown')
    scope = replay_scope(model, decisions, period)
    if sid not in gate_scope(model, decisions, period):
        reasons.append('not_in_frozen_company_basis' if any(d['period'] == period for d in decisions) else 'not_operating_in_gate_scope')
    overhead_known = any(d['period'] == period and d['kind'] == 'overhead_total' for d in decisions) or model.overhead(period) is not None
    if not (set(scope) - {sid}) and overhead_known:
        reasons.append('can_close_not_derivable')
    record.update(platform=stores[sid].platform, payload=payload, snapshot=snapshot, confirmation=confirmation,
                  expected=expected_scope(payload, confirmation, cost_line_total), run_shas=row['shas'],
                  profit_exclusion_rows=src.registry.execute('SELECT count(*) FROM profit_exclusion WHERE store_id=? AND period=? AND finance_run=?',
                                                             (sid, period, run_id)).fetchone()[0])
    if legacy and not reasons:
        # The expensive original-engine proof runs only for otherwise clean store-months.
        code = reproduce_inputs(src, record, legacy['engine'], legacy['model'])
        if code:
            reasons.append(code)
        else:
            record['manifest_origin'] += '+original_engine_run_shas'
    record['reasons'] = sorted(set(reasons))
    return record


def select(records, model, periods, per_platform, explicit):
    clean = defaultdict(set)
    for r in records:
        if not r['reasons']:
            clean[r['store_id']].add(r['period'])
    platform = {s.id: s.platform for s in model.stores}
    if explicit:
        chosen = [sid for sid in explicit if clean.get(sid)]
    else:
        chosen = []
        for name in sorted({platform[sid] for sid in clean}):
            ranked = sorted((sid for sid in clean if platform[sid] == name), key=lambda sid: (-len(clean[sid]), sid))
            chosen.extend(ranked[:per_platform])
    targets = sorted((sid, p) for sid in chosen for p in clean[sid])
    skipped = sorted((sid, p, 'not_requested' if explicit else 'per_platform_limit')
                     for sid, months in clean.items() if sid not in chosen for p in months)
    return targets, skipped


def coverage(model, decisions, periods, targets):
    operating = {p: gate_scope(model, decisions, p) for p in periods}
    platform = {s.id: s.platform for s in model.stores}
    required = sorted({platform[sid] for ids in operating.values() for sid in ids})
    covered = sorted({platform[sid] for sid, _p in targets})
    months = defaultdict(set)
    for sid, p in targets:
        months[sid].add(p)
    return {'operating_store_count': {p: len(ids) for p, ids in operating.items()},
            'required_platforms': required, 'covered_platforms': covered,
            'missing_platforms': sorted(set(required) - set(covered)),
            'covered_periods': sorted({p for _sid, p in targets}),
            'missing_periods': sorted(set(periods) - {p for _sid, p in targets}),
            'cross_period_stores': sorted(sid for sid, ms in months.items() if set(periods) <= ms),
            'untargeted_operating_stores': sorted({sid for ids in operating.values() for sid in ids} - {sid for sid, _p in targets})}


def stage(src, record, staging, feed_copy):
    """A private root whose slot table is the frozen close-time slot set."""
    staging.mkdir()
    with sqlite3.connect(staging / 'workspace.db') as conn:
        conn.execute('CREATE TABLE slot(store_id TEXT,name TEXT,sha TEXT,PRIMARY KEY(store_id,name))')
        conn.execute('CREATE TABLE period(store_id TEXT,period TEXT,state TEXT,run_id INTEGER)')
        conn.executemany('INSERT INTO slot VALUES(?,?,?)', [(s['store_id'], s['name'], s['sha']) for s in record['slots']])
        conn.execute("INSERT INTO period VALUES(?,?,'closed',?)", (record['store_id'], record['period'], record['run_id']))
    conn.close()
    for slot in record['slots']:
        link = staging / 'files' / slot['sha'][:2] / slot['sha']
        if not link.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(src.root / 'files' / slot['sha'][:2] / slot['sha'])
    (staging / 'order-feed.db').symlink_to(feed_copy)
    for folder in ('order-components', 'order-date-context'):
        if (src.root / folder).is_dir():
            shutil.copytree(src.root / folder, staging / folder, symlinks=False)


def write_case(src, record, out, catalog, approval, decisions_ref):
    sid, period = record['store_id'], record['period']
    name = _slug(sid) + '-' + period
    bundle = out / 'bundles' / name
    staging = Path(tempfile.mkdtemp(dir=src.scratch)) / 'root'
    stage(src, record, staging, src.feed_copy())
    frozen.capture(staging, src.feed_root, src.model, period, bundle, catalog)
    inputs = json.loads((bundle / 'inputs.json').read_text(encoding='utf-8'))
    snapshot_path, reference, seed = _seed(record['snapshot'], bundle)
    inputs.update(file_policy='close_time_slots', data_kind='historical_close_time_inputs',
        close_time={'store_id': sid, 'period': period, 'run_id': record['run_id'],
                    'slots_from': record['manifest_origin'], 'feed_boundary': record['feed_boundary'],
                    'run_shas_sha256': hashlib.sha256((record['run_shas'] or '').encode()).hexdigest()},
        registry_snapshot={'path': snapshot_path.name, 'sha256': reference['sha256']})
    _write_json(bundle / 'inputs.json', inputs)
    frozen.seal(bundle)
    inputs, _fingerprint = frozen.verify(bundle)
    steps = [{'operation': 'recompute', 'store_id': sid}, {'operation': 'drain_pending'}]
    if record['confirmation']:
        steps.append({'operation': 'confirm', 'store_id': sid, 'period': period,
            'payouts': [{'person_id': p['person_id'], 'amount': _money(p['amount'])} for p in record['confirmation']['payouts']],
            'reason': '按已冻结生产实发确认恢复（finance_run ' + str(record['run_id']) + '）；依据：' + approval})
    case = {'schema_version': 1, 'case_id': 'historical-' + sid + '-' + period, 'kind': 'historical_golden',
        'approval_reference': approval, 'bundle': '../bundles/' + name, 'registry': seed, 'steps': steps,
        'expected': {'scopes': {sid + '/' + period: record['expected']}},
        'source_evidence': {'kind': 'historical_export',
            'reference': approval + ' | frozen closed run ' + str(record['run_id']) + ' of ' + sid + '/' + period,
            'input_sha256': sorted({s['sha'] for s in inputs['slots']})}}
    if decisions_ref:
        case['workspace_decisions'] = {**decisions_ref, 'path': '../' + decisions_ref['path']}
        case['source_evidence']['workspace_decisions_sha256'] = decisions_ref['sha256']
    path = out / 'cases' / (name + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, ensure_ascii=False, indent=1, sort_keys=True), encoding='utf-8')
    return path


def build(workspace_root, feed_root, model_path, target_period, approval, output, *, per_platform=2, stores=None):
    workspace_root, feed_root, output = Path(workspace_root).resolve(), Path(feed_root).resolve(), Path(output).resolve()
    if not isinstance(approval, str) or not approval.strip() or gate.FIXTURE.search(approval):
        raise BuildError('approval_reference 必须是非空真实批准依据，且不能含 fixture/synthetic 声明')
    approval = approval.strip()
    if output.exists():
        raise BuildError('输出目录已存在，禁止覆盖：' + str(output))
    if any(output.is_relative_to(root) or root.is_relative_to(output) for root in (workspace_root, feed_root)):
        raise BuildError('输出目录不能位于来源工作区或订单台目录内')
    if per_platform < 1:
        raise BuildError('--per-platform 至少为 1')
    model = gate._model(Path(model_path).resolve())
    if gate.FIXTURE.search(model.id) or gate.FIXTURE.search(model.name):
        raise BuildError('候选模型身份含 fixture/synthetic 声明')
    periods = [gate._previous(target_period), target_period]
    explicit = [s.strip() for s in (stores or '').split(',') if s.strip()] if stores else None
    unknown = set(explicit or []) - {s.id for s in model.stores}
    if unknown:
        raise BuildError('--stores 含候选模型没有的店铺：' + ', '.join(sorted(unknown)))
    catalog = store_classification.resolve([{'id': s.id, 'name': s.name, 'platform': s.platform} for s in model.stores])
    if gate.FIXTURE.search(str(catalog.get('version', ''))):
        raise BuildError('店铺分类目录版本含 fixture 声明')
    scratch = tempfile.mkdtemp(prefix='historical-golden-')
    src = None
    try:
        src = Source(workspace_root, feed_root, model, scratch)
        basis, basis_codes = frozen_basis(src, periods)
        decisions = workspace_decisions(model, basis, approval)
        rows = _rows(src.db, '''SELECT p.store_id,p.period,p.state,p.run_id,p.at_version,p.changed_at,r.id AS rid,r.at,
            r.evidence_ready,r.evidence_error,r.result,r.engine,r.model_revision,r.shas FROM period p LEFT JOIN run r ON r.id=p.run_id
            WHERE p.period IN (?,?) ORDER BY p.period,p.store_id''', periods)
        records, not_closed = [], []
        for row in rows:
            if row['state'] != 'closed':
                not_closed.append({'store_id': row['store_id'], 'period': row['period'], 'reasons': ['not_closed']})
                continue
            records.append(assess(src, row, decisions))
        seen = {(r['store_id'], r['period']) for r in rows}
        for period in periods:
            for sid in gate_scope(model, decisions, period):
                if (sid, period) not in seen:
                    not_closed.append({'store_id': sid, 'period': period, 'reasons': ['no_period_record']})
        targets, skipped = select(records, model, periods, per_platform, explicit)
        covered = coverage(model, decisions, periods, targets)
        out = output
        out.mkdir(parents=True)
        decisions_ref = None
        if decisions:
            body = {'schema_version': 1, 'kind': 'approved_workspace_decisions', 'approval_reference': approval, 'decisions': decisions}
            path = out / 'workspace-decisions.json'
            path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding='utf-8')
            decisions_ref = {'path': path.name, 'sha256': _sha_file(path), 'approval_reference': approval}
        by_key = {(r['store_id'], r['period']): r for r in records}
        cases = []
        if targets:
            for sid, period in targets:
                path = write_case(src, by_key[(sid, period)], out, catalog, approval, decisions_ref)
                cases.append({'path': path.relative_to(out).as_posix(), 'sha256': _sha_file(path)})
            manifest = {'schema_version': 1, 'kind': 'historical_golden', 'approval_reference': approval,
                'latest_period': target_period, 'target_scopes': sorted(sid + '/' + p for sid, p in targets), 'cases': cases}
            if decisions_ref:
                manifest['workspace_decisions'] = decisions_ref
            (out / 'approved.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
        selection = {'schema_version': 1, 'kind': 'historical_golden_selection', 'target_period': target_period,
            'periods': periods, 'approval_reference': approval, 'model_digest': gate.model_digest(model),
            'workspace_root': str(workspace_root), 'feed_root': str(feed_root),
            'selected': [{'store_id': sid, 'period': p, 'platform': by_key[(sid, p)]['platform'], 'run_id': by_key[(sid, p)]['run_id'],
                          'slots_from': by_key[(sid, p)]['manifest_origin'], 'confirm_step': bool(by_key[(sid, p)]['confirmation']),
                          'profit_exclusion_rows': by_key[(sid, p)]['profit_exclusion_rows']}
                         for sid, p in targets],
            'excluded': sorted([{'store_id': r['store_id'], 'period': r['period'], 'reasons': r['reasons']} for r in records if r['reasons']]
                               + not_closed, key=lambda r: (r['period'], r['store_id'])),
            'clean_not_selected': [{'store_id': sid, 'period': p, 'reasons': [code]} for sid, p, code in skipped],
            'company_basis': {p: {'frozen': p in basis, 'source': basis[p]['source'] if p in basis else None,
                                  'overhead_total_decided': bool(basis.get(p, {}).get('total') is not None),
                                  'unproven_code': basis_codes.get(p), 'basis_store_count': len(basis[p]['store_ids']) if p in basis else None,
                                  'unverified_revenue_in_basis': basis[p]['unverified_revenue'] if p in basis else None,
                                  'basis_stores_not_in_model': sorted(set(basis[p]['store_ids']) - {s.id for s in model.stores}) if p in basis else []}
                              for p in periods},
            'workspace_decisions': {'store_scope_excluded': sum(d['kind'] == 'store_scope' for d in decisions),
                                    'overhead_total_periods': sorted(d['period'] for d in decisions if d['kind'] == 'overhead_total')},
            'legacy_fallbacks': {'missing_candidate_tables_treated_as_empty': src.missing,
                'input_proof_by_store_month': dict(Counter(r.get('manifest_origin') or 'none' for r in records)),
                'company_basis_sources': {p: (basis[p]['source'] if p in basis else None) for p in periods}},
            'coverage': covered,
            'complete': bool(targets) and not covered['missing_platforms'] and not covered['missing_periods'] and bool(covered['cross_period_stores']),
            'expected_field_sources': {'statement': 'frozen run.result', 'commission_total': 'frozen run.result.commission',
                'commission_people': 'frozen run.result.commission', 'commission_complete': 'frozen run.result.commission.amount_complete',
                'allocation_pending_count': 'frozen run.result', 'source_sync_pending': 'frozen run.result (must be false)',
                'close_time_inputs': 'run proof/validation slots + feed boundary, or legacy version history checked by re-running run.engine to reproduce run.shas',
                'confirmed_total': 'registry payout_confirmation latest for frozen run', 'confirmed_people': 'registry payout_confirmation latest for frozen run',
                'cost_line_total': 'replay_service._posted_cost_line_total on frozen source', 'state': 'open: replay recomputes, no close step',
                'can_close': 'false: isolated store-month replay lacks other operating stores revenue or overhead total'},
            'limitations': LIMITATIONS}
        (out / 'selection.json').write_text(json.dumps(selection, ensure_ascii=False, indent=1), encoding='utf-8')
        return selection
    finally:
        if src is not None:
            src.close()
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--workspace-root', type=Path, required=True, help='Backup copy: workspace.db, commission/registry.db, order-feed.db, files/')
    parser.add_argument('--feed-root', type=Path, required=True, help='Order feed snapshot root; every replay reads the feed')
    parser.add_argument('--model', type=Path, required=True, help='Candidate model directory or model.json')
    parser.add_argument('--target-period', required=True)
    parser.add_argument('--approval-reference', required=True)
    parser.add_argument('--output', type=Path, required=True, help='New directory; never overwritten')
    parser.add_argument('--per-platform', type=int, default=2)
    parser.add_argument('--stores', help='Optional comma separated store ids')
    args = parser.parse_args(argv)
    try:
        selection = build(args.workspace_root, args.feed_root, args.model, args.target_period, args.approval_reference,
                          args.output, per_platform=args.per_platform, stores=args.stores)
    except (BuildError, frozen.InputError, gate.CaseBlocked, replay_service.ReplayError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'status': 'blocked', 'error': f'{type(exc).__name__}: {exc}'}, ensure_ascii=False))
        return 2
    print(json.dumps({'status': 'complete' if selection['complete'] else 'incomplete', 'selected': len(selection['selected']),
                      'excluded': len(selection['excluded']), 'coverage': selection['coverage']}, ensure_ascii=False))
    return 0 if selection['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
