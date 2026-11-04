"""助手服务：台账 8000 端口把 /api/agent/* 转发到这里。"""
import asyncio
import os
import queue
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import agent, autoclose, config, costs, docs, events, machine, store, tools, webui

_runs = {}
_stops = {}
_tasks = {}
_inboxes = {}
_tasks_lock = threading.Lock()
# 同时最多几轮在查；多出来的排队，避免几个人同时提问把模型并发和台账都压满。
_slots = threading.Semaphore(3)
SSE_HEADERS = {'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
# 群聊里 @同事 的消息只转给人看；@助手 或不带 @ 的才由助手回答。
COLLEAGUE = re.compile(r'^\s*[@＠](?!助手)\S')


@asynccontextmanager
async def lifespan(_app):
    store.conn()
    stop = autoclose.start() if os.environ.get('COPILOT_AUTO_CLOSE', '1') == '1' else None
    machine.start()
    threading.Timer(5, _resume_unanswered).start()
    yield
    if stop:
        stop.set()


app = FastAPI(title='台账助手', lifespan=lifespan)


class Chat(BaseModel):
    thread_id: str | None = None
    text: str = ''
    images: list[dict] = []
    page: dict | None = None
    user: str = ''
    files: list[dict] = []


class Steer(BaseModel):
    run_id: str | None = None
    thread_id: str | None = None
    text: str = ''
    files: list[dict] = []
    user: str = ''


class Stop(BaseModel):
    thread_id: str | None = None
    run_id: str | None = None
    user: str = ''


def _task_view(task):
    return {k: v for k, v in task.items() if not k.startswith('_')}


def _update_task(run_id, **changes):
    with _tasks_lock:
        task = _tasks.get(run_id)
        if not task:
            return
        task.update(changes)
        view = _task_view(task)
    events.publish('task', view)


def _similar(a, b):
    first, second = store._terms(a), store._terms(b)
    if len(first) < 3 or len(second) < 3:
        return False
    return len(first & second) / len(first | second) >= 0.5


def _duplicate(text):
    with _tasks_lock:
        return next((t for t in _tasks.values() if t['status'] in {'queued', 'running'}
                     and t['thread_id'] == store.GROUP and _similar(t['question'], text)), None)


def _prune_tasks():
    cutoff = time.time() - 2 * 3600
    with _tasks_lock:
        for run_id in [r for r, t in _tasks.items() if t['status'] not in {'queued', 'running', 'waiting'}
                       and t['_ended'] < cutoff]:
            _tasks.pop(run_id, None)


def _single(*items):
    def stream():
        yield from items
    return StreamingResponse(stream(), media_type='text/event-stream', headers=SSE_HEADERS)


@app.post('/api/agent/chat')
def chat(body: Chat):
    group = body.thread_id == store.GROUP
    if group and COLLEAGUE.match(body.text):
        thread_id, message_id, _ = agent.post_message(store.GROUP, body.text, body.images, body.page, body.user,
                                                      docs.resolve(body.files))
        return _single(events.sse('thread', {'thread_id': thread_id, 'title': '公共群聊', 'run_id': '', 'message_id': message_id}),
                       events.sse('done', None))
    same = _duplicate(body.text) if group and not body.images and not body.files else None
    if same:
        thread_id, message_id, _ = agent.post_message(store.GROUP, body.text, body.images, body.page, body.user)
        followers = same['followers'] + [body.user or '同事']
        _update_task(same['run_id'], followers=followers)
        return _single(events.sse('thread', {'thread_id': thread_id, 'title': '公共群聊', 'run_id': same['run_id'],
                                             'message_id': message_id}),
                       events.sse('merged', {'run_id': same['run_id'], 'asker': same['user']}),
                       events.sse('done', None))
    outbox = _launch(body)

    def stream():
        while True:
            try:
                item = outbox.get(timeout=15)
            except queue.Empty:
                yield ': keepalive\n\n'
                continue
            if item is None:
                return
            yield item

    return StreamingResponse(stream(), media_type='text/event-stream', headers=SSE_HEADERS)


def _resume_unanswered():
    for message in store.unanswered(time.time() - 3600):
        if message['thread_id'] == store.GROUP and COLLEAGUE.match(message['text']):
            continue
        body = Chat(thread_id=message['thread_id'], text=message['text'], page=message['page'], user=message['user'],
                    files=message['files'])
        store.log_event('turn_resumed', {'message_id': message['id']})
        _launch(body, resume=message['id'])


def _launch(body, resume=None):
    group = body.thread_id == store.GROUP
    if not group:
        previous = _runs.get(body.thread_id or '')
        if previous:
            previous.set()
    _prune_tasks()
    outbox = queue.Queue()
    stop = threading.Event()
    run_id = uuid.uuid4().hex[:10]
    holder = {'thread': body.thread_id}
    _stops[run_id] = stop
    inbox = _inboxes[run_id] = queue.Queue()
    now = time.time()
    with _tasks_lock:
        _tasks[run_id] = {'run_id': run_id, 'thread_id': body.thread_id or '', 'user': body.user,
                          'question': (body.text.strip() or ('（文件）' if body.files else '（截图）'))[:80], 'message_id': None, 'status': 'queued',
                          'line': '排队中', 'started_at': datetime.now().astimezone().isoformat(timespec='seconds'),
                          'ms': 0, 'cost_cny': 0.0, 'followers': [], 'cards': [], '_started': now, '_ended': 0}
    events.publish('task', _task_view(_tasks[run_id]))

    def emit(kind, value):
        if kind == 'thread':
            holder['thread'] = value['thread_id']
            if not group:
                _runs[value['thread_id']] = stop
            _update_task(run_id, thread_id=value['thread_id'], message_id=value.get('message_id'))
        else:
            events.publish('turn', {'thread_id': holder['thread'], 'run_id': run_id, 'user': body.user,
                                    'kind': kind, 'data': value})
            if kind == 'step' and value.get('status') == 'running':
                _update_task(run_id, line=value.get('label') or '处理中')
            elif kind == 'card':
                _update_task(run_id, cards=_tasks[run_id]['cards'] + [value['card_id']])
            elif kind == 'done':
                holder['receipt'] = value
        outbox.put(events.sse(kind, value))

    def work():
        status = 'done'
        try:
            with _slots:
                if stop.is_set():
                    status = 'stopped'
                    return
                _update_task(run_id, status='running', line='在看', _started=time.time())
                agent.run_turn(body.thread_id, body.text, body.images, body.page, body.user, emit, stop, run_id,
                               body.files, inbox, resume)
            if stop.is_set():
                status = 'stopped'
        except Exception as exc:
            status = 'failed'
            store.log_event('turn_error', {'error': str(exc)[:2000]})
            outbox.put(events.sse('error', {'message': '这次处理遇到了问题，请稍后再试。'}))
        finally:
            if _runs.get(holder['thread']) is stop:
                _runs.pop(holder['thread'], None)
            _stops.pop(run_id, None)
            with _tasks_lock:
                _inboxes.pop(run_id, None)
            task = _tasks.get(run_id, {})
            pending = [c for c in task.get('cards', []) if (store.card(c) or {'status': ''})['status'] == 'pending']
            if status == 'done' and pending:
                status = 'waiting'
            receipt = holder.get('receipt')
            _update_task(run_id, status=status, line={'done': '已完成', 'waiting': '等确认', 'stopped': '已停止',
                                                       'failed': '没查完'}[status],
                         ms=int((time.time() - task.get('_started', now)) * 1000),
                         cost_cny=(receipt or {}).get('cost_cny', 0.0), _ended=time.time())
            outbox.put(None)
            if tools._pending_self_restart:
                threading.Timer(2, lambda: os._exit(0)).start()

    threading.Thread(target=work, name='agent-turn', daemon=True).start()
    return outbox


@app.get('/api/agent/events')
async def event_stream(user: str = ''):
    sid, inbox = events.subscribe(user)

    async def stream():
        try:
            with _tasks_lock:
                tasks = [_task_view(t) for t in _tasks.values()]
            yield events.sse('hello', {'users': events.presence(), 'costs': costs.summary(), 'tasks': tasks})
            idle = 0.0
            while True:
                try:
                    item = inbox.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.25)
                    idle += 0.25
                    if idle >= 15:
                        idle = 0.0
                        yield ': keepalive\n\n'
                    continue
                idle = 0.0
                yield item
        finally:
            events.unsubscribe(sid)

    return StreamingResponse(stream(), media_type='text/event-stream', headers=SSE_HEADERS)


