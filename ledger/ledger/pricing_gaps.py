"""待核价明细按次留档，列表分页和导出使用同一份记录。"""
from pathlib import Path

import polars as pl

from .model.config import csv_cell
from .view import _excel_identifier_cell
from .pricing_categories import categorized, LABELS


def rows(path: Path, q: str = "", category: str = '') -> pl.LazyFrame:
    frame = categorized(pl.scan_parquet(path))
    if q.strip():
        names = frame.collect_schema().names()
        frame = frame.filter(pl.any_horizontal([
            pl.col(c).cast(pl.Utf8).str.contains(q.strip(), literal=True).fill_null(False)
            for c in ("order_id", "coverage_key", "internal_order_id", "internal_sub_order_id", "sku", "reason") if c in names
        ]))
    return frame.filter(pl.col('category') == category) if category else frame


def page(path: Path, *, q: str = "", category: str = '', offset: int = 0, limit: int = 100) -> dict:
    frame = rows(path, q, category)
    summary, reasons, items = pl.collect_all([
        frame.select(pl.len().alias("total"),
                     pl.col("reference_unit_cost").is_not_null().sum().alias("reference_count")),
        frame.group_by(pl.col("reason").fill_null("其他待核对事项")).len().sort("len", descending=True),
        frame.slice(max(0, offset), max(1, min(limit, 200))),
    ])
    categories = rows(path,q).group_by('category').len().collect()
    return {"total": summary["total"][0], "reference_count": summary["reference_count"][0],
            "category_counts": [{"category": r['category'], "label": LABELS.get(r['category'],r['category']), "count":r['len']} for r in categories.to_dicts()],
            "reason_counts": [{"reason": row["reason"], "count": row["len"]} for row in reasons.to_dicts()],
            "offset": max(0, offset), "items": items.to_dicts()}


def csv(path: Path, q: str = "", category: str = '') -> str:
    fields = [("period", "下单月份"), ("order_date", "下单日期"), ("order_id", "平台订单号"),
              ("internal_order_id", "聚水潭订单号"), ("sku", "商品编码"),
              ("quantity", "数量"), ("reference_unit_cost", "参考单价（未计入）"),
              ("reason", "待核对事项"), ("file_name", "来源文件"),
              ("sheet", "工作表"), ("row_no", "原表行号"),
              ('internal_sub_order_id','聚水潭商品行ID'),('cost_source','成本来源'),('cost_status','来源状态'),
              ('cost_as_of','来源取价日'),('register_basis','登记价依据类型'),('cost_company_id','成本公司'),
              ('price_begin_date','历史价格开始日'),('price_end_date','历史价格截止日'),
              ('carry_days','沿用天数'),('upstream_failure_reason','上游待核原因')]
    lines = [",".join(label for _, label in fields)]
    for row in rows(path, q, category).collect().iter_rows(named=True):
        lines.append(",".join(_excel_identifier_cell(row.get(key)) if key.endswith("order_id")
                              else csv_cell(row.get(key)) for key, _ in fields))
    return "\n".join(lines) + "\n"
