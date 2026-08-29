"""Generate and safely materialize the NAS directory contract.

The script only creates missing directories and a manifest.  It never removes, renames, or
overwrites an existing business file, so the legacy NAS layout remains untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ledger.model import load_model


INVALID_WINDOWS = re.compile(r'[\\/:*?"<>|]')
SHARED_SOURCE_IDS = {"after_sales", "freight"}


def safe_segment(value: str) -> str:
    cleaned = INVALID_WINDOWS.sub("_", value).strip().rstrip(".")
    if not cleaned:
        raise ValueError(f"目录名为空：{value!r}")
    return cleaned


def model_fingerprint(model_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in model_root.iterdir() if p.is_file()):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_layout(model_root: Path) -> dict:
    model = load_model(model_root)
    platforms = {platform.id: safe_segment(platform.name) for platform in model.platforms}
    sources = {source.id: safe_segment(source.name) for source in model.sources}
    directories = {
        "00_上传区/00_全公司共享",
        "10_已接收/00_全公司共享",
        "20_需修正",
        "90_历史版本",
        "99_系统/manifests",
        "99_系统/migrations",
        "99_系统/health",
    }
    for source_id in SHARED_SOURCE_IDS:
        if source_id in sources:
            directories.add(f"00_上传区/00_全公司共享/{sources[source_id]}")
            directories.add(f"10_已接收/00_全公司共享/{sources[source_id]}")

    stores = []
    for store in model.active_stores():
        platform_name = platforms[store.platform]
        folder = f"{safe_segment(store.name)} [{safe_segment(store.id)}]"
        stores.append({
            "id": store.id,
            "name": store.name,
            "platform_id": store.platform,
            "platform_name": platform_name,
            "folder": folder,
        })
        for area in ("00_上传区", "10_已接收"):
            for source_name in sources.values():
                directories.add(f"{area}/{platform_name}/{folder}/{source_name}")

    return {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_fingerprint": model_fingerprint(model_root),
        "platforms": [
            {"id": platform.id, "name": platform.name} for platform in model.platforms
        ],
        "stores": stores,
        "sources": [
            {"id": source.id, "name": source.name} for source in model.sources
        ],
        "directories": sorted(directories),
    }


def apply_layout(root: Path, manifest: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for relative in manifest["directories"]:
        (root / Path(relative)).mkdir(parents=True, exist_ok=True)
    target = root / "99_系统" / "manifests" / "layout.json"
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    if target.exists() and target.read_bytes() == encoded:
        return
    temporary = target.with_suffix(".json.tmp")
    temporary.write_bytes(encoded)
    temporary.replace(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply-root", type=Path)
    args = parser.parse_args()
    manifest = build_layout(args.model.resolve())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    if args.apply_root:
        apply_layout(args.apply_root, manifest)
    if not args.output and not args.apply_root:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