def _running_for(body):
    with _tasks_lock:
        if body.run_id and body.run_id in _inboxes:
            return _tasks.get(body.run_id)
        mine = [t for t in _tasks.values() if t['run_id'] in _inboxes and t['thread_id'] == (body.thread_id or '')
                and (body.thread_id != store.GROUP or t['user'] == body.user)]
        return max(mine, key=lambda t: t['_started'], default=None)


@app.post('/api/agent/steer')
def steer(body: Steer):
    task = _running_for(body)
    if not task or task['status'] not in {'queued', 'running'}:
        return {'status': 'finished'}
    prompt = agent.steer(task['thread_id'], body.text, body.files, body.user)
    with _tasks_lock:
        inbox = _inboxes.get(task['run_id'])
    if inbox is None:
        return {'status': 'finished', 'stored': True}
    inbox.put(prompt)
    _update_task(task['run_id'], line='收到补充，接着查')
    return {'status': 'queued', 'run_id': task['run_id']}


@app.post('/api/agent/files')
async def upload_file(request: Request, name: str = 'file'):
    data = await request.body()
    if not data:
        raise HTTPException(400, '空文件')
    try:
        info = docs.save(name, data)
    except ValueError as exc:
        raise HTTPException(413, str(exc)) from exc
    return {k: info[k] for k in ('file_id', 'name', 'size', 'kind', 'url')}


