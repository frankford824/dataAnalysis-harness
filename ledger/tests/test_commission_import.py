from __future__ import annotations

from io import BytesIO

import openpyxl

from ledger.commission_import import ROLE_COLUMNS, activate, read_workbook, stage
from ledger.commission_registry import Registry
from ledger.model.schema import Store
from test_commission import _model


def workbook(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "汇总表-最终-winnie"
    header = [None, "归属店铺（下拉选择）", "宝贝编码（ID）（手动填写）", "小计点数(不填写）"]
    header += [value for _, person, points in ROLE_COLUMNS for value in (person, points)]
    ws.append(header)
    for store, product, total, allocations in rows:
        row = [None, store, product, total]
        for role, _, _ in ROLE_COLUMNS:
            row.extend(allocations.get(role, (None, None)))
        ws.append(row)
    wb.create_sheet("平台字典").append(["淘宝"])
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def test_exact_role_columns_never_copy_senior_into_lead(tmp_path):
    raw = workbook([("测试店", "123456789012", .05, {"组长": ("组长甲", .05)}),
                    ("测试店", "123456789013", .03, {"高级组长": ("高级组长乙", .01), "运营1": ("运营丙", .02)})])
    _, rows, sheets = read_workbook(raw)
    assert rows[0]["row_no"] == 2
    assert rows[0]["allocations"][1] == {"role": "组长", "person": "组长甲", "rate_raw": "0.05"}
    assert rows[1]["allocations"][1]["person"] == ""
    r = Registry(tmp_path)
    batch = stage(r, _model(), raw, "input.xlsx", "2026-06-01", "tester")
    assert batch["summary"]["ready"] == 2
    activate(r, batch["id"], "tester", "统一六月启用")
    assert len(r.active("s1")[1]) == 2
    assert activate(r, batch["id"], "tester", "重试")["reused"]
    assert stage(r, _model(), raw, "input.xlsx", "2026-06-01", "tester")["reused"]


def test_duplicate_scope_and_conflicts_are_preserved(tmp_path):
    raw = workbook([("测试店", "123456789012", .05, {"组长": ("甲", .05)}),
                    ("测试店二", "123456789012", .05, {"组长": ("乙", .05)}),
                    ("测试店", "123456789013", .05, {"组长": ("甲", .05)}),
                    ("测试店", "123456789013", .05, {"组长": ("乙", .05)})])
    model = _model(stores=(Store(id="s1", name="测试店", platform="taobao"),
                           Store(id="s2", name="测试店二", platform="taobao")))
    r = Registry(tmp_path)
    batch = stage(r, model, raw, "input.xlsx", "2026-06-01", "tester")
    assert batch["summary"]["ready"] == 2
    assert batch["summary"]["review"] == 2
    result = activate(r, batch["id"], "tester", "确认")
    assert result["summary"]["activated_groups"] == 2
    assert len(r.active("s1")[1]) == len(r.active("s2")[1]) == 1


def test_new_import_preserves_previous_effective_interval(tmp_path):
    r = Registry(tmp_path)
    a = stage(r, _model(), workbook([("测试店", "123456789012", .05, {"组长": ("甲", .05)})]),
              "a.xlsx", "2026-06-01", "tester")
    activate(r, a["id"], "tester", "六月")
    b = stage(r, _model(), workbook([("测试店", "123456789012", .03, {"组长": ("乙", .03)})]),
              "b.xlsx", "2026-08-15", "tester")
    activate(r, b["id"], "tester", "八月调整")
    body = r.active("s1")[1][0]["body"]
    assert body["segments"][0]["valid_to"] == "2026-08-15T00:00:00"
    assert body["segments"][1]["valid_from"] == "2026-08-15T00:00:00"
