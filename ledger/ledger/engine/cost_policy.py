"""ERP costs and supplier fulfilment must not both charge the same DF item."""
import polars as pl


def is_dropship(frame):
    if 'sku' not in frame.columns:
        return pl.lit(False)
    return pl.col('sku').cast(pl.Utf8).str.to_lowercase().str.contains('df',literal=True).fill_null(False)


def is_exempt(frame):
    after_sale = (pl.col('__cost_exempt_reason').is_not_null()
                  if '__cost_exempt_reason' in frame.columns else pl.lit(False))
    return is_dropship(frame) | is_cancelled(frame) | after_sale


def is_cancelled(frame):
    if 'order_state' not in frame.columns:
        return pl.lit(False)
    return pl.col('order_state').is_in(['Cancelled','已取消','取消']).fill_null(False)
