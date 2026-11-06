"""资料或订单流到达后，通知本机房间。通知失败不影响交表和订单同步。"""
import json
import os
import urllib.error
import urllib.request


def post(pairs, kind, note=''):
    items = [{'store_id': store_id, 'period': period, 'kind': kind, 'note': note[:200]}
             for store_id, period in pairs if store_id and period][:12]
    if not items:
        return
    port = os.environ.get('COPILOT_PORT', '8020')
    body = json.dumps({'items': items}, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        f'http://127.0.0.1:{port}/api/agent/rooms/arrive', data=body,
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=0.4) as response:
            response.read()
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return
