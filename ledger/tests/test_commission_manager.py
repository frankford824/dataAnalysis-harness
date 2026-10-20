import json
from types import SimpleNamespace

import pytest

from ledger.commission_manager import Manager
from ledger.commission_registry import Registry
from ledger.workspace import Workspace
from ledger.model.schema import Store
from test_commission import _model


def test_new_source_while_calculating_remains_in_durable_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_ORDER_FEED_ENABLED", "0")
    ws = Workspace(tmp_path)
    registry = Registry(tmp_path)
    registry.enqueue_source({"s1"}, "order-feed:snapshot:10")
    def compute(*args, **kwargs):
        registry.enqueue_source({"s1"}, "order-feed:snapshot:20")
        return SimpleNamespace(failure=None, periods=[])
    monkeypatch.setattr("ledger.commission_manager.service.recompute", compute)
    manager = Manager(lambda: ws, _model)
    manager.once()
    with registry.connect() as conn:
        pending = conn.execute("SELECT * FROM pending WHERE store_id='s1'").fetchone()
        assert pending["source_seq"] == 20
    assert registry.revision() == 0


@pytest.mark.parametrize("updated_store", ["s1", "s2"], ids=["same-store", "other-store"])
def test_completed_input_retires_only_its_own_captured_work(tmp_path, monkeypatch, updated_store):
    from ledger.engine.runtime import Ingestion
    from ledger.order_feed import OrderFeed
    from test_order_feed import FakeClient, _fixture

    monkeypatch.setenv("LEDGER_ORDER_FEED_ENABLED", "1")
    ws = Workspace(tmp_path / "workspace")
    registry = Registry(ws.root)
    root = tmp_path / "feed"
    feed = OrderFeed(ws.root, client=FakeClient(_fixture(root)), feed_root=root)
    feed.sync()
    with feed._connect() as conn:
        conn.execute("UPDATE feed_store SET ledger_store_id='s1' WHERE order_store_id='10'")
    registry.enqueue_source({"s1"}, "order-feed:s1:11")
    frames = feed._frames

    def read_frames(*args, **kwargs):
        result = frames(*args, **kwargs)
        with feed._connect() as conn:
            conn.execute("UPDATE feed_state SET consumed_seq=12,source_latest_seq=12 WHERE id=1")
            conn.execute(
                "INSERT INTO feed_entity(entity_type,entity_id,seq,revision,operation,order_store_id) "
                "VALUES('order','new-order',12,12,'upsert',?)",
                ("10" if updated_store == "s1" else "20",),
            )
        registry.enqueue_source({updated_store}, "order-feed:s1:12")
        return result

    monkeypatch.setattr(feed, "_frames", read_frames)

    def compute(_ws, _model, store, **kwargs):
        ingestion = Ingestion(model=None)
        feed.append_to(ingestion, store)
        return SimpleNamespace(failure=None, periods=[{
            "run_id": 99, "source_sync_pending": ingestion.source_sync_pending,
        }])

    monkeypatch.setattr("ledger.commission_manager.service.recompute", compute)
    Manager(lambda: ws, _model).once()
    with registry.connect() as conn:
        pending = [dict(row) for row in conn.execute("SELECT * FROM pending")]
        job = json.loads(conn.execute("SELECT result FROM job").fetchone()[0])
    assert job["provisional"] is (updated_store == "s1")
    assert [(row["store_id"], row["source_seq"]) for row in pending] == [(updated_store, 12)]


