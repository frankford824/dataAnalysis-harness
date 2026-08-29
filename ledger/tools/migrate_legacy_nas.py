"""Copy the legacy NAS tree into the new authority tree, leaving legacy files untouched."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from tools.migrate_workspace_to_nas import copy_verified, file_hash, source_for_name


def unique_target(target: Path, sha: str) -> Path:
    if not target.exists() or file_hash(target) == sha:
        return target
    return target.with_name(f"{target.stem}__nas_{sha[:8]}{target.suffix}")


def migrate(legacy_root: Path, nas_root: Path, layout_path: Path, *, apply: bool) -> dict:
    layout = json.loads(layout_path.read_text(encoding="utf-8-sig"))
    stores_by_name = {store["name"].casefold(): store for store in layout["stores"]}
    entries, errors = [], []
    counts: dict[str, int] = {}

    candidates = [
        path for path in legacy_root.rglob("*") if path.is_file()
        and nas_root not in path.parents and "#recycle" not in path.parts and "@eaDir" not in path.parts
    ]
    for source_path in sorted(candidates):
        try:
            sha = file_hash(source_path)
            archive = nas_root / "90_历史版本" / sha[:2] / sha / "payload"
            archive_status = copy_verified(source_path, archive, sha, apply)
            relative_parts = source_path.relative_to(legacy_root).parts
            authority = "calculation"
            if len(relative_parts) == 1:
                source_name = source_for_name(source_path.name)
                destination = nas_root / "10_已接收" / "00_全公司共享" / source_name / source_path.name
                store_id = "__shared__"
            else:
                marker_index = next((i for i, value in enumerate(relative_parts)
                    if value in {"聚水潭店铺名称-导入版本", "聚水潭店铺名称-手工"}), None)
                if marker_index is None or marker_index + 1 >= len(relative_parts):
                    raise ValueError(f"不认识的旧目录：{source_path}")
                marker = relative_parts[marker_index]
                authority = "search_only" if marker.endswith("手工") else "calculation"
                store_name = relative_parts[marker_index + 1]
                store = stores_by_name.get(store_name.casefold())
                if store is None:
                    raise ValueError(f"旧目录店铺未登记：{store_name}")
                source_name = source_for_name(source_path.name)
                destination = (nas_root / "10_已接收" / store["platform_name"] / store["folder"]
                    / source_name)
                if authority == "search_only":
                    destination /= "仅搜索_手工"
                destination /= source_path.name
                store_id = store["id"]
            destination = unique_target(destination, sha)
            active_status = copy_verified(source_path, destination, sha, apply)
            counts[f"archive_{archive_status}"] = counts.get(f"archive_{archive_status}", 0) + 1
            counts[f"active_{active_status}"] = counts.get(f"active_{active_status}", 0) + 1
            entries.append({
                "legacy_path": str(source_path), "sha256": sha, "store_id": store_id,
                "source": source_name, "authority": authority,
                "relative_path": destination.relative_to(nas_root).as_posix(),
            })
        except Exception as exc:
            errors.append(str(exc))

    manifest = {
        "schema": 1, "created_at": datetime.now(timezone.utc).isoformat(), "apply": apply,
        "counts": counts, "entries": entries, "errors": errors,
    }
    if apply:
        target = nas_root / "99_系统" / "migrations" / (
            "legacy-nas-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--nas-root", type=Path, required=True)
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = migrate(args.legacy_root, args.nas_root, args.layout, apply=args.apply)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
