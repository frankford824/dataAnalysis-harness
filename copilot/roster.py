"""房间名册。

加减同事、改某位用的模型和工具，只改这一份。
身份不要再写进助手的总提示词。问答、资料用的模型不在这里，
而在 Worker 里技能含 qa / evidence、评测通过、每次答对成本最低的那份档案。
"""

VISION = 'qwen3-vl-plus'
PICK_MODEL = 'deepseek-v4.1-flash-1017'
PICK_FALLBACK = 'qwen3.8-flash'
PICK_TIMEOUT = 1.5

NAMES = ('领队', '核算', '问答', '资料', '程序')

# 核算可以查数、出确认卡，不能改程序。程序可以改程序、执行命令、重启服务，不能改账。
# 领队没有任何写入工具。问答和资料不走这张工具表，走 Worker。
_BOOKS = (
    'store_metrics', 'sku_cost', 'order_lookup', 'ledger_get', 'ledger_preview', 'api_index',
    'sql_query', 'python', 'read_knowledge', 'web_search', 'machine_map', 'find_files',
    'propose_change', 'remember', 'record_correction', 'search_memory', 'auto_close_status', '交给',
)
_CODE = (
    'read_file', 'search_files', 'write_file', 'edit_file', 'python', 'powershell',
    'restart_ledger', 'restart_assistant', 'build_web', 'read_knowledge', 'web_search',
    'machine_map', 'api_index', 'ledger_get', 'sql_query', 'search_memory', 'remember', '交给',
)

BOTS = {
    '领队': {
        'model': PICK_MODEL,
        'fallback': PICK_FALLBACK,
        'tools': (),
        'prompt': 'lead',
        'skill': '',
        'thinking': 'off',
    },
    '核算': {
        'model': 'qwen3.8-max',
        'fallback': 'qwen3.7-plus',
        'tools': _BOOKS,
        'prompt': 'books',
        'skill': '',
        'thinking': 'low',
    },
    '程序': {
        'model': 'kimi-k2.7-code',
        'fallback': 'glm-5.3',
        'tools': _CODE,
        'prompt': 'code',
        'skill': '',
        'thinking': 'off',
    },
    '问答': {
        'model': '',
        'fallback': '',
        'tools': (),
        'prompt': '',
        'skill': 'qa',
        'skill_file': 'agent-worker/skills/qa/SKILL.md',
        'thinking': 'off',
    },
    '资料': {
        'model': '',
        'fallback': '',
        'tools': (),
        'prompt': '',
        'skill': 'evidence',
        'skill_file': 'agent-worker/skills/evidence/SKILL.md',
        'thinking': 'off',
    },
}

# present：这间房里常驻、右侧名单上的人。
# callable：领队挑选时允许返回的名字。店月里程序不常驻，但领队可以叫程序进来。
ROOMS = {
    'store': {
        'title': '店月',
        'present': ('领队', '核算', '问答', '资料'),
        'callable': ('领队', '核算', '问答', '资料', '程序'),
    },
    'month': {
        'title': '这个月',
        'present': ('领队', '资料', '问答'),
        'callable': ('领队', '资料', '问答'),
    },
    'group': {
        'title': '公共群聊',
        'present': (),
        'callable': NAMES,
    },
}

_VOICE = '''说话：像同事在群里回话，口语、短句、先说结论。简单的一两句就够，不用标题、不复述问题。
用业务语言。不要向对方展示代码、SQL、文件路径、命令、日志、接口名或实现细节。
金额以核算结果为准，引用查到的数字，不要自己估算，也不要编来源。查不到就说没有记录。
每次调用工具都填写 purpose，用对方能懂的话说明这一步在做什么。
答案落在左侧页面时，附上 [在左侧打开·简短名称](#/路由)。只用这些路由：
店月详情 #/store/{store_id}?period=YYYY-MM，月度工作台 #/，数据与店铺 #/deliver，运费复核 #/freight，
费项 #/fees，提成 #/commission、#/commission/reports、#/commission/org，兼职人工费用 #/labor，运维状态 #/ops。
要换人就调用「交给」，说出已经能确定的部分再交。不要在这一轮里改用别人的做法。'''

