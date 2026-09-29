"""Shared human-facing descriptions; never changes accounting facts."""
SOURCE_AMOUNT = '来源金额（勿合计）'
BOOKED_AMOUNT = '本行计入金额（合计此列）'
IDENTIFIER = '订单号/商品ID'


def current_terms(text):
    """Translate old navigation labels for display, not saved audit content."""
    value=text or ''
    for before,after in [('没进账','未计入'),('进了账','计入金额（可汇总）'),('已含分摊','全店分摊明细')]:
        for left,right in [('「','」'),('“','”'),('‘','’')]:
            value=value.replace(left+before+right,left+after+right)
    return value


def describe(row, *, graded=True):
    counted=row.get('counted')
    route=row.get('booking_status')
    control=row.get('link_key')=='__store_wide__'
    if not graded or counted not in (True,False):
        status='待核对（缺少入账标记）'
        explanation='旧留档未记录本行是否计入，不能把来源金额当成已计入金额。'
    elif counted and row.get('contribution') is None:
        status='待核对（缺少计入金额）'
        explanation='留档有计入标记，但没有记录本行计入金额；不能视为0元。'
    elif route=='allocated' and not counted:
        status='汇总分摊已计入'
        explanation='已在分摊汇总行计入；本行0用于防止重复，不是漏记。'
    elif route=='store_wide' and counted:
        status='全店分摊已计入'
        explanation='费用已在本店本月分摊；本行金额勿重复相加。'
    elif route in ('allocated','store_wide'):
        status='待核对（分摊标记不一致）'
        explanation='本行入账标记与分摊标记不一致，请核对留档；不要据此另加费用。'
    elif counted and control:
        status='全店汇总已计入'
        explanation='汇总计入金额已含分摊费用；勿再加来源总额或分摊明细。'
    elif counted and str(row.get('record_type') or '').startswith('结账'):
        status='冻结金额已计入'
        explanation='本行采用结账时保存的核定记录，未按后来更新的源数据覆盖。'
    elif counted:
        status='本行已计入' if row.get('contribution')!=0 else '本行已计入（金额为0）'
        explanation='本行金额已计入本次核算。' if row.get('contribution')!=0 else '规则计算结果为0，不代表漏记；具体依据见原始计算说明。'
    else:
        status='本行未计入'
        explanation='本行未计入，也未含在全店分摊内；具体原因见依据。'
    linked=row.get('linked')
    matching=('全店汇总，不按单个订单匹配' if control else
              '已找到关联订单资料' if linked is True else
              '未直接匹配订单资料' if linked is False else '旧留档未记录匹配情况')
    note=current_terms(row.get('source_note'))
    return {
        'accounting_status':status,
        'accounting_hint':explanation,
        'accounting_explanation':explanation+((' 原始计算说明：'+note) if note else ''),
        'order_match_status':matching,
        'display_identifier':'全店分摊汇总（非订单号）' if control else row.get('link_key'),
    }
