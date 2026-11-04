import threading
import time
from types import SimpleNamespace

from ledger import automation_actions as actions
from ledger import frozen_refund_guard as guard


def test_concurrent_month_readers_share_one_evaluation(monkeypatch):
    calls = []
    started = threading.Event()

    def slow(ws, model, period):
        calls.append(period)
        started.set()
        time.sleep(0.3)
        return {'period': period, 'stores': [{'store_id': 's1'}]}

    monkeypatch.setattr(actions, '_month', slow)
    ws = SimpleNamespace(root='/tmp/ws-share')
    results = []
    first = threading.Thread(target=lambda: results.append(actions.month(ws, None, '2030-01')))
    first.start()
    started.wait(2)
    others = [threading.Thread(target=lambda: results.append(actions.month(ws, None, '2030-01'))) for _ in range(3)]
    for thread in others: thread.start()
    for thread in [first, *others]: thread.join(5)
    assert calls == ['2030-01']
    assert len(results) == 4 and all(r == results[0] for r in results)
    results[1]['stores'].append('mutated')
    assert results[0]['stores'] == [{'store_id': 's1'}]
    actions.month(ws, None, '2030-01')
    assert calls == ['2030-01', '2030-01']


def test_month_failure_reaches_every_waiter_and_clears(monkeypatch):
    def broken(ws, model, period):
        time.sleep(0.2)
        raise ValueError('boom')

    monkeypatch.setattr(actions, '_month', broken)
    ws = SimpleNamespace(root='/tmp/ws-fail')
    errors = []

    def read():
        try: actions.month(ws, None, '2030-02')
        except ValueError as exc: errors.append(str(exc))

    threads = [threading.Thread(target=read) for _ in range(3)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(5)
    assert errors == ['boom'] * 3
    assert not actions._month_flights


def test_refund_validation_reuse_only_when_requested(monkeypatch, tmp_path):
    calls = []

    class FakeContext:
        def __init__(self, ws, model, store): self.snapshot = [('2030-01', 7, 1, None)]

    def compute(ws, model, store, period, run_id, context):
        calls.append(run_id)
        return {'sha': 'x', 'recompute_required': False}

    ws = SimpleNamespace(root=tmp_path, facts_path=lambda run_id: tmp_path / f'{run_id}.parquet')
    store = SimpleNamespace(id='s1')
    model = object()
    monkeypatch.setattr(guard, 'Context', FakeContext)
    monkeypatch.setattr(guard, '_validate_run', compute)
    guard._validations.clear()
    guard.validate_run(ws, model, store, '2030-02', 9, reuse=True)
    guard.validate_run(ws, model, store, '2030-02', 9, reuse=True)
    assert calls == [9]
    guard.validate_run(ws, model, store, '2030-02', 9)
    assert calls == [9, 9]
    (tmp_path / '7.parquet').write_bytes(b'changed archive')
    guard.validate_run(ws, model, store, '2030-02', 9, reuse=True)
    assert calls == [9, 9, 9]
    guard.validate_run(ws, object(), store, '2030-02', 9, reuse=True)
    assert calls == [9, 9, 9, 9]
