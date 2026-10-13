"""Sales goods-cost gaps must not ask for a replacement shipment's cost."""
import polars as pl

VERSION = 1


def sales_only(frame):
    # Use confirmed row-level type, not a shared master ID, an amount match,
    # or an $Asr prefix. Unknown types remain visible for investigation.
    if 'order_type' not in frame.columns:
        return frame
    replacement = pl.col('order_type').cast(pl.String).str.strip_chars().is_in(['补发订单', '补发']).fill_null(False)
    return frame.filter(~replacement)