def test_repeated_source_updates_preserve_waiting_time_and_worker_lease(tmp_path, monkeypatch):
    registry=Registry(tmp_path)
    monkeypatch.setattr('ledger.commission_registry.time.time',lambda:100)
    registry.enqueue_source({'s1'},'order-feed:snapshot:10')
    monkeypatch.setattr('ledger.commission_registry.time.time',lambda:200)
    registry.enqueue_source({'s1'},'order-feed:snapshot:20')
    with registry.transaction() as conn:
        row=conn.execute("SELECT * FROM pending WHERE store_id='s1'").fetchone()
        assert row['next_attempt']==100 and row['source_seq']==20
        conn.execute("UPDATE pending SET next_attempt=500 WHERE store_id='s1'")
    registry.enqueue_source({'s1'},'order-feed:snapshot:30')
    registry.enqueue_files({'s1'},'nas:'+'a'*64)
    with registry.connect() as conn:
        assert conn.execute("SELECT next_attempt FROM pending WHERE store_id='s1'").fetchone()[0]==500


def test_new_file_work_precedes_older_continuous_erp_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    ws=Workspace(tmp_path);registry=Registry(tmp_path)
    registry.enqueue_source({'s1'},'order-feed:snapshot:10')
    registry.enqueue_files({'s2'},'nas:'+'a'*64)
    with registry.transaction() as conn:
        conn.execute("UPDATE pending SET next_attempt=1 WHERE store_id='s1'")
        conn.execute("UPDATE pending SET next_attempt=2 WHERE store_id='s2'")
    model=_model(stores=(Store(id='s1',name='一店',platform='taobao'),Store(id='s2',name='二店',platform='taobao')))
    calls=[]
    def compute(_ws,_model,store,**kwargs):
        calls.append(store.id)
        return SimpleNamespace(failure=None,periods=[{'run_id':99}])
    monkeypatch.setattr('ledger.commission_manager.service.recompute',compute)
    Manager(lambda:ws,lambda:model).once()
    assert calls==['s2']


def test_explicit_setting_changes_keep_their_immediate_priority(tmp_path, monkeypatch):
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    ws=Workspace(tmp_path);registry=Registry(tmp_path)
    registry.enqueue_source({'s1'},'order-feed:snapshot:10')
    registry.enqueue_files({'s2'},'nas:'+'a'*64)
    with registry.transaction() as conn:
        conn.execute("UPDATE pending SET next_attempt=0 WHERE store_id='s1'")
    model=_model(stores=(Store(id='s1',name='一店',platform='taobao'),Store(id='s2',name='二店',platform='taobao')))
    calls=[]
    def compute(_ws,_model,store,**kwargs):
        calls.append(store.id)
        return SimpleNamespace(failure=None,periods=[{'run_id':99}])
    monkeypatch.setattr('ledger.commission_manager.service.recompute',compute)
    Manager(lambda:ws,lambda:model).once()
    assert calls==['s1']


def test_completed_file_generation_loses_priority_despite_new_erp_events(tmp_path, monkeypatch):
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    ws=Workspace(tmp_path);registry=Registry(tmp_path)
    registry.enqueue_files({'s1','s2'},'nas:'+'a'*64)
    with registry.transaction() as conn:
        conn.execute("UPDATE pending SET next_attempt=1 WHERE store_id='s1'")
        conn.execute("UPDATE pending SET next_attempt=2 WHERE store_id='s2'")
    model=_model(stores=(Store(id='s1',name='一店',platform='taobao'),Store(id='s2',name='二店',platform='taobao')))
    calls=[]
    def compute(_ws,_model,store,**kwargs):
        calls.append(store.id)
        registry.enqueue_source({store.id},'order-feed:snapshot:20')
        return SimpleNamespace(failure=None,periods=[{'run_id':99,'source_sync_pending':True}])
    monkeypatch.setattr('ledger.commission_manager.service.recompute',compute)
    manager=Manager(lambda:ws,lambda:model);manager.once();manager.once()
    assert calls==['s1','s2']
    with registry.connect() as conn:
        assert all(r['files_revision']==r['files_applied_revision']==1 for r in conn.execute('SELECT * FROM pending'))


