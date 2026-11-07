"""会话里的一轮：后台先定由哪位同事办，再用这位的模型和工具把话说完。"""
import base64
import json
import queue
import re
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

_COPILOT = Path(__file__).resolve().parents[1]
if str(_COPILOT) not in sys.path:
    sys.path.insert(0, str(_COPILOT))
import roster as team

from . import config, docs, events, llm, pi_engine, room_worker, store, tools, verify

MAX_STEPS = 16
WRAP_UP_AT = 8
WRAP_UP = ('（系统提示：已经查了 8 步。优先用已拿到的结果回答；同一个数换了两种方法还拿不到就别再试，'
           '直接说清拿到了什么、还缺什么。）')
FINAL = ('（系统提示：步数用完了。不要再调用工具，用已经查到的结果直接回答：先给结论或目前能确定的部分，'
         '再用一句话说还缺什么、怎么补。）')
RECENT = 10

ENGINEER_WORDS = re.compile(r'报错|错误提示|打不开|白屏|卡死|很卡|太慢|加载不出|500|bug|程序|页面.*(坏|错|空)|按钮.*(没反应|不能)|改代码|功能|强制|直接(做|改|执行|补)|执行|挡住|不让|(不能|无法)(补|改|录|保存)')

# 只有明确的寒暄由领队自己回；“是”“好的”“手动补录”这类短句多半是在接着办上一件事。
SMALL_TALK = re.compile(r'^(你好|您好|在吗|在不在|hi|hello|嗨|早|早上好|中午好|下午好|晚上好|谢谢|多谢|谢啦|感谢|辛苦了?|'
                        r'哈+|你是谁|你能做什么|你会什么|你会干什么)[\s！!。.~～？?呀啊呢哦]*$', re.I)
MENTION = re.compile(r'[@＠](核算|问答|资料|程序|领队|助手)')
# 这些要求在挑选之前直接拒绝，不交给程序或核算。挑选模型会把它们派给程序。
_REFUSE = (
    (re.compile(r'反结账|取消结账|撤销结账'), '反结账要在店月页上由人点，我不能代做。'),
    (re.compile(r'(改|修改|调整|重算|删掉|冲掉|冲销).{0,12}已结账|已结账.{0,16}(改|修改|调整|重算|删|冲)'),
     '已结账的月份不能改。要处理的话，放在还没结账的月份。'),
    (re.compile(r'(改|修改|调整|重算|核定|发放|重发).{0,8}实发|实发.{0,8}(改|修改|调整|重算|核定|重发)'),
     '实发要人在页面上确认，我不能改。'),
    (re.compile(r'(空值|空白|空的|空着|没有数|没数|暂无|缺失).{0,20}(显示|看成|当成|当作|写成|算成|填成|补成).{0,8}[0０零]'),
     '没有数不能显示成 0。空着就是还没有这笔数。'),
)
_HUMAN_AT = re.compile(r'^\s*[@＠](?!核算|问答|资料|程序|领队|助手)\S')


def mentioned(text):
    found = MENTION.search(text or '')
    if not found:
        return ''
    name = found.group(1)
    return '领队' if name == '助手' else name


def human_only(thread_id, text):
    """只有开头在 @某个人时，才不叫模型。没点名的话由模型自己分派。"""
    if thread_id != store.GROUP:
        return False
    return bool(_HUMAN_AT.match(text or ''))


def room_kind(thread_id, page=None):
    if thread_id == store.GROUP:
        return 'group'
    text = thread_id or ''
    if text.startswith('store:'):
        return 'store'
    if text.startswith('month:'):
        return 'month'
    kind = (page or {}).get('room')
    if kind in team.ROOMS:
        return kind
    if (page or {}).get('name') == 'period':
        return 'store'
    return 'month'


def room_from_page(page):
    if not page:
        return None
    kind = page.get('room') or ('store' if page.get('name') == 'period' else 'month' if page.get('name') == 'board' else None)
    if kind not in team.ROOMS or kind == 'group':
        return None
    period = str(page.get('period') or (page.get('query') or {}).get('period') or '')
    if kind == 'store':
        store_id = (page.get('params') or {}).get('id') or page.get('store_id') or ''
        if not store_id:
            return None
        return f'store:{store_id}:{period}'
    return f'month:{period}'


def bind_thread(thread_id, page):
    """会话由人自己开，不跟着页面换。页面只当背景带给模型。"""
    return thread_id or uuid.uuid4().hex[:12]


def parse_room(thread_id):
    text = thread_id or ''
    if text.startswith('store:'):
        parts = text.split(':', 2)
        return 'store', parts[1] if len(parts) > 1 else '', parts[2] if len(parts) > 2 else ''
    if text.startswith('month:'):
        return 'month', '', text.split(':', 1)[1]
    return 'group', '', ''


def room_title(thread_id, page=None):
    kind, store_id, period = parse_room(thread_id)
    if kind == 'group':
        return '公共群聊'
    if kind == 'month':
        return f'这个月 {period}'.strip()
    heading = ((page or {}).get('heading') or '').strip()
    name = heading or store_id
    return f'{name} {period}'.strip() or '店月'


def _refusal(text):
    for pattern, line in _REFUSE:
        if pattern.search(text or ''):
            return line
    return ''


