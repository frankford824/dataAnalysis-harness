"""Stable review categories shared by saved rows, filters and exports."""
import polars as pl

LABELS = {
    'original_date': '缺原订单日期',
    'register_evidence': '登记价依据待核',
    'history_carry': '沿用价待升级',
    'historical_price': '缺下单日历史价',
    'identity': '商品身份或数量异常',
    'amount': '成本金额异常',
    'sync': '成本同步待完成',
    'other': '其他待核对',
}


def category_expr(frame):
    names = frame.columns if hasattr(frame, 'columns') else frame.names()
    col = lambda n: pl.col(n).cast(pl.String) if n in names else pl.lit(None, dtype=pl.String)
    reason = col('reason').fill_null('')
    return (pl.when(col('cost_source') == 'history_carry').then(pl.lit('history_carry'))
        .when(col('cost_source').is_in(['register', 'register_first']) | reason.str.contains('登记价|登记成本|成本登记'))
        .then(pl.lit('register_evidence'))
        .when(reason.str.contains('日期|下单日不一致')).then(pl.lit('original_date'))
        .when(reason.str.contains('商品信息|商品身份|商品与|组件|数量|身份')).then(pl.lit('identity'))
        .when(reason.str.contains('金额差异|金额异常')).then(pl.lit('amount'))
        .when(reason.str.contains('尚未同步|同步待')).then(pl.lit('sync'))
        .when(reason.str.contains('历史成本|历史依据|成本单价|没有可用')).then(pl.lit('historical_price'))
        .otherwise(pl.lit('other')).alias('category'))


def categorized(frame):
    if 'category' not in frame.collect_schema().names():
        frame = frame.with_columns(category_expr(frame.collect_schema()))
    return frame
