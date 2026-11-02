"""After an upgrade, compare each open store-month with the previous engine.

Runs are immutable and record the engine that produced them, so the check
needs no replay: the last run of the previous engine is compared with the
first available run of the current engine. Identical results pass without a
person; any difference becomes one card, and rule executors stay idle for that
month until a person confirms what they saw. Closed store-months are skipped.
"""
from __future__ import annotations

import json

from . import automation_store as journal
from .automation_actions import Conflict, _request, human

TOLERANCE = 0.005
_cache: dict[tuple[str, int], dict] = {}


def _engine():
    from .version import engine_version
    return engine_version()


def _nodes(ws, run_id):
    key = (str(ws.root), run_id)
    if key not in _cache:
        row = ws.conn.execute('SELECT result FROM run WHERE id=?', (run_id,)).fetchone()
        statement = json.loads(row[0]).get('statement', []) if row else []
        _cache[key] = {n['id']: (n.get('name', n['id']), n.get('value') if n.get('available') else None,
                                 n.get('display')) for n in statement}
        if len(_cache) > 2048:_cache.pop(next(iter(_cache)))
    return _cache[key]


def _differences(before, after):
    result = []
    for node_id, (name, new, display) in after.items():
        old = before.get(node_id, (name, None, display))[1]
        numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (old, new))
        if (numeric and abs(new-old) <= TOLERANCE) or (not numeric and old == new):continue
        result.append({'node_id': node_id, 'name': name, 'before': old, 'after': new, 'display': display})
    return result


def _confirmed(conn, engine, period):
    rows = conn.execute("SELECT scope_json FROM automation_authorization WHERE action='upgrade.confirm' AND identity_kind='human'").fetchall()
    return [json.loads(r[0]) for r in rows if json.loads(r[0]).get('engine') == engine and json.loads(r[0]).get('period') == period]


def check(ws, model, period):
    journal.period(period);engine = _engine()
    names = {s.id: s.name for s in model.stores} if model is not None else {}
    closed = {r[0] for r in ws.conn.execute("SELECT store_id FROM period WHERE period=? AND state='closed'", (period,))}
    items = []
    for (store_id,) in ws.conn.execute('SELECT DISTINCT store_id FROM run WHERE period=? ORDER BY store_id', (period,)).fetchall():
        if store_id in closed:continue
        previous = ws.conn.execute("SELECT id,engine,input_fingerprint FROM run WHERE store_id=? AND period=? AND engine NOT IN (?, '') ORDER BY id DESC LIMIT 1",
            (store_id, period, engine)).fetchone()
        if not previous:continue
        # The first run after the switch isolates the engine change from later data.
        current = ws.conn.execute('SELECT id,input_fingerprint FROM run WHERE store_id=? AND period=? AND engine=? AND id>? ORDER BY id LIMIT 1',
            (store_id, period, engine, previous['id'])).fetchone()
        base = {'store_id': store_id, 'store_name': names.get(store_id, store_id), 'from_engine': previous['engine'], 'to_engine': engine}
        if not current:
            items.append({**base, 'status': 'pending_recompute', 'differences': [], 'same_input': None});continue
        diff = _differences(_nodes(ws, previous['id']), _nodes(ws, current['id']))
        items.append({**base, 'status': 'changed' if diff else 'same', 'differences': diff,
            'same_input': previous['input_fingerprint'] == current['input_fingerprint'],
            'run_from': previous['id'], 'run_to': current['id']})
    accepted = {s for c in _confirmed(ws.conn, engine, period) for s in c.get('stores', [])}
    for item in items:item['confirmed'] = item['status'] == 'changed' and item['store_id'] in accepted
    review = [i for i in items if i['status'] != 'same' and not i['confirmed']]
    input_sha = journal.digest([engine, period, [[i['store_id'], i['status'], i.get('run_to'), i['differences']] for i in review]])
    return {'period': period, 'engine': engine, 'items': items, 'same': sum(i['status'] == 'same' for i in items),
        'changed': sum(i['status'] == 'changed' for i in review),
        'pending_recompute': sum(i['status'] == 'pending_recompute' for i in review),
        'needs_confirmation': bool(review), 'input_sha': input_sha}


def blocking(ws, model, period):
    """True while a person has not yet seen this month's upgrade differences."""
    return check(ws, model, period)['needs_confirmation']


def confirm(ws, model, *, request_id, period, input_sha, actor):
    human(actor);_request(request_id)
    current = check(ws, model, period)
    if current['pending_recompute']:raise Conflict('还有店铺未用新版本重算，请先重算后再确认')
    if input_sha != current['input_sha']:raise Conflict('核对结果已变化，请刷新后重新确认')
    key = 'upgrade-confirm:'+request_id
    signature = journal.digest([key, period, input_sha, actor['id']])
    with ws.transaction() as conn:
        old = journal.receipt(conn, key, signature)
        if old:return old
        aid = journal.authorize(conn, key, actor, 'upgrade.confirm', input_sha, '',
            {'engine': current['engine'], 'period': period, 'input_sha': input_sha,
             'stores': [i['store_id'] for i in current['items'] if i['status'] == 'changed' and not i['confirmed']]})
        result = journal.finish(conn, key, 'upgrade.confirm', signature, aid,
            {'status': 'confirmed', 'period': period, 'engine': current['engine'], 'changed': current['changed']})
    from .automation_deterministic import run_available
    from .automation_actions import deliver
    run_available(ws, model, period, aid);deliver(ws)
    return result
