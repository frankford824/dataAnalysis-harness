"""Replay NAS candidates against a workspace copy and enforce cutover equivalence gates."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ledger import service
from ledger.model import load_model
from ledger.workspace import Workspace


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(workspace: Workspace) -> dict:
    return {
        f"{state.store_id}/{state.period}": {
            "state": state.state, "stale": state.stale, "result": state.result,
        }
        for state in workspace.overview()
    }


def compare(before: dict, after: dict) -> dict:
    common = sorted(before.keys() & after.keys())
    return {
        "result_changed": [key for key in common if before[key]["result"] != after[key]["result"]],
        "state_changed": [key for key in common if before[key]["state"] != after[key]["state"]],
        "stale_changed": [key for key in common if before[key]["stale"] != after[key]["stale"]],
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "closed_result_changed": [
            key for key in common
            if before[key]["state"] == "closed" and before[key]["result"] != after[key]["result"]
        ],
    }


def intake(workspace: Workspace, model, paths: list[Path], assignments: dict) -> dict:
    if not paths:
        return {"kept": 0, "rejected": [], "failures": [], "unknown_tables": []}
    source_ids = {source.name: source.id for source in model.sources}
    uploads = []
    for path in paths:
        sha = sha256(path)
        assignment = assignments[(path.name, sha)]
        uploads.append((
            path.name, path, assignment["store_id"], source_ids[assignment["source"]],
        ))
    result = service.intake_assigned(
        workspace, model, uploads, by="NAS切换副本回放",
    )
    return {
        "kept": len(result.kept),
        "unchanged": sum(item.unchanged for item in result.kept),
        "rejected": [item.__dict__ for item in result.rejected],
        "failures": result.failures,
        "unknown_tables": result.unknown_tables,
    }


def replay(
    workspace_root: Path, input_root: Path, model_root: Path, assignment_manifest: Path,
) -> dict:
    workspace = Workspace(workspace_root)
    model = load_model(model_root)
    manifest = json.loads(assignment_manifest.read_text(encoding="utf-8-sig"))
    assignments = {
        (Path(entry["relative_path"]).name, entry["sha256"]): entry
        for entry in manifest["entries"] if entry["authority"] == "calculation"
    }
    active_scope_shas = {
        (row[0], row[1]) for row in workspace.conn.execute("select store_id,sha from slot")
    }
    files = sorted(path for path in input_root.rglob("*") if path.is_file())
    existing, new = [], []
    for path in files:
        sha = sha256(path)
        assignment = assignments[(path.name, sha)]
        target = existing if (assignment["store_id"], sha) in active_scope_shas else new
        target.append(path)

    stored_baseline = snapshot(workspace)
    affected = {assignments[(path.name, sha256(path))]["store_id"] for path in existing}
    if "__shared__" in affected:
        active_ids = {store.id for store in model.active_stores()}
        affected.update(store_id for store_id in workspace.store_ids() if store_id in active_ids)
    affected.discard("__shared__")
    baseline_unknown = []
    for store_id in sorted(affected):
        recomputed = service.recompute(workspace, model, model.store(store_id))
        baseline_unknown.extend(recomputed.unknown_tables)
    baseline = snapshot(workspace)
    engine_compare = compare(stored_baseline, baseline)
    existing_intake = intake(workspace, model, existing, assignments)
    after_existing = snapshot(workspace)
    existing_compare = compare(baseline, after_existing)
    existing_equivalent = not any(
        existing_compare[key] for key in ("result_changed", "state_changed", "added", "removed")
    )

    new_intake = intake(workspace, model, new, assignments)
    final = snapshot(workspace)
    new_compare = compare(after_existing, final)
    closed_invariant = not new_compare["closed_result_changed"]
    unknown_key = lambda item: (
        item.get("store_id"), item.get("file"), item.get("sheet"), item.get("signature"),
    )
    existing_unknown = {
        unknown_key(item) for item in [*baseline_unknown, *existing_intake["unknown_tables"]]
    }
    introduced_unknown = [
        item for item in new_intake["unknown_tables"] if unknown_key(item) not in existing_unknown
    ]
    no_processing_errors = not any((
        existing_intake["rejected"], existing_intake["failures"],
        new_intake["rejected"], new_intake["failures"], introduced_unknown,
    ))
    return {
        "workspace": str(workspace_root),
        "input_files": len(files),
        "existing_sha_files": len(existing),
        "new_sha_files": len(new),
        "baseline_periods": len(baseline),
        "stored_engine_compare": engine_compare,
        "baseline_unknown_tables": baseline_unknown,
        "existing_intake": existing_intake,
        "existing_compare": existing_compare,
        "existing_equivalent": existing_equivalent,
        "new_intake": new_intake,
        "introduced_unknown_tables": introduced_unknown,
        "new_compare": new_compare,
        "closed_invariant": closed_invariant,
        "no_processing_errors": no_processing_errors,
        "cutover_gate": existing_equivalent and closed_invariant and no_processing_errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--assignments", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = replay(args.workspace, args.input, args.model, args.assignments)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if result["cutover_gate"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
