"""慢的只读接口：先答上次的结果，后台再刷新。

这些接口要算几十秒到几分钟，同时被几个页面轮询时会占满线程池，连 /api/version
都要排队。缓存按完整 URL 存；超过有效期后，下一次读取仍立即返回旧结果，同时在
后台单独重算一份。有人改过数据后，下一次读取直接重算并等待，免得刚改完的人看到
旧数。手动刷新带 X-Ledger-Fresh: 1，同样直接重算。
改账的动作各自重新校验输入，所以页面上稍旧的数字不会导致改错账。
"""
import re
import threading
import time
import urllib.request
from collections import OrderedDict

from starlette.responses import Response

CACHED = [re.compile(p) for p in (
    r'/api/commission-v2/status', r'/api/automation/inbox', r'/api/overview', r'/api/operations/month',
    r'/api/stores', r'/api/stores/[^/]+', r'/api/stores/[^/]+/periods/[^/]+', r'/api/navigation',
    r'/api/order-feed/status', r'/api/trend', r'/api/index/storage', r'/api/operations/sentinel/\w+',
    r'/api/stores/[^/]+/pricing-status', r'/api/commission-v2/people',
)]
TTL = 90
LIMIT = 400
REFRESH = 'X-Ledger-Refresh'
FRESH = 'X-Ledger-Fresh'
DROP = {'content-length', 'content-type'}


def _writes():
    from .automation_actions import _writes as counter
    return counter[0]


def cacheable(request):
    return request.method == 'GET' and any(p.fullmatch(request.url.path) for p in CACHED)


def _target(request):
    return request.url.path + ('?' + request.url.query if request.url.query else '')


def _key(request):
    from . import api
    return f'{api.WORKSPACE_ROOT}|{_target(request)}'


class _Cache:
    def __init__(self):
        self.entries = OrderedDict()
        self.flights = set()
        self.lock = threading.Lock()

    def put(self, key, status, body, media, headers):
        if status != 200:
            return
        with self.lock:
            self.entries[key] = {'body': body, 'media': media, 'headers': headers, 'at': time.monotonic(),
                                 'writes': _writes()}
            self.entries.move_to_end(key)
            while len(self.entries) > LIMIT:
                self.entries.popitem(last=False)

    def refresh(self, port, key, target):
        with self.lock:
            if key in self.flights:
                return
            self.flights.add(key)

        def run():
            try:
                request = urllib.request.Request(f'http://127.0.0.1:{port}{target}', headers={REFRESH: '1'})
                with urllib.request.urlopen(request, timeout=3600) as response:
                    headers = {k: v for k, v in response.headers.items() if k.lower() not in DROP}
                    self.put(key, response.status, response.read(), response.headers.get('content-type'), headers)
            except Exception:
                pass
            finally:
                with self.lock:
                    self.flights.discard(key)

        threading.Thread(target=run, name='ledger-read-refresh', daemon=True).start()


def install(app):
    cache = _Cache()

    @app.middleware('http')
    async def stale_while_revalidate(request, call_next):
        if not cacheable(request) or request.headers.get(REFRESH) or request.headers.get('if-none-match'):
            return await call_next(request)
        key = _key(request)
        entry = cache.entries.get(key)
        if entry is not None and not request.headers.get(FRESH) and entry['writes'] == _writes():
            if time.monotonic() - entry['at'] > TTL:
                cache.refresh(request.url.port or 8000, key, _target(request))
            return Response(entry['body'], media_type=entry['media'], headers={**entry['headers'], 'X-Ledger-Cached': '1'})
        response = await call_next(request)
        if response.status_code != 200:
            return response
        body = b''.join([chunk async for chunk in response.body_iterator])
        media = response.headers.get('content-type')
        headers = {k: v for k, v in response.headers.items() if k.lower() not in DROP}
        cache.put(key, response.status_code, body, media, headers)
        return Response(body, status_code=200, headers=headers, media_type=media)
