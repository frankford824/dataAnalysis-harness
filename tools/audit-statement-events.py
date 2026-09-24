"""Read-only candidate event-dedup audit against immutable active source blobs.

Run with the candidate statement_dedupe module and templates YAML alongside
this script. Never opens the business database for writing or publishes runs.
"""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from collections import defaultdict
from decimal import Decimal

import polars as pl
import yaml

from ledger.engine.runtime import Ingestion, ingest
from ledger.model.loader import load_model

sys.stdout.reconfigure(encoding="utf-8")
root, model_root = map(Path, sys.argv[1:3])
candidate = Path(__file__).parent
spec = importlib.util.spec_from_file_location("ledger.engine.statement_dedupe", candidate / "statement_dedupe.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
metadata = {t["id"]: t for t in yaml.safe_load((candidate / "templates.yaml").read_text(encoding="utf-8"))}
model = load_model(model_root)
model = model.model_copy(update={
    "sources": tuple(s.model_copy(update={"dedupe_key": ()}) for s in model.sources),
    "templates": tuple(t.model_copy(update={k: metadata.get(t.id, {}).get(k) for k in ("event_namespace", "event_id_role", "event_directional")}) for t in model.templates),
})
with sqlite3.connect(root.joinpath("workspace.db").as_uri() + "?mode=ro", uri=True) as db:
    rows = db.execute("select store_id,name,sha from slot where name like '%对账%' or name like '%结算%' order by store_id,name").fetchall()
groups = defaultdict(list)
for store, name, sha in rows:
    groups[store].append((name, sha))
results = []
for store, slots in groups.items():
    try:
        registered = next(s for s in model.stores if s.id == store)
        files = [root / "files" / sha[:2] / sha for name, sha in slots]
        ing = ingest(files, model, [registered.name, *registered.aliases], default_store=registered.name)
        # Blob paths carry no display name; audit labels use original slot names.
        names = {sha: name for name, sha in slots}
        from dataclasses import replace
        for i in ing.items:
            i.ref = replace(i.ref, filename=names.get(i.ref.sha256, i.ref.filename))
        ing.validation_errors, ing.deduplication = {}, []
        before = sum(i.frame.height for i in ing.frames_of("settlement"))
        module.dedupe_statements(ing, model.source("settlement"))
        after = sum(i.frame.height for i in ing.frames_of("settlement"))
        result = dict(store=store, name=registered.name, files=len(slots), before=before, after=after,
            removed=before-after, conflicts=ing.validation_errors,
            deduplication=ing.deduplication,
            unrecognized=[dict(file=i.ref.filename, error=i.error) for i in ing.unknown if not i.derivative and i.rows])
        results.append(result)
        print(json.dumps({k: v for k, v in result.items() if k not in ("deduplication", "conflicts", "unrecognized")} | {"conflict_groups": len(ing.validation_errors), "unrecognized": len(result["unrecognized"])}, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(json.dumps(dict(store=store, error=str(exc)), ensure_ascii=False), flush=True)
        results.append(dict(store=store, error=str(exc)))
(candidate / "audit-statements.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
