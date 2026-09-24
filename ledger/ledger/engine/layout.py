"""Narrow, evidence-preserving repairs of proven source-column transpositions."""
from dataclasses import replace

from .normalize import to_date


def supplier_columns(table, template, model):
    """Recover only date <-> registered shop swaps, never infer an unknown shop.

    This leaves the source workbook and physical row anchors untouched. A row
    that does not independently satisfy both column domains remains unresolved.
    """
    if template.source != "dropship":
        return table, {}
    headers = [str(h).strip() for h in table.headers]
    if headers.count("店铺") != 1 or headers.count("付款日期") != 1:
        return table, {}
    owner_index, date_index = headers.index("店铺"), headers.index("付款日期")
    owners = {}
    for shop in model.stores:
        for label in (shop.name, *shop.aliases):
            owners.setdefault(label.strip(), set()).add(shop.id)
    rows, evidence = [], {}
    for row in table.rows:
        if len(row.cells) <= max(owner_index, date_index):
            rows.append(row)
            continue
        written, alternate = row.cells[owner_index], row.cells[date_index]
        name = str(alternate or "").strip()
        date = to_date(written)
        if (str(written or "").strip() not in owners and len(owners.get(name, ())) == 1
                and date is not None and 2000 <= date.year <= 2100):
            cells = list(row.cells)
            cells[owner_index], cells[date_index] = alternate, written
            rows.append(replace(row, cells=tuple(cells)))
            evidence[row.row_no] = f"代发表列互换校验：原店铺列为{date.isoformat()}，付款日期列为已登记店铺{name}；仅解析时交换，原文件保留"
        else:
            rows.append(row)
    return replace(table, rows=rows), evidence


def payment_reference(table, originals):
    """Prove that an unheaded side-paste repeats real payment rows verbatim."""
    if [h for h in table.headers if str(h).strip()] != ["网店名称"]:
        return None
    columns = ["序号", "网店名称", "下单日期", "收款人账号", "收款人姓名", "付款金额", "报销人", "报销日期", "摘要", "订单号"]
    cell = lambda value: "" if value is None else str(value).strip()
    records = {tuple(cell(v) for v in row.cells[:10]) for original in originals
               if list(original.headers[:10]) == columns for row in original.rows if len(row.cells) >= 10}
    copied = 0
    for row in table.rows:
        cells = list(row.cells)
        if any(cell(v) for v in cells[1:4]):
            return None
        if not any(cell(v) for v in cells[4:]):
            continue
        if len(cells) < 14 or tuple(cell(v) for v in cells[4:14]) not in records:
            return None
        if any(cell(v) not in {"", "批量付款", "微信", "支付宝", "网银", "银行卡"} for v in cells[14:]):
            return None
        copied += 1
    return copied