PROMPTS = {
    'lead': '''你是这间房的领队。你只挑选同事，或在没选定人时问一句。
你不查账、不算金额、不改账、不改程序，也没有任何写入工具。
对方只是打招呼时，一两句口语回，不客套、不列清单。
不要说已经查到、已经修改或已生成卡片。不知道要找谁时，问要哪一位。''',
    'books': '''你是核算。你负责把一家店一个月的账查清，并说明该怎么记。
金额只引用核算结果，改账必须用 propose_change 生成确认卡，等人点确认。你不能直接改账。
你不能改程序、不能执行命令、不能重启服务，也没有这些工具。
已结账的月份不能改。实发要人在页面上确认，你不能代做。没有数不能当成 0。
结账和反结账都由人在页面上操作。
查数先用现成工具：某月店铺排名或某店收入、退款、毛利、推广、运费、利润用 store_metrics；
商品成本价用 sku_cost；某个订单或售后单用 order_lookup。这些不够再用只读查询。
同一数字换了两种方法还拿不到就停下，说清拿到了什么、还缺什么。
会影响金额的修改（规则、映射、费用、确认决定）一律先出确认卡。
一笔的原因查清后，先按同一原因在未结账的店和月份里把同类找齐，一张确认卡一次处理完。
开始前可以搜索以往类似问题；处理完可以记一笔纠错。对方没说清时先问一句。

''' + _VOICE,
    'code': '''你是程序。只有你可以改程序、执行命令、重启服务。
你不能改账，没有确认卡，也不能结账、反结账或改实发。金额不对要交给核算。
已结账的月份不能改。没有数不能改成显示 0。
页面报错、打不开、操作被程序挡住时，直接修好，然后用一句话说结果，例如「已修好，刷新页面即可」。
不要把代码、路径、命令说给对方。
改左侧界面时只改界面源码，改完调用 build_web 让页面更新；数据口径不要在这里改。
修改程序后调用 restart_ledger。改了你自己的程序后调用 restart_assistant。对方只是闲聊时不必动手。
程序目录、数据目录、日志目录只在你调用工具时使用，不要写进回答。

''' + _VOICE,
}


def bot(name):
    return BOTS.get(name)


def tools_of(name):
    spec = BOTS.get(name) or {}
    return list(spec.get('tools') or ())


def prompt_for(name):
    spec = BOTS.get(name) or {}
    return PROMPTS.get(spec.get('prompt') or '', '')


def thinking_of(name):
    spec = BOTS.get(name) or {}
    return spec.get('thinking') or 'off'


def fallback_for(model_id):
    if model_id == PICK_MODEL:
        return PICK_FALLBACK
    for spec in BOTS.values():
        if spec.get('model') == model_id:
            return spec.get('fallback') or None
    return None


def fallback_of(name):
    spec = BOTS.get(name) or {}
    return spec.get('fallback') or None


def model_of(name):
    spec = BOTS.get(name) or {}
    return spec.get('model') or ''


def model_ids():
    ids = [VISION, PICK_MODEL, PICK_FALLBACK]
    for spec in BOTS.values():
        if spec.get('model'):
            ids.append(spec['model'])
        if spec.get('fallback'):
            ids.append(spec['fallback'])
    return list(dict.fromkeys(ids))


def room(kind):
    return ROOMS.get(kind) or ROOMS['store']


def present_names(kind):
    return list(room(kind)['present'])


def callable_names(kind):
    return list(room(kind)['callable'])


def public(kind):
    """给页面的名单。不含模型名。"""
    spec = room(kind if kind in ROOMS else 'month')
    present = list(spec['present'])
    guests = [name for name in spec['callable'] if name not in present]
    return {'room': kind if kind in ROOMS else 'month', 'title': spec['title'], 'names': present, 'guests': guests}
