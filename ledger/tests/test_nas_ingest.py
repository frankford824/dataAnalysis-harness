from __future__ import annotations

import hashlib
import shutil
import sqlite3
import time
from pathlib import Path

from ledger.model import load_model
from ledger.nas_ingest import (
    APPLY_SCHEMA,
    _extract_store_name,
    reconcile_missing,
    reconcile_ready,
)
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
    assert apply_states(catalog) == {}
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
