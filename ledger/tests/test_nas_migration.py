from __future__ import annotations

import hashlib
import json
import sqlite3

from tools.migrate_legacy_nas import migrate as migrate_legacy
from tools.migrate_workspace_to_nas import migrate as migrate_workspace


def layout(tmp_path):
    path = tmp_path / "layout.json"
    path.write_text(json.dumps({"stores": [{
        "id": "taobao_xibishun", "name": "淘宝喜必顺", "platform_name": "淘宝天猫",
        "folder": "淘宝喜必顺 [taobao_xibishun]",
    }]}), encoding="utf-8")
    return path


def test_workspace_copy_is_verified_and_idempotent(tmp_path):
    workspace = tmp_path / "workspace"
    files = workspace / "files"
    files.mkdir(parents=True)
    payload = b"order_id,amount\nA1,1\n"
    sha = hashlib.sha256(payload).hexdigest()
    blob = files / sha[:2] / sha
    blob.parent.mkdir()
    blob.write_bytes(payload)
    connection = sqlite3.connect(workspace / "workspace.db")
    connection.executescript("""
      create table file(sha text,name text,size integer,first_seen text);
      create table slot(store_id text,name text,sha text,updated_at text,by text);
      create table version(id integer,store_id text,name text,sha text,at text,by text);
    """)
    name = "订单明细-淘宝喜必顺.csv"
    connection.execute("insert into file values(?,?,?,?)", (sha, name, len(payload), "now"))
    connection.execute("insert into slot values(?,?,?,?,?)", ("taobao_xibishun", name, sha, "now", ""))
    connection.execute("insert into version values(?,?,?,?,?,?)", (1, "taobao_xibishun", name, sha, "now", ""))
    connection.commit()
    connection.close()
    root = tmp_path / "nas"

    first = migrate_workspace(workspace, root, layout(tmp_path), apply=True)
    second = migrate_workspace(workspace, root, layout(tmp_path), apply=True)
    assert not first["errors"] and not second["errors"]
    assert first["counts"] == {"archive_copied": 1, "active_copied": 1}
    assert second["counts"] == {"archive_existing": 1, "active_existing": 1}
    assert (root / "90_历史版本" / sha[:2] / sha / "payload").read_bytes() == payload


def test_legacy_manual_is_search_only_and_source_is_untouched(tmp_path):
    legacy = tmp_path / "legacy"
    imported = legacy / "2026年6月份" / "聚水潭店铺名称-导入版本" / "淘宝喜必顺"
    manual = legacy / "2026年6月份" / "聚水潭店铺名称-手工" / "淘宝喜必顺"
    imported.mkdir(parents=True)
    manual.mkdir(parents=True)
    (imported / "订单明细-淘宝喜必顺.csv").write_text("id\nA1\n", encoding="utf-8")
    (manual / "订单明细-淘宝喜必顺.xlsx").write_bytes(b"manual")
    root = tmp_path / "nas"

    result = migrate_legacy(legacy, root, layout(tmp_path), apply=True)
    assert not result["errors"]
    assert len(result["entries"]) == 2
    manual_entry = next(entry for entry in result["entries"] if entry["authority"] == "search_only")
    assert "仅搜索_手工" in manual_entry["relative_path"]
    assert (manual / "订单明细-淘宝喜必顺.xlsx").read_bytes() == b"manual"
