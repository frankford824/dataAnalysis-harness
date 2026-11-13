"""局域网入口：台账再忙，页面也立即有数。

台账进程在后台重算时会吃满 CPU 和磁盘，连最轻的接口都要排队几十秒。这里是独立
进程，监听局域网地址的 8000 端口；台账本身只听 127.0.0.1:8000，本机脚本、Worker、
助手照旧直连。页面、静态资源和慢的只读接口由这里直接回答：先给上次的结果，过期
后在后台单独去台账取一份。改数据的请求原样转给台账；写成功后所有缓存标为待更新，
刚改过数据的那台电脑下一次读取会等台账算出新结果，其他人照常先看旧数再刷新。
手动刷新带 X-Ledger-Fresh: 1，直接等台账。缓存落在磁盘上，两边谁重启都不会冷。
"""
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

LEDGER_PORT = int(os.environ.get('LEDGER_FRONT_UPSTREAM_PORT', '8000'))
COPILOT_PORT = int(os.environ.get('COPILOT_PORT', '8020'))
STATIC = Path(__file__).resolve().parent / 'static'
DB = Path(os.environ.get('LEDGER_HOME', r'D:\ledger\home')) / 'front-cache.db'
HOP = {'connection', 'keep-alive', 'transfer-encoding', 'content-length', 'host', 'te', 'upgrade',
       'proxy-connection', 'if-none-match', 'if-modified-since', 'expect'}
FRESH = 'x-ledger-fresh'
LIMIT = 1000
WRITER_WAIT = 120

CACHED = [(re.compile(pattern), ttl) for pattern, ttl in (
    (r'/api/recompute/progress', 3),
    (r'/api/automation/rule-contracts', 300),
    (r'/api/automation/months/[^/]+', 90),
    (r'/api/automation/inbox', 90),
    (r'/api/commission-v2/(status|people)', 90),
    (r'/api/(overview|navigation|trend|stores|order-feed/status|index/storage)', 90),
    (r'/api/operations/month', 90),
    (r'/api/operations/sentinel/\w+', 90),
    (r'/api/stores/[^/]+', 90),
    (r'/api/stores/[^/]+/(pricing-status|periods/[^/]+)', 90),
)]

# 店铺账期页台账本身就不分人缓存，这里也只存一份；每天凌晨把所有店最近几个月的提前算好。
SHARED = re.compile(r'/api/stores/[^/]+/periods/[^/]+')
PREWARM_HOUR = 4
PREWARM_MONTHS = 5

NOT_WRITES = re.compile(r'/api/(automation/telemetry|.*preview.*|search.*|commission-v2/export/.*)')

_entries = OrderedDict()
_flights = {}
_writers = {}
_refresh_slots = asyncio.Semaphore(3)


def _ttl(path):
    return next((ttl for pattern, ttl in CACHED if pattern.fullmatch(path)), None)


def _db():
    db = sqlite3.connect(DB, timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS entry(key TEXT PRIMARY KEY, target TEXT, cookie TEXT, headers TEXT, '
               'body BLOB, at REAL)')
    return db


def _load():
    try:
        db = _db()
        rows = db.execute('SELECT key, target, cookie, headers, body, at FROM entry ORDER BY at DESC LIMIT ?',
                          (LIMIT,)).fetchall()
        db.close()
    except sqlite3.Error:
        return
    for key, target, cookie, headers, body, at in reversed(rows):
        _entries[key] = {'target': target, 'cookie': cookie, 'headers': json.loads(headers), 'body': body,
                         'fetched': 0.0, 'saved_at': at, 'dirty': False, 'used': 0.0}


def _persist(key, entry):
    try:
        db = _db()
        db.execute('INSERT OR REPLACE INTO entry VALUES(?,?,?,?,?,?)',
                   (key, entry['target'], entry['cookie'], json.dumps(entry['headers']), entry['body'], time.time()))
        db.commit()
        db.close()
    except sqlite3.Error:
        pass


async def _open(port, method, target, headers, body_chunks, host=None):
    reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 5)
    # 台账拒绝 Origin 与 Host 不一致的写入，所以转发时保留浏览器看到的 Host。
    head = [f'{method} {target} HTTP/1.0', f'Host: {host or f"127.0.0.1:{port}"}']
    head += [f'{k}: {v}' for k, v in headers if k.lower() not in HOP]
    if body_chunks is None:
        head.append('Content-Length: 0')
        writer.write(('\r\n'.join(head) + '\r\n\r\n').encode('latin-1'))
    else:
        body = b''.join([chunk async for chunk in body_chunks])
        head.append(f'Content-Length: {len(body)}')
        writer.write(('\r\n'.join(head) + '\r\n\r\n').encode('latin-1') + body)
    await writer.drain()
    status = int((await reader.readline()).split()[1])
    while 100 <= status < 200:
        while (await reader.readline()) not in (b'\r\n', b'\n', b''):
            pass
        status = int((await reader.readline()).split()[1])
    response_headers, chunked = [], False
    while True:
        line = await reader.readline()
        if line in (b'\r\n', b'\n', b''):
            break
        name, _, value = line.decode('latin-1').partition(':')
        name, value = name.strip(), value.strip()
        if name.lower() == 'transfer-encoding' and 'chunked' in value.lower():
            chunked = True
        if name.lower() not in HOP:
            response_headers.append((name, value))
    return status, response_headers, _chunks(reader, chunked), writer


