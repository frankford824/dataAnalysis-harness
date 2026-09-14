from types import SimpleNamespace
import sqlite3
import threading

import pytest

from ledger.commission_manager import Manager
from ledger.commission_registry import Registry
from ledger.order_feed import Worker, SyncResult
from ledger.pricing_status import status
from ledger.workspace import Workspace, WorkspaceError
from test_commission import _model


def feed_db(root, consumed=10, latest=20):
    with sqlite3.connect(root / 'order-feed.db') as c:
        c.execute('CREATE TABLE feed_state(id INTEGER,snapshot_id TEXT,consumed_seq INTEGER,source_latest_seq INTEGER,last_success TEXT,last_error TEXT)')
        c.execute("INSERT INTO feed_state VALUES(1,'snapshot',?,?,'now','')", (consumed, latest))


def test_job_uses_captured_watermark_instead_of_moving_global_head(tmp_path, monkeypatch):
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED', '1')
    ws = Workspace(tmp_path); reg = Registry(tmp_path); feed_db(tmp_path)
    reg.enqueue_source({'s1'}, 'order-feed:snapshot:10')
    calls = []
    monkeypatch.setattr('ledger.commission_manager.service.recompute', lambda *a, **k: calls.append(1) or SimpleNamespace(failure=None, periods=[]))
    Manager(lambda: ws, _model).once()
    assert calls == [1]
    reg.enqueue_source({'s1'}, 'order-feed:snapshot:30')
    Manager(lambda: ws, _model).once()
    assert calls == [1]
    ws.close()


def test_worker_notifies_durable_progress_before_global_catchup(monkeypatch):
    pending = {'s1'}; callbacks = []; clock = [100.0]
    monkeypatch.setattr('ledger.order_feed.time.monotonic', lambda: clock[0])
    feed = SimpleNamespace(sync=lambda: SyncResult(snapshot_id='s', consumed_seq=10, affected_stores=set()),
                           pending_stores=lambda: pending.copy(), fingerprint=lambda: 'order-feed:s:10',
                           acknowledge_stores=lambda stores, seq: pending.difference_update(stores))
    worker = Worker(feed, lambda stores, fingerprint: callbacks.append((stores, fingerprint)))
    worker.poll(); assert len(callbacks) == 1 and not pending
    pending.add('s2'); clock[0] = 110; worker.poll()
    assert len(callbacks) == 1 and pending == {'s2'}
    clock[0] = 161; worker.poll(); assert len(callbacks) == 2 and not pending


def test_failed_notification_keeps_durable_pending():
    ack = []; feed = SimpleNamespace(sync=lambda: SyncResult(caught_up=True), pending_stores=lambda: {'s1'},
                                     fingerprint=lambda: 'order-feed:s:1', acknowledge_stores=lambda *x: ack.append(x))
    def fail(*args): raise RuntimeError('locked')
    worker = Worker(feed, fail)
    with pytest.raises(RuntimeError): worker.poll()
    assert not ack and worker.pending_stores == {'s1'}


def test_failure_logger_lock_does_not_kill_background_worker(tmp_path, monkeypatch):
    manager = Manager(lambda: SimpleNamespace(root=tmp_path), _model)
    calls = []
    def once():
        calls.append(1)
        if len(calls) == 1: raise sqlite3.OperationalError('database is locked')
        manager.stop_event.set()
    monkeypatch.setattr(manager, 'once', once)
    monkeypatch.setattr(manager.stop_event, 'wait', lambda *a: None)
    def locked(*args): raise sqlite3.OperationalError('logger is also locked')
    monkeypatch.setattr('ledger.commission_manager.Registry', locked)
    manager.run()
    assert calls == [1, 1]


def test_registry_read_transaction_does_not_block_source_enqueue(tmp_path):
    reg = Registry(tmp_path)
    with reg.connect() as reader:
        reader.execute('BEGIN'); reader.execute('SELECT * FROM pending').fetchall()
        errors = []
        def write():
            try: reg.enqueue_source({'s1'}, 'order-feed:s:10')
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=write); thread.start(); thread.join(2)
        assert not thread.is_alive() and not errors


