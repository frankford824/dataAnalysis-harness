"""已过去月份的自动结账：检查全部通过的店铺自动结账，其余留给助手向用户解释。

结账预检要完整读取账务文件，白天跑会拖慢所有人的页面，所以只在凌晨跑；
白天要结账时让助手按需调用 run。
"""
import json
import threading
import uuid
from datetime import date, datetime, timedelta

from . import store, tools

NIGHTLY_HOUR = 2
MONTHS_BACK = 3
_pending = {}
_running = threading.Lock()


def periods(today=None):
    today = today or date.today()
    year, month = today.year, today.month
    out = []
    for _ in range(MONTHS_BACK):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        out.append(f'{year}-{month:02d}')
    return out


def close_period(period):
    request_id = _pending.get(period) or f'auto-close-{period}-{uuid.uuid4().hex[:8]}'
    status, text = tools._http('POST', '/api/automation/close-batches/preview',
                               {'request_id': request_id, 'period': period, 'store_ids': []}, timeout=3600)
    if status >= 400:
        return {'period': period, 'result': 'preview_failed', 'detail': tools._friendly_error(text)}
    preview = json.loads(text)
    if not preview.get('proposal_id'):
        _pending[period] = request_id
        return {'period': period, 'result': preview.get('status', 'pending'), 'detail': preview.get('message', '')}
    _pending.pop(period, None)
    eligible = preview.get('eligible') or []
    blocked = [{'store_id': b['store_id'], 'issues': [i.get('message') or i.get('code') for i in b.get('issues', [])][:3]}
               for b in preview.get('blocked') or []]
    if not eligible:
        return {'period': period, 'result': 'nothing_ready', 'closed': [], 'blocked': blocked}
    status, text = tools._http('POST', '/api/automation/close-batches/confirm', {
        'request_id': f'auto-confirm-{uuid.uuid4().hex[:12]}', 'proposal_id': preview['proposal_id'],
        'proposal_sha': preview['proposal_sha'], 'input_sha': preview['input_sha']}, timeout=3600)
    if status >= 400:
        return {'period': period, 'result': 'confirm_failed', 'detail': tools._friendly_error(text), 'blocked': blocked}
    result = json.loads(text)
    return {'period': period, 'result': result.get('status'), 'closed': [c['store_id'] for c in result.get('completed', [])],
            'unfinished': [u['store_id'] for u in result.get('unfinished', [])], 'blocked': blocked}


def run(selected=None):
    outcomes = []
    with _running:
        for period in selected or periods():
            try:
                outcome = close_period(period)
            except Exception as exc:
                outcome = {'period': period, 'result': 'error', 'detail': f'{type(exc).__name__}: {exc}'[:500]}
            store.log_event('auto_close', outcome)
            outcomes.append(outcome)
    return outcomes


def _seconds_until(hour):
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def loop(stop):
    while not stop.wait(_seconds_until(NIGHTLY_HOUR)):
        run()


def start():
    stop = threading.Event()
    threading.Thread(target=loop, args=(stop,), name='auto-close', daemon=True).start()
    return stop
