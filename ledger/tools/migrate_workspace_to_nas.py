"""Copy a Ledger workspace into the NAS authority layout without touching the source.

Every historical blob is copied to the content-addressed archive. Active slots are additionally
materialized under ``10_已接收``. Existing files are never overwritten with different bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


SOURCE_PREFIXES = (
    ("聚水潭成本", "聚水潭成本"),
    ("售后单", "聚水潭售后单"),
    ("订单明细", "订单明细"),
    ("对账", "对账（资金流水）"),
    ("推广", "推广"),
    ("运费", "运费"),
    ("小额打款", "小额打款"),
    ("刷单", "刷单（本金佣金）"),
    ("代发", "代发"),
    ("代购", "代发"),
    ("补发", "补发"),
    ("权益保险", "权益保险（保费支出）"),
    ("保费支出", "权益保险（保费支出）"),
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def source_for_name(name: str) -> str:
    normalized = Path(name).name.replace(" ", "")
    for prefix, source in SOURCE_PREFIXES:
        if normalized.startswith(prefix):
            return source
    raise ValueError(f"无法从文件名判定数据源：{name}")


def copy_verified(source: Path, target: Path, sha: str, apply: bool) -> str:
    if not source.is_file():
        raise FileNotFoundError(source)
    if file_hash(source) != sha:
        raise ValueError(f"源文件哈希不匹配：{source}")
    if target.exists():
        if file_hash(target) != sha:
            raise FileExistsError(f"目标已存在不同内容：{target}")
        return "existing"
    if not apply:
        return "planned"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".{os.getpid()}.part")
    shutil.copy2(source, temporary)
    if file_hash(temporary) != sha:
        temporary.unlink(missing_ok=True)
        raise ValueError(f"复制后哈希不匹配：{target}")
    temporary.replace(target)
    return "copied"


def migrate(workspace: Path, nas_root: Path, layout_path: Path, *, apply: bool) -> dict:
    database = workspace / "workspace.db"
    layout = json.loads(layout_path.read_text(encoding="utf-8-sig"))
    stores = {store["id"]: store for store in layout["stores"]}
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    files = list(connection.execute("select sha,name,size,first_seen from file order by sha"))
    slots = list(connection.execute("select store_id,name,sha,updated_at,by from slot order by store_id,name"))
    versions_by_sha: dict[str, list[dict]] = defaultdict(list)
    for row in connection.execute("select id,store_id,name,sha,at,by from version order by id"):
        versions_by_sha[row["sha"]].append(dict(row))
    connection.close()

    counts = defaultdict(int)
    errors: list[str] = []
    archive_entries = []
    for row in files:
        sha = row["sha"]
        source = workspace / "files" / sha[:2] / sha
        target = nas_root / "90_历史版本" / sha[:2] / sha / "payload"
        try:
            status = copy_verified(source, target, sha, apply)
            counts[f"archive_{status}"] += 1
            archive_entries.append({
                "sha256": sha, "size": row["size"], "original_name": row["name"],
                "first_seen": row["first_seen"], "versions": versions_by_sha.get(sha, []),
            })
        except Exception as exc:  # keep a complete audit rather than stop at the first bad blob
            errors.append(str(exc))

    active_entries = []
    for row in slots:
        sha, name, store_id = row["sha"], Path(row["name"]).name, row["store_id"]
        source = workspace / "files" / sha[:2] / sha
        try:
            source_name = source_for_name(name)
            if store_id == "__shared__":
                relative = Path("10_已接收") / "00_全公司共享" / source_name / name
            else:
                store = stores[store_id]
                relative = Path("10_已接收") / store["platform_name"] / store["folder"] / source_name / name
            status = copy_verified(source, nas_root / relative, sha, apply)
            counts[f"active_{status}"] += 1
            active_entries.append({
                "store_id": store_id, "name": name, "sha256": sha,
                "source": source_name, "relative_path": relative.as_posix(), "status": status,
            })
        except Exception as exc:
            errors.append(str(exc))

    manifest = {
        "schema": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "apply": apply,
        "workspace": str(workspace),
        "nas_root": str(nas_root),
        "counts": dict(counts),
        "archive": archive_entries,
        "active": active_entries,
        "errors": errors,
    }
    if apply:
        manifest_dir = nas_root / "99_系统" / "migrations"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = manifest_dir / f"workspace-{stamp}.json"
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--nas-root", type=Path, required=True)
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = migrate(args.workspace, args.nas_root, args.layout, apply=args.apply)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
