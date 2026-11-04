"""助手服务：台账 8000 端口把 /api/agent/* 转发到这里。"""
import asyncio
import os
import queue
import re
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import agent, autoclose, config, costs, events, store, tools, webui

_runs = {}
SSE_HEADERS = {'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
# 群聊里 @同事 的消息只转给人看；@助手 或不带 @ 的才由助手回答。
COLLEAGUE = re.compile(r'^\s*[@＠](?!助手)\S')


@asynccontextmanager
async def lifespan(_app):
    store.conn()
    stop = autoclose.start() if os.environ.get('COPILOT_AUTO_CLOSE', '1') == '1' else None
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


class Stop(BaseModel):
    thread_id: str | None = None


def _single(*items):
    def stream():
        yield from items
    return StreamingResponse(stream(), media_type='text/event-stream', headers=SSE_HEADERS)


@app.post('/api/agent/chat')
def chat(body: Chat):
    if body.thread_id == store.GROUP and COLLEAGUE.match(body.text):
        thread_id, message_id, _ = agent.post_message(store.GROUP, body.text, body.images, body.page, body.user)
        return _single(events.sse('thread', {'thread_id': thread_id, 'title': '公共群聊', 'run_id': '', 'message_id': message_id}),
                       events.sse('done', None))
    previous = _runs.get(body.thread_id or '')
    if previous:
        previous.set()
    outbox = queue.Queue()
    stop = threading.Event()
    run_id = uuid.uuid4().hex[:10]
    holder = {'thread': body.thread_id}

    def emit(kind, value):
        if kind == 'thread':
            holder['thread'] = value['thread_id']
            _runs[value['thread_id']] = stop
        else:
            events.publish('turn', {'thread_id': holder['thread'], 'run_id': run_id, 'user': body.user,
                                    'kind': kind, 'data': value})
        outbox.put(events.sse(kind, value))

    def work():
        try:
            agent.run_turn(body.thread_id, body.text, body.images, body.page, body.user, emit, stop, run_id)
        except Exception as exc:
            store.log_event('turn_error', {'error': str(exc)[:2000]})
            outbox.put(events.sse('error', {'message': '这次处理遇到了问题，请稍后再试。'}))
        finally:
            if _runs.get(holder['thread']) is stop:
                _runs.pop(holder['thread'], None)
            outbox.put(None)
            if tools._pending_self_restart:
                threading.Timer(2, lambda: os._exit(0)).start()

    threading.Thread(target=work, name='agent-turn', daemon=True).start()

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


@app.get('/api/agent/events')
async def event_stream(user: str = ''):
    sid, inbox = events.subscribe(user)

    async def stream():
        try:
            yield events.sse('hello', {'users': events.presence(), 'costs': costs.summary()})
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


@app.post('/api/agent/stop')
def stop_run(body: Stop):
    flag = _runs.get(body.thread_id or '')
    if flag:
        flag.set()
    return {'stopped': bool(flag)}


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
    return tools.execute_card(card_id, (body or {}).get('user', ''))


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
