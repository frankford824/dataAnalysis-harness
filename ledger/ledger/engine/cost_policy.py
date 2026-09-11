"""Identify supplier fulfilment at the product line, never the whole order."""
import re
from functools import lru_cache
import polars as pl


@lru_cache(maxsize=32768)
def remark_dropship(sku, remark):
    sku = str(sku or '').strip()
    if not sku or not remark or '代发' not in remark:
        return False
    # A remark can describe both warehouse and supplier goods. Only an explicit
    # SKU in the same clause qualifies; a bare QT code is not evidence.
    token = re.compile(r'(?<![A-Za-z0-9])' + re.escape(sku) + r'(?![A-Za-z0-9])', re.I)
    if re.search(r'取消.{0,3}代发|不.{0,3}代发|无需.{0,3}代发|是否|待确认|[？?]', remark):
        return False
    for clause in re.split(r'[，,；;。\n#]', remark):
        if ('代发' in clause and token.search(clause)
                and not re.search(r'不.{0,3}代发|取消.{0,3}代发|无需.{0,3}代发|非代发|改.{0,4}(自发|仓库发)|仓库发|自发|部分|[0-9]+\s*(件|个|套).{0,4}代发', clause)):
            return True
    return False


def prepare_dropship(frame):
    frame = frame.drop([c for c in ['__remark_dropship','__dropship_ambiguous'] if c in frame.columns])
    if not {'sku', 'order_remark'} <= set(frame.columns):
        return frame
    pairs = frame.select('sku', 'order_remark').filter(
        pl.col('order_remark').str.contains('代发', literal=True).fill_null(False)).unique()
    if pairs.is_empty():
        return frame
    pairs = pairs.with_columns(pl.struct('sku', 'order_remark').map_elements(
        lambda r: remark_dropship(r['sku'], r['order_remark']), return_dtype=pl.Boolean).alias('__remark_dropship'))
    frame = frame.join(pairs, on=['sku', 'order_remark'], how='left', maintain_order='left')
    order_key = next((k for k in ['original_order_id','order_id','internal_order_id'] if k in frame.columns), None)
    if order_key:
        scope = (["store_name"] if "store_name" in frame.columns else []) + [order_key]
        recognized = is_dropship(frame).any().over(scope)
        frame = frame.with_columns((pl.col('order_remark').str.replace_all(r'(不|取消|无需|非).{0,3}代发','').str.contains('代发',literal=True).fill_null(False)
            & ~recognized).alias('__dropship_ambiguous'))
    return frame


def is_dropship(frame):
    if 'sku' not in frame.columns:
        return pl.lit(False)
    code = pl.col('sku').cast(pl.Utf8).str.to_lowercase().str.contains('df|代发').fill_null(False)
    remark = pl.col('__remark_dropship').fill_null(False) if '__remark_dropship' in frame.columns else pl.lit(False)
    return code | remark


def is_exempt(frame):
    after_sale = (pl.col('__cost_exempt_reason').is_not_null()
                  if '__cost_exempt_reason' in frame.columns else pl.lit(False))
    return is_dropship(frame) | is_cancelled(frame) | after_sale


def is_cancelled(frame):
    if 'order_state' not in frame.columns:
        return pl.lit(False)
    return pl.col('order_state').is_in(['Cancelled','已取消','取消']).fill_null(False)


def missing_supplier_costs(facts):
    required = {'metric_id','order_id','sku','source_note','counted','contribution'}
    if not required <= set(facts.columns):
        return pl.DataFrame()
    dropship = facts.filter(pl.col('counted') & pl.col('metric_id').is_in(['goods_cost','reshipment_cost'])
        & pl.col('source_note').str.contains('代发商品：聚水潭成本计 0',literal=True).fill_null(False))
    supplied = facts.filter(pl.col('counted') & (pl.col('metric_id')=='dropship_cost') & (pl.col('contribution')!=0))
    keys = ['order_id']
    for col in ['store','period']:
        if col in facts.columns: keys.append(col)
    return dropship.select(*keys,'sku').unique().join(supplied.select(keys).unique(),on=keys,how='anti')
