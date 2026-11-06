"""问答和资料仍由现有 Worker 与租约办事。这里只把过程和结果说进房间。

没有评测通过的档案时，不换一个别的模型顶上，也不在这里打开模型。
"""
import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, events, store

_LEDGER_ROOT = Path(__file__).resolve().parents[2] / 'ledger'
_WORKER_ROOT = Path(__file__).resolve().parents[2] / 'agent-worker'
_MODEL_ROOT = Path(__file__).resolve().parents[2] / 'models' / 'cn-ecommerce'
_UNANSWERED = '暂时无法回答'
_WAIT = '我正在看这期账单，看完写在这间房里。'


def qualified(skill):
    """技能含 skill、评测通过、每次答对成本最低的一份。没有就返回空。"""
    try:
        return _best(skill) is not None
    except Exception:
        return False


def _config_path():
    for key in ('LEDGER_AGENT_CONFIG', 'COPILOT_WORKER_CONFIG'):
        raw = os.environ.get(key, '').strip()
        if raw and Path(raw).is_file():
            return Path(raw)
    return None


def _best(skill):
    path = _config_path()
    if path is None:
        return None
    raw = json.loads(path.read_text(encoding='utf-8'))
    if raw.get('model_enabled', True) is False or os.environ.get('LEDGER_AGENT_MODEL_ENABLED') == '0':
        return None
    if str(_WORKER_ROOT) not in sys.path:
        sys.path.insert(0, str(_WORKER_ROOT))
    from ledger_agent.runtime import Config, ModelSpec
    base = raw.get('base_url') or os.environ.get('LEDGER_LLM_BASE_URL') or ''
    config_obj = Config(base_url=base, api_key='unused', models=tuple(ModelSpec(**row) for row in raw.get('models', [])),
                        evaluation=raw.get('skip_model_qualification') is True)
    if skill == 'qa':
        from ledger_agent.qa import Answerer
        return Answerer(config_obj, None).select()
    return config_obj.select((skill,))


def _workspace_root():
    raw = os.environ.get('LEDGER_HOME', '').strip()
    root = Path(raw).expanduser() if raw else Path.home() / '.ledger'
    if not (root / 'workspace.db').is_file():
        return None
    return root


def _open_books():
    root = _workspace_root()
    if root is None or not _MODEL_ROOT.is_dir():
        return None
    if str(_LEDGER_ROOT) not in sys.path:
        sys.path.insert(0, str(_LEDGER_ROOT))
    from ledger.model.repository import ModelRepository
    from ledger.workspace import Workspace
    ws = Workspace(root)
    model = ModelRepository(_MODEL_ROOT).get().model
    return ws, model


def _view(ws, question_id):
    from ledger.agent_questions import view
    row = ws.conn.execute('SELECT * FROM agent_question WHERE id=?', (question_id,)).fetchone()
    if not row:
        return None
    return view(ws, dict(row))


def _enqueue(store_id, period, question, user, subject):
    opened = _open_books()
    if opened is None:
        return None
    ws, model = opened
    from ledger.agent_questions import QuestionError, ask
    actor = {'kind': 'human', 'authenticated': True, 'id': (user or '同事')[:80]}
    payload = {'node_id': subject.get('node_id'), 'row': subject.get('row') or {}} if subject and subject.get('node_id') else None
    try:
        saved = ask(ws, model, request_id=uuid.uuid4().hex, store_id=store_id, period=period,
                    question=question[:500], actor=actor, subject=payload)
    except QuestionError as exc:
        return {'error': str(exc)}
    except PermissionError:
        return {'error': _UNANSWERED}
    return {'ws': ws, 'question_id': saved['question_id']}


def _spoken(item):
    if not item:
        return ''
    if item.get('state') == 'answered':
        return item.get('answer') or _UNANSWERED
    if item.get('state') == 'failed':
        if '模型' in (item.get('failure') or '') or '未配置' in (item.get('failure') or ''):
            return _UNANSWERED
        return item.get('failure') or _UNANSWERED
    return ''