def choose(text, room, trigger=None, *, has_images=False, original=None, picker=True):
    """按固定顺序定这一轮是谁。picker=False 时不调用挑选模型。"""
    basis = ((original if original is not None else text) or '').strip()
    picture = (text or '').strip()
    routed = basis or picture
    richer = (basis + '\n' + picture).strip() if picture and picture != basis else routed
    if room == 'group' and _HUMAN_AT.match(routed):
        return {'kind': 'silent', 'bot': '', 'via': 'group'}
    # 反结账、已结账、实发、空值当 0：点了名也不交给程序或核算。
    refused = _refusal(richer)
    if refused:
        return {'kind': 'refuse', 'bot': '领队', 'via': 'refuse', 'text': refused}
    name = mentioned(routed)
    if name:
        return {'kind': 'bot', 'bot': name, 'via': 'mention', 'announce': False}
    if trigger in {'materials', 'orders'}:
        return {'kind': 'bot', 'bot': '资料', 'via': 'trigger', 'announce': False}
    if trigger == 'dispute' or '这笔不对' in routed or '这笔不对' in richer:
        return {'kind': 'bot', 'bot': '问答', 'via': 'trigger', 'announce': False}
    if ENGINEER_WORDS.search(routed):
        return {'kind': 'bot', 'bot': '程序', 'via': 'engineer', 'announce': False}
    if not has_images and basis and SMALL_TALK.match(basis):
        return {'kind': 'chat', 'bot': '领队', 'via': 'talk'}
    if not picker:
        return {'kind': 'pending', 'bot': '', 'via': 'pick'}
    picked = _pick(richer[:1500], room)
    if picked['kind'] == 'bot' and picked['bot'] != '领队':
        picked['announce'] = True
    return picked


_BOOKS = re.compile(r'利润|成本|费用|金额|亏|赚|毛利|净利|偏低|偏高|差额|对不上|入账|科目|算一算|为什么')
_FILES = re.compile(r'资料|缺什么|账单|对账单|文件|没到|补录|导入|附件')


def _route_work(text):
    """挑选模型没给出干活的人时，按这句话自己定，不再反问找谁。"""
    if _FILES.search(text or ''):
        return '资料'
    if _BOOKS.search(text or ''):
        return '核算'
    return '问答'


def _pick(text, room):
    """温度 0，只从核算、问答、资料、程序里收一个名字。领队不接活。"""
    names = [name for name in team.callable_names(room) if name != '领队'] or ['核算', '问答', '资料', '程序']
    prompt = ('你只从名单里选一个名字来处理下面这句话。只回复那个名字，不要解释，不要做别的事。\n'
              f'名单：{"、".join(names)}\n\n这句话：{(text or "")[:1500]}')
    messages = [{'role': 'user', 'content': prompt}]
    try:
        out, _, _ = llm.complete(team.PICK_MODEL, messages, max_tokens=16, timeout=team.PICK_TIMEOUT,
                                 extra={'temperature': 0})
    except Exception as exc:
        store.log_event('pick_error', {'error': str(exc)[:500]})
        return {'kind': 'bot', 'bot': _route_work(text), 'via': 'pick', 'announce': True}
    cleaned = (out or '').strip().strip('。．.！!？? \n"「」"\'')
    if cleaned in names:
        return {'kind': 'bot', 'bot': cleaned, 'via': 'pick', 'announce': True}
    store.log_event('pick_rejected', {'text': cleaned[:80]})
    return {'kind': 'bot', 'bot': _route_work(text), 'via': 'pick', 'announce': True}


def _save_images(images):
    folder = config.ROOT / 'uploads' / datetime.now().strftime('%Y%m')
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for image in images[:6]:
        header, _, data = image.get('data_url', '').partition(',')
        if not data:
            continue
        ext = 'png' if 'png' in header else 'jpg'
        name = f'{uuid.uuid4().hex[:12]}.{ext}'
        (folder / name).write_bytes(base64.b64decode(data))
        saved.append((f'/api/agent/uploads/{folder.name}/{name}', image['data_url']))
    return saved


def _transcribe(images, text, emit):
    step = {'id': uuid.uuid4().hex[:8], 'label': '识别截图内容', 'status': 'running'}
    emit('step', step)
    started = time.time()
    content = [{'type': 'text', 'text': '这是用户在台账系统中截的图，用户的问题是：' + text +
                '\n请准确提取截图中的全部文字和数字（表格按行列整理，保留店铺名、月份、金额、列名），并说明这是什么页面、用户可能关注哪个数字。只输出提取结果。'}]
    content += [{'type': 'image_url', 'image_url': {'url': data_url}} for _, data_url in images]
    try:
        out, tokens, cost = llm.complete(config.models()['vision']['id'], [{'role': 'user', 'content': content}], max_tokens=3000)
    except Exception as exc:
        out, tokens, cost = f'（截图识别失败：{llm.describe(exc)}）', 0, 0
    emit('step', {**step, 'status': 'done', 'ms': int((time.time() - started) * 1000)})
    return out, tokens, cost


def _page_text(page):
    if not page:
        return ''
    parts = [f"页面：{page.get('heading') or page.get('title') or ''}", f"地址：{page.get('path', '')}"]
    if page.get('selection'):
        parts.append('用户选中的文字：' + page['selection'][:2000])
    return '【用户当前在左侧看的页面】' + '；'.join(p for p in parts if p.split('：', 1)[1])


