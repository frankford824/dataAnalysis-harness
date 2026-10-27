from io import BytesIO
import json

import polars as pl
import pytest

from ledger import service, recompute_reuse
from ledger.commission_registry import Registry
from ledger.order_feed import OrderFeed
from ledger.engine.types import ANCHOR_SHA
from ledger.workspace import Workspace
from test_pdd_historical_pricing import scenario


def test_feed_content_identity_ignores_only_external_cursor():
    frame = pl.DataFrame({"order_id": ["1"], "unit_cost": [3.], ANCHOR_SHA: ["cursor-a"]})
    template = OrderFeed._cost_template()
    first = OrderFeed._item("order_cost", template.id, "cost", frame, template, "cursor-a")
    second = OrderFeed._item("order_cost", template.id, "cost", frame.with_columns(pl.lit("cursor-b").alias(ANCHOR_SHA)), template, "cursor-b")
    changed = OrderFeed._item("order_cost", template.id, "cost", frame.with_columns(pl.lit(4.).alias("unit_cost")), template, "cursor-b")
    assert first.ref.sha256 == second.ref.sha256
    assert first.frame.equals(second.frame)
    assert changed.ref.sha256 != first.ref.sha256


def setup_calculation(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_ORDER_FEED_ENABLED", "0")
    monkeypatch.setattr(recompute_reuse, "engine_version", lambda: "verified-release")
    model, result = scenario()
    ws = Workspace(tmp_path)
    ws.keep("input.csv", BytesIO(b"input"), "s")
    registry = Registry(tmp_path)
    calls = []
    monkeypatch.setattr(service, "ingest", lambda *a, **k: result.ingestion)
    def calculate(*a, **k):
        calls.append(1)
        return result
    monkeypatch.setattr(service, "run", calculate)
    return ws, model, result, registry, calls


def test_unchanged_captured_inputs_skip_engine_but_not_integrity_checks(tmp_path, monkeypatch):
    ws, model, result, registry, calls = setup_calculation(tmp_path, monkeypatch)
    first = service.recompute(ws, model, model.store("s"))
    second = service.recompute(ws, model, model.store("s"))
    assert first.failure is second.failure is None
    assert len(calls) == 1
    assert first.periods == second.periods
    path = ws.facts_path(first.periods[0]["run_id"])
    path.write_bytes(b"damaged")
    service.recompute(ws, model, model.store("s"))
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["registry", "frame", "sync", "model", "dirty"])
def test_each_material_input_invalidates_reuse(tmp_path, monkeypatch, change):
    ws, model, result, registry, calls = setup_calculation(tmp_path, monkeypatch)
    service.recompute(ws, model, model.store("s"))
    if change == "registry":
        with registry.transaction() as conn:
            registry.audit(conn, "tester", "change", "s", "new policy", {}, {})
    elif change == "frame":
        result.ingestion.items[0].frame = result.ingestion.items[0].frame.with_columns(pl.lit("different").alias("order_remark"))
    elif change == "sync":
        result.ingestion.source_sync_pending = True
    elif change == "model":
        model = model.model_copy(update={"name": "changed"})
    else:
        monkeypatch.setattr(recompute_reuse, "engine_version", lambda: "same-dirty")
    service.recompute(ws, model, model.store("s"))
    assert len(calls) == 2


def test_shared_change_reaches_feed_only_stores_with_existing_periods(tmp_path):
    ws = Workspace(tmp_path)
    ws.record("feed-only", "2026-07", {"can_close": False}, [])
    assert ws.store_ids() == ["feed-only"]


def test_changed_saved_payload_cannot_reuse_same_run_id(tmp_path, monkeypatch):
    ws, model, result, registry, calls = setup_calculation(tmp_path, monkeypatch)
    first = service.recompute(ws, model, model.store("s"))
    ws.conn.execute("update run set result=json_set(result, '$.changed', 1) where id=?", (first.periods[0]["run_id"],))
    ws.conn.commit()
    service.recompute(ws, model, model.store("s"))
    assert len(calls) == 2


@pytest.mark.parametrize('change',['missing','different_source'])
def test_eligible_reuse_requires_first_proof_of_the_same_source_bytes(tmp_path,monkeypatch,change):
    ws,model,result,registry,calls=setup_calculation(tmp_path,monkeypatch)
    first=service.recompute(ws,model,model.store('s'))
    service.recompute(ws,model,model.store('s'))
    assert not first.failure and len(calls)==1  # Prove the clean-only cache is eligible.
    run=first.periods[0]['run_id']
    with ws.conn:
        # Explicit corruption simulation of a temporary database; production
        # continues to protect the first immutable proof from these writes.
        if change=='missing':
            ws.conn.execute('DROP TRIGGER automation_run_proof_no_delete')
            ws.conn.execute('DELETE FROM automation_run_proof WHERE run_id=?',(run,))
        else:
            row=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(run,)).fetchone()
            manifest=json.loads(row[0]);manifest['source_code_sha']='0'*64
            ws.conn.execute('DROP TRIGGER automation_run_proof_no_update')
            ws.conn.execute('UPDATE automation_run_proof SET manifest_json=? WHERE run_id=?',(json.dumps(manifest),run))
    service.recompute(ws,model,model.store('s'))
    assert len(calls)==2


def test_value_digest_survives_parquet_roundtrip_and_preserves_nan(tmp_path):
    from hashlib import sha256
    from ledger.content_fingerprint import update_frame
    frame = pl.DataFrame({"value": [1., None, float('nan'), float('inf'), -0.], "key": ["a", None, "b", "c", "d"]})
    path = tmp_path / 'cache.parquet'
    frame.write_parquet(path)
    def hashed(value):
        h = sha256()
        update_frame(h, value)
        return h.hexdigest()
    assert hashed(frame) == hashed(pl.read_parquet(path))
    assert hashed(frame) != hashed(frame.with_columns(pl.col('value').fill_nan(None)))