def answer_question(store_id, period, question, user, subject, stop, inbox, step):
    """走现有提问队列，等 Worker 用租约答完。返回 (正文, question_id)。"""
    if not qualified('qa'):
        return _UNANSWERED, ''
    if not store_id or not period:
        return '要看哪一家店的哪个月？打开那家店的店月页再问我。', ''
    step('正在看这期账单')
    queued = _enqueue(store_id, period, question, user, subject)
    if queued is None:
        return _UNANSWERED, ''
    if queued.get('error'):
        return queued['error'], ''
    question_id = queued['question_id']
    ws = queued['ws']
    extras = _drain(inbox)
    deadline = time.time() + 90
    while time.time() < deadline:
        if stop is not None and stop.is_set():
            return '（已停止）', question_id
        item = _view(ws, question_id)
        spoken = _spoken(item)
        if spoken:
            more = _follow(ws, store_id, period, user, extras + _drain(inbox), stop, deadline)
            text = spoken if not more else spoken + '\n\n' + more
            return text, question_id
        extras.extend(_drain(inbox))
        if not _sleep(stop, 2):
            return '（已停止）', question_id
    _watch(ws, store_id, period, question_id, user, extras)
    return _WAIT, question_id


def _follow(ws, store_id, period, user, extras, stop, deadline):
    text = '\n'.join(part.strip() for part in extras if part and part.strip())
    if not text or time.time() >= deadline:
        return ''
    queued = _enqueue(store_id, period, text[:500], user, None)
    if not queued or queued.get('error'):
        return ''
    while time.time() < deadline:
        if stop is not None and stop.is_set():
            return ''
        spoken = _spoken(_view(ws, queued['question_id']))
        if spoken:
            return spoken
        if not _sleep(stop, 2):
            return ''
    return ''


def _watch(ws, store_id, period, question_id, user, extras):
    def wait():
        for _ in range(40):
            item = _view(ws, question_id)
            spoken = _spoken(item)
            if spoken:
                more = ''
                text = '\n'.join(part.strip() for part in extras if part and part.strip())
                if text:
                    queued = _enqueue(store_id, period, text[:500], user, None)
                    if queued and not queued.get('error'):
                        for _ in range(30):
                            more = _spoken(_view(ws, queued['question_id']))
                            if more:
                                break
                            time.sleep(3)
                _post(store_id, period, spoken if not more else spoken + '\n\n' + more, question_id)
                return
            time.sleep(3)
    import threading
    threading.Thread(target=wait, name='room-qa', daemon=True).start()


def _post(store_id, period, text, question_id):
    thread_id = f'store:{store_id}:{period}'
    if not store.thread(thread_id):
        store.ensure_thread(thread_id, f'{store_id} {period}', '')
    receipt = {'bot': '问答', 'model': '', 'tokens': 0, 'cost_cny': 0, 'reply_to': None, 'question_id': question_id}
    reply_id = store.add_message(thread_id, 'assistant', text, receipt=receipt, bot='问答')
    events.publish('message', {'thread_id': thread_id, 'message': store.messages(thread_id, reply_id - 1)[0]})


def describe_materials(store_id, period, kind, note, stop, inbox):
    """资料不在这里打开模型。缺口用账本已经算好的清单说。"""
    if stop is not None and stop.is_set():
        return '（已停止）'
    prefix = '新的订单到了。' if kind == 'orders' else '新的资料到了。' if kind == 'materials' else ''
    if note:
        prefix += note.strip() + '。'
    extra = _drain(inbox)
    if extra:
        prefix += '\n' + '\n'.join(extra)
    body = _gaps(store_id, period)
    late = _drain(inbox)
    if late:
        body += '\n' + '\n'.join(late)
    if not qualified('evidence'):
        held = '自动对原始凭据这会儿停着。'
        return (prefix + held + body).strip()
    return (prefix + body).strip() or '这批资料我这会儿没看出新的缺口。'


def _gaps(store_id, period):
    query = {'period': period or ''}
    if store_id:
        query['store_id'] = store_id
    url = config.LEDGER_URL + '/api/gaps?' + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except (OSError, urllib.error.URLError, ValueError, TimeoutError):
        return '账上的缺口清单这会儿没取到。'
    cells = payload.get('cells') or []
    if store_id:
        cells = [cell for cell in cells if cell.get('store_id') == store_id]
    if period:
        cells = [cell for cell in cells if cell.get('period') == period]
    lines = []
    for cell in cells[:6]:
        titles = [gap.get('title') for gap in (cell.get('gaps') or []) if gap.get('title')][:6]
        if not titles:
            continue
        who = cell.get('store') or cell.get('store_id') or ''
        when = cell.get('period') or ''
        lines.append(f'{who} {when}：' + '；'.join(titles))
    if not lines:
        return '目前没有记下新的缺口。'
    return '还缺这些：\n' + '\n'.join(lines)


def _drain(inbox):
    import queue
    found = []
    while inbox is not None:
        try:
            found.append(inbox.get_nowait())
        except queue.Empty:
            break
    return found


def _sleep(stop, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if stop is not None and stop.is_set():
            return False
        time.sleep(0.2)
    return True
