"""Small durable journals for the ledger's existing work queue.

No scheduler lives here. Business decisions and their authorization/receipt use
the Workspace transaction which changes the financial state.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS automation_context (id INTEGER PRIMARY KEY CHECK(id=1),model_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS automation_worker (
 id TEXT PRIMARY KEY,scopes_json TEXT NOT NULL,status TEXT NOT NULL,heartbeat_at INTEGER NOT NULL,
 configuration_sha TEXT NOT NULL DEFAULT '',configuration_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS automation_case (
 id TEXT PRIMARY KEY,store_id TEXT NOT NULL,period TEXT NOT NULL,status TEXT NOT NULL,
 stage TEXT NOT NULL,input_sha TEXT NOT NULL,run_id INTEGER,issues_json TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(store_id,period));
CREATE TABLE IF NOT EXISTS automation_attempt (
 id TEXT PRIMARY KEY,case_id TEXT NOT NULL,request_id TEXT NOT NULL,input_sha TEXT NOT NULL,
 state TEXT NOT NULL,result_json TEXT NOT NULL,at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS automation_attempt_case ON automation_attempt(case_id,at);
CREATE TABLE IF NOT EXISTS automation_source_handoff (
 id TEXT PRIMARY KEY,case_id TEXT NOT NULL,input_sha TEXT NOT NULL,context_sha TEXT NOT NULL,
 reason_code TEXT NOT NULL,source_kind TEXT NOT NULL,evidence_type TEXT NOT NULL,
 role TEXT NOT NULL,kind TEXT NOT NULL,tool_id TEXT NOT NULL,source_refs_json TEXT NOT NULL,
 worker_id TEXT NOT NULL,attempt_id TEXT NOT NULL,since TEXT NOT NULL,
 UNIQUE(case_id,input_sha,context_sha,reason_code,source_kind,evidence_type));
CREATE INDEX IF NOT EXISTS automation_handoff_case ON automation_source_handoff(case_id,input_sha);
CREATE TABLE IF NOT EXISTS automation_proposal (
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,period TEXT NOT NULL,store_id TEXT NOT NULL,
 input_sha TEXT NOT NULL,proposal_sha TEXT NOT NULL,payload_json TEXT NOT NULL,
 actor TEXT NOT NULL,expires_at INTEGER NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS automation_decision (
 id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,kind TEXT NOT NULL,period TEXT NOT NULL,
 store_id TEXT NOT NULL,action TEXT NOT NULL,ref_id TEXT NOT NULL,values_json TEXT NOT NULL,
 reason TEXT NOT NULL,input_sha TEXT NOT NULL,proposal_sha TEXT NOT NULL,actor TEXT NOT NULL,at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS automation_decision_scope ON automation_decision(period,store_id,kind,at);
CREATE TABLE IF NOT EXISTS automation_authorization (
 id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,actor TEXT NOT NULL,identity_kind TEXT NOT NULL,
 action TEXT NOT NULL,input_sha TEXT NOT NULL,proposal_sha TEXT NOT NULL,scope_json TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS automation_receipt (
 id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,action TEXT NOT NULL,request_sha TEXT NOT NULL,
 authorization_id TEXT NOT NULL,result_json TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS automation_overhead_close (
 period TEXT NOT NULL,input_sha TEXT NOT NULL,total TEXT NOT NULL,basis_json TEXT NOT NULL,
 shares_json TEXT NOT NULL,actor TEXT NOT NULL,at TEXT NOT NULL,PRIMARY KEY(period,input_sha));
CREATE TABLE IF NOT EXISTS automation_run_proof (
 run_id INTEGER PRIMARY KEY,dependency_sha TEXT NOT NULL,result_sha TEXT NOT NULL,
 evidence_sha TEXT NOT NULL,manifest_json TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS automation_run_validation (
 id TEXT PRIMARY KEY,run_id INTEGER NOT NULL,dependency_sha TEXT NOT NULL,input_sha TEXT NOT NULL,
 manifest_json TEXT NOT NULL,at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS automation_validation_run ON automation_run_validation(run_id);
CREATE TABLE IF NOT EXISTS automation_legacy_source (
 run_id INTEGER PRIMARY KEY,source_sha TEXT NOT NULL,source_path TEXT NOT NULL,
 cutoff TEXT NOT NULL,reference TEXT NOT NULL,actor TEXT NOT NULL,at TEXT NOT NULL);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def ident():
    return uuid.uuid4().hex


def period(value):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',value or ''):
        raise ValueError('请选择有效月份')
    return value


def install(conn):
    conn.executescript(SCHEMA)
    columns={row[1] for row in conn.execute('PRAGMA table_info(automation_case)')}
    for column,definition in [('lease_owner',"TEXT NOT NULL DEFAULT ''"),('lease_token','INTEGER NOT NULL DEFAULT 0'),
        ('lease_until','INTEGER NOT NULL DEFAULT 0'),('worker_attempt_id',"TEXT NOT NULL DEFAULT ''")]:
        if column not in columns:conn.execute(f'ALTER TABLE automation_case ADD COLUMN {column} {definition}')
    if 'configuration_sha' not in {row[1] for row in conn.execute('PRAGMA table_info(automation_worker)')}:
        conn.execute("ALTER TABLE automation_worker ADD COLUMN configuration_sha TEXT NOT NULL DEFAULT ''")
    if 'configuration_json' not in {row[1] for row in conn.execute('PRAGMA table_info(automation_worker)')}:
        conn.execute("ALTER TABLE automation_worker ADD COLUMN configuration_json TEXT NOT NULL DEFAULT '{}'")
    # The first run manifest is the immutable expected-hash authority. Later
    # equivalent captures append validation rows; they never rewrite that proof.
    # Decisions revoke by appending a predecessor reference as well.
    for table in ('automation_decision','automation_authorization','automation_receipt','automation_overhead_close','automation_run_proof','automation_run_validation','automation_legacy_source','automation_source_handoff'):
        for event in ('UPDATE','DELETE'):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{event.lower()} BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT,'immutable automation journal'); END")
    # SQLite REPLACE may omit DELETE triggers when recursive_triggers is off.
    # Preserve save()'s existing insert-or-ignore contract for every duplicate.
    conn.execute("CREATE TRIGGER IF NOT EXISTS automation_run_proof_no_replace BEFORE INSERT ON automation_run_proof WHEN EXISTS(SELECT 1 FROM automation_run_proof WHERE run_id=NEW.run_id) BEGIN SELECT RAISE(IGNORE); END")


def active_decisions(conn,period):
    # A dated store/rule policy is approved once and resolved for each target
    # month. Its anchor month is audit metadata, never an implicit scope limit.
    rows=conn.execute('SELECT rowid,* FROM automation_decision ORDER BY rowid').fetchall()
    revoked={r['ref_id'] for r in rows if r['action']=='revoke'}
    result={}
    for row in rows:
        if row['action']=='apply' and row['id'] not in revoked:
            value=json.loads(row['values_json']);kind=row['kind']
            if kind=='store_scope':
                start,end=value.get('valid_from',row['period']),value.get('valid_until',row['period'])
                if not start<=period<=end:continue
            elif kind=='rule_grant':
                start,end=value.get('period_from',row['period']),value.get('period_until',row['period'])
                if not start<=period<=end:continue
                kind='rule_grant:'+str(value.get('rule_id',''))
            elif row['period']!=period:continue
            result[(row['store_id'],kind)]={**dict(row),'values':value}
    return result


def receipt(conn,request_id,request_sha):
    row=conn.execute('SELECT * FROM automation_receipt WHERE request_id=?',(request_id,)).fetchone()
    if not row:return None
    if row['request_sha']!=request_sha:
        raise ValueError('同一请求编号不能用于不同操作或内容')
    return json.loads(row['result_json'])


def authorize(conn,request_id,actor,action,input_sha,proposal_sha,scope):
    if not actor or not actor.get('id') or actor.get('kind')!='human' or not actor.get('authenticated'):
        raise PermissionError('此操作需要已登录的人工身份确认')
    aid=ident()
    conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
                 (aid,request_id,actor['id'],'human',action,input_sha,proposal_sha,encoded(scope),now()))
    return aid


def finish(conn,request_id,action,request_sha,authorization_id,result):
    rid=ident();result={**result,'receipt_id':rid}
    conn.execute('INSERT INTO automation_receipt VALUES(?,?,?,?,?,?,?)',
                 (rid,request_id,action,request_sha,authorization_id,encoded(result),now()))
    return result
