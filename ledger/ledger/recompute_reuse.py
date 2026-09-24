"""Bounded reuse of a complete, captured input set before running the engine.

This is a process-local optimization, never another ledger or source of truth.
Freshness is read on every call; archived files and current run pointers are
validated on every hit. Dirty/unknown builds deliberately do not use it.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path

from .storage_integrity import digest, verified
from .version import engine_version
from .content_fingerprint import update_frame

_lock = threading.Lock()
_entries = OrderedDict()
_LIMIT = 64


def revisions(ws, registry):
    return (
        registry.revision() if registry else None,
        ws.conn.execute("select coalesce(max(id),0) from config_log").fetchone()[0],
        ws.conn.execute("select coalesce(max(id),0) from cost_line_log").fetchone()[0],
    )


def fingerprint(ws, model, store, ingestion, registry):
    version = engine_version()
    if not version or version == "unknown" or "dirty" in version:
        return None
    state = revisions(ws, registry)
    h = hashlib.sha256(json.dumps([version, model.model_dump_json(), store.id,
                                  state, ingestion.source_sync_pending, ingestion.validation_errors,
                                  ingestion.deduplication], ensure_ascii=False, sort_keys=True).encode())
    for item in ingestion.items:
        # All post-feed enrichment is included, not just the raw file hash or
        # amounts. Thus flags, identities, dates, quantities and price evidence
        # invalidate reuse even when their numeric totals happen to match.
        h.update(json.dumps([item.ref.sha256, item.ref.filename, item.ref.sheet,
                             item.error, item.recognition.template_id,
                             item.template.model_dump_json() if item.template else None,
                             asdict(item.derivative) if item.derivative else None],
                            ensure_ascii=False, default=str, sort_keys=True).encode())
        if item.frame is not None:
            update_frame(h, item.frame)
    return h.hexdigest(), state


def _key(ws, store):
    return str(ws.root.resolve()), store.id


def _run_signature(row):
    return hashlib.sha256(json.dumps([row["result"], row["engine"], row["model_revision"],
                                      row["input_fingerprint"]], ensure_ascii=False).encode()).hexdigest()


def load(ws, store, signature, registry):
    if signature is None:
        return None
    with _lock:
        entry = _entries.get(_key(ws, store))
    if not entry or entry[0] != signature:
        return None
    output = []
    for period, run_id, recorded, paths in entry[1]:
        row = ws.latest_run(store.id, period)
        if not row or row["id"] != run_id or not row["evidence_ready"] or row["evidence_error"]:
            return None
        if _run_signature(row) != recorded:
            return None
        if any(not verified(path) for path in paths):
            return None
        payload = json.loads(row["result"])
        calculation = (payload.get("commission") or {}).get("calculation_id")
        if calculation:
            if registry is None:
                return None
            with registry.connect() as conn:
                proof = conn.execute("select path,sha from calculation where id=? and finance_run=?",
                                     (calculation, run_id)).fetchone()
            if not proof:
                return None
            path = registry.root / "calculations" / Path(proof["path"]).name
            if not path.is_file() or digest(path) != proof["sha"]:
                return None
        state = ws.state(store.id, period)
        shown = state.result if state and state.result else payload
        output.append({**shown, "run_id": run_id, "state": state.state if state else "open",
                       "stale": bool(state and state.stale)})
    if revisions(ws, registry) != signature[1]:
        return None
    return output


def remember(ws, store, signature, registry, periods):
    if signature is None or revisions(ws, registry) != signature[1] or not periods:
        return
    runs = []
    for payload in periods:
        run_id, period = payload["run_id"], payload["period"]
        row = ws.latest_run(store.id, period)
        if not row or row["id"] != run_id or not row["evidence_ready"]:
            return
        main = ws.facts_path(run_id)
        if not main.is_file():
            return
        original = json.loads(row["result"])
        if original.get("has_allocation_evidence") and not main.with_suffix(".allocation.parquet").is_file():
            return
        if original.get("pricing_pending_count") and not main.with_suffix(".pricing.parquet").is_file():
            return
        paths = tuple(path for path in [main, *[main.with_suffix(suffix) for suffix in
                      (".pricing.parquet", ".coverage.parquet", ".allocation.parquet")]] if path.is_file())
        runs.append((period, run_id, _run_signature(row), paths))
    with _lock:
        _entries[_key(ws, store)] = (signature, runs)
        _entries.move_to_end(_key(ws, store))
        while len(_entries) > _LIMIT:
            _entries.popitem(last=False)
