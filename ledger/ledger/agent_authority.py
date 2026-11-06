"""One human grant lets the Worker apply every published deterministic rule.

The grant expands into ordinary store x rule ``rule_grant`` decisions, each
with its own human ``decision.apply`` authorization, so the Checker, rule
executors, revocation and audit contracts are the same as individual approval.
Amounts stay engine-computed from original evidence; closing stays a human
confirmation and closed store-months are never touched by rule executors.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import automation_store as journal
from . import automation_checker as checker
from .automation_actions import (Conflict, _audit_cases, _decision_input, _load_proposal,
                                 _proposal, _request, decision_revoke, deliver, human)

MAX_MONTHS = 24
MAX_DAYS = 366
OPEN_END = '9999-12'
STANDING_UNTIL = '9999-12-31T23:59:59+00:00'


def _months(first, last):
    year, month = map(int, first.split('-'));result = []
    while f'{year:04d}-{month:02d}' <= last:
        result.append(f'{year:04d}-{month:02d}');month += 1
        if month > 12:year, month = year+1, 1
    return result


def _rules():
    from .automation_rules import contracts, metadata
    labels = {row['id']: row['label'] for row in metadata() if row.get('id')}
    return [{'id': rule, 'sha': sha, 'label': labels.get(rule, rule)} for rule, (sha, _) in sorted(contracts().items())]


def _marked(conn, request_id):
    row = conn.execute("SELECT identity_kind,action,scope_json FROM automation_authorization WHERE request_id=?", (request_id,)).fetchone()
    if not row or row['identity_kind'] != 'human' or row['action'] != 'decision.apply':return False
    return bool(json.loads(row['scope_json']).get('agent_authority'))


def standing(conn):
    """The latest standing consent, unless a person stopped delegation after it."""
    current = None
    for row in conn.execute("SELECT * FROM automation_authorization WHERE identity_kind='human' AND action IN ('agent_authority.apply','agent_authority.revoke') ORDER BY rowid"):
        scope = json.loads(row['scope_json'])
        if row['action'] == 'agent_authority.revoke':current = None
        elif scope.get('standing'):current = {**dict(row), 'scope': scope}
    return current


def _grant(ws, model, conn, actor, key, proposal_sha, store_id, period, rules, window, reason, grant_key, basis=None):
    if basis is None:
        basis = _decision_input(ws, model, 'rule_grant', store_id, period,
            {'rule_id': rules[0]['id'], 'rule_sha': rules[0]['sha'], 'enabled': True, **window})
    granted = []
    for rule in rules:
        values = {'rule_id': rule['id'], 'rule_sha': rule['sha'], 'enabled': True, **window}
        request = grant_key(store_id, rule)
        if conn.execute('SELECT 1 FROM automation_authorization WHERE request_id=?', (request,)).fetchone():continue
        grant_auth = journal.authorize(conn, request, actor, 'decision.apply', basis, proposal_sha,
            {'period': period, 'store_id': store_id, 'agent_authority': key})
        did = journal.ident()
        conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (did, request, 'rule_grant', period, store_id, 'apply', '', journal.encoded(values),
             reason, basis, proposal_sha, actor['id'], journal.now()))
        journal.finish(conn, request, 'decision.apply', journal.digest([request, did]), grant_auth,
            {'decision_id': did, 'status': 'applied', 'period': period, 'store_id': store_id})
        granted.append(did)
    return granted


def extend(ws, model, period):
    """Cover stores or months that appeared after a standing consent.

    The grants carry the consenting person's authorization and the consent key,
    so they stay revocable and auditable like the original ones. Rules whose
    contract changed after the consent are left for the person to accept again.
    """
    consent = standing(ws.conn)
    if not consent:return 0
    accepted = {tuple(r) for r in consent['scope'].get('rules', [])}
    rules = [r for r in _rules() if (r['id'], r['sha']) in accepted]
    if not rules:return 0
    stamp = datetime.now(timezone.utc);active = journal.active_decisions(ws.conn, period)
    todo = {}
    for store in checker.operating_stores(ws, model, period):
        missing = [r for r in rules if not _current(active.get((store.id, 'rule_grant:'+r['id'])), r, stamp)]
        if missing:todo[store.id] = missing
    if not todo:return 0
    actor = {'id': consent['actor'], 'kind': 'human', 'authenticated': True}
    key = consent['request_id'];window = {'valid_from': stamp.isoformat(), 'valid_until': STANDING_UNTIL,
        'period_from': period, 'period_until': OPEN_END}
    prepared=[]
    for store_id, missing in sorted(todo.items()):
        basis=_decision_input(ws, model, 'rule_grant', store_id, period,
            {'rule_id': missing[0]['id'], 'rule_sha': missing[0]['sha'], 'enabled': True, **window})
        prepared.append((store_id, missing, basis))
    granted=[]
    for store_id, missing, basis in prepared:
        with ws.transaction() as conn:
            granted += _grant(ws, model, conn, actor, key, consent['proposal_sha'], store_id, period, missing, window,
                '长期授权自动覆盖新店铺或新月份',
                lambda sid, rule: 'agent-authority-extend:'+journal.digest([key, sid, rule['id'], rule['sha'], period])[:40],
                basis=basis)
    if granted:_audit_cases(ws, model, period, key, 'decision_applied', {'agent_authority': key, 'grants': len(granted), 'extended': True})
    return len(granted)


def _current(grant, rule, stamp):
    value = grant['values'] if grant else {}
    try:
        return (value.get('enabled') is True and value.get('rule_id') == rule['id'] and value.get('rule_sha') == rule['sha']
                and datetime.fromisoformat(value['valid_from']) <= stamp < datetime.fromisoformat(value['valid_until']))
    except (KeyError, TypeError, ValueError):
        return False


def changes(ws, model, period):
    """Rule-executed changes in this month; humans may withdraw open ones."""
    rules = {r['id']: r['label'] for r in _rules()}
    names = {s.id: s.name for s in model.stores}
    rows = ws.conn.execute(
        f"SELECT * FROM automation_decision WHERE period=? AND action='apply' AND kind IN ({','.join('?'*len(rules))}) ORDER BY rowid DESC",
        (period, *rules)).fetchall()
    revoked = {r[0] for r in ws.conn.execute("SELECT ref_id FROM automation_decision WHERE action='revoke'")}
    closed = {r[0] for r in ws.conn.execute("SELECT store_id FROM period WHERE period=? AND state='closed'", (period,))}
    result = []
    for row in rows:
        status = 'revoked' if row['id'] in revoked else 'frozen' if row['store_id'] in closed else 'applied'
        result.append({'decision_id': row['id'], 'rule_id': row['kind'], 'label': rules[row['kind']],
            'store_id': row['store_id'], 'store_name': names.get(row['store_id'], row['store_id']),
            'at': row['at'], 'status': status, 'can_revoke': status == 'applied'})
    return result


def status(ws, model, period):
    journal.period(period)
    from .automation_deterministic import enabled
    rules, stamp = _rules(), datetime.now(timezone.utc)
    stores = checker.operating_stores(ws, model, period)
    active = journal.active_decisions(ws.conn, period)
    covered = delegated = 0;missing = [];until = []
    for store in stores:
        for rule in rules:
            grant = active.get((store.id, 'rule_grant:'+rule['id']))
            if not _current(grant, rule, stamp):
                missing.append({'store_id': store.id, 'store_name': store.name, 'rule_id': rule['id']});continue
            covered += 1
            if _marked(ws.conn, grant['request_id']):
                delegated += 1;until.append(grant['values']['valid_until'])
    total = len(stores)*len(rules)
    consent = standing(ws.conn)
    accepted = {tuple(r) for r in consent['scope'].get('rules', [])} if consent else set()
    standing_view = {'active': bool(consent), 'actor': consent['actor'] if consent else None, 'at': consent['at'] if consent else None,
        'outdated_rules': [r['label'] for r in rules if (r['id'], r['sha']) not in accepted] if consent else []}
    return {'period': period, 'enabled': enabled(), 'standing': standing_view, 'rules': [{'id': r['id'], 'label': r['label']} for r in rules],
        'store_count': len(stores), 'grant_total': total, 'covered': covered, 'delegated': delegated,
        'missing': missing[:200], 'missing_count': len(missing), 'active': total > 0 and covered == total,
        'valid_until': min(until) if until else None, 'financial_close': 'human_confirmation',
        'changes': changes(ws, model, period)}


def _scope(ws, model, first, last, days):
    journal.period(first)
    if days is None:
        if last != OPEN_END:raise Conflict('长期授权不设结束月')
    else:
        journal.period(last)
        if last < first:raise Conflict('授权业务月范围无效')
        if len(_months(first, last)) > MAX_MONTHS:raise Conflict(f'一次最多授权{MAX_MONTHS}个业务月')
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_DAYS:raise Conflict(f'执行有效期须为1至{MAX_DAYS}天')
    stores = sorted(s.id for s in checker.operating_stores(ws, model, first))
    if not stores:raise Conflict('起始业务月没有营业店铺')
    rules = _rules()
    return stores, rules, journal.digest({'stores': stores, 'rules': [[r['id'], r['sha']] for r in rules],
        'period_from': first, 'period_until': last, 'valid_days': days})


def preview(ws, model, *, request_id, period_from, period_until, valid_days, reason, actor, standing=False):
    human(actor);_request(request_id)
    if not reason.strip():raise Conflict('请填写授权依据')
    if standing:period_until, valid_days = OPEN_END, None
    stores, rules, sha = _scope(ws, model, period_from, period_until, valid_days)
    key = 'agent-authority-preview:'+request_id
    signature = journal.digest([key, period_from, period_until, valid_days, reason, actor['id']])
    with ws.transaction() as conn:
        old = journal.receipt(conn, key, signature)
        if old:return old
        aid = journal.authorize(conn, key, actor, 'agent_authority.preview', sha, '', {'period_from': period_from, 'period_until': period_until})
        payload = {'kind': 'agent_authority', 'period_from': period_from, 'period_until': period_until,
            'valid_days': valid_days, 'standing': valid_days is None, 'reason': reason.strip(), 'stores': stores,
            'rules': [{'id': r['id'], 'sha': r['sha'], 'label': r['label']} for r in rules],
            'grant_count': len(stores)*len(rules), 'financial_close': 'human_confirmation',
            'closed_store_months_changed': False}
        result = _proposal(ws, actor, 'agent_authority', period_from, '', sha, payload)
        return journal.finish(conn, key, 'agent_authority.preview', signature, aid, result)


def apply(ws, model, *, request_id, proposal_id, proposal_sha, input_sha, actor):
    human(actor);_request(request_id)
    key = 'agent-authority-apply:'+request_id
    signature = journal.digest([key, proposal_id, proposal_sha, input_sha, actor['id']])
    try:old = journal.receipt(ws.conn, key, signature)
    except ValueError as exc:raise Conflict(str(exc)) from exc
    if old:return old
    proposal, payload = _load_proposal(ws.conn, proposal_id, proposal_sha, input_sha, actor, 'agent_authority')
    first, last, days = payload['period_from'], payload['period_until'], payload['valid_days']
    stores, rules, sha = _scope(ws, model, first, last, days)
    if sha != input_sha:raise Conflict('预览后店铺或规则版本已变化，本次未授权，请重新预览')
    stamp = datetime.now(timezone.utc)
    until = STANDING_UNTIL if days is None else (stamp+timedelta(days=days)).isoformat()
    window = {'valid_from': stamp.isoformat(), 'valid_until': until, 'period_from': first, 'period_until': last}
    bases={}
    for store_id in stores:
        bases[store_id]=_decision_input(ws, model, 'rule_grant', store_id, first,
            {'rule_id': rules[0]['id'], 'rule_sha': rules[0]['sha'], 'enabled': True, **window})
    granted=[]
    for store_id in stores:
        with ws.transaction() as conn:
            granted += _grant(ws, model, conn, actor, key, proposal_sha, store_id, first, rules, window, payload['reason'],
                lambda sid, rule: 'agent-authority:'+request_id+':'+journal.digest([sid, rule['id']])[:24],
                basis=bases[store_id])
    _, _, sha = _scope(ws, model, first, last, days)
    if sha != input_sha:raise Conflict('预览后店铺或规则版本已变化，本次未授权，请重新预览')
    with ws.transaction() as conn:
        old = journal.receipt(conn, key, signature)
        if old:return old
        aid = journal.authorize(conn, key, actor, 'agent_authority.apply', input_sha, proposal_sha,
            {'period_from': first, 'period_until': last, 'grant_count': len(stores)*len(rules),
             'standing': days is None, 'rules': [[r['id'], r['sha']] for r in rules]})
        result = journal.finish(conn, key, 'agent_authority.apply', signature, aid,
            {'status': 'applied', 'grant_count': len(granted), 'period_from': first, 'period_until': last,
             'valid_until': window['valid_until']})
    _audit_cases(ws, model, first, key, 'decision_applied', {'agent_authority': key, 'grants': len(granted)})
    from .automation_deterministic import run_available
    present = {r[0] for r in ws.conn.execute("SELECT DISTINCT period FROM period WHERE period BETWEEN ? AND ? AND state<>'closed'", (first, last))}
    for period in sorted(present):run_available(ws, model, period, aid)
    deliver(ws)
    return result


def revoke(ws, model, *, request_id, period, reason, actor):
    """Stop delegation: withdraw every grant it made, in every month."""
    human(actor);_request(request_id);journal.period(period)
    if not reason.strip():raise Conflict('撤销授权必须填写原因')
    key = 'agent-authority-revoke:'+request_id
    with ws.transaction() as conn:
        if not conn.execute('SELECT 1 FROM automation_authorization WHERE request_id=?', (key,)).fetchone():
            journal.authorize(conn, key, actor, 'agent_authority.revoke', journal.digest([period, reason]), '', {'period': period})
    gone = {r[0] for r in ws.conn.execute("SELECT ref_id FROM automation_decision WHERE action='revoke'")}
    rows = ws.conn.execute("SELECT rowid,* FROM automation_decision WHERE kind='rule_grant' AND action='apply' ORDER BY rowid").fetchall()
    targets = [{**dict(r), 'values': json.loads(r['values_json'])} for r in rows
               if r['id'] not in gone and _marked(ws.conn, r['request_id'])]
    revoked, failed = [], []
    for grant in sorted(targets, key=lambda g: g['id']):
        values = grant['values']
        basis = _decision_input(ws, model, 'rule_grant', grant['store_id'], grant['period'], values)
        try:
            decision_revoke(ws, model, grant['id'], request_id=request_id[:80]+':'+grant['id'][:32],
                            reason=reason, input_sha=basis, actor=actor)
            revoked.append(grant['id'])
        except (Conflict, PermissionError, ValueError) as exc:
            failed.append({'decision_id': grant['id'], 'store_id': grant['store_id'], 'reason': str(exc)})
    return {'status': 'revoked' if not failed else 'partial', 'revoked': len(revoked), 'failed': failed}
