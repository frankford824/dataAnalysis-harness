"""Sales-org hierarchy, store ownership, and inherited product cuts."""
from __future__ import annotations

import sqlite3

from ledger.commission_registry import Registry, RegistryError, _initialized


def _people(reg, names):
    return [reg.person_save({"name": name}, "test", "登记") for name in names]


def test_person_hierarchy_fields_and_cycle(tmp_path):
    reg = Registry(tmp_path)
    lead, group, member = _people(reg, ("张总", "宋永康", "宗玲"))
    lead = reg.person_save({**lead, "alias": "运营一部", "default_cut_rate": "0.01"}, "test", "设团队", lead["revision"])
    group = reg.person_save({**group, "parent_id": lead["id"], "alias": "宋永康组",
                             "default_cut_rate": "2%"}, "test", "设组长", group["revision"])
    member = reg.person_save({**member, "parent_id": group["id"]}, "test", "设组员", member["revision"])
    people = {p["id"]: p for p in reg.people()}
    assert people[lead["id"]]["alias"] == "运营一部"
    assert people[lead["id"]]["default_cut_rate"] == "0.01"
    assert people[group["id"]]["parent_id"] == lead["id"]
    assert people[group["id"]]["default_cut_rate"] == "0.02"
    assert people[member["id"]]["parent_id"] == group["id"]
    tree = reg.org_tree({"s1": "婚庆店"})
    assert tree["tree"][0]["role"] == "团队长"
    assert tree["tree"][0]["children"][0]["role"] == "组长"
    assert tree["tree"][0]["children"][0]["children"][0]["role"] == "成员"
    try:
        reg.person_save({**lead, "parent_id": member["id"]}, "test", "成环", lead["revision"])
        raise AssertionError("cycle should be rejected")
    except RegistryError as exc:
        assert "下级" in str(exc)


def test_org_move_and_stores(tmp_path):
    reg = Registry(tmp_path)
    lead, member, extra = _people(reg, ("张总", "李某", "王某"))
    moved = reg.org_move([member["id"], extra["id"]], lead["id"], "test", "挂到团队下")
    assert {row["parent_id"] for row in moved} == {lead["id"]}
    saved = reg.save_org_stores(lead["id"], ["s1", "s2"], "test", "团队负责店铺")
    assert saved["store_ids"] == ["s1", "s2"]
    owned = reg.org_stores(lead["id"], include_descendants=True)
    assert {row["store_id"] for row in owned} == {"s1", "s2"}
    tree = reg.org_tree({"s1": "婚庆", "s2": "喜铺", "s3": "未分配店"})
    assert [store["id"] for store in tree["tree"][0]["stores"]] == ["s1", "s2"]
    assert tree["unassigned_stores"][0]["id"] == "s3"


def test_hierarchy_cut_preview_and_apply(tmp_path):
    reg = Registry(tmp_path)
    lead, group, member = _people(reg, ("张总", "宋永康", "宗玲"))
    reg.person_save({**lead, "default_cut_rate": "0.01"}, "test", "团队抽成", lead["revision"])
    reg.person_save({**group, "parent_id": lead["id"], "default_cut_rate": "0.02"}, "test", "组长抽成", group["revision"])
    reg.person_save({**member, "parent_id": group["id"]}, "test", "组员", member["revision"])
    first = reg.save_setting({
        "store_id": "s1", "product_id": "p1", "product_name": "礼盒",
        "mode": "distribute", "valid_from": "2026-08-01T00:00:00",
        "allocations": [{"person_id": member["id"], "rate": "0.05", "duty": "produce"}],
        "expected_revision": 0, "reason": "组员做货",
    }, "test")
    preview = reg.hierarchy_allocations(first["body"]["segments"][0]["allocations"])
    assert [line["person_id"] for line in preview if line.get("source") == "hierarchy"] == [group["id"], lead["id"]]
    applied = reg.fill_hierarchy(
        store_ids=["s1"], product_ids=["p1"], valid_from="2026-08-01T00:00:00",
        actor="test", reason="补上级抽成", apply=True)
    assert applied["added"] == 2
    body = reg.scheme(first["id"])["versions"][0]["body"]
    by_person = {line["person_id"]: line for line in body["segments"][0]["allocations"]}
    assert by_person[member["id"]]["duty"] == "produce"
    assert by_person[group["id"]]["source"] == "hierarchy"
    assert by_person[group["id"]]["rate"] == "0.02"
    assert by_person[lead["id"]]["source"] == "hierarchy"
    assert by_person[lead["id"]]["duty"] == "cut"


def test_allocation_source_persisted(tmp_path):
    reg = Registry(tmp_path)
    lead, member = _people(reg, ("张总", "宗玲"))
    result = reg.save_setting({
        "store_id": "s1", "product_id": "p1", "product_name": "礼盒",
        "mode": "distribute", "valid_from": "2026-08-01T00:00:00",
        "allocations": [
            {"person_id": member["id"], "rate": "0.05", "duty": "produce"},
            {"person_id": lead["id"], "rate": "0.01", "duty": "cut", "source": "hierarchy", "role": "团队长"},
        ],
        "expected_revision": 0, "reason": "带层级抽成",
    }, "test")
    line = next(item for item in result["body"]["segments"][0]["allocations"] if item["person_id"] == lead["id"])
    assert line["source"] == "hierarchy"
    assert line["role"] == "团队长"


def test_legacy_leader_migrates_to_parent_and_org_store(tmp_path):
    path = tmp_path / "commission" / "registry.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL);
        INSERT INTO meta VALUES(1,0);
        CREATE TABLE person (
         id TEXT PRIMARY KEY, name TEXT NOT NULL, employee_no TEXT NOT NULL DEFAULT '',
         external_user_id TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
         revision INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '');
        CREATE TABLE store_member (
         id TEXT PRIMARY KEY, store_id TEXT NOT NULL, person_id TEXT NOT NULL,
         valid_from TEXT NOT NULL, valid_to TEXT NOT NULL DEFAULT '',
         duty TEXT NOT NULL DEFAULT 'produce', leader_id TEXT NOT NULL DEFAULT '',
         revision INTEGER NOT NULL DEFAULT 1);
    """)
    conn.execute("INSERT INTO person(id,name) VALUES('leader','宋永康'),('member','宗玲')")
    conn.execute("INSERT INTO store_member VALUES('sm1','s1','member','1970-01-01T00:00:00','','produce','leader',1)")
    conn.commit()
    conn.close()
    _initialized.discard(path.resolve())
    reg = Registry(tmp_path)
    people = {p["id"]: p for p in reg.people()}
    assert people["member"]["parent_id"] == "leader"
    assert people["leader"]["parent_id"] == ""
    assert any(row["person_id"] == "leader" and row["store_id"] == "s1" for row in reg.org_stores())
    reg.save_org_stores("leader", [], "test", "清空归属")
    _initialized.discard(path.resolve())
    again = Registry(tmp_path)
    assert again.org_stores() == []
