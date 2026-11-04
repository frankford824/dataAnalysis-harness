"""整个系统的模型开销：台账助手、Agent Worker、台账自身（接表识别列）。

各处只记 token，费用一律按 pricing 的价目表现算，改价后历史费用随之更正。
"""
import json
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config, pricing, store

WORKER_DB = config.LEDGER_HOME.parent / 'agent-worker' / 'state' / 'agent-audit.db'
LEDGER_LOG = config.LEDGER_HOME / 'llm_calls.jsonl'
_cache = {'at': 0.0, 'value': None}
_lock = threading.Lock()


def _assistant_calls(since):
    rows = store.conn().execute('SELECT at, model, tokens_in, tokens_out, coalesce(cached,0) FROM usage WHERE at>=?',
                                (since.timestamp(),)).fetchall()
    return [(datetime.fromtimestamp(r[0]), r[1], r[2], r[3], r[4]) for r in rows]


def _worker_calls(since):
    if not WORKER_DB.exists():
        return []
    db = sqlite3.connect(f'file:{WORKER_DB}?mode=ro', uri=True, timeout=5)
    try:
        rows = db.execute('SELECT at, model, input_tokens, output_tokens, coalesce(cache_tokens,0) FROM model_call').fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    calls = []
    for at, model, tokens_in, tokens_out, cached in rows:
        when = datetime.fromisoformat(at).replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        if when >= since:
            calls.append((when, model, tokens_in or 0, tokens_out or 0, cached))
    return calls


def _ledger_calls(since):
    if not LEDGER_LOG.exists():
        return []
    calls = []
    for line in LEDGER_LOG.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            row = json.loads(line)
            when = datetime.strptime(row['at'], '%Y-%m-%dT%H:%M:%S%z').astimezone().replace(tzinfo=None)
        except (ValueError, KeyError):
            continue
        if when < since:
            continue
        usage = (row.get('got') or {}).get('usage') or {}
        tokens_in = usage.get('prompt_tokens') or len(json.dumps(row.get('sent') or '', ensure_ascii=False)) // 2
        tokens_out = usage.get('completion_tokens') or len(json.dumps(row.get('got') or '', ensure_ascii=False)) // 2
        cached = (usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0
        calls.append((when, row.get('model', ''), tokens_in, tokens_out, cached))
    return calls


SOURCES = {'assistant': ('台账助手', _assistant_calls), 'worker': ('Agent Worker', _worker_calls),
           'ledger': ('台账识别', _ledger_calls)}


def summary(force=False):
    with _lock:
        if not force and _cache['value'] and time.time() - _cache['at'] < 20:
            return _cache['value']
    now = datetime.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_ago = now - timedelta(hours=1)
    sources, models = [], defaultdict(lambda: {'calls': 0, 'tokens_in': 0, 'tokens_out': 0, 'cached': 0, 'cost': 0.0})
    total = today = last_hour = 0.0
    tokens_month = 0
    for key, (label, reader) in SOURCES.items():
        month_cost = day_cost = 0.0
        calls = tokens = 0
        for when, model, tokens_in, tokens_out, cached in reader(month_start):
            charge = pricing.cost(model, tokens_in, tokens_out, cached, when)
            month_cost += charge
            calls += 1
            tokens += (tokens_in or 0) + (tokens_out or 0)
            if when >= day_start:
                day_cost += charge
            if when >= hour_ago:
                last_hour += charge
            row = models[model]
            row['calls'] += 1
            row['tokens_in'] += tokens_in or 0
            row['tokens_out'] += tokens_out or 0
            row['cached'] += cached or 0
            row['cost'] += charge
        sources.append({'key': key, 'label': label, 'month_cny': round(month_cost, 4), 'today_cny': round(day_cost, 4),
                        'calls': calls, 'tokens': tokens})
        total += month_cost
        today += day_cost
        tokens_month += tokens
    value = {
        'month': month_start.strftime('%Y-%m'), 'month_cny': round(total, 4), 'today_cny': round(today, 4),
        'last_hour_cny': round(last_hour, 4), 'month_tokens': tokens_month,
        'budget_cny': config.MONTH_BUDGET_CNY, 'sources': sources,
        'models': sorted(({'model': m, **{k: round(v, 4) if k == 'cost' else v for k, v in row.items()}}
                          for m, row in models.items()), key=lambda r: -r['cost']),
        'price_version': pricing.VERSION, 'price_source': pricing.SOURCE,
        'updated_at': now.astimezone().isoformat(timespec='seconds'),
    }
    with _lock:
        _cache.update(at=time.time(), value=value)
    return value


def month_total():
    return summary()['month_cny']