def test_file_received_during_calculation_keeps_new_generation_pending(tmp_path, monkeypatch):
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    ws=Workspace(tmp_path);registry=Registry(tmp_path)
    registry.enqueue_files({'s1'},'nas:'+'a'*64)
    def compute(*args,**kwargs):
        registry.enqueue_files({'s1'},'nas:'+'b'*64)
        return SimpleNamespace(failure=None,periods=[{'run_id':99}])
    monkeypatch.setattr('ledger.commission_manager.service.recompute',compute)
    Manager(lambda:ws,_model).once()
    with registry.connect() as conn:
        row=conn.execute("SELECT * FROM pending WHERE store_id='s1'").fetchone()
        assert row['files_revision']==2 and row['files_applied_revision']==1


def test_file_receipt_cannot_be_erased_when_feed_repeats_same_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_ORDER_FEED_ENABLED", "0")
    ws = Workspace(tmp_path)
    registry = Registry(tmp_path)
    registry.enqueue_source({"s1"}, "order-feed:snapshot:10")
    def compute(*args, **kwargs):
        registry.enqueue_files({"s1"}, "nas:" + "a" * 64)
        # Same feed prefix arriving again must not undo the independent file generation.
        registry.enqueue_source({"s1"}, "order-feed:snapshot:10")
        return SimpleNamespace(failure=None, periods=[])
    monkeypatch.setattr("ledger.commission_manager.service.recompute", compute)
    Manager(lambda: ws, _model).once()
    with registry.connect() as conn:
        row = conn.execute("SELECT * FROM pending WHERE store_id='s1'").fetchone()
        assert row["source_seq"] == 10
        assert row["files_revision"] == 1
    assert registry.revision() == 0


def test_component_fingerprint_does_not_replace_the_numeric_sequence(tmp_path, monkeypatch):
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    ws=Workspace(tmp_path);registry=Registry(tmp_path)
    first='order-feed:snapshot:10:components:'+'a'*64
    next_version='order-feed:snapshot:10:components:'+'b'*64
    registry.enqueue_source({'s1'},first)
    def compute(*args,**kwargs):
        registry.enqueue_source({'s1'},next_version)
        return SimpleNamespace(failure=None,periods=[])
    monkeypatch.setattr('ledger.commission_manager.service.recompute',compute)
    Manager(lambda:ws,_model).once()
    with registry.connect() as conn:
        pending=conn.execute("SELECT * FROM pending WHERE store_id='s1'").fetchone()
        assert pending['source_seq']==10
        assert pending['source_fingerprint']==next_version


def test_new_events_keep_worker_lease_so_other_stores_are_not_starved(tmp_path, monkeypatch):
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    ws=Workspace(tmp_path);registry=Registry(tmp_path)
    registry.enqueue_source({'s1'},'order-feed:snapshot:10')
    registry.enqueue_source({'s2'},'order-feed:snapshot:10')
    with registry.transaction() as conn:
        conn.execute("UPDATE pending SET next_attempt=0 WHERE store_id='s1'")
        conn.execute("UPDATE pending SET next_attempt=1 WHERE store_id='s2'")
    model=_model(stores=(Store(id='s1',name='一店',platform='taobao'),
                         Store(id='s2',name='二店',platform='taobao')))
    calls=[]
    def compute(_ws,_model,store,**_kwargs):
        calls.append(store.id)
        if store.id=='s1':
            registry.enqueue_source({'s1'},'order-feed:snapshot:20')
            return SimpleNamespace(failure=None,periods=[{'source_sync_pending':True}])
        return SimpleNamespace(failure=None,periods=[])
    monkeypatch.setattr('ledger.commission_manager.service.recompute',compute)
    manager=Manager(lambda:ws,lambda:model)
    manager.once();manager.once()
    assert calls==['s1','s2']
    with registry.connect() as conn:
        pending=conn.execute("SELECT * FROM pending WHERE store_id='s1'").fetchone()
        assert pending['source_seq']==20 and pending['next_attempt']>int(__import__('time').time())
