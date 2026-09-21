"""Current order-console labels, never accounting identities or source aliases.

Only a confirmed, unambiguous mapping can supply a display label. Missing or
conflicting metadata falls back to the model. Reading this module never writes
the model, feed, historical snapshots, or financial generations.
"""
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

_cache = OrderedDict()
_lock = threading.Lock()


def snapshot(root):
    path = Path(root).resolve() / 'order-feed.db'
    try:
        inode = path.stat().st_ino
    except FileNotFoundError:
        return '', {}
    # data_version is checked on the SAME read-only connection. File mtimes can
    # collide for rapid writes (and WAL writes do not touch the main DB).
    key = (str(path),inode)
    with _lock:
        entry = _cache.get(key)
        try:
            if entry is None:
                conn = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1,check_same_thread=False)
                entry = [conn,None,None]
                _cache[key] = entry
            _cache.move_to_end(key)
            while len(_cache)>8:
                _cache.popitem(last=False)[1][0].close()
            version = entry[0].execute('PRAGMA data_version').fetchone()[0]
            if version != entry[1]:
                entry[2] = _load(entry[0])
                entry[1] = version
            return entry[2]
        except sqlite3.OperationalError:
            if entry is not None:
                entry[0].close()
                _cache.pop(key,None)
            # An absent/unavailable feed must not prevent historical reads.
            return '', {}


def _load(conn):
    rows = conn.execute("SELECT ledger_store_id,payload_json FROM feed_store WHERE mapping_status='confirmed' AND ledger_store_id<>''").fetchall()
    candidates = {}
    for sid, raw in rows:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        label = value.get('shop_name')
        if isinstance(label, str) and label.strip():
            candidates.setdefault(sid, set()).add(label.strip())
    labels = {sid: next(iter(values)) for sid, values in candidates.items() if len(values) == 1}
    revision = hashlib.sha256(json.dumps(labels,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return revision, labels


def names(root, model):
    labels = snapshot(root)[1]
    return {store.id: labels.get(store.id, store.name) for store in model.stores}


def store_dict(root, store):
    from .view import store_dict as canonical
    label = snapshot(root)[1].get(store.id, store.name)
    return {**canonical(store), 'name': label, 'accounting_name': store.name,
            'aliases': list(dict.fromkeys([store.name, *store.aliases]))}