async def _chunks(reader, chunked):
    if not chunked:
        while chunk := await reader.read(65536):
            yield chunk
        return
    while True:
        size = int((await reader.readline()).split(b';')[0].strip() or b'0', 16)
        if size == 0:
            return
        yield await reader.readexactly(size)
        await reader.readline()


def _response(status, headers, body):
    response = Response(body, status_code=status)
    response.raw_headers = [(k.lower().encode('latin-1'), v.encode('latin-1')) for k, v in headers] + \
        [(b'content-length', str(len(body)).encode())]
    return response


async def _fetch(key, target, cookie, fresh):
    """单独去台账取一份完整结果；同一个地址同时只取一次。"""
    flight = _flights.get(key)
    if flight:
        return await asyncio.shield(flight)

    async def run():
        headers = [('X-Ledger-Refresh', '1')] + ([('Cookie', cookie)] if cookie else [])
        if fresh:
            headers.append(('X-Ledger-Fresh', '1'))
        status, response_headers, reader, writer = await _open(LEDGER_PORT, 'GET', target, headers, None)
        try:
            body = b''.join([chunk async for chunk in reader])
        finally:
            writer.close()
        if status == 200:
            entry = {'target': target, 'cookie': cookie, 'headers': response_headers, 'body': body,
                     'fetched': time.monotonic(), 'saved_at': time.time(), 'dirty': False,
                     'used': _entries.get(key, {}).get('used', time.monotonic())}
            _entries[key] = entry
            _entries.move_to_end(key)
            while len(_entries) > LIMIT:
                _entries.popitem(last=False)
            await asyncio.to_thread(_persist, key, entry)
        return status, response_headers, body

    task = asyncio.ensure_future(run())
    _flights[key] = task
    task.add_done_callback(lambda _: _flights.pop(key, None))
    return await asyncio.shield(task)


def _background(key, target, cookie):
    if key in _flights:
        return

    async def run():
        async with _refresh_slots:
            try:
                await _fetch(key, target, cookie, False)
            except Exception:
                pass

    asyncio.ensure_future(run())


def _client(request):
    return request.client.host if request.client else ''


def _cache_key(target, cookie):
    if SHARED.fullmatch(target.split('?', 1)[0]):
        return target
    return target + ('|' + hashlib.sha1(cookie.encode()).hexdigest()[:12] if cookie else '')


async def _cached_get(request, target, ttl):
    cookie = request.headers.get('cookie', '')
    key = _cache_key(target, cookie)
    entry = _entries.get(key)
    fresh = request.headers.get(FRESH) == '1'
    just_wrote = time.monotonic() - _writers.get(_client(request), -1e9) < WRITER_WAIT
    if entry is None or fresh or (entry['dirty'] and just_wrote):
        try:
            status, headers, body = await _fetch(key, target, cookie, fresh)
        except Exception:
            if entry is None:
                return JSONResponse({'detail': '台账服务正在重启，请稍后再试'}, status_code=503)
            status, headers, body = 200, entry['headers'], entry['body']
        return _response(status, headers + [('X-Ledger-Front', 'miss')], body)
    entry['used'] = time.monotonic()
    if entry['dirty'] or time.monotonic() - entry['fetched'] > ttl:
        _background(key, target, cookie)
    etag = next((v for k, v in entry['headers'] if k.lower() == 'etag'), None)
    if etag and request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers={'ETag': etag})
    return _response(200, entry['headers'] + [('X-Ledger-Front', 'hit')], entry['body'])


