from types import SimpleNamespace

from ledger.commission_manager import Manager
from ledger.commission_registry import Registry
from ledger.workspace import Workspace
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
