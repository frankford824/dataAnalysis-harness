"""一轮问答：识别截图 → 选模型 → 工具循环 → 记录。"""
import base64
import json
import re
import threading
import time
import uuid
from datetime import datetime

from . import config, events, llm, store, tools

MAX_STEPS = 16
RECENT = 10

SYSTEM = '''你是「台账助手」，运行在公司台账系统的生产机（Windows）上，拥有这台机器的完整权限。
使用者是 2-3 位财务/运营同事，他们在左侧看经营分析和明细，发现问题时用文字和截图问你。你的任务是直接查清、解释并解决问题，让他们不用再手工导表、写公式核对。

回答规则：
- 用业务语言回答，先给结论，再给关键依据（具体店铺、月份、金额），最后说下一步。简洁。
- 绝对不要向用户展示代码、SQL、文件路径、命令、日志、接口名或技术实现细节；不要说“我修改了某某文件/重启了服务”。程序问题修好后只说“已修复，刷新页面即可”这类结果。
- 每次调用工具都要填写 purpose：用用户能懂的话说明这一步在做什么（例如“查看 8 月这家店的退款明细”）。
- 金额以台账引擎的计算结果为准，引用工具返回的数字，不要自己估算最终金额。
- 不要自己编算式。列出构成时只列工具返回的项目；需要加减核对时用 python 工具算，算不平就如实说明还有哪些项目没列出。
- purpose 只写业务动作（如“查看 9 月各店毛利”），不要出现“接口”“文件”“字段”“编码”“数据库”等技术词。

做事规则：
- 只读查询、排查原因、修复程序故障、运维处理：直接做。
- 会影响账务数据或金额的修改（规则、映射、费用、确认决定、结账等）：必须用 propose_change 生成确认卡片，等用户点确认，不能直接改。需要预览凭据的，先用 ledger_preview 生成预览，再把确认步骤放进卡片。
- 已结账的历史不能改。问题落在已结账月份时，说明情况和影响，建议在未结账月份处理。
- 修改程序后调用 restart_ledger 使修改生效（会自动检查，异常自动恢复）。
- 用户对左侧界面提出布局/文案/样式/交互修改时直接动手：改 D:\\ledger\\app\\ledger\\web\\src 下的 .vue/.js/.css，
  再调用 build_web 推送给所有人（用户可一键撤销，不需要确认卡片）。改完用一句话告诉用户“已改好，页面会自动更新”。
  只改界面不涉及数据计算；数据口径的修改仍走 propose_change。
- 开始排查前先用 search_memory 查以往类似问题；查清并处理一个问题后用 record_correction 记录；学到稳定的业务口径或用户偏好时用 remember 记一句。
- 用户只是闲聊或问简单问题时不必调用工具。
- 先弄清用户要什么：问题不明确时先问一句，不要猜着深挖。工具调用超过 8 次仍没有结论，先把阶段性发现告诉用户，问是否继续。
- 用户反馈的多是数据问题；只有用户明确说页面报错、功能坏了，才去查程序实现。

环境：
- 台账服务：本机 8000 端口，ledger_get 调只读接口，api_index 可查全部接口。按问题选最直接的接口：
  · 店铺清单、各店有哪些账期及状态：/api/navigation（很快）
  · 全公司各月收入/毛利趋势：/api/trend
  · 某月全公司经营汇总：/api/operations/month?period=YYYY-MM
  · 某店某月的核算结果：/api/stores/{store_id}/periods/{period}；金额下钻：/api/runs/{run_id}/drill/{node_id}
  · 某月各店结账状态、卡在哪里：/api/automation/months/{period}；缺资料/缺口：/api/gaps?period=YYYY-MM
  · 待人处理事项：/api/automation/inbox；搜索订单/商品：/api/search?q=
  · 不要用 /api/overview（全量矩阵，极慢）。
- 数据库：workspace（台账主库，100GB 以上，查询必须走索引、按店铺/账期过滤、带 LIMIT；表 period=店月状态，run=核算批次，核算结果为压缩快照，金额请走接口）、commission（提成库）。
- 目录：D:\\ledger\\app（台账程序）、D:\\ledger\\home（台账数据）、D:\\ledger\\logs（服务日志）、D:\\ledger\\agent（助手自身程序）。
- 自动结账：每天凌晨 2 点对最近三个已过去的月份自动发起结账，检查全部通过的店铺自动结账；auto_close_status 看结果，用户要求立即结账时用 run_auto_close。结账是确定的业务动作且用户已授权自动执行，不需要确认卡片。
- 效率：接口很慢时改用 sql_query；每次只取需要的列和行；同一问题不要反复查同样的数据。
今天是 {today}。'''

