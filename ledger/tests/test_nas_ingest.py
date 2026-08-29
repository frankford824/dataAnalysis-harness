from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

from ledger.model import load_model
from ledger.nas_ingest import APPLY_SCHEMA, reconcile_missing, reconcile_ready
from ledger.workspace import Workspace


MODEL = Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"


CATALOG = """
create table file_catalog (
 path text primary key, sha256 text, size integer, mtime_ns integer,
 platform text, store_id text, source text, authority text, state text,
 rows integer, sheets integer, parquet_path text, error text, indexed_at text,
 last_seen_generation integer default 0, last_changed integer default 0,
 missing_scans integer default 0, missing_since integer
);
create table scan_meta (
 id integer primary key, generation integer, last_started text, last_completed text,
 root_reachable integer, last_error text
);
insert into scan_meta values(1,1,'now','now',1,'');
"""


def add_catalog(catalog: Path, path: Path, *, authority="calculation", missing=0, missing_since=None):
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    connection = sqlite3.connect(catalog)
    connection.execute(
        "insert into file_catalog(path,sha256,size,mtime_ns,platform,store_id,source,authority,state,rows,sheets,parquet_path,error,indexed_at,missing_scans,missing_since) "
        "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(path), sha, path.stat().st_size, 1, "淘宝天猫", "taobao_xibishun", "运费",
         authority, "ready", 1, 1, "", "", "now", missing, missing_since),
    )
    connection.commit()
    connection.close()
    return sha


def test_ready_file_is_applied_and_search_only_is_not(tmp_path):
    root = tmp_path / "台账系统"
    accepted = root / "10_已接收" / "淘宝天猫" / "汪学成-天猫喜必顺旗舰店 [taobao_xibishun]" / "运费"
    accepted.mkdir(parents=True)
    active = accepted / "运费-淘宝喜必顺.csv"
    active.write_text("运单号,金额\nA1,1\n", encoding="utf-8")
    manual = accepted / "手工" / "运费-淘宝喜必顺-手工.csv"
    manual.parent.mkdir()
    manual.write_text("运单号,金额\nA2,2\n", encoding="utf-8")
    catalog = tmp_path / "catalog.db"
    connection = sqlite3.connect(catalog)
    connection.executescript(CATALOG)
    connection.close()
    add_catalog(catalog, active)
    add_catalog(catalog, manual, authority="search_only")

    workspace = Workspace(tmp_path / "workspace")
    result = reconcile_ready(workspace, load_model(MODEL), catalog, root)
    assert not result["errors"]
    assert any(row["name"] == active.name for row in workspace.submissions())
    assert all(row["name"] != manual.name for row in workspace.submissions())
    states = dict(sqlite3.connect(catalog).execute("select name,state from ledger_apply"))
    assert states[active.name] == "applied"
    assert states[manual.name] == "search_only"


def test_missing_requires_guard_then_forgets(tmp_path):
    root = tmp_path / "台账系统"
    file = root / "10_已接收" / "淘宝天猫" / "store" / "运费" / "运费-淘宝喜必顺.csv"
    file.parent.mkdir(parents=True)
    file.write_text("运单号,金额\nA1,1\n", encoding="utf-8")
    catalog = tmp_path / "catalog.db"
    connection = sqlite3.connect(catalog)
    connection.executescript(CATALOG)
    connection.close()
    sha = add_catalog(catalog, file, missing=3, missing_since=int(time.time()) - 700)
    workspace = Workspace(tmp_path / "workspace")
    workspace.keep(file.name, file, "taobao_xibishun")
    connection = sqlite3.connect(catalog)
    connection.executescript(APPLY_SCHEMA)
    connection.execute(
        "insert into ledger_apply(path,sha256,store_id,name,state,applied_at) values(?,?,?,?,?,?)",
        (str(file), sha, "taobao_xibishun", file.name, "applied", "now"),
    )
    connection.commit()
    connection.close()
    file.unlink()

    result = reconcile_missing(workspace, load_model(MODEL), catalog)
    assert result["removed"] == 1
    assert not workspace.submissions("taobao_xibishun")
