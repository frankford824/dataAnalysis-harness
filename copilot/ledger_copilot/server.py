"""助手服务：台账 8000 端口把 /api/agent/* 转发到这里。"""
import json
import os
import queue
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import agent, autoclose, config, store, tools

_runs = {}


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


def _sse(kind, value):
    return f'event: {kind}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n'


@app.post('/api/agent/chat')
def chat(body: Chat):
    previous = _runs.get(body.thread_id or '')
    if previous:
        previous.set()
    events = queue.Queue()
    stop = threading.Event()
    holder = {'thread': body.thread_id}

    def emit(kind, value):
        if kind == 'thread':
            holder['thread'] = value['thread_id']
            _runs[value['thread_id']] = stop
        events.put(_sse(kind, value))

    def work():
        try:
            agent.run_turn(body.thread_id, body.text, body.images, body.page, body.user, emit, stop)
        except Exception as exc:
            store.log_event('turn_error', {'error': str(exc)[:2000]})
            events.put(_sse('error', {'message': '这次处理遇到了问题，请稍后再试。'}))
        finally:
            if _runs.get(holder['thread']) is stop:
                _runs.pop(holder['thread'], None)
            events.put(None)
            if tools._pending_self_restart:
                threading.Timer(2, lambda: os._exit(0)).start()

    threading.Thread(target=work, name='agent-turn', daemon=True).start()

    def stream():
        while True:
            try:
                item = events.get(timeout=15)
            except queue.Empty:
                yield ': keepalive\n\n'
                continue
            if item is None:
                return
            yield item

    return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


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
    row = store.thread(thread_id)
    if not row:
        raise HTTPException(404, '话题不存在')
    messages = [m for m in store.messages(thread_id) if m['role'] in {'user', 'assistant'}]
    return {'id': row['id'], 'title': row['title'], 'messages': messages}


@app.post('/api/agent/cards/{card_id}/confirm')
def confirm_card(card_id: str, body: dict | None = None):
    return tools.execute_card(card_id, (body or {}).get('user', ''))


@app.post('/api/agent/cards/{card_id}/reject')
def reject_card(card_id: str, body: dict | None = None):
    row = store.card(card_id)
    if not row or row['status'] != 'pending':
        return {'status': row['status'] if row else 'missing', 'message': '这张卡片已经处理过了'}
    store.decide_card(card_id, 'rejected', '已取消，未做修改', (body or {}).get('user', ''))
    store.add_message(row['thread_id'], 'system', f'用户没有采纳「{row["title"]}」，未做修改。')
    return {'status': 'rejected', 'message': '已取消，未做修改'}


@app.get('/api/agent/status')
def status():
    return {'online': True, 'month_cost_cny': round(store.month_cost(), 2), 'month_budget_cny': config.MONTH_BUDGET_CNY}


@app.get('/api/agent/uploads/{folder}/{name}')
def upload(folder: str, name: str):
    path = (config.ROOT / 'uploads' / folder / name).resolve()
    if config.ROOT.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


def main():
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=config.PORT, log_level='warning')


if __name__ == '__main__':
    main()
