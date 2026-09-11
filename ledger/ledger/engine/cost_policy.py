"""ERP costs and supplier fulfilment must not both charge the same DF item."""
import polars as pl


def is_dropship(frame):
    if 'sku' not in frame.columns:
        return pl.lit(False)
    return pl.col('sku').cast(pl.Utf8).str.to_lowercase().str.contains('df',literal=True).fill_null(False)
