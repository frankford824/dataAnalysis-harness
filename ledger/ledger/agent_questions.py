"""Ask the Worker about one store-month; the engine supplies every number.

The Worker receives statement structure, availability, signs and reason codes
but no amounts. Its answer cites amounts only as ``{{node:<id>}}``; the ledger
renders them from the run captured when the question was claimed, so values
never leave this process and an invented figure is rejected, not displayed.
"""
from __future__ import annotations

import json
import re
import time

from . import automation_store as journal
from .workspace import WorkspaceError

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_question (
 id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,store_id TEXT NOT NULL,period TEXT NOT NULL,
 question TEXT NOT NULL,asked_by TEXT NOT NULL,asked_at TEXT NOT NULL,state TEXT NOT NULL,
 run_id INTEGER,lease_owner TEXT NOT NULL DEFAULT '',lease_token INTEGER NOT NULL DEFAULT 0,
 lease_until INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
 answer TEXT NOT NULL DEFAULT '',failure TEXT NOT NULL DEFAULT '',answered_by TEXT NOT NULL DEFAULT '',answered_at TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS agent_question_scope ON agent_question(store_id,period,asked_at);
CREATE INDEX IF NOT EXISTS agent_question_queue ON agent_question(state,lease_until,asked_at);
"""
PLACEHOLDER = re.compile(r'\{\{node:([A-Za-z0-9_]+)\}\}')
NUMERIC = re.compile(r'\d|[零一二三四五六七八九十百千万亿]+(?:元|块|笔|家|单|%)')
MAX_ATTEMPTS = 3
FAILURES = {'privacy_blocked': '问题里含订单号、手机号等原始标识，未发送给模型；请改用文字描述后重新提问',
            'model_unavailable': 'Agent 暂时无法回答（模型未配置、未通过评测或本月预算已用完），请稍后再问',
            'unsupported_number': 'Agent 的回答包含无法由账本核实的数字，已拦截；请换个问法或查看报表明细',
            'technical_failure': 'Agent 回答时发生技术故障，已停止重试'}


class QuestionError(WorkspaceError):
    pass


def install(conn):conn.executescript(SCHEMA)


def _mask(text):return re.sub(r'\d+(?:\.\d+)?', 'N', str(text or ''))[:200]


def _sign(node):
    value = node.get('value')
    if not node.get('available') or not isinstance(value, (int, float)) or isinstance(value, bool):return None
    return 'positive' if value > 0 else 'negative' if value < 0 else 'zero'


def _result(ws, run_id):
    row = ws.conn.execute('SELECT result FROM run WHERE id=?', (run_id,)).fetchone() if run_id else None
    return json.loads(row[0]) if row else {}


def ask(ws, model, *, request_id, store_id, period, question, actor):
    if not actor or actor.get('kind') != 'human' or not actor.get('authenticated') or not actor.get('id'):
        raise PermissionError('请使用已登录的人工身份提问')
    journal.period(period)
    text = question.strip()
    if not text or len(text) > 500:raise QuestionError('问题需为1至500字')
    if store_id not in {s.id for s in model.stores}:raise QuestionError('请选择已登记店铺')
    if not ws.latest_run(store_id, period):raise QuestionError('本店本月还没有核算结果，暂时无法提问')
    with ws.transaction() as conn:
        row = conn.execute('SELECT * FROM agent_question WHERE request_id=?', (request_id,)).fetchone()
        if row:
            if (row['store_id'], row['period'], row['question'], row['asked_by']) != (store_id, period, text, actor['id']):
                raise QuestionError('同一请求编号不能用于不同问题')
            return view(ws, dict(row))
        qid = journal.ident()
        conn.execute('INSERT INTO agent_question(id,request_id,store_id,period,question,asked_by,asked_at,state) VALUES(?,?,?,?,?,?,?,?)',
            (qid, request_id, store_id, period, text, actor['id'], journal.now(), 'queued'))
    return view(ws, dict(ws.conn.execute('SELECT * FROM agent_question WHERE id=?', (qid,)).fetchone()))


def _render(ws, row):
    nodes = {n['id']: n for n in _result(ws, row['run_id']).get('statement', [])}
    def value(match):
        node = nodes.get(match[1])
        if not node:return '（该项不在本期报表中）'
        if not node.get('available') or node.get('value') is None:return f"{node['name']}（暂不可用）"
        if node.get('display') == 'amount':return f"{float(node['value']):,.2f} 元"
        return str(node['value'])
    return PLACEHOLDER.sub(value, row['answer'])


def view(ws, row, clock=time.time):
    if row['state'] == 'claimed' and row['lease_until'] <= int(clock()) and row['attempts'] >= MAX_ATTEMPTS:
        row = {**row, 'state': 'failed', 'failure': 'technical_failure'}
    answered = row['state'] == 'answered'
    return {'question_id': row['id'], 'store_id': row['store_id'], 'period': row['period'], 'question': row['question'],
        'asked_by': row['asked_by'], 'asked_at': row['asked_at'], 'state': row['state'],
        'answer': _render(ws, row) if answered else '', 'answered_at': row['answered_at'],
        'failure': FAILURES.get(row['failure'], '') if row['state'] == 'failed' else '',
        'numbers_from': 'engine_run' if answered else None, 'run_id': row['run_id'] if answered else None}


def listing(ws, store_id, period, limit=50):
    journal.period(period)
    rows = ws.conn.execute('SELECT * FROM agent_question WHERE store_id=? AND period=? ORDER BY asked_at DESC LIMIT ?',
        (store_id, period, limit)).fetchall()
    return {'items': [view(ws, dict(r)) for r in rows]}


def context(ws, model, row):
    """Model-visible scope: structure and reason codes, no amounts."""
    result = _result(ws, row['run_id'])
    formula = {n.id: {'op': n.formula.op, 'of': list(n.formula.of)} for n in model.statement if n.formula}
    store = next((s for s in model.stores if s.id == row['store_id']), None)
    case = ws.conn.execute('SELECT stage,issues_json FROM automation_case WHERE store_id=? AND period=?',
        (row['store_id'], row['period'])).fetchone()
    state = ws.state(row['store_id'], row['period'])
    approved = (result.get('period_cost_input') or {}).get('status') == 'applied'
    return {'store_name': store.name if store else row['store_id'], 'platform': store.platform if store else '',
        'period': row['period'], 'period_state': state.state if state else 'open',
        'statement': [{'id': n['id'], 'name': n['name'], 'level': n.get('level'), 'available': bool(n.get('available')),
            'sign': _sign(n), 'is_total': bool(n.get('is_total')), 'formula': formula.get(n['id']),
            'amount_scope': n.get('amount_scope', ''), 'unavailable_reason': _mask(n.get('unavailable_reason')),
            'missing_sources': [_mask(s) for s in n.get('missing_sources', [])]} for n in result.get('statement', [])],
        'findings': [{'id': f.get('id'), 'name': f.get('name'), 'passed': f.get('passed'), 'blocking': f.get('blocking'),
            'head': _mask(f.get('head'))} for f in result.get('findings', [])],
        'case_stage': case['stage'] if case else '', 'case_issue_codes': sorted({i.get('code', '') for i in json.loads(case['issues_json'])}) if case else [],
        'period_cost_approved': approved, 'missing_source_names': [_mask(s) for s in result.get('missing_sources', [])]}


def claim(ws, model, identity, *, lease_seconds=180, clock=time.time):
    now = int(clock())
    with ws.transaction() as conn:
        row = conn.execute("SELECT * FROM agent_question WHERE state IN ('queued','claimed') AND lease_until<=? AND attempts<? ORDER BY asked_at LIMIT 1",
            (now, MAX_ATTEMPTS)).fetchone()
        if not row:return None
        run = ws.latest_run(row['store_id'], row['period'])
        token = row['lease_token']+1
        conn.execute("UPDATE agent_question SET state='claimed',lease_owner=?,lease_token=?,lease_until=?,attempts=attempts+1,run_id=? WHERE id=? AND lease_token=?",
            (identity['id'], token, now+lease_seconds, run['id'] if run else None, row['id'], row['lease_token']))
        row = dict(conn.execute('SELECT * FROM agent_question WHERE id=?', (row['id'],)).fetchone())
    return {'question_id': row['id'], 'lease_token': token, 'question': row['question'], 'context': context(ws, model, row)}


def _owned(conn, question_id, identity, token, clock):
    row = conn.execute('SELECT * FROM agent_question WHERE id=?', (question_id,)).fetchone()
    if (not row or row['state'] != 'claimed' or row['lease_owner'] != identity['id'] or row['lease_token'] != token
            or row['lease_until'] <= int(clock())):
        raise QuestionError('问答租约已失效，旧 Worker 不能提交回答')
    return dict(row)


def _allowed_tokens(ws, model, row):
    result = _result(ws, row['run_id'])
    store = next((s for s in model.stores if s.id == row['store_id']), None)
    year, month = row['period'].split('-')
    names = [row['period'], f'{year}年{month}月', f'{year}年{int(month)}月', f'{year}年', f'{int(month)}月', f'{month}月',
             '每一笔', '每一单', '每一家', *(n.get('name', '') for n in result.get('statement', [])),
             *(f.get('name', '') for f in result.get('findings', []))]
    if store:names += [store.name, store.platform]
    return sorted({n for n in names if n}, key=len, reverse=True)


def answer(ws, model, question_id, identity, *, lease_token, text, clock=time.time):
    body = str(text or '').strip()
    if not body or len(body) > 4000:raise QuestionError('回答需为1至4000字')
    with ws.transaction() as conn:
        row = _owned(conn, question_id, identity, lease_token, clock)
        nodes = {n['id'] for n in _result(ws, row['run_id']).get('statement', [])}
        cited = PLACEHOLDER.findall(body)
        if any(c not in nodes for c in cited) or '{{' in PLACEHOLDER.sub('', body):
            raise QuestionError('回答引用了本期报表不存在的项目')
        remainder = PLACEHOLDER.sub('', body)
        for token in _allowed_tokens(ws, model, row):remainder = remainder.replace(token, '')
        if NUMERIC.search(remainder):
            conn.execute("UPDATE agent_question SET state='failed',failure='unsupported_number',answered_by=?,answered_at=? WHERE id=?",
                (identity['id'], journal.now(), question_id))
            return {'status': 'rejected', 'reason': 'unsupported_number'}
        conn.execute("UPDATE agent_question SET state='answered',answer=?,answered_by=?,answered_at=?,lease_until=0 WHERE id=?",
            (body, identity['id'], journal.now(), question_id))
    return {'status': 'answered', 'question_id': question_id}


def fail(ws, question_id, identity, *, lease_token, reason, retryable, clock=time.time):
    if reason not in FAILURES:reason = 'technical_failure'
    with ws.transaction() as conn:
        row = _owned(conn, question_id, identity, lease_token, clock)
        final = not retryable or row['attempts'] >= MAX_ATTEMPTS
        conn.execute('UPDATE agent_question SET state=?,failure=?,lease_until=0,answered_by=?,answered_at=? WHERE id=?',
            ('failed' if final else 'queued', reason, identity['id'], journal.now(), question_id))
    return {'status': 'failed' if final else 'queued'}