EPHEMERAL = {'type': 'ephemeral'}
TECH_WORDS = re.compile(r'端口|目录|接口|路径|进程|脚本|日志|代码|数据库|表结构|字段|SQL|API|python|json|http|\.py|\.db', re.I)
DATED = re.compile(r'\d{4}-\d{2}|\d{1,2}月|[a-z0-9]+_[a-z0-9]+')
GROUP_NOTE = ('\n\n【公共群聊】这是 2-3 位同事共用的群聊，消息前的【名字】是发言人。回答时可以称呼对方；'
              '别人刚才说过的事实和结论直接沿用，不要重复查。')


def _blocks(text, mark=False):
    block = {'type': 'text', 'text': text}
    if mark:
        block['cache_control'] = EPHEMERAL
    return [block]


def _memory_split(query):
    """长期记忆分两半：通用口径放进稳定前缀；带月份或店铺的条目只在相关时随问题带上。"""
    general, specific = [], []
    for line in store.memory().splitlines():
        if line.strip():
            (specific if DATED.search(line) else general).append(line)
    terms = store._terms(query)
    scored = sorted(((len(terms & store._terms(line)), line) for line in specific), key=lambda x: -x[0])
    return '\n'.join(general), '\n'.join(line for score, line in scored[:15] if score >= 2)


def _stable(thread_id, bot):
    """这位同事自己的提示词。名册不写在这里。"""
    system = team.prompt_for(bot)
    if 'read_knowledge' in team.tools_of(bot):
        index = tools.knowledge_index()
        if index:
            system += '\n\n【系统地图目录】（详细内容用 read_knowledge 按章节读取；先查这里再动手，能少走很多步）\n' + index
    if thread_id == store.GROUP:
        system += GROUP_NOTE
    return system


def _context(thread_id, query, exclude, bot):
    general, _ = _memory_split(query)
    row = store.thread(thread_id)
    background = ''
    if general:
        background += '【长期记忆】\n' + general
    if row and row['summary']:
        background += '\n\n【这段对话早前的经过】\n' + row['summary']
    content = _blocks(_stable(thread_id, bot), mark=True) + (_blocks(background.strip()) if background else [])
    messages = [{'role': 'system', 'content': content}]
    group = thread_id == store.GROUP
    history = store.messages(thread_id, (row['summarized_upto'] if row else 0) or 0, prompts=True)
    for index, m in enumerate(history):
        if m['id'] == exclude:
            continue
        if m['role'] == 'assistant' and _corrected(m['text'], history[index + 1:]):
            continue
        if m['role'] == 'system':
            messages.append({'role': 'user', 'content': _blocks('（系统通知）' + m['text'])})
        elif m['role'] == 'user':
            prompt = m.get('prompt') or ((f"【{m['user'] or '同事'}】" if group else '') + m['text'])
            if prompt:
                messages.append({'role': 'user', 'content': _blocks(prompt)})
        elif m['text']:
            who = m.get('bot') or '同事'
            messages.append({'role': 'assistant', 'content': f'【{who}】' + OLD_ANSWER + m['text']})
    return messages


OLD_ANSWER = '（旧回答，未核实）'
QUOTED = re.compile(r'[“"]([^”"]{4,80})[”"]')


def _corrected(text, later):
    """后面有“更正”点名引用过这条回答里的原话，这条就不再带给模型。"""
    for m in later:
        if m['role'] == 'system' and (m['text'] or '').startswith('更正'):
            if any(q in (text or '') for q in QUOTED.findall(m['text'])):
                return True
    return False


def _evidence(messages, user_text, results):
    """核对依据：工具结果、用户说过的话、系统提示（含长期记忆）。助手旧回答不算。"""
    parts = [user_text, *results]
    for m in messages:
        if m['role'] in {'system', 'user', 'tool'}:
            blocks = [m['content']] if isinstance(m['content'], str) else [b.get('text', '') for b in m['content']]
            parts += [b.split('【这段对话早前的经过】')[0] for b in blocks]
    return '\n'.join(parts)


def _checked(thread_id, answer, evidence, cards, used_tools, emit, model_id):
    """发出前核对。没依据的用这一轮同一位的模型改写一遍，还剩的就地标（未核实）。返回 (正文, tokens, cost)。"""
    draft = ''.join(answer)
    problems = verify.issues(draft, evidence, cards=len(cards), tools=used_tools)
    if not problems:
        return draft, 0, 0.0
    tokens, cost, revised = 0, 0.0, ''
    try:
        prompt = verify.REVISE.format(items='\n'.join('- ' + p for p in problems), draft=draft)
        revised, tokens, cost = llm.complete(model_id, [{'role': 'user', 'content': prompt}], max_tokens=2500, timeout=90)
    except Exception as exc:
        store.log_event('verify_error', {'thread': thread_id, 'error': str(exc)[:500]})
    revised = revised.strip() or draft
    left = verify.issues(revised, evidence, cards=len(cards), tools=used_tools)
    final = verify.mark(revised, left)
    store.log_event('verify', {'thread': thread_id, 'flagged': problems[:20], 'left': left[:20]})
    emit('replace', {'text': final})
    return final, tokens, cost


