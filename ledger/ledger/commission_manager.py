"""Durable background work for catalog refresh, imports and relationship changes."""
from __future__ import annotations

import json
import threading
import time
import sqlite3
import logging
import uuid
from contextlib import closing

from . import commission_catalog, commission_import, order_feed, service, fee_jobs, publication_scope
from .commission_registry import Registry, json_text


def working_months(ws):
    """Months still being closed, oldest first.

    A month counts when it has at least half the stores of the busiest month
    and at least half of its stores are still open; a few stray open cells in
    an old month do not pull the whole company back to it.
    """
    rows=ws.conn.execute("SELECT period,count(*) n,sum(state<>'closed') open FROM period "
                         "WHERE period GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' GROUP BY period").fetchall()
    busiest=max((row['n'] for row in rows),default=0)
    return sorted(row['period'] for row in rows if 2*row['n']>=busiest and 2*row['open']>=row['n'] and row['open'])


class Manager:
    def __init__(self, workspace, model, *, publication_scope=None):
        self.workspace = workspace
        self.model = model
        self.stop_event = threading.Event()
        self.thread = None
        self.last_catalog = 0.0
        self.current_pending = None
        self.current_file_revision = 0
        self.last_error = ""
        self.last_error_store = None
        self._yield_fee = False
        self._yield_quote = False
        self.lease_owner = 'commission-manager:' + uuid.uuid4().hex
        self.current_lease = None
        self._publication_scope = publication_scope
        # Months whose other stores still show the company basis from before
        # a recompute, with the time the first such recompute finished.
        self.month_dirty = {}
        self.last_busy_month_sync = time.time()

    # A full month pass checks every store (minutes per month). It waits for
    # the queue to drain; under a queue that never drains, one month per
    # interval, oldest month first.
    BUSY_MONTH_SYNC_INTERVAL = 1800

    def _sync_dirty_month(self, ws, scope, *, idle):
        if not self.month_dirty:
            return False
        now = time.time()
        if not idle and now - self.last_busy_month_sync < self.BUSY_MONTH_SYNC_INTERVAL:
            return False
        due = [p for p, since in self.month_dirty.items() if idle or now - since >= self.BUSY_MONTH_SYNC_INTERVAL]
        if not due:
            return False
        period = min(due)
        self.month_dirty.pop(period, None)
        if not idle:
            self.last_busy_month_sync = now
        from .automation_actions import sync_month
        try:
            sync_month(ws, self.model(), period, worker=self.lease_owner)
        except Exception:
            self.month_dirty.setdefault(period, now)
            raise
        return True

    def _scope(self, ws):
        value = self._publication_scope
        if callable(value):value = value(ws, self.model())
        if value is None:value = publication_scope.load_automatic(ws.root, self.model())
        if not isinstance(value, publication_scope.PublicationScope):
            raise publication_scope.PublicationScopeError('automatic_publication_scope_object_invalid')
        return value.verify(ws.root, self.model())

    def start(self):
        ws=self.workspace()
        try:self._scope(ws)
        except publication_scope.PublicationScopeError as exc:
            self.last_error=str(exc)
            return
        registry = Registry(ws.root)
        self._enqueue_code_refresh(ws,registry)
        fee_jobs.restart(registry)
        with registry.transaction() as conn:
            conn.execute("UPDATE job SET status='queued' WHERE status='running'")
        self.thread = threading.Thread(target=self.run, name="commission-manager", daemon=True)
        self.thread.start()

    def _enqueue_code_refresh(self,ws,registry):
        """An approved code update is input work in the existing dedup queue."""
        from .version import assert_code_integrity,CodeIntegrityError
        from .automation_store import digest
        try:identity=assert_code_integrity()
        except CodeIntegrityError:return 0
        code=identity['source_code_sha']
        scope=self._scope(ws)
        rows=ws.conn.execute('''SELECT DISTINCT r.store_id,r.period FROM period p JOIN run r ON r.id=(
            SELECT max(latest.id) FROM run latest WHERE latest.store_id=p.store_id AND latest.period=p.period)
            LEFT JOIN automation_run_proof proof ON proof.run_id=r.id
            LEFT JOIN run_report_slice projection ON projection.run_id=r.id AND projection.payload_kind='run'
            WHERE p.state='open' AND json_type(snapshot_json(coalesce(projection.overview_json,r.result)),'$.calculation_inputs') IS NOT NULL
            AND coalesce(json_extract(proof.manifest_json,'$.source_code_sha'),'')<>?''',(code,)).fetchall()
        known={store.id for store in self.model().stores}
        targets={row['store_id'] for row in rows if row['store_id'] in known and scope.allows(row['store_id'],row['period'])}
        for sid in sorted(targets):
            scope.verify(ws.root)
            registry.enqueue_automation({sid},'automation:'+digest(['approved-code-refresh',code,sid,scope.identity]))
        return len(targets)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)

    def once(self):
        try:
            return self._once()
        finally:
            # once() is also used by maintenance/testing and can return early
            # for a missing mapping/source. A finished pass owns no live lease.
            if self.current_lease:
                try:
                    registry=Registry(self.workspace().root)
                    with registry.transaction() as conn:
                        conn.execute("UPDATE pending SET lease_owner='',lease_until=0 WHERE store_id=? AND lease_owner=? AND lease_token=?",
                            (self.current_lease[0],self.lease_owner,self.current_lease[1]))
                except Exception:
                    logging.getLogger(__name__).exception('Unable to release accounting lease')
            self.current_lease=None

    def _once(self):
        ws=self.workspace()
        scope=self._scope(ws)
        with publication_scope.bind(scope,ws.root):
            return self._once_scoped(ws,scope)

    def _once_scoped(self,ws,scope):
        self.current_pending = None
        self.current_file_revision = 0
        # A finite upstream quote refresh is a technical read task, not a
        # reason to parse/recompute the entire financial store repeatedly.
        skip_quote=self._yield_quote
        self._yield_quote=False
        if not skip_quote:
            from .automation_source_cost_v3 import process_one_quote_refresh
            if process_one_quote_refresh(ws,self.model(),scope):
                self._yield_quote=True
                return
        registry = Registry(ws.root)
        from .automation_actions import deliver
        deliver(ws)
        fee_jobs.recover(ws, self.model)
        skip_fee = self._yield_fee
        self._yield_fee = False
        with registry.transaction() as conn:
            row = conn.execute("SELECT * FROM job WHERE status='queued' " +
                ("AND kind<>'fee_rules' " if skip_fee else "") + "ORDER BY at LIMIT 1").fetchone()
            if row:
                conn.execute("UPDATE job SET status='running' WHERE id=?", (row["id"],))
        if row:
            job = dict(row)
            payload = json.loads(job["payload"])
            try:
                if job["kind"] == fee_jobs.KIND:
                    fee_jobs.step(ws, self.model, job)
                    self._yield_fee = True
                    return
                elif job["kind"] == "catalog":
                    result = commission_catalog.refresh(registry, self.model())
                    self.last_catalog = time.time()
                elif job["kind"] == "import":
                    path = registry.root / "uploads" / payload["sha"]
                    raw = path.read_bytes()
                    import hashlib
                    if hashlib.sha256(raw).hexdigest() != payload["sha"]:
                        raise ValueError("原文件校验失败")
                    result = commission_import.stage(registry, self.model(), raw, payload["filename"],
                                                     payload["effective_from"], job["actor"])
                elif job["kind"] == "activate":
                    result = commission_import.activate(registry, payload["batch_id"], job["actor"], payload["reason"])
                else:
                    raise ValueError("未知任务类型")
                with registry.transaction() as conn:
                    conn.execute("UPDATE job SET status='done',result=?,error='' WHERE id=?", (json_text(result), job["id"]))
            except Exception as exc:
                with registry.transaction() as conn:
                    conn.execute("UPDATE job SET status='failed',error=? WHERE id=?", (str(exc)[:2000], job["id"]))
            return
        if self._sync_dirty_month(ws,scope,idle=False):
            return
        claimed_at = int(time.time())
        allowed_stores=sorted({sid for sid,_period in scope.scopes})
        scope.verify(ws.root,self.model())
        # Month by month: stores still open in the earliest month go first.
        working=set(working_months(ws))
        oldest_open={}
        for row in ws.conn.execute("SELECT store_id,period FROM period WHERE state<>'closed'"):
            if row[1] in working:oldest_open[row[0]]=min(oldest_open.get(row[0],row[1]),row[1])
        with registry.transaction() as conn:
            candidates = conn.execute("SELECT * FROM pending WHERE next_attempt<=? AND lease_until<=? "
                                      "AND store_id IN (%s)" % ','.join('?' for _ in allowed_stores),
                                      (claimed_at,claimed_at,*allowed_stores)).fetchall()
            pending = min(candidates, key=lambda r: (r['next_attempt']!=0, r['files_revision']<=r['files_applied_revision'],
                                                     oldest_open.get(r['store_id'],'9999-99'), r['next_attempt'],
                                                     r['revision'], r['store_id'])) if candidates else None
            if pending:
                pending=dict(pending)
                pending['lease_token']+=1
                conn.execute("UPDATE pending SET next_attempt=?,lease_owner=?,lease_token=?,lease_until=?,attempt_count=attempt_count+1 WHERE store_id=? AND revision=? AND source_seq=? AND source_fingerprint=? AND files_revision=?",
                             (claimed_at + 180,self.lease_owner,pending['lease_token'],claimed_at+180,pending["store_id"], pending["revision"],
                              pending["source_seq"], pending["source_fingerprint"], pending["files_revision"]))
                self.current_lease=(pending['store_id'],pending['lease_token'])
        if pending:
            if order_feed.enabled():
                feed_path = ws.root / "order-feed.db"
                if not feed_path.exists():
                    return
                with closing(sqlite3.connect(f"file:{feed_path.as_posix()}?mode=ro", uri=True)) as source:
                    state = source.execute("SELECT consumed_seq,source_latest_seq,snapshot_id FROM feed_state").fetchone()
                # This job requires its captured source prefix, not a global
                # head which keeps moving as unrelated shops receive orders.
                if not state or not state[2] or state[0] < pending["source_seq"]:
                    return
            store_id, revision = pending["store_id"], pending["revision"]
            self.current_pending = (store_id, revision)
            self.current_file_revision = pending["files_revision"]
            if order_feed.enabled():
                with closing(sqlite3.connect(f"file:{(ws.root / 'order-feed.db').as_posix()}?mode=ro", uri=True)) as source:
                    mapped = source.execute("SELECT 1 FROM feed_store WHERE ledger_store_id=? AND mapping_status='confirmed' LIMIT 1", (store_id,)).fetchone()
                if not mapped:
                    # Missing mappings cannot be repaired by rereading all
                    # source files. Retain the durable work with a clear cause.
                    error = f"订单台没有 {self.model().store(store_id).name} 的已确认店铺映射；确认映射后自动重试"
                    with registry.transaction() as conn:
                        conn.execute("UPDATE pending SET next_attempt=?,error=? WHERE store_id=? AND revision=? AND source_seq=? AND source_fingerprint=? AND files_revision=?",
                                     (int(time.time())+300,error,store_id,revision,pending['source_seq'],pending['source_fingerprint'],pending['files_revision']))
                    return
            # Registry/period proofs already capture rule generations. A
            # store-global version bump would mutate every old month's input.
            scope.verify(ws.root)
            file_work = pending['files_revision'] > pending['files_applied_revision']
            note = ("原文件更新" if file_work else
                    "订单数据更新" if pending["source_seq"] else "提成设置更新")
            stopped=threading.Event()
            def renew():
                while not stopped.wait(20):
                    try:
                        with registry.transaction() as connection:
                            changed=connection.execute('UPDATE pending SET lease_until=? WHERE store_id=? AND lease_owner=? AND lease_token=?',
                                (int(time.time())+180,store_id,self.lease_owner,pending['lease_token']))
                            if not changed.rowcount:return
                    except Exception:
                        logging.getLogger(__name__).exception('Unable to renew accounting lease')
            renewal=threading.Thread(target=renew,name='commission-lease',daemon=True)
            renewal.start()
            try:
                from contextlib import nullcontext
                from .feed_freshness import hold_task
                boundary=(hold_task(ws.root,store_id,state[2],int(pending['source_seq']) or int(state[1]))
                          if order_feed.enabled() else nullcontext())
                with boundary:
                    result = service.recompute(ws, self.model(), self.model().store(store_id), note=note)
                for period_result in result.periods:
                    period=period_result.get('period')
                    if not period or not scope.allows(store_id,period) or period_result.get('store_id',store_id)!=store_id:
                        raise publication_scope.PublicationScopeError('automatic_recompute_result_scope_unverified')
                from .automation_deterministic import run_available
                automatic=[]
                for period_result in result.periods:
                    if period_result.get('state')=='closed' or not period_result.get('period'):continue
                    automatic.extend(run_available(ws,self.model(),period_result['period'],store_ids=[store_id]))
            finally:
                stopped.set();renewal.join(timeout=2)
            for period_result in result.periods:
                rid=period_result.get('run_id')
                evidence=ws.conn.execute('SELECT evidence_ready,evidence_error FROM run WHERE id=?',(rid,)).fetchone() if rid else None
                if evidence and (not evidence['evidence_ready'] or evidence['evidence_error']):
                    raise ValueError(evidence['evidence_error'] or '事实证据留档未完成，保留任务重试')
            if result.failure:
                why = result.failure.get("why") or str(result.failure)
                if "没有任何数据" not in why and "没算出结果" not in why:
                    raise ValueError(why)
                # No order inputs yet is a normal state for a newly registered
                # listing/store. Its next source revision will enqueue it again.
            # Frozen close-time flags describe historical evidence, not the
            # current work. Only open periods can require another sync pass.
            provisional = any(p.get("state") != "closed" and p.get("source_sync_pending")
                              for p in result.periods)
            scope.verify(ws.root)
            with registry.transaction() as conn:
                lease=conn.execute('SELECT lease_owner,lease_token FROM pending WHERE store_id=?',(store_id,)).fetchone()
                if not lease or lease['lease_owner']!=self.lease_owner or lease['lease_token']!=pending['lease_token']:
                    raise ValueError('核算租约已变化，旧执行不能确认新任务')
                # A published trial has consumed the captured file generation
                # even if newer ERP events still require another pass. Newer
                # uploaded files retain their own priority and cannot be acked
                # by this older calculation.
                if result.periods and not result.failure:
                    conn.execute("UPDATE pending SET files_applied_revision=max(files_applied_revision,?) "
                                 "WHERE store_id=? AND files_revision>=?",
                                 (pending['files_revision'],store_id,pending['files_revision']))
                    # Rules, files and code captured by this run now reach every
                    # month; what is left can only be order changes, which the
                    # month check bounds by business date.
                    conn.execute("UPDATE pending SET full_applied_revision=max(full_applied_revision,?) WHERE store_id=?",
                                 (pending.get('full_revision',1),store_id))
                if provisional:
                    retry_at = int(time.time()) + 60
                    changed = conn.execute("UPDATE pending SET next_attempt=? WHERE store_id=? AND revision<=? AND source_seq<=? AND source_fingerprint=? AND files_revision=?",
                                           (retry_at, store_id, revision, pending["source_seq"], pending["source_fingerprint"], pending["files_revision"]))
                    if not changed.rowcount:
                        conn.execute("UPDATE pending SET next_attempt=min(next_attempt,?) WHERE store_id=?",
                                     (retry_at, store_id))
                else:
                    removed = conn.execute("DELETE FROM pending WHERE store_id=? AND revision<=? AND source_seq<=? AND source_fingerprint=? AND files_revision=?",
                                          (store_id, revision, pending["source_seq"], pending["source_fingerprint"], pending["files_revision"]))
                    if not removed.rowcount:
                        conn.execute("UPDATE pending SET next_attempt=min(next_attempt,?) WHERE store_id=?",
                                     (int(time.time()) + 10, store_id))
                conn.execute("INSERT INTO job VALUES(?,?,?,?,?,?,?,?)",
                             (__import__("uuid").uuid4().hex, "recompute", "system",
                              __import__("datetime").datetime.now().isoformat(), "done",
                              json_text({"store_id": store_id}), json_text({"store_id": store_id,
                              "periods": len(result.periods), "waiting_for_orders": bool(result.failure), "provisional": provisional,
                              "file_work":file_work,"files_revision":pending['files_revision'],
                              "source_seq":pending['source_seq'],
                              "run_ids":[p.get('run_id') for p in result.periods if p.get('run_id')]}), ""))
                conn.execute("UPDATE pending SET lease_owner='',lease_until=0 WHERE store_id=? AND lease_owner=? AND lease_token=?",(store_id,self.lease_owner,pending['lease_token']))
            if not provisional and result.periods and not result.failure:
                from .finance_guard import authority
                # A coalesced newer delivery can survive this pass. Never mark
                # its attempt completed just because an older run succeeded.
                # Hold the same authority as Registry delivery while checking
                # queue drain and writing the Workspace observation receipt.
                with authority(ws.root),registry.connect() as queue:
                    remains=queue.execute('SELECT 1 FROM pending WHERE store_id=?',(store_id,)).fetchone()
                    if not remains:
                        with ws.transaction() as conn:
                            from .automation_store import encoded,now
                            completed_periods={p['period'] for p in result.periods}
                            attempts=conn.execute("SELECT a.id,c.store_id,c.period FROM automation_attempt a LEFT JOIN automation_case c ON c.id=a.case_id "
                                "WHERE a.state='queued' AND json_extract(a.result_json,'$.store_id')=?",(store_id,)).fetchall()
                            for attempt in attempts:
                                if attempt['store_id']!=store_id or attempt['period'] not in completed_periods:
                                    publication_scope.hold(scope,ws.root,attempt['id'],'completion_scope_metadata_unverified',
                                        store_id=attempt['store_id'],period=attempt['period'])
                                    continue
                                scope.require(store_id,attempt['period'],workspace_root=ws.root)
                                conn.execute("UPDATE automation_attempt SET state='completed',result_json=? WHERE id=? AND state='queued'",
                                    (encoded({'store_id':store_id,'period':attempt['period'],
                                        'run_ids':[p.get('run_id') for p in result.periods if p['period']==attempt['period']],
                                        'worker':self.lease_owner,'completed_at':now(),'publication_scope':scope.identity}),attempt['id']))
            # Exact actions publish their own durable handoff. Queue it only
            # after retiring the captured generation so an older run cannot ack
            # away the newly approved source decision.
            if automatic:deliver(ws)
            from .automation_actions import sync_store_month
            from .automation_cost_actions import run_available as cost_actions
            periods={p['period']:p for p in result.periods if p.get('period')}
            # A real delivery can fail to yield any slice (missing/unknown
            # originals). Existing store-month waits still need reconciliation.
            cases={row['period']:row['stage'] for row in ws.conn.execute('SELECT period,stage FROM automation_case WHERE store_id=?',(store_id,))}
            for period in cases:
                if scope.allows(store_id,period):periods.setdefault(period,{'period':period})
            for period in scope.periods_for_store(store_id):periods.setdefault(period,{'period':period})
            for period,period_result in sorted(periods.items()):
                scope.require(store_id,period,workspace_root=ws.root)
                if period_result.get('state')=='closed' and cases.get(period)=='closed':continue
                sync_store_month(ws,self.model(),period,store_id,worker=self.lease_owner)
                if period_result.get('state')!='closed':
                    cost_actions(ws,self.model(),period,store_id,worker=self.lease_owner)
                    self.month_dirty.setdefault(period,time.time())
            deliver(ws)
            return
        if self._sync_dirty_month(ws,scope,idle=True):
            return
        if order_feed.enabled() and time.time() - self.last_catalog > 12 * 3600:
            self.last_catalog = time.time()
            registry.enqueue("catalog", {}, "system")

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.once()
                self.last_error = ""
                self.last_error_store = None
            except Exception as exc:
                # Pending rows remain durable. The status endpoint exposes the
                # failure, and the next attempt never changes frozen results.
                self.last_error = str(exc)[:2000]
                self.last_error_store = self.current_pending[0] if self.current_pending else None
                logging.getLogger(__name__).exception("Recompute failed for %s; durable work will retry", self.current_pending)
                try:
                    registry = Registry(self.workspace().root)
                    with registry.transaction() as conn:
                        if self.current_pending:
                            conn.execute("UPDATE pending SET next_attempt=?,error=? WHERE store_id=? AND revision<=? AND files_revision=?",
                                         (int(time.time()) + 120, str(exc)[:2000], *self.current_pending, self.current_file_revision))
                        conn.execute("INSERT INTO job VALUES(?,?,?,?,?,?,?,?)",
                                     (__import__("uuid").uuid4().hex, "recompute", "system",
                                      __import__("datetime").datetime.now().isoformat(), "failed", "{}", "{}", str(exc)[:2000]))
                except Exception:
                    logging.getLogger(__name__).exception("Unable to persist recompute failure; worker will retry")
                self.stop_event.wait(2)
            finally:
                if self.current_lease:
                    try:
                        registry=Registry(self.workspace().root)
                        with registry.transaction() as conn:
                            conn.execute("UPDATE pending SET lease_owner='',lease_until=0 WHERE store_id=? AND lease_owner=? AND lease_token=?",(self.current_lease[0],self.lease_owner,self.current_lease[1]))
                    except Exception:
                        logging.getLogger(__name__).exception('Unable to release accounting lease')
                self.current_lease=None
                self.current_pending = None
            self.stop_event.wait(2)
