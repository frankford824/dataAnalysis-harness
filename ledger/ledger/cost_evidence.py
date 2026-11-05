"""Cost-source vocabulary shared by feed capture and accounting certification."""

GOODS_COST_CLOSE_THRESHOLD = 0.95

# These sources carry a concrete unit price and remain available for review.
CAPTURED_COST_SOURCES = frozenset({
    "history", "component_history", "manual", "blue_flag", "register_dated",
    "register", "register_first", "mirror", "scrape", "unknown_evidence",
})

# These sources may enter profit only after calculate.historical_price_evidence
# also proves exact order-day alignment, valid quantity/price and stable identity.
ORDER_DAY_COST_SOURCES = frozenset({
    "history", "component_history", "manual", "blue_flag", "register_dated",
})

# register_dated is the 商品维护 cost price the ERP had on the order day, dated
# by the ERP cost change log rather than by an item timestamp. The log is
# complete from REGISTER_CHANGE_LOG_SINCE, so for a later day the newest change
# at or before it, the old value of the first change after it, or an untouched
# register is the price in force. Made-to-order goods never enter daily
# weighted costing; for them this is the only cost that exists, and a SKU that
# had no price yet on the day takes the first price set after it (custom_first).
REGISTER_CHANGE_LOG_SINCE = "2025-08-29"
REGISTER_DATED_BASES = frozenset({
    "change_log", "change_log_prior", "log_unchanged", "custom_first",
})

# Both register labels were derived from generic inventory metadata timestamps.
# Those timestamps prove neither the first price nor historical effectiveness.
# Preserve the amounts for review; dated official history or independently
# verified manual evidence is required before entering historical profit.