def _volatile(query, page):
    """每次都会变的放在最后一条用户消息里，前面的前缀才能被缓存命中。"""
    _, specific = _memory_split(query + ' ' + json.dumps(page or {}, ensure_ascii=False))
    related = store.search_corrections(query, 3)
    parts = [f"（今天 {datetime.now():%Y-%m-%d %H:%M}）"]
    if page:
        parts.append(_page_text(page))
    if specific:
        parts.append('【相关记忆】\n' + specific)
    if related:
        parts.append('【可能相关的以往纠错】\n' + json.dumps(related, ensure_ascii=False))
    return '\n'.join(parts)


def _mark(messages):
    """缓存标记只留两处：固定规则末尾，和本次请求的最后一条消息（每一步都往后挪）。"""
    for m in messages[1:]:
        if isinstance(m.get('content'), list):
            for block in m['content']:
                block.pop('cache_control', None)
    last = messages[-1]
    if isinstance(last.get('content'), str) and last['content']:
        last['content'] = _blocks(last['content'])
    if isinstance(last.get('content'), list) and last['content']:
        last['content'][-1]['cache_control'] = EPHEMERAL


def _size(messages):
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)


def _drain(inbox, messages):
    added = 0
    while inbox is not None:
        try:
            prompt = inbox.get_nowait()
        except queue.Empty:
            break
        messages.append({'role': 'user', 'content': _blocks(prompt)})
        added += 1
    return added


def _merge_alternating(messages):
    merged = []
    for m in messages:
        if merged and m['role'] == merged[-1]['role'] == 'user' and isinstance(m['content'], str):
            merged[-1] = {'role': 'user', 'content': merged[-1]['content'] + '\n' + m['content']}
        else:
            merged.append(m)
    return merged


def post_message(thread_id, text, images, page, user, files=(), prompt=None, title=None):
    if not title:
        title = text.strip().splitlines()[0] if text.strip() else (files[0]['name'] if files else '新会话')
    thread_id, _ = store.ensure_thread(thread_id, title, user)
    saved = _save_images(images or [])
    message_id = store.add_message(thread_id, 'user', text, images=[u for u, _ in saved], page=page, user=user,
                                   files=[{k: f[k] for k in ('file_id', 'name', 'size', 'kind', 'url') if k in f} for f in files],
                                   prompt=prompt)
    events.publish('message', {'thread_id': thread_id, 'message': store.messages(thread_id, message_id - 1)[0]})
    return thread_id, message_id, saved


def steer(thread_id, text, files, user):
    """处理中途补充的话：存进对话、广播，返回要追加给正在进行的那一轮的内容。"""
    group = thread_id == store.GROUP
    resolved = docs.resolve(files)
    attachments, _, _ = _attachments(resolved, text, None)
    prompt = (f'【{user or "同事"}】' if group else '') + '【补充，不要从头再来，接着刚才的进度】' + text + attachments
    post_message(thread_id, text, [], None, user, resolved, prompt)
    return prompt


def _attachments(files, text, step_emit):
    """附件变成一段文字：图片走识别，文档抽取摘要。返回 (文字, tokens, cost)。"""
    out, tokens, cost = '', 0, 0.0
    images = [f for f in files if f['kind'] == 'image']
    if images and step_emit:
        transcript, t, c = _transcribe([(f['url'], docs.data_url(f)) for f in images], text, step_emit)
        out += '\n\n【图片内容（已识别）】\n' + transcript
        tokens, cost = tokens + t, cost + c
    for f in files:
        if f['kind'] == 'image':
            continue
        summary, page_images = docs.describe(f)
        out += f"\n\n【附件：{f['name']}】原文件：{f['path']}\n{summary}"
        if page_images and step_emit:
            transcript, t, c = _transcribe([(None, url) for url in page_images], text, step_emit)
            out += '\n【扫描页内容（已识别）】\n' + transcript
            tokens, cost = tokens + t, cost + c
    return out, tokens, cost


