from __future__ import annotations

import hashlib
import shutil
import sqlite3
import time
from pathlib import Path

from ledger.model import load_model
from ledger import service
from ledger.commission_registry import Registry
from ledger.nas_ingest import (
    APPLY_SCHEMA,
    _extract_store_name,
    reconcile_missing,
    reconcile_ready,
    _pending_rows, _source_ids, _record, _relocate_catalog, _accept_uploaded, application_errors,
)


def test_reviewed_source_aliases_are_exact():
    labels = _source_ids(load_model(MODEL))
    assert labels["刷单"] == labels["补单"] == labels["刷单（本金佣金）"] == "brushing"
    assert "刷单截图" not in labels


def test_error_retry_is_bounded_and_visible(tmp_path):
    catalog = tmp_path / "catalog.db"
    conn = sqlite3.connect(catalog)
    conn.row_factory = sqlite3.Row
    conn.executescript(CATALOG + APPLY_SCHEMA)
    path = tmp_path / "file.csv"
    path.write_text("col\n1\n")
    add_catalog(catalog, path)
    row = conn.execute("select * from file_catalog").fetchone()
    _record(conn, row, "error", "read failed")
    conn.commit()
    assert _pending_rows(conn) == []
    assert application_errors(catalog)[0]["error"] == "read failed"
    conn.execute("update ledger_apply set applied_at='2020-01-01T00:00:00+00:00'")
    conn.commit()
    assert len(_pending_rows(conn)) == 1
    _record(conn, row, "applied")
    conn.commit()
    assert not application_errors(catalog)
    conn.close()


def test_duplicate_catalog_destination_is_idempotent_and_retains_alias(tmp_path):
    catalog = tmp_path / "catalog.db"
    conn = sqlite3.connect(catalog)
    conn.row_factory = sqlite3.Row
    conn.executescript(CATALOG + APPLY_SCHEMA)
    before, after = tmp_path / "before.csv", tmp_path / "after.csv"
    before.write_text("same")
    after.write_text("same")
    sha = add_catalog(catalog, before)
    add_catalog(catalog, after)
    for row in conn.execute("select * from file_catalog").fetchall():
        _record(conn, row, "applied")
    _relocate_catalog(conn, str(before), str(after), sha)
    _relocate_catalog(conn, str(before), str(after), sha)
    assert conn.execute("select state from ledger_apply where path=?", (str(before),)).fetchone()[0] == "relocated"
    assert conn.execute("select state from ledger_apply where path=?", (str(after),)).fetchone()[0] == "applied"
    conn.close()