def test_syncing_blocks_close_and_status_explains_saved_result(tmp_path):
    ws = Workspace(tmp_path); feed_db(tmp_path)
    ws.record('s1', '2026-06', {'can_close': True}, [])
    with pytest.raises(WorkspaceError, match='仍在同步'): ws.close_period('s1', '2026-06')
    result = status(ws, 's1', '2026-06')
    assert result['state'] == 'syncing' and result['run_id'] and result['calculated_at']
    with sqlite3.connect(tmp_path/'order-feed.db') as c: c.execute('UPDATE feed_state SET consumed_seq=source_latest_seq')
    assert status(ws, 's1', '2026-06')['state'] == 'review'
    reg = Registry(tmp_path); reg.enqueue_source({'s1'}, 'order-feed:snapshot:20')
    assert status(ws, 's1', '2026-06')['state'] == 'queued'
    manager = SimpleNamespace(current_pending=('s1', 1), thread=SimpleNamespace(is_alive=lambda: True), last_error='')
    assert status(ws, 's1', '2026-06', manager)['state'] == 'running'
    manager.thread = None
    assert status(ws, 's1', '2026-06', manager)['state'] == 'error'
    assert status(ws, 's1', '2026-06', manager, {'state': 'running', 'phase': '读表'})['state'] == 'running'
    ws.close()


def test_manual_recompute_reports_phase_and_clears_on_failure(tmp_path, monkeypatch):
    from ledger import service
    ws = Workspace(tmp_path); model = _model(); store = model.stores[0]
    def compute(*args, report, **kwargs):
        report('核对历史成本')
        assert service.recompute_activity(store.id) == {'state': 'running', 'phase': '核对历史成本'}
        raise RuntimeError('test failure')
    monkeypatch.setattr(service, '_recompute_locked', compute)
    with pytest.raises(RuntimeError): service.recompute(ws, model, store)
    assert service.recompute_activity(store.id) is None
    ws.close()


def test_feed_connection_is_closed_and_repeated_readers_do_not_request_write_lock(tmp_path):
    from ledger.order_feed import OrderFeed
    feed = OrderFeed(tmp_path, client=object())
    with feed._connect() as connection:
        connection.execute('SELECT 1').fetchone()
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')
    # A status reader used to execute INSERT/DDL and wait on this writer.
    with feed._connect() as writer:
        writer.execute('BEGIN IMMEDIATE')
        another = OrderFeed(tmp_path, client=object())
        assert another._guard is feed._guard
        assert another.state()['id'] == 1


def test_feed_connection_rolls_back_on_failure(tmp_path):
    from ledger.order_feed import OrderFeed
    feed = OrderFeed(tmp_path, client=object())
    with pytest.raises(RuntimeError):
        with feed._connect() as conn:
            conn.execute("UPDATE feed_state SET last_error='not committed'")
            raise RuntimeError('stop')
    assert feed.state()['last_error'] == ''


def test_recovered_producer_with_unchanged_revision_can_resume_backlog(tmp_path):
    import json
    from ledger.order_feed import OrderFeed
    from test_order_feed import FakeClient, _fixture
    root = tmp_path/'feed'; client = FakeClient(_fixture(root))
    feed = OrderFeed(tmp_path/'workspace', client=client, feed_root=root)
    feed.sync()
    with feed._connect() as conn:
        conn.execute('UPDATE feed_state SET consumed_seq=10,source_latest_seq=11,health_json=?',
                     (json.dumps({'healthy': False, 'degraded': ['cost_api_worker']}),))
    client.calls.clear(); feed._last_health_probe = 0
    result = feed.sync()
    assert result.caught_up and result.consumed_seq == 11
    assert 'health' in client.calls and 'changes' in client.calls