def run_turn(thread_id, text, images, page, user, emit, stop, run_id='', files=(), inbox=None, resume=None,
             thinking='off', bot=None, trigger=None, subject=None):
    page = page or {}
    thread_id = bind_thread(thread_id, page)
    title = room_title(thread_id, page) if store.is_room(thread_id) else None
    files = docs.resolve(files)
    if resume:
        message_id, saved = resume, []
        if not store.thread(thread_id):
            store.ensure_thread(thread_id, title, user)
    else:
        thread_id, message_id, saved = post_message(thread_id, text, images, page, user, files, title=title)
    emit('thread', {'thread_id': thread_id, 'title': store.thread(thread_id)['title'], 'run_id': run_id,
                    'message_id': message_id})
    started = time.time()
    kind = room_kind(thread_id, page)
    steps = []

    def step_emit(event, value):
        if event == 'step':
            value = {**value, 'bot': value.get('bot') or ''}
            existing = next((s for s in steps if s['id'] == value['id']), None)
            if existing:
                existing.update(value)
            else:
                steps.append(dict(value))
        emit(event, value)

    user_text = text or ''
    if saved:
        transcript, _, _ = _transcribe(saved, text, step_emit)
        user_text += '\n\n【截图内容（已识别）】\n' + transcript
    if files:
        extra, _, _ = _attachments(files, text, step_emit)
        user_text += extra
    if (saved or files) and not (text or '').strip():
        user_text += '\n\n（用户只发了附件，没有写问题。先用一两句话说清这是什么、可能值得注意的地方，然后问用户想做什么；不要直接深入排查。）'

    if bot:
        decision = {'kind': 'bot', 'bot': bot, 'via': trigger or 'given', 'announce': False}
    else:
        decision = choose(user_text, kind, trigger, has_images=bool(saved or files), original=text, picker=True)
    store.log_event('room_route', {'thread': thread_id, 'kind': decision.get('kind'), 'bot': decision.get('bot'),
                                   'via': decision.get('via')})
    if decision['kind'] == 'silent':
        emit('done', {'bot': '', 'reply_to': message_id, 'run_id': run_id})
        return thread_id
    if decision['kind'] == 'refuse':
        return _finish_say(thread_id, '领队', decision['text'], emit, run_id, message_id, user, started, steps)
    if decision['kind'] == 'ask':
        decision = {'kind': 'bot', 'bot': _route_work(text or user_text), 'via': 'pick', 'announce': True}
    if decision['kind'] == 'chat':
        return _plain_reply(thread_id, text or user_text, user, emit, stop, run_id, message_id, started, '领队')

    current = decision['bot']
    hops = 0
    while current and hops < 3 and not stop.is_set():
        hops += 1
        emit('bot', {'bot': current})
        if current == '问答':
            store_id, period = _scope(thread_id, page, subject)
            question = (text or '').strip() or '请说明这项'
            if subject and subject.get('node_name') and subject['node_name'] not in question:
                question = f'{question}：{subject["node_name"]}'

            step = {'id': uuid.uuid4().hex[:8], 'label': '正在看这期账单', 'status': 'running', 'bot': current}
            step_emit('step', step)
            spoken, question_id = room_worker.answer_question(store_id, period, question, user, subject, stop, inbox, lambda _label: None,
                                                                 thread_id)
            step_emit('step', {**step, 'status': 'done', 'ms': int((time.time() - started) * 1000)})
            _finish_say(thread_id, current, spoken, emit, run_id, message_id, user, started, steps,
                        extra={'question_id': question_id} if question_id else None)
            break
        if current == '资料':
            store_id, period = _scope(thread_id, page, subject)
            arrival = trigger if trigger in {'materials', 'orders'} else ''
            spoken = room_worker.describe_materials(store_id, period, arrival, '', stop, inbox)
            _finish_say(thread_id, current, spoken, emit, run_id, message_id, user, started, steps)
            break
        nxt = _tool_turn(thread_id, current, user_text, text, page, user, emit, stop, run_id, message_id, inbox,
                         thinking, started, step_emit, steps, kind)
        if not nxt or nxt == current:
            break
        current = nxt
        user_text = f'请接着这段对话里已经说出来的内容办。刚才的问题是：{(text or user_text)[:1500]}'
    else:
        if stop.is_set():
            pass
    from . import costs
    events.publish('costs', costs.summary(force=True))
    threading.Thread(target=_summarize, args=(thread_id,), daemon=True).start()
    return thread_id


def _scope(thread_id, page, subject):
    kind, store_id, period = parse_room(thread_id)
    if not store_id:
        store_id = (page or {}).get('store_id') or ((page or {}).get('params') or {}).get('id') or ''
    if not period:
        period = str((page or {}).get('period') or ((page or {}).get('query') or {}).get('period') or '')
    if subject and not store_id:
        store_id = subject.get('store_id') or store_id
    return store_id, period


def _finish_say(thread_id, bot, text, emit, run_id, message_id, user, started, steps, extra=None, done=True):
    emit('bot', {'bot': bot})
    if text:
        emit('delta', {'text': text})
    receipt = {'model': team.model_of(bot), 'bot': bot, 'tokens': 0, 'cost_cny': 0,
               'ms': int((time.time() - started) * 1000), 'reply_to': message_id, 'run_id': run_id, 'asker': user,
               **(extra or {})}
    reply_id = store.add_message(thread_id, 'assistant', text or '', steps=steps, receipt=receipt, bot=bot)
    events.publish('message', {'thread_id': thread_id, 'run_id': run_id,
                               'message': store.messages(thread_id, reply_id - 1)[0]})
    if done:
        emit('done', receipt)
    return thread_id


def _specs(bot):
    allowed = set(team.tools_of(bot))
    return [spec for spec in tools.SPECS if spec['function']['name'] in allowed]


