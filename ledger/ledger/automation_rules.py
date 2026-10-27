"""Versioned deterministic contracts; executable handlers stay in their modules."""
def contracts():
    from .automation_deterministic import RULE as statement,RULE_SHA as statement_sha,RULE_CONTRACT as statement_contract
    from .automation_cost_actions import RULE as cost,RULE_SHA as cost_sha,RULE_CONTRACT as cost_contract
    from .historical_cost_facts import RULE as facts,RULE_SHA as facts_sha,RULE_CONTRACT as facts_contract
    return {statement:(statement_sha,statement_contract),cost:(cost_sha,cost_contract),facts:(facts_sha,facts_contract)}


def metadata():
    copy={
        'statement_unique_candidate.v1':('核对唯一订单候选的重复流水',
            '同一原始流水组仅有一个候选能由原订单互证时，系统自动核对并重算，保留原金额、日期与科目。'),
        'history_cost_single_component.v1':('凭原始历史价补齐单组件成本',
            '原销售订单仅缺一个商品组件时，系统核对原始日、SKU、公司与数量，再取原日有效历史价；原始乘积恰好到分才自动补录，含厘或条件不齐继续待核实。'),
        'history_cost_source_facts.v2':('凭原始历史价补齐商品成本事实',
            '批量核对已发货普通组件，或独立核清原销售成员的补发组件；原始日、SKU、公司与原数量一致才按原报价交原引擎计价。已价或人工决定不覆盖，未展开组合、原成员或单位不明继续待核，不自动月结。'),
    }
    return [{**contract,'label':copy[rule][0],'description':copy[rule][1],
        'rule_sha':sha,'contract_sha':sha,
        'allowed_scope':{'kind':'store_month_range','store_count':1,'business_month_format':'YYYY-MM',
            'execution_time_format':'ISO8601_WITH_TIMEZONE','default_business_months':1,'financial_close':'human_confirmation'}}
        for rule,(sha,contract) in contracts().items()]
