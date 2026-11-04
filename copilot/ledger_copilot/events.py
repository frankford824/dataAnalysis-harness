"""给所有打开页面的人推送：群聊消息、在线名单、助手处理进度、界面和数据更新。"""
import itertools
import json
import queue
import threading

_subscribers = {}
_ids = itertools.count(1)
_lock = threading.Lock()


def sse(kind, value):
    return f'event: {kind}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n'


def subscribe(user):
    inbox = queue.Queue(maxsize=2000)
    with _lock:
        sid = next(_ids)
        _subscribers[sid] = (inbox, user or '')
    publish('presence', {'users': presence()})
    return sid, inbox


def unsubscribe(sid):
    with _lock:
        _subscribers.pop(sid, None)
    publish('presence', {'users': presence()})


def presence():
    with _lock:
        return sorted({user for _, user in _subscribers.values() if user})


def publish(kind, value):
    message = sse(kind, value)
    with _lock:
        targets = list(_subscribers.values())
    for inbox, _ in targets:
        try:
            inbox.put_nowait(message)
        except queue.Full:
            pass