def _tool_turn(thread_id, bot, user_text, text, page, user, emit, stop, run_id, message_id, inbox, thinking, started,
               step_emit, steps, kind):
    """一位同事的工具循环。模型写死在这位身上，失败只用这位的备用模型。返回要交给的下一位，或空。"""
    thinking = thinking if thinking in llm.THINKING else 'off'
    if thinking == 'off':
        thinking = team.thinking_of(bot)
    model_id = team.model_of(bot)
    messages = _context(thread_id, text or user_text, message_id, bot)
    prompt = (f'【{user or "同事"}】' if thread_id == store.GROUP else '') + (user_text or '').strip()
    prompt += '\n\n' + _volatile(text or user_text, page)
    if message_id:
        store.set_prompt(message_id, prompt)
    specs = _specs(bot)
    allowed = {spec['function']['name'] for spec in specs}
    handoff_names = set(team.callable_names(kind))
    used = [model_id] if model_id else []
    tokens = cost = 0
    cards, answer = [], []
    results, used_tools = [], set()
    error = None
    handoff = ''
    used_fallback = False
    piped = None
    session = pi_engine.claim_session(f'{thread_id}:{bot}')
    if model_id and pi_engine.available():
        try:
            piped = _pi_turn(thread_id, bot, model_id, messages, prompt, inbox, stop, emit, step_emit, cards, answer, used,
                             results, used_tools, thinking, session, message_id)
        except pi_engine.Unavailable as exc:
            store.log_event('pi_unavailable', {'thread': thread_id, 'error': str(exc)[:1000]})
            answer.clear()
            pi_engine.release_session(f'{thread_id}:{bot}', session, keep=False)
            session = None
    if piped:
        tokens, cost, error = piped[0], piped[1], piped[2]
        handoff = piped[3] if len(piped) > 3 and piped[3] else handoff
    messages.append({'role': 'user', 'content': _blocks(prompt)})
    try:
        for index in range(0 if piped else MAX_STEPS):
            if stop.is_set() or error:
                break
            if index == WRAP_UP_AT:
                messages.append({'role': 'user', 'content': _blocks(WRAP_UP)})
            _drain(inbox, messages)
            if _size(messages) > 600_000:
                _shrink_old_results(messages)
            _mark(messages)
            text_parts, end = [], None
            try:
                extra = llm.thinking_extra(model_id, thinking) or {}
                for event, value in llm.stream(model_id, messages, specs or None, stop=stop, extra=extra):
                    if event == 'text':
                        text_parts.append(value)
                        answer.append(value)
                        emit('delta', {'text': value})
                    else:
                        end = value
            except llm.BudgetExhausted:
                raise
            except Exception as exc:
                fallback = team.fallback_of(bot)
                if fallback and not used_fallback and fallback != model_id:
                    model_id = fallback
                    used_fallback = True
                    used.append(model_id)
                    continue
                raise RuntimeError(llm.describe(exc)) from exc
            if end is None:
                break
            tokens += end['tokens']
            cost += end['cost']
            calls = end['tool_calls']
            assistant = {'role': 'assistant', 'content': ''.join(text_parts) or None}
            if calls:
                assistant['tool_calls'] = [{'id': c['id'] or f'call_{index}_{i}', 'type': 'function',
                                            'function': {'name': c['name'], 'arguments': c['arguments'] or '{}'}}
                                           for i, c in enumerate(calls)]
            messages.append(assistant)
            if not calls:
                if _drain(inbox, messages):
                    answer.append('\n\n')
                    emit('delta', {'text': '\n\n'})
                    continue
                break
            if text_parts:
                answer.append('\n\n')
                emit('delta', {'text': '\n\n'})
            for call in assistant['tool_calls']:
                name = call['function']['name']
                if name not in allowed:
                    result = '这轮不能做这件事。'
                else:
                    result = _call(call, thread_id, step_emit, cards, emit, bot)
                if name == '交给':
                    try:
                        body = json.loads(result)
                    except ValueError:
                        body = {}
                    target = body.get('handoff') if isinstance(body, dict) else ''
                    if target and target != bot and (target in handoff_names or target == '程序' and kind == 'store'):
                        handoff = target
                        result = f'已交给{target}。'
                    elif name == '交给':
                        result = '这件事不能交给这位。'
                used_tools.add(name)
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)})
                if handoff:
                    break
            if handoff:
                break
        else:
            if not piped and not handoff:
                t, c = _final_answer(thread_id, model_id, messages, stop, emit, answer, specs)
                tokens, cost = tokens + t, cost + c
    except llm.BudgetExhausted:
        error = '本月的模型费用已到上限，下月自动恢复；如需提高上限请联系管理员。'
    except Exception as exc:
        error = '这次处理遇到了问题，请稍后再试或换个说法。'
        store.log_event('turn_error', {'thread': thread_id, 'bot': bot, 'error': str(exc)[:2000]})
    if not error and not stop.is_set() and ''.join(answer).strip() and not handoff:
        checked, t, c = _checked(thread_id, answer, _evidence(messages, user_text, results), cards, used_tools, emit, model_id)
        answer[:] = [checked]
        tokens, cost = tokens + t, cost + c
    if error:
        emit('error', {'message': error})
        answer.append(('\n\n' if answer else '') + error)
    elif stop.is_set() and not handoff:
        answer.append(('\n\n' if answer else '') + '（已停止）')
    spoken = ''.join(answer).strip()
    if spoken or cards:
        receipt = {'model': ' → '.join(dict.fromkeys(used)), 'bot': bot, 'tokens': tokens, 'cost_cny': round(cost, 4),
                   'ms': int((time.time() - started) * 1000), 'reply_to': message_id, 'run_id': run_id, 'asker': user}
        reply_id = None
        try:
            reply_id = store.add_message(thread_id, 'assistant', ''.join(answer), steps=steps, cards=cards, receipt=receipt, bot=bot)
        finally:
            pi_engine.release_session(f'{thread_id}:{bot}', session, seen=reply_id, keep=bool(piped and reply_id and not error))
        events.publish('message', {'thread_id': thread_id, 'run_id': run_id,
                                   'message': store.messages(thread_id, reply_id - 1)[0]})
        if not handoff:
            emit('done', receipt)
    else:
        pi_engine.release_session(f'{thread_id}:{bot}', session, keep=False)
        if not handoff:
            emit('done', {'bot': bot, 'model': model_id, 'tokens': tokens, 'cost_cny': round(cost, 4),
                          'reply_to': message_id, 'run_id': run_id, 'asker': user})
    steps.clear()
    return handoff