ENGINEER_WORDS = re.compile(r'报错|错误提示|打不开|白屏|卡死|很卡|太慢|加载不出|500|bug|程序|页面.*(坏|错|空)|按钮.*(没反应|不能)|改代码|功能')
INVESTIGATE_WORDS = re.compile(r'不对|不一致|错|为什么|怎么回事|核对|差|少了|多了|异常|查一下|排查|对不上|偏高|偏低|原因|改|调整|修正')


def route(text, has_images):
    if ENGINEER_WORDS.search(text):
        return 'engineer'
    if has_images or INVESTIGATE_WORDS.search(text) or len(text) > 120:
        return 'investigate'
    return 'quick'


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


def _context(thread_id, text, exclude):
    memory = store.memory()
    related = store.search_corrections(text, 5)
    row = store.thread(thread_id)
    history = [m for m in store.messages(thread_id) if m['id'] > (row['summarized_upto'] or 0) and m['id'] != exclude]
    group = thread_id == store.GROUP
    system = SYSTEM.replace('{today}', datetime.now().strftime('%Y-%m-%d'))
    index = tools.knowledge_index()
    if index:
        system += '\n\n【系统地图目录】（详细内容用 read_knowledge 按章节读取；先查这里再动手，能少走很多步）\n' + index
    if group:
        system += ('\n\n【公共群聊】这是 2-3 位同事共用的群聊，消息前的【名字】是发言人。回答时可以称呼对方；'
                   '别人刚才说过的事实和结论直接沿用，不要重复查。')
    if memory:
        system += '\n\n【长期记忆】\n' + memory
    if related:
        system += '\n\n【可能相关的以往纠错】\n' + json.dumps(related, ensure_ascii=False)
    if row['summary']:
        system += '\n\n【本话题早前的经过】\n' + row['summary']
    messages = [{'role': 'system', 'content': system}]
    for m in history[-RECENT:]:
        if m['role'] == 'system':
            messages.append({'role': 'user', 'content': '（系统通知）' + m['text']})
        elif m['text']:
            prefix = f"【{m['user'] or '同事'}】" if group and m['role'] == 'user' else ''
            messages.append({'role': m['role'], 'content': prefix + m['text']})
    return messages


def _merge_alternating(messages):
    merged = []
    for m in messages:
        if merged and m['role'] == merged[-1]['role'] == 'user' and isinstance(m['content'], str):
            merged[-1] = {'role': 'user', 'content': merged[-1]['content'] + '\n' + m['content']}
        else:
            merged.append(m)
    return merged


def post_message(thread_id, text, images, page, user):
    thread_id, _ = store.ensure_thread(thread_id, text.strip().splitlines()[0] if text.strip() else '截图问题', user)
    saved = _save_images(images or [])
    message_id = store.add_message(thread_id, 'user', text, images=[u for u, _ in saved], page=page, user=user)
    events.publish('message', {'thread_id': thread_id, 'message': store.messages(thread_id, message_id - 1)[0]})
    return thread_id, message_id, saved


