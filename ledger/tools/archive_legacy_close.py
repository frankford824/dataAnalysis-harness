"""Dry-run or apply the audited pre-Ledger close migration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ledger.historical_close import apply_plan, build_plan, summarize
from ledger.model.repository import ModelRepository
from ledger.workspace import open_workspace


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--source-2025", type=Path, required=True)
    parser.add_argument("--source-2026", type=Path, required=True)
    parser.add_argument("--nas-root", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    snapshot = ModelRepository(args.model).get()
    plan = build_plan(args.source_2025, args.source_2026, snapshot.model)
    result = summarize(plan)
    if args.apply:
        if not args.nas_root or not args.backup_dir:
            parser.error("--apply 必须同时给 --nas-root 和 --backup-dir")
        if plan.errors:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2
        workspace = open_workspace(args.workspace)
        try:
            result["applied"] = apply_plan(
                workspace,
                snapshot.model,
                snapshot.revision,
                plan,
                nas_root=args.nas_root,
                backup_dir=args.backup_dir,
            )
        finally:
            workspace.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if plan.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