def _final_answer(thread_id, model_id, messages, stop, emit, answer, specs=None):
    messages.append({'role': 'user', 'content': _blocks(FINAL)})
    _mark(messages)
    parts, tokens, cost = [], 0, 0.0
    try:
        for kind, value in llm.stream(model_id, messages, specs or None, max_tokens=1500, stop=stop,
                                      extra={'tool_choice': 'none'}):
            if kind == 'text':
                parts.append(value)
                answer.append(value)
                emit('delta', {'text': value})
            else:
                tokens, cost = value['tokens'], value['cost']
    except llm.BudgetExhausted:
        raise
    except Exception as exc:
        store.log_event('turn_error', {'thread': thread_id, 'error': 'final: ' + str(exc)[:1000]})
    if not ''.join(parts).strip():
        note = '\n\n这个问题步骤较多，我先停在这里。你可以回复“继续”让我接着查。'
        answer.append(note)
        emit('delta', {'text': note})
    return tokens, cost


def _transcript(messages, limit=60_000):
    lines = []
    for m in messages[1:]:
        content = m['content'] if isinstance(m['content'], str) else ''.join(b.get('text', '') for b in m['content'])
        if content:
            lines.append(('用户：' if m['role'] == 'user' else '助手：') + content[:3000])
    while lines and sum(map(len, lines)) > limit:
        lines.pop(0)
    return '\n\n'.join(lines)


AUTO_THINK = {'investigate': 'low'}
SESSION_NOTE = ('（提醒：前面几轮你自己回答里的数字、来源和“已办好”，只有前面工具结果里对得上的才算数，其余要这一轮重新查；'
                '系统通知里的更正优先。）')


def _since(thread_id, seen, current):
    """上一轮 Pi 会话之后、话题里新出现的消息（同事的话、系统通知、别的路线给的回答）。"""
    lines = []
    for m in store.messages(thread_id, seen or 0):
        if m['id'] == current or not m['text']:
            continue
        if m['role'] == 'user':
            lines.append(f'{m["user"] or "同事"}：{m["text"][:1500]}')
        elif m['role'] == 'system':
            lines.append('（系统通知）' + m['text'][:1500])
        else:
            lines.append(OLD_ANSWER + m['text'][:1500])
    while lines and sum(map(len, lines)) > 20_000:
        lines.pop(0)
    return '\n\n'.join(lines)


def _pi_turn(thread_id, role, model_id, messages, prompt, inbox, stop, emit, step_emit, cards, answer, used,
             results, used_tools, thinking, session=None, message_id=None):
    """交给 Pi 跑工具循环。返回 (tokens, cost, 错误说明, 交给谁)。模型在这一轮里不换人。"""
    run = pi_engine.Run(thread_id, role, step_emit, cards, emit)
    run.results, run.used_tools = results, used_tools
    run.thinking = thinking if thinking != 'off' else 'off'
    system = ''.join(block['text'] for block in messages[0]['content'])
    if session and not session['fresh']:
        results.extend(pi_engine.session_results(session['id']))
        news = _since(thread_id, session['seen'], message_id)
        message = (f'【上次之后话题里的新消息】\n{news}\n\n' if news else '') + f'【现在的问题】\n{prompt}\n\n{SESSION_NOTE}'
    else:
        history = _transcript(messages)
        message = (f'【前面的对话】（标“旧回答，未核实”的是你以前的回答，里面的数字、来源和“已办好”都没核实过，'
                   f'要用必须这一轮重新查；“系统通知”里的更正优先）\n{history}\n\n【现在的问题】\n{prompt}') if history else prompt

    def on_text(text):
        answer.append(text)
        emit('delta', {'text': text})

    def escalate(turns, current):
        return None

    _, error = pi_engine.run(run, model_id=model_id, system=system, prompt=message, inbox=inbox, stop=stop,
                             on_text=on_text, escalate=escalate, session=session)
    for model in run.models:
        if model not in used:
            used.append(model)
    return run.tokens, run.cost, error, getattr(run, 'handoff', '') or ''