@app.post('/api/agent/stop')
def stop_run(body: Stop):
    flag = _stops.get(body.run_id or '') or _runs.get(body.thread_id or '')
    if flag:
        flag.set()
        if body.run_id:
            _update_task(body.run_id, line=f'{body.user}停止了' if body.user else '正在停止')
    return {'stopped': bool(flag)}


@app.get('/api/agent/tasks')
def list_tasks():
    _prune_tasks()
    with _tasks_lock:
        tasks = sorted((_task_view(t) for t in _tasks.values()), key=lambda t: t['started_at'], reverse=True)
    return {'tasks': tasks[:30]}


def _settle_card(card_id):
    with _tasks_lock:
        task = next((t for t in _tasks.values() if card_id in t['cards'] and t['status'] == 'waiting'), None)
    if task and all((store.card(c) or {'status': ''})['status'] != 'pending' for c in task['cards']):
        _update_task(task['run_id'], status='done', line='已完成')


@app.get('/api/agent/threads')
def list_threads():
    return {'threads': store.threads()}


@app.get('/api/agent/threads/{thread_id}')
def get_thread(thread_id: str):
    if thread_id == store.GROUP:
        store.ensure_thread(store.GROUP, '', '') if not store.thread(store.GROUP) else None
    row = store.thread(thread_id)
    if not row:
        raise HTTPException(404, '话题不存在')
    messages = [m for m in store.messages(thread_id, last=300) if m['role'] in {'user', 'assistant'}]
    return {'id': row['id'], 'title': row['title'], 'messages': messages, 'running': thread_id in _runs}


@app.post('/api/agent/cards/{card_id}/confirm')
def confirm_card(card_id: str, body: dict | None = None):
    outcome = tools.execute_card(card_id, (body or {}).get('user', ''))
    _settle_card(card_id)
    return outcome


@app.post('/api/agent/cards/{card_id}/reject')
def reject_card(card_id: str, body: dict | None = None):
    row = store.card(card_id)
    if not row or row['status'] != 'pending':
        return {'status': row['status'] if row else 'missing', 'message': '这张卡片已经处理过了'}
    by = (body or {}).get('user', '')
    message = '已取消，未做修改' + (f'（{by}）' if by else '')
    store.decide_card(card_id, 'rejected', message, by)
    store.add_message(row['thread_id'], 'system', f'{by or "用户"}没有采纳「{row["title"]}」，未做修改。')
    events.publish('card_update', {'card_id': card_id, 'status': 'rejected', 'message': message, 'by': by})
    _settle_card(card_id)
    return {'status': 'rejected', 'message': message}


@app.get('/api/agent/costs')
def cost_summary():
    return costs.summary()


@app.get('/api/agent/ui/history')
def ui_history():
    return webui.history()


@app.post('/api/agent/ui/undo')
def ui_undo(body: dict | None = None):
    return webui.undo((body or {}).get('user', ''))


@app.get('/api/agent/status')
def status():
    summary = costs.summary()
    return {'online': True, 'month_cost_cny': round(summary['month_cny'], 2), 'month_budget_cny': config.MONTH_BUDGET_CNY,
            'users': events.presence()}


@app.get('/api/agent/uploads/{folder}/{name}')
def upload(folder: str, name: str):
    path = (config.ROOT / 'uploads' / folder / name).resolve()
    if config.ROOT.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


def _push_costs():
    last = None
    while True:
        threading.Event().wait(60)
        try:
            summary = costs.summary(force=True)
        except Exception:
            continue
        if summary['month_cny'] != last:
            last = summary['month_cny']
            events.publish('costs', summary)


def main():
    import uvicorn
    threading.Thread(target=_push_costs, name='cost-push', daemon=True).start()
    uvicorn.run(app, host='127.0.0.1', port=config.PORT, log_level='warning')


if __name__ == '__main__':
    main()
