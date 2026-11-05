"""回答发出前的核对：金额、比例、订单号、商品编码和“已办好”必须有本轮依据。

依据只认两样：本轮工具真正返回的内容、用户自己说过的话。助手以前的回答不算依据。
"""
import re
from decimal import Decimal, InvalidOperation

UNVERIFIED = '（未核实）'
NUMBER = re.compile(r'(?<![\w.])(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)(\s*(?:万|%|个百分点))?')
CODE = re.compile(r'(?<![A-Za-z0-9])(?:[A-Za-z]{2,}[-_]?\d{3,}[A-Za-z0-9-]*|\d{12,})(?![A-Za-z0-9])')
DATES = re.compile(r'\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?|\d{1,2}月\d{1,2}[日号]?|\d{1,2}:\d{2}(?::\d{2})?')
COUNT_UNIT = re.compile(r'^\s*(?:月|日|号|年|家|笔|单|个|条|步|次|天|名|位|件|秒|分钟|小时|人|项|行|页|张|周|季度|倍|成)')
APPROX = re.compile(r'(?:近|约|大约|大概|将近|超过|不到|接近|左右|上下|多)\s*$')
DONE = re.compile(r'已(?:经)?(?:帮你)?(?:手动|人工|强制)?(?:补录|回填|改好|改成|修好|修复|修改了|写入|删除了|删掉|撤销了|生成[^。\n，,]{0,8}卡片)')
WRITE_TOOLS = {'edit_file', 'write_file', 'restart_ledger', 'restart_assistant', 'build_web', 'run_auto_close', 'powershell', 'remember', 'record_correction'}


def _decimal(text):
    try:
        return Decimal(text.replace(',', ''))
    except InvalidOperation:
        return None


def _places(text):
    return len(text.split('.', 1)[1]) if '.' in text else 0


def _evidence_numbers(evidence):
    values = set()
    for match in NUMBER.finditer(evidence):
        value = _decimal(match.group(1))
        if value is not None:
            values.add(abs(value))
    return values


def _supported(raw, unit, values, evidence_flat):
    if raw.replace(',', '') in evidence_flat:
        return True
    value = _decimal(raw)
    if value is None:
        return True
    value, places = abs(value), _places(raw)
    unit = (unit or '').strip()
    quantum = Decimal(1).scaleb(-places)
    for e in values:
        candidates = [e]
        if unit == '万':
            candidates = [e / 10000]
        elif unit in {'%', '个百分点'}:
            candidates = [e, e * 100]
        for c in candidates:
            try:
                if abs(c.quantize(quantum) - value) <= quantum:
                    return True
            except InvalidOperation:
                continue
    return False


def issues(answer, evidence, *, cards=0, tools=()):
    """返回回答里找不到依据的片段（原文），用于改写或标注。"""
    found = []
    flat = evidence.replace(',', '')
    values = _evidence_numbers(evidence)
    lowered = flat.lower()
    masked = DATES.sub(lambda m: ' ' * len(m.group(0)), answer)
    for match in CODE.finditer(masked):
        if match.group(0).lower() not in lowered:
            found.append(match.group(0))
    masked = CODE.sub(lambda m: ' ' * len(m.group(0)), masked)
    for match in NUMBER.finditer(masked):
        raw, unit = match.group(1), match.group(2)
        plain = raw.replace(',', '').lstrip('-')
        if not unit and '.' not in plain and len(plain) < 3:
            continue
        if not unit and '.' not in plain and COUNT_UNIT.match(masked[match.end():]):
            continue
        if not unit and '.' not in plain and 1990 <= int(plain) <= 2100:
            continue
        if APPROX.search(masked[max(0, match.start() - 3):match.start()]):
            continue
        if not _supported(raw, unit, values, flat):
            found.append(match.group(0).strip())
    tools = set(tools)
    for match in DONE.finditer(answer):
        claim = match.group(0)
        if '卡片' in claim:
            if not cards:
                found.append(claim)
        elif not tools & WRITE_TOOLS:
            found.append(claim)
    return list(dict.fromkeys(found))


def mark(answer, problems):
    """把仍然没有依据的片段在原文里标上（未核实）。"""
    for item in sorted(problems, key=len, reverse=True):
        answer = re.sub(re.escape(item) + f'(?!{re.escape(UNVERIFIED)})', lambda m: m.group(0) + UNVERIFIED, answer)
    return answer


REVISE = ('下面是你刚写给同事的回答草稿。程序核对发现，其中这些内容在本轮实际查到的数据里找不到依据：\n{items}\n\n'
          '请改写这条回答：没有依据的数字、订单号、来源说法直接删掉，或改成“没查到”“需要再查”；'
          '没有真正做过的操作不要说已经做了（生成确认卡片不等于已经修改）。'
          '其余有依据的内容保持原样，不要新增任何数字。只输出改写后的回答本身。\n\n【草稿】\n{draft}')