def run_turn(thread_id, text, images, page, user, emit, stop, run_id=''):
    thread_id, message_id, saved = post_message(thread_id, text, images, page, user)
    emit('thread', {'thread_id': thread_id, 'title': store.thread(thread_id)['title'], 'run_id': run_id,
                    'message_id': message_id})
    started = time.time()
    tokens = 0
    cost = 0.0
    steps, cards, answer = [], [], []

    def step_emit(kind, value):
        if kind == 'step':
            existing = next((s for s in steps if s['id'] == value['id']), None)
            if existing:
                existing.update(value)
            else:
                steps.append(dict(value))
        emit(kind, value)

    user_text = text
    if saved:
        transcript, t, c = _transcribe(saved, text, step_emit)
        tokens += t
        cost += c
        user_text += '\n\n【截图内容（已识别）】\n' + transcript
    if saved and not text.strip():
        user_text += '\n\n（用户只发了截图，没有写问题。先用一两句话说清截图里是什么、可能值得注意的地方，然后问用户想查哪一点；不要直接深入排查。）'
    user_text = (_page_text(page) + '\n\n' + user_text).strip()

    role = route(text, bool(saved))
    specs = config.models()
    model_id = specs[role]['id']
    messages = _context(thread_id, text, message_id)
    if thread_id == store.GROUP:
        user_text = f'【{user or "同事"}】' + user_text
    messages.append({'role': 'user', 'content': user_text})
    messages = _merge_alternating(messages)
    used = [model_id]
    error = None
    try:
        for index in range(MAX_STEPS):
            if stop.is_set():
                break
            if role == 'quick' and index >= 4:
                role, model_id = 'investigate', specs['investigate']['id']
                used.append(model_id)
            _shrink_old_results(messages)
            text_parts, end = [], None
            try:
                for kind, value in llm.stream(model_id, messages, tools.SPECS, stop=stop):
                    if kind == 'text':
                        text_parts.append(value)
                        answer.append(value)
                        emit('delta', {'text': value})
                    else:
                        end = value
            except llm.BudgetExhausted:
                raise
            except Exception as exc:
                fallback = config.FALLBACK.get(role)
                if fallback and fallback != model_id:
                    model_id = fallback
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
                break
            if text_parts:
                answer.append('\n\n')
                emit('delta', {'text': '\n\n'})
            for call in assistant['tool_calls']:
                result = _call(call, thread_id, step_emit, cards, emit, role)
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': result})
                if call['function']['name'] in {'edit_file', 'write_file'} and role != 'engineer':
                    role, model_id = 'engineer', specs['engineer']['id']
                    used.append(model_id)
        else:
            note = '\n\n这个问题步骤较多，我先停在这里。你可以回复“继续”让我接着查。'
            answer.append(note)
            emit('delta', {'text': note})
    except llm.BudgetExhausted:
        error = '本月助手的模型费用已到上限，下月自动恢复；如需提高上限请联系管理员。'
    except Exception as exc:
        error = '这次处理遇到了问题，请稍后再试或换个说法。'
        store.log_event('turn_error', {'thread': thread_id, 'error': str(exc)[:2000]})
    if error:
        emit('error', {'message': error})
        answer.append(('\n\n' if answer else '') + error)
    elif stop.is_set():
        answer.append(('\n\n' if answer else '') + '（已停止）')
    receipt = {'model': ' → '.join(dict.fromkeys(used)), 'tokens': tokens, 'cost_cny': round(cost, 4),
               'ms': int((time.time() - started) * 1000)}
    reply_id = store.add_message(thread_id, 'assistant', ''.join(answer), steps=steps, cards=cards, receipt=receipt)
    events.publish('message', {'thread_id': thread_id, 'run_id': run_id,
                               'message': store.messages(thread_id, reply_id - 1)[0]})
    emit('done', receipt)
    from . import costs
    events.publish('costs', costs.summary(force=True))
    threading.Thread(target=_summarize, args=(thread_id,), daemon=True).start()
    return thread_id


def _shrink_old_results(messages, keep=4, size=1500):
    results = [m for m in messages if m['role'] == 'tool']
    for m in results[:-keep]:
        if len(m['content']) > size:
            m['content'] = m['content'][:size] + '\n…（较早的结果已压缩）'


def _call(call, thread_id, step_emit, cards, emit, role):
    name = call['function']['name']
    try:
        args = json.loads(call['function']['arguments'] or '{}')
    except ValueError:
        args = {}
    step = {'id': uuid.uuid4().hex[:8], 'label': (args.get('purpose') or tools.DEFAULT_LABEL.get(name, '处理中'))[:60],
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
        if len(pending) <= RECENT + 4:
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