def _plain_reply(thread_id, text, user, emit, stop, run_id, message_id, started, bot):
    """领队自己回一句。没有工具，失败时换领队的备用模型再问一句。"""
    model_id = team.model_of(bot) or team.PICK_FALLBACK
    emit('bot', {'bot': bot})
    recent = [m for m in store.messages(thread_id, last=6) if m['role'] in {'user', 'assistant'} and m['id'] != message_id]
    system = team.prompt_for(bot)
    messages = [{'role': 'system', 'content': system}]
    messages += [{'role': m['role'], 'content': m['text'][:500]} for m in recent if m['text']]
    messages = _merge_alternating(messages + [{'role': 'user', 'content': text}])
    answer, tokens, cost = [], 0, 0.0
    try:
        for kind, value in llm.stream(model_id, messages, None, max_tokens=300, timeout=60, stop=stop, extra={'temperature': 0}):
            if kind == 'text':
                answer.append(value)
                emit('delta', {'text': value})
            else:
                tokens, cost = value['tokens'], value['cost']
    except Exception as exc:
        store.log_event('turn_error', {'thread': thread_id, 'error': str(exc)[:2000]})
        fallback = team.fallback_of(bot)
        answer, tokens, cost = [], 0, 0.0
        if fallback and fallback != model_id:
            try:
                for kind, value in llm.stream(fallback, messages, None, max_tokens=120, timeout=20, stop=stop, extra={'temperature': 0}):
                    if kind == 'text':
                        answer.append(value)
                        emit('delta', {'text': value})
                    else:
                        tokens, cost = value['tokens'], value['cost']
                model_id = fallback
            except Exception:
                answer = ['我这边刚才没接上，你再说一次？']
                emit('delta', {'text': answer[0]})
        else:
            answer = ['我这边刚才没接上，你再说一次？']
            emit('delta', {'text': answer[0]})
    else:
        evidence = '\n'.join([text, system] + [m['text'] or '' for m in recent if m['role'] == 'user'])
        checked, t, c = _checked(thread_id, answer, evidence, [], set(), emit, model_id)
        answer[:] = [checked]
        tokens, cost = tokens + t, cost + c
    receipt = {'model': model_id, 'bot': bot, 'tokens': tokens, 'cost_cny': round(cost, 4),
               'ms': int((time.time() - started) * 1000), 'reply_to': message_id, 'run_id': run_id, 'asker': user}
    reply_id = store.add_message(thread_id, 'assistant', ''.join(answer), receipt=receipt, bot=bot)
    events.publish('message', {'thread_id': thread_id, 'run_id': run_id,
                               'message': store.messages(thread_id, reply_id - 1)[0]})
    emit('done', receipt)
    return thread_id


def _shrink_old_results(messages, keep=4, size=1500):
    results = [m for m in messages if m['role'] == 'tool']
    for m in results[:-keep]:
        text = m['content'] if isinstance(m['content'], str) else m['content'][0]['text']
        if len(text) > size:
            m['content'] = text[:size] + '\n…（较早的结果已压缩）'


def _call(call, thread_id, step_emit, cards, emit, role):
    name = call['function']['name']
    try:
        args = json.loads(call['function']['arguments'] or '{}')
    except ValueError:
        args = {}
    if name == '交给':
        who = (args.get('who') or '').strip()
        return json.dumps({'handoff': who}, ensure_ascii=False)
    purpose = args.get('purpose') or ''
    if not purpose or TECH_WORDS.search(purpose):
        purpose = tools.DEFAULT_LABEL.get(name, '处理中')
    step = {'id': uuid.uuid4().hex[:8], 'label': purpose[:60],
            'status': 'running'}
    step_emit('step', step)
    started = time.time()
    status = 'done'
    try:
        if name == 'propose_change':
            card_id = store.add_card(thread_id, args.get('title', '修改确认'), args.get('summary', ''),
                                     args.get('items') or [], args.get('actions') or [])
            cards.append(card_id)
            emit('card', store.card_view(card_id))
            result = '已生成确认卡片，等待用户确认。不要重复生成；简短告诉用户确认后会发生什么。'
        elif name == 'record_correction':
            result = tools.record_correction(thread_id=thread_id, **{k: v for k, v in args.items() if k != 'thread_id'})
        elif name in tools.HANDLERS:
            result = tools.HANDLERS[name](**args)
        else:
            result = f'没有这个工具：{name}'
    except TypeError as exc:
        result = f'参数不正确：{exc}'
        status = 'error'
    except Exception as exc:
        result = f'执行出错：{type(exc).__name__}: {exc}'
        status = 'error'
    step_emit('step', {**step, 'status': status, 'ms': int((time.time() - started) * 1000)})
    return result


def _summarize(thread_id):
    try:
        row = store.thread(thread_id)
        pending = [m for m in store.messages(thread_id) if m['id'] > (row['summarized_upto'] or 0)]
        if len(pending) <= RECENT + 20:
            return
        old = pending[:-RECENT]
        transcript = '\n'.join(f"{m['role']}: {m['text'][:1500]}" for m in old)
        prompt = ('把下面的对话经过压缩成不超过 600 字的要点（问题、已查明的事实与数字、已做的修改、未决事项），'
                  '与已有要点合并去重。\n\n已有要点：\n' + (row['summary'] or '无') + '\n\n新对话：\n' + transcript)
        summary, _, _ = llm.complete(config.models()['quick']['id'], [{'role': 'user', 'content': prompt}], max_tokens=1200)
        store.set_summary(thread_id, summary.strip(), old[-1]['id'])
    except Exception as exc:
        store.log_event('summary_error', {'thread': thread_id, 'error': str(exc)[:500]})


def compact_memory():
    text = store.memory()
    prompt = ('下面是台账助手的长期记忆，已经超出长度。请合并重复、删除过时或被后来结论推翻的条目，'
              f'保留所有仍有效的业务口径、店铺特例和用户偏好，压缩到 {store.MEMORY_LIMIT * 6 // 10} 字以内，'
              '每条一行，以“- ”开头，保留日期。只输出记忆本身。\n\n' + text)
    out, _, _ = llm.complete(config.models()['quick']['id'], [{'role': 'user', 'content': prompt}], max_tokens=4000)
    if out.strip():
        store.write_memory(out.strip() + '\n')