def _note_write(request):
    _writers[_client(request)] = time.monotonic()
    recent = time.monotonic() - 600
    for key, entry in list(_entries.items()):
        entry['dirty'] = True
        if entry['used'] > recent:
            _background(key, entry['target'], entry['cookie'])


async def _forward(request, port, target):
    body = request.stream() if request.method not in {'GET', 'HEAD'} else None
    try:
        status, headers, reader, writer = await _open(port, request.method, target, request.headers.items(), body,
                                                      request.headers.get('host'))
    except (OSError, asyncio.TimeoutError, IndexError, ValueError):
        return JSONResponse({'detail': '服务正在重启，请稍后再试'}, status_code=503)
    if port == LEDGER_PORT and request.method not in {'GET', 'HEAD'} and status < 400 \
            and not NOT_WRITES.fullmatch(request.url.path):
        _note_write(request)

    async def stream():
        try:
            async for chunk in reader:
                yield chunk
        finally:
            writer.close()

    response = StreamingResponse(stream(), status_code=status)
    response.raw_headers = [(k.lower().encode('latin-1'), v.encode('latin-1')) for k, v in headers]
    return response


async def entry(request):
    path = request.url.path
    target = path + ('?' + request.url.query if request.url.query else '')
    if path == '/':
        return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-store'})
    if path.startswith('/static/assets/'):
        file = (STATIC / path[len('/static/'):]).resolve()
        if STATIC.resolve() in file.parents and file.is_file():
            return FileResponse(file, headers={'Cache-Control': 'public,max-age=31536000,immutable'})
    if path.startswith('/api/agent/'):
        return await _forward(request, COPILOT_PORT, target)
    ttl = _ttl(path) if request.method == 'GET' else None
    if ttl is not None:
        return await _cached_get(request, target, ttl)
    return await _forward(request, LEDGER_PORT, target)


async def _warm():
    await asyncio.sleep(20)
    for key, entry in list(_entries.items())[-80:]:
        _background(key, entry['target'], entry['cookie'])
        await asyncio.sleep(0.5)


def _recent_periods():
    month, out = date.today().replace(day=1), []
    for _ in range(PREWARM_MONTHS):
        out.append(f'{month:%Y-%m}')
        month = (month - timedelta(days=1)).replace(day=1)
    return out


async def _prewarm(only_missing):
    try:
        status, _, reader, writer = await _open(LEDGER_PORT, 'GET', '/api/stores', [], None)
        try:
            body = b''.join([chunk async for chunk in reader])
        finally:
            writer.close()
        stores = [s['id'] for s in json.loads(body).get('stores', []) if not s.get('archived')] if status == 200 else []
    except Exception:
        return
    for period in _recent_periods():
        for store_id in stores:
            target = f'/api/stores/{urllib.parse.quote(store_id)}/periods/{period}'
            if only_missing and target in _entries:
                continue
            try:
                await _fetch(target, target, '', False)
            except Exception:
                pass


async def _prewarm_loop():
    await asyncio.sleep(120)
    await _prewarm(only_missing=True)
    while True:
        now = datetime.now()
        start = now.replace(hour=PREWARM_HOUR, minute=0, second=0, microsecond=0)
        if start <= now:
            start += timedelta(days=1)
        await asyncio.sleep((start - now).total_seconds())
        await _prewarm(only_missing=False)


@asynccontextmanager
async def lifespan(_app):
    _load()
    tasks = [asyncio.ensure_future(_warm()), asyncio.ensure_future(_prewarm_loop())]
    yield
    for task in tasks:
        task.cancel()


METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']
app = Starlette(routes=[Route('/{path:path}', entry, methods=METHODS)], lifespan=lifespan)


def main():
    import uvicorn
    uvicorn.run(app, host=os.environ.get('LEDGER_FRONT_HOST', '192.168.0.155'),
                port=int(os.environ.get('LEDGER_FRONT_PORT', '8000')), log_level='warning',
                timeout_keep_alive=75)


if __name__ == '__main__':
    main()
