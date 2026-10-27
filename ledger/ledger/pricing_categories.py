"""Stable review categories shared by saved rows, filters and exports."""
import polars as pl

LABELS = {
    'original_date': '缺原订单日期',
    'register_evidence': '登记价依据待核',
    'history_carry': '沿用价待升级',
    'historical_price': '缺下单日历史价',
    'identity': '商品身份或数量异常',
    'amount': '成本金额异常',
    'price_conflict': '历史价格依据冲突',
    'sync': '取价或同步待完成',
    'other': '其他待核对',
}

def required_evidence(row):
    category = row.get('category')
    if category == 'register_evidence':
        if row.get('cost_source') == 'register_first' or row.get('register_basis') == 'first':
            return '与该订单或货号直接关联的首次成本原表及生效依据；不能用晚于下单日的首次登记价直接回填。'
        if row.get('register_basis') == 'change_log_prior':
            return '覆盖下单日的历史成本原表或调价回执；后续变更记录中的旧值不足以确定下单日价格。'
        if row.get('register_basis') == 'change_log':
            return '原始调价单和成功回执，需有公司、货号、前后单价及生效时间。'
        return '首次导入成本原表或原始调价回执，明确公司、货号、单价与历史有效期。'
    return {
        'original_date': '原平台订单明细或拆合前独立原单，需能确认店铺、主子订单和下单日期。',
        'history_carry': '覆盖下单日的历史价格区间或当日出入库台账日结；较早日期的价格仅作参考。',
        'historical_price': '覆盖下单日的历史成本、首次成本原表或期初补算明细，需能核对货号和成本公司。',
        'identity': '原订单商品清单及拆单、合单、替换记录，逐行核对子订单、SKU和数量。',
        'amount': '原始商品数量和成本计价凭据，核对异常金额的来源。',
        'price_conflict': '同一日期涉及的原始调价记录与成本回执，核对前后单价、公司及生效时间，不能任选一个价格。',
        'sync': '先完成上游取价及同步，再核算；暂不重复补录金额。',
    }.get(category, '核对来源明细与原始成本凭据；未查到不能视为不存在。')


def category_expr(frame):
    names = frame.columns if hasattr(frame, 'columns') else frame.names()
    col = lambda n: pl.col(n).cast(pl.String) if n in names else pl.lit(None, dtype=pl.String)
    reason = col('reason').fill_null('')
    return (pl.when(reason.str.contains('商品信息|商品身份|商品与|组件|数量|身份')).then(pl.lit('identity'))
        .when(reason.str.contains('金额差异|金额异常')).then(pl.lit('amount'))
        .when(reason.str.contains('多个历史成本价|登记记录相互冲突')).then(pl.lit('price_conflict'))
        .when(reason == '原订单日期已确认，等待历史成本取价').then(pl.lit('sync'))
        .when(col('upstream_failure_reason') == 'missing_original_order_date').then(pl.lit('original_date'))
        .when(col('cost_source') == 'history_carry').then(pl.lit('history_carry'))
        .when(col('cost_source').is_in(['register', 'register_first']) | reason.str.contains('登记价|登记成本|成本登记'))
        .then(pl.lit('register_evidence'))
        .when(reason.str.contains('价格记录无法证明|历史依据')).then(pl.lit('historical_price'))
        .when(reason.str.contains('日期|下单日不一致')).then(pl.lit('original_date'))
        .when(reason.str.contains('尚未同步|同步待')).then(pl.lit('sync'))
        .when(reason.str.contains('历史成本|历史依据|成本单价|没有可用')).then(pl.lit('historical_price'))
        .otherwise(pl.lit('other')).alias('category'))


def categorized(frame):
    if 'category' not in frame.collect_schema().names():
        frame = frame.with_columns(category_expr(frame.collect_schema()))
    return frame