def test_replacement_preserves_previous_accepted_bytes(tmp_path):
    upload = tmp_path / "00_上传区" / "s" / "f.csv"
    target = tmp_path / "10_已接收" / "s" / "f.csv"
    upload.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    upload.write_bytes(b"new")
    target.write_bytes(b"old")
    assert _accept_uploaded(upload, tmp_path, hashlib.sha256(b"new").hexdigest()) == target
    assert target.read_bytes() == b"new"
    old_sha = hashlib.sha256(b"old").hexdigest()
    assert (tmp_path / "90_历史版本" / old_sha[:2] / old_sha / "payload").read_bytes() == b"old"
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
insert into scan_meta values(1,1,'now',strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,'');
"""


def add_catalog(
    catalog: Path,
    path: Path,
    *,
    authority="calculation",
    missing=0,
    missing_since=None,
    platform="淘宝天猫",
    store_id="taobao_xibishun",
    source="运费",
    catalog_path: Path | None = None,
):
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    recorded = str(catalog_path or path)
    connection = sqlite3.connect(catalog)
    connection.execute(
        "insert into file_catalog(path,sha256,size,mtime_ns,platform,store_id,source,authority,state,rows,sheets,parquet_path,error,indexed_at,missing_scans,missing_since) "
        "values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (recorded, sha, path.stat().st_size, 1, platform, store_id, source,
         authority, "ready", 1, 1, "", "", "now", missing, missing_since),
    )
    connection.commit()
    connection.close()
    return sha


def write_feed(workspace_root: Path, ledger_store_id: str) -> None:
    connection = sqlite3.connect(workspace_root / "order-feed.db")
    connection.execute(
        "create table if not exists feed_store ("
        " order_store_id text primary key,"
        " ledger_store_id text not null,"
        " mapping_status text not null,"
        " payload_json text not null)"
    )
    connection.execute(
        "insert into feed_store(order_store_id,ledger_store_id,mapping_status,payload_json) "
        "values(?,?,?,?)",
        ("999001", ledger_store_id, "confirmed", "{}"),
    )
    connection.commit()
    connection.close()


def apply_states(catalog: Path) -> dict[str, str]:
    return dict(sqlite3.connect(catalog).execute("select name,state from ledger_apply"))


def test_extract_store_name_strips_source_and_dates():
    labels = ("聚水潭成本", "订单明细", "运费", "对账（资金流水）", "权益保险（保费支出）")
    assert _extract_store_name(
        "蔡果-抖音喜品-聚水潭成本_20260831115536_188931298_1.xlsx", labels,
    ) == "蔡果-抖音喜品"
    assert _extract_store_name(
        "蔡果-抖音喜品-保单明细-2026-08-27 20_12_21.csv", labels,
    ) == "蔡果-抖音喜品"
    assert _extract_store_name("运费-淘宝喜必顺.csv", labels) == "淘宝喜必顺"
    assert _extract_store_name("订单明细-PddLucky惊喜派对.xlsx", labels) == "PddLucky惊喜派对"


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
    states = apply_states(catalog)
    assert states[active.name] == "applied"
    assert states[manual.name] == "search_only"


def test_shared_receipt_queues_durably_without_waiting_for_all_stores(tmp_path, monkeypatch):
    root = tmp_path / "nas"
    uploaded = root / "00_上传区" / "00_全公司共享" / "代发" / "代发-全店铺-7-8月.csv"
    uploaded.parent.mkdir(parents=True)
    uploaded.write_text("订单号,金额\nA1,10\n", encoding="utf-8")
    catalog = tmp_path / "catalog.db"
    sqlite3.connect(catalog).executescript(CATALOG).connection.close()
    add_catalog(catalog, uploaded, store_id="__shared__", source="代发")
    ws = Workspace(tmp_path / "workspace")
    ws.keep("seed.csv", uploaded, "taobao_xibishun")
    registry = Registry(ws.root)
    calls = []
    def enqueue(stores, signature):
        assert uploaded.exists()  # Queue commit precedes acknowledgment/move.
        assert ws.submissions("__shared__")
        registry.enqueue_files(stores, signature)
        calls.append(stores)
    def no_sync_recompute(*args, **kwargs):
        raise AssertionError("NAS receipt must not wait for computation")
    monkeypatch.setattr(service, "recompute", no_sync_recompute)
    result = reconcile_ready(ws, load_model(MODEL), catalog, root, enqueue=enqueue)
    assert not result["errors"]
    accepted = root / "10_已接收" / "00_全公司共享" / "代发" / uploaded.name
    assert accepted.exists() and not uploaded.exists()
    assert calls == [{"taobao_xibishun"}]
    with registry.connect() as conn:
        assert conn.execute("SELECT source_fingerprint FROM pending WHERE store_id='taobao_xibishun'").fetchone()[0].startswith("nas:")
    assert not reconcile_ready(ws, load_model(MODEL), catalog, root, enqueue=enqueue)["errors"]
    assert len(calls) == 1


def test_queue_failure_then_unchanged_file_retry_cannot_lose_accounting(tmp_path, monkeypatch):
    root = tmp_path / "nas"
    uploaded = root / "00_上传区" / "淘宝天猫" / "汪学成-天猫喜必顺旗舰店 [taobao_xibishun]" / "运费" / "运费-淘宝喜必顺.csv"
    uploaded.parent.mkdir(parents=True)
    uploaded.write_text("运单号,金额\nA1,1\n", encoding="utf-8")
    catalog = tmp_path / "catalog.db"
    sqlite3.connect(catalog).executescript(CATALOG).connection.close()
    add_catalog(catalog, uploaded)
    ws = Workspace(tmp_path / "workspace")
    monkeypatch.setattr(service, "recompute", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("sync recompute")))
    def failed(stores, signature):
        raise RuntimeError("queue offline")
    result = reconcile_ready(ws, load_model(MODEL), catalog, root, enqueue=failed)
    assert result["errors"] and uploaded.exists()
    assert apply_states(catalog)[uploaded.name] == "error"
    assert ws.submissions("taobao_xibishun")
    with sqlite3.connect(catalog) as conn:
        conn.execute("UPDATE ledger_apply SET applied_at='2020-01-01T00:00:00+00:00'")
    registry = Registry(ws.root)
    assert not reconcile_ready(ws, load_model(MODEL), catalog, root, enqueue=registry.enqueue_files)["errors"]
    assert not uploaded.exists()
    with registry.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending WHERE store_id='taobao_xibishun'").fetchone()[0] == 1


def test_file_queue_preserves_source_watermark_and_invalidates_inflight_guard(tmp_path):
    registry = Registry(tmp_path)
    registry.enqueue_source({"s"}, "order-feed:snapshot:50")
    with registry.connect() as conn:
        previous = dict(conn.execute("SELECT * FROM pending WHERE store_id='s'").fetchone())
    registry.enqueue_files({"s"}, "nas:" + "a" * 64)
    with registry.transaction() as conn:
        updated = conn.execute("SELECT * FROM pending WHERE store_id='s'").fetchone()
        assert updated["source_seq"] == 50
        assert updated["source_fingerprint"] == "nas:" + "a" * 64
        removed = conn.execute("DELETE FROM pending WHERE store_id=? AND revision<=? AND source_seq<=? AND source_fingerprint=?",
            ("s",previous["revision"],previous["source_seq"],previous["source_fingerprint"]))
        assert removed.rowcount == 0


def test_existing_pending_queue_migrates_without_losing_work(tmp_path):
    directory = tmp_path / "commission"
    directory.mkdir()
    with sqlite3.connect(directory / "registry.db") as conn:
        conn.execute("CREATE TABLE pending (store_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, next_attempt INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', source_seq INTEGER NOT NULL DEFAULT 0, source_fingerprint TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT INTO pending(store_id,revision,source_seq,source_fingerprint) VALUES('s',4,50,'order-feed:snapshot:50')")
    registry = Registry(tmp_path)
    with registry.connect() as conn:
        row = conn.execute("SELECT * FROM pending WHERE store_id='s'").fetchone()
        assert row["revision"] == 4 and row["source_seq"] == 50
        assert row["source_fingerprint"] == "order-feed:snapshot:50"
        assert row["files_revision"] == 0
        assert row["files_applied_revision"] == 0


def test_missing_file_never_holds_catalog_writer_during_compute_and_requires_fresh_scan(tmp_path, monkeypatch):
    root = tmp_path / 'nas'
    original = root / '10_已接收' / 's' / 'missing.csv'
    original.parent.mkdir(parents=True)
    original.write_text('A,1\n')
    catalog = tmp_path / 'catalog.db'
    sqlite3.connect(catalog).executescript(CATALOG + APPLY_SCHEMA).connection.close()
    sha = add_catalog(catalog, original, missing=3, missing_since=int(time.time())-700)
    ws = Workspace(tmp_path / 'workspace')
    ws.keep(original.name, original, 'taobao_xibishun')
    with sqlite3.connect(catalog) as conn:
        conn.execute('INSERT INTO ledger_apply(path,sha256,store_id,name,state) VALUES(?,?,?,?,?)',
            (str(original),sha,'taobao_xibishun',original.name,'applied'))
        conn.execute("UPDATE scan_meta SET last_completed='2020-01-01T00:00:00Z'")
    original.unlink()
    assert reconcile_missing(ws,load_model(MODEL),catalog)['removed']==0
    assert ws.submissions('taobao_xibishun')
    with sqlite3.connect(catalog) as conn:
        conn.execute("UPDATE scan_meta SET last_completed=strftime('%Y-%m-%dT%H:%M:%SZ','now')")
    def compute(*args,**kwargs):
        with sqlite3.connect(catalog,timeout=.05) as writer:
            writer.execute('UPDATE scan_meta SET generation=generation+1')
    monkeypatch.setattr(service,'recompute',compute)
    assert reconcile_missing(ws,load_model(MODEL),catalog)['removed']==1


def test_withdrawal_queue_failure_is_retryable_and_restored_source_is_received(tmp_path, monkeypatch):
    root = tmp_path/'nas'
    original=root/'10_已接收'/'淘宝天猫'/'汪学成-天猫喜必顺旗舰店 [taobao_xibishun]'/'运费'/'运费-淘宝喜必顺.csv'
    original.parent.mkdir(parents=True)
    raw='运单号,金额\nA1,1\n'
    original.write_text(raw,encoding='utf-8')
    catalog=tmp_path/'catalog.db'
    sqlite3.connect(catalog).executescript(CATALOG+APPLY_SCHEMA).connection.close()
    sha=add_catalog(catalog,original,missing=3,missing_since=int(time.time())-700)
    ws=Workspace(tmp_path/'workspace');ws.keep(original.name,original,'taobao_xibishun')
    with sqlite3.connect(catalog) as conn:
        conn.execute('INSERT INTO ledger_apply(path,sha256,store_id,name,state) VALUES(?,?,?,?,?)',
            (str(original),sha,'taobao_xibishun',original.name,'applied'))
    original.unlink()
    def fail(*args):raise RuntimeError('queue offline')
    assert reconcile_missing(ws,load_model(MODEL),catalog,enqueue=fail)['errors']
    assert application_errors(catalog)[0]['state']=='removal_pending'
    assert not ws.submissions('taobao_xibishun')
    original.write_text(raw,encoding='utf-8')
    with sqlite3.connect(catalog) as conn:
        conn.execute('UPDATE file_catalog SET missing_scans=0,missing_since=NULL')
    monkeypatch.setattr(service,'recompute',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('sync')))
    registry=Registry(ws.root)
    assert not reconcile_ready(ws,load_model(MODEL),catalog,root,enqueue=registry.enqueue_files)['errors']
    assert ws.submissions('taobao_xibishun')
    assert apply_states(catalog)[original.name]=='applied'


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


def test_renamed_same_content_forgets_the_old_name(tmp_path):
    """删掉 6月对账单.csv 再传带店名的同一份：旧名必须从店铺清单里拿掉。"""
    root = tmp_path / "台账系统"
    old = root / "10_已接收" / "拼多多" / "store" / "对账" / "6月对账单.csv"
    new = root / "10_已接收" / "拼多多" / "store" / "对账" / "宋永康-PDD国风-6月对账单.csv"
    old.parent.mkdir(parents=True)
    old.write_text("订单号,金额\nA1,1\n", encoding="utf-8")
    catalog = tmp_path / "catalog.db"
    sqlite3.connect(catalog).executescript(CATALOG).connection.close()
    sha = add_catalog(catalog, old, missing=3, missing_since=int(time.time()) - 700)
    new.write_bytes(old.read_bytes())
    add_catalog(catalog, new)
    workspace = Workspace(tmp_path / "workspace")
    workspace.keep(old.name, old, "pdd_mt9sojk5")
    workspace.keep(new.name, new, "pdd_mt9sojk5")
    connection = sqlite3.connect(catalog)
    connection.executescript(APPLY_SCHEMA)
    connection.executemany(
        "insert into ledger_apply(path,sha256,store_id,name,state,applied_at) values(?,?,?,?,?,?)",
        [
            (str(old), sha, "pdd_mt9sojk5", old.name, "applied", "now"),
            (str(new), sha, "pdd_mt9sojk5", new.name, "applied", "now"),
        ],
    )
    connection.commit()
    connection.close()
    old.unlink()

    result = reconcile_missing(workspace, load_model(MODEL), catalog)
    assert result["removed"] == 1
    names = {f["name"] for f in workspace.submissions("pdd_mt9sojk5")}
    assert old.name not in names
    assert new.name in names


def test_relocated_same_name_is_kept(tmp_path):
    """同一文件从上传区搬到已接收，名字没变：不能当成删除。"""
    root = tmp_path / "台账系统"
    uploaded = root / "00_上传区" / "淘宝天猫" / "store" / "运费" / "运费-淘宝喜必顺.csv"
    accepted = root / "10_已接收" / "淘宝天猫" / "store" / "运费" / "运费-淘宝喜必顺.csv"
    uploaded.parent.mkdir(parents=True)
    accepted.parent.mkdir(parents=True)
    uploaded.write_text("运单号,金额\nA1,1\n", encoding="utf-8")
    accepted.write_bytes(uploaded.read_bytes())
    catalog = tmp_path / "catalog.db"
    sqlite3.connect(catalog).executescript(CATALOG).connection.close()
    sha = add_catalog(
        catalog, uploaded, missing=3, missing_since=int(time.time()) - 700,
    )
    add_catalog(catalog, accepted)
    workspace = Workspace(tmp_path / "workspace")
    workspace.keep(accepted.name, accepted, "taobao_xibishun")
    connection = sqlite3.connect(catalog)
    connection.executescript(APPLY_SCHEMA)
    connection.execute(
        "insert into ledger_apply(path,sha256,store_id,name,state,applied_at) values(?,?,?,?,?,?)",
        (str(uploaded), sha, "taobao_xibishun", uploaded.name, "applied", "now"),
    )
    connection.commit()
    connection.close()
    uploaded.unlink()

    result = reconcile_missing(workspace, load_model(MODEL), catalog)
    assert result["removed"] == 0
    assert workspace.submissions("taobao_xibishun")


def test_unrecognized_filename_learns_alias_and_applies(tmp_path):
    model_dir = tmp_path / "model"
    shutil.copytree(MODEL, model_dir)
    root = tmp_path / "台账系统"
    uploaded = (
        root / "00_上传区" / "淘宝天猫" / "汪学成-天猫喜必顺旗舰店 [taobao_xibishun]" / "运费"
    )
    uploaded.mkdir(parents=True)
    file = uploaded / "运费-喜必顺旗舰店.csv"
    file.write_text("运单号,金额\nA1,1\n", encoding="utf-8")
    catalog = tmp_path / "catalog.db"
    sqlite3.connect(catalog).executescript(CATALOG).connection.close()
    add_catalog(catalog, file)

    workspace = Workspace(tmp_path / "workspace")
    result = reconcile_ready(
        workspace, load_model(model_dir), catalog, root, model_dir=model_dir,
    )
    assert not result["errors"]
    assert any("自动学习别名：喜必顺旗舰店" in item for item in result["audits"])
    assert apply_states(catalog)[file.name] == "applied"
    store = load_model(model_dir).store("taobao_xibishun")
    assert "喜必顺旗舰店" in store.aliases
    accepted = (
        root / "10_已接收" / "淘宝天猫" / "汪学成-天猫喜必顺旗舰店 [taobao_xibishun]"
        / "运费" / file.name
    )
    assert accepted.is_file()
    assert not file.exists()


def test_filename_matching_other_store_is_still_quarantined(tmp_path):
    model_dir = tmp_path / "model"
    shutil.copytree(MODEL, model_dir)
    root = tmp_path / "台账系统"
    uploaded = (
        root / "00_上传区" / "淘宝天猫" / "汪学成-天猫喜必顺旗舰店 [taobao_xibishun]" / "运费"
    )
    uploaded.mkdir(parents=True)
    file = uploaded / "运费-京东皇莉诗.csv"
    file.write_text("运单号,金额\nA1,1\n", encoding="utf-8")
    catalog = tmp_path / "catalog.db"
    sqlite3.connect(catalog).executescript(CATALOG).connection.close()
    add_catalog(catalog, file)

    workspace = Workspace(tmp_path / "workspace")
    result = reconcile_ready(
        workspace, load_model(model_dir), catalog, root, model_dir=model_dir,
    )
    assert result["errors"]
    assert "冲突" in result["errors"][0]
    assert apply_states(catalog)[file.name] == "quarantined"
    assert (root / "20_需修正" / "淘宝天猫" / "汪学成-天猫喜必顺旗舰店 [taobao_xibishun]"
            / "运费" / file.name).is_file()
    assert "京东皇莉诗" not in load_model(model_dir).store("taobao_xibishun").aliases


def test_unknown_store_registers_when_order_feed_confirms(tmp_path):
    model_dir = tmp_path / "model"
    shutil.copytree(MODEL, model_dir)
    root = tmp_path / "台账系统"
    uploaded = (
        root / "00_上传区" / "快手" / "蔡果-快手自动测 [kuaishou_autotest]" / "运费"
    )
    uploaded.mkdir(parents=True)
    file = uploaded / "运费-蔡果-快手自动测.csv"
    file.write_text("运单号,金额\nA1,1\n", encoding="utf-8")
    catalog = tmp_path / "catalog.db"
    sqlite3.connect(catalog).executescript(CATALOG).connection.close()
    add_catalog(catalog, file, platform="快手", store_id="kuaishou_autotest")
    workspace = Workspace(tmp_path / "workspace")
    write_feed(workspace.root, "kuaishou_autotest")

    result = reconcile_ready(
        workspace, load_model(model_dir), catalog, root, model_dir=model_dir,
    )
    assert not result["errors"]
    assert any("自动登记店铺：kuaishou_autotest" in item for item in result["audits"])
    store = load_model(model_dir).store("kuaishou_autotest")
    assert store.name == "蔡果-快手自动测"
    assert store.platform == "kuaishou"
    assert apply_states(catalog)[file.name] == "applied"
    assert any(row["name"] == file.name for row in workspace.submissions("kuaishou_autotest"))


def test_unknown_store_without_feed_is_skipped_not_quarantined(tmp_path):
    model_dir = tmp_path / "model"
    shutil.copytree(MODEL, model_dir)
    root = tmp_path / "台账系统"
    uploaded = root / "00_上传区" / "快手" / "幽灵店 [ghost_shop]" / "运费"
    uploaded.mkdir(parents=True)
    file = uploaded / "运费-幽灵店.csv"
    file.write_text("运单号,金额\nA1,1\n", encoding="utf-8")
    catalog = tmp_path / "catalog.db"
    sqlite3.connect(catalog).executescript(CATALOG).connection.close()
    add_catalog(catalog, file, platform="快手", store_id="ghost_shop")

    workspace = Workspace(tmp_path / "workspace")
    result = reconcile_ready(
        workspace, load_model(model_dir), catalog, root, model_dir=model_dir,
    )
    assert result["errors"]
    assert "不在订单台映射中" in result["errors"][0]
    assert file.is_file()
    assert not list((root / "20_需修正").rglob("*.csv")) if (root / "20_需修正").exists() else True
    assert apply_states(catalog) == {file.name: "error"}
    assert "ghost_shop" not in {store.id for store in load_model(model_dir).stores}


def test_quarantined_file_is_rescued_after_alias_learning(tmp_path):
    model_dir = tmp_path / "model"
    shutil.copytree(MODEL, model_dir)
    root = tmp_path / "台账系统"
    original = (
        root / "00_上传区" / "抖音" / "蔡果-抖店喜品 [douyin_mt9sbkne]" / "运费"
        / "蔡果-抖音喜品-运费.csv"
    )
    quarantined = (
        root / "20_需修正" / "抖音" / "蔡果-抖店喜品 [douyin_mt9sbkne]" / "运费"
        / "蔡果-抖音喜品-运费.csv"
    )
    quarantined.parent.mkdir(parents=True)
    quarantined.write_text("运单号,金额\nA1,1\n", encoding="utf-8")
    catalog = tmp_path / "catalog.db"
    sqlite3.connect(catalog).executescript(CATALOG).connection.close()
    sha = add_catalog(
        catalog, quarantined,
        catalog_path=original,
        platform="抖音",
        store_id="douyin_mt9sbkne",
    )
    connection = sqlite3.connect(catalog)
    connection.executescript(APPLY_SCHEMA)
    connection.execute(
        "insert into ledger_apply(path,sha256,store_id,name,state,applied_at,error) "
        "values(?,?,?,?,?,?,?)",
        (str(original), sha, "douyin_mt9sbkne", original.name, "quarantined", "now",
         "文件名无法识别店铺；目录登记为 douyin_mt9sbkne"),
    )
    connection.commit()
    connection.close()

    workspace = Workspace(tmp_path / "workspace")
    result = reconcile_ready(
        workspace, load_model(model_dir), catalog, root, model_dir=model_dir,
    )
    assert not result["errors"]
    assert "蔡果-抖音喜品" in load_model(model_dir).store("douyin_mt9sbkne").aliases
    accepted = (
        root / "10_已接收" / "抖音" / "蔡果-抖店喜品 [douyin_mt9sbkne]" / "运费"
        / original.name
    )
    assert accepted.is_file()
    assert not quarantined.exists()
    states = dict(sqlite3.connect(catalog).execute("select path,state from ledger_apply"))
    assert states[str(accepted)] == "applied"
    catalog_path, missing = sqlite3.connect(catalog).execute(
        "select path,missing_scans from file_catalog"
    ).fetchone()
    assert catalog_path == str(accepted)
    assert missing == 0
    assert any(row["name"] == original.name for row in workspace.submissions("douyin_mt9sbkne"))
