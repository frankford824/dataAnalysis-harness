"""Cost-source vocabulary shared by feed capture and accounting certification."""

GOODS_COST_CLOSE_THRESHOLD = 0.95

# These sources carry a concrete unit price and remain available for review.
CAPTURED_COST_SOURCES = frozenset({
    "history", "component_history", "manual", "blue_flag",
    "register", "register_first", "mirror", "scrape", "unknown_evidence",
})

# These sources may enter profit only after calculate.historical_price_evidence
# also proves exact order-day alignment, valid quantity/price and stable identity.
ORDER_DAY_COST_SOURCES = frozenset({
    "history", "component_history", "manual", "blue_flag",
    "register", "register_first",
})
