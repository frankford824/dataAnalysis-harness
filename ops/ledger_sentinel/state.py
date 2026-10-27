from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

SESSION=uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


class Busy(RuntimeError):
    pass


class State:
    def __init__(self, home):
        self.home = Path(home); self.home.mkdir(parents=True,exist_ok=True)
        self.path = self.home/'sentinel.db'
        with self.connect() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS observation(id TEXT PRIMARY KEY,status TEXT NOT NULL,reason TEXT NOT NULL,at TEXT NOT NULL,body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS event(id TEXT PRIMARY KEY,at TEXT NOT NULL,kind TEXT NOT NULL,scope TEXT NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS notification(event_id TEXT PRIMARY KEY,at TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS execution(request_id TEXT PRIMARY KEY,kind TEXT NOT NULL,config_sha TEXT NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS lease(scope TEXT PRIMARY KEY,owner TEXT NOT NULL,expires REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS readiness_observation(id TEXT PRIMARY KEY,phase TEXT NOT NULL,period TEXT NOT NULL,identity_sha TEXT NOT NULL,at TEXT NOT NULL,body TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS readiness_sample(id TEXT PRIMARY KEY,at TEXT NOT NULL,body TEXT NOT NULL);
              CREATE TRIGGER IF NOT EXISTS readiness_sample_immutable_update BEFORE UPDATE ON readiness_sample BEGIN SELECT RAISE(ABORT,'immutable rollout sample'); END;
              CREATE TRIGGER IF NOT EXISTS readiness_sample_immutable_delete BEFORE DELETE ON readiness_sample BEGIN SELECT RAISE(ABORT,'immutable rollout sample'); END;
              CREATE TRIGGER IF NOT EXISTS readiness_observation_immutable_update BEFORE UPDATE ON readiness_observation BEGIN SELECT RAISE(ABORT,'immutable rollout observation'); END;
              CREATE TRIGGER IF NOT EXISTS readiness_observation_immutable_delete BEFORE DELETE ON readiness_observation BEGIN SELECT RAISE(ABORT,'immutable rollout observation'); END;
              CREATE TRIGGER IF NOT EXISTS event_immutable_update BEFORE UPDATE ON event BEGIN SELECT RAISE(ABORT,'immutable sentinel event'); END;
              CREATE TRIGGER IF NOT EXISTS event_immutable_delete BEFORE DELETE ON event BEGIN SELECT RAISE(ABORT,'immutable sentinel event'); END;
            ''')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def event(self,kind,scope,status,body,*,notify=True,db=None):
        if db is None:
            with self.connect() as transaction:
                return self.event(kind,scope,status,body,notify=notify,db=transaction)
        identifier=uuid.uuid4().hex;stamp=now()
        encoded=json.dumps(body,ensure_ascii=False,sort_keys=True,allow_nan=False)
        db.execute('INSERT INTO event VALUES(?,?,?,?,?,?)',(identifier,stamp,kind,scope,status,encoded))
        if notify:
            db.execute('INSERT INTO notification VALUES(?,?,?,?)',(identifier,stamp,scope+'：'+status,encoded))
        return identifier

    def observe(self,result):
        result=dict(result)
        with self.connect() as db:
            old=db.execute('SELECT * FROM observation WHERE id=?',(result['id'],)).fetchone()
            transition=not old or old['status']!=result['status'] or old['reason']!=result['reason']
            if transition:
                identifier=self.event('probe',result['id'],result['status'],result,
                    notify=result['status']!='ok' or bool(old and old['status']!='ok'),db=db)
                result['incident_id']=identifier
            else:
                result['incident_id']=json.loads(old['body']).get('incident_id')
            db.execute('INSERT INTO observation VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason,at=excluded.at,body=excluded.body',
                (result['id'],result['status'],result['reason'],result['at'],json.dumps(result,ensure_ascii=False)))
        return result

    def begin(self,request_id,kind,config_sha,lease_scope='ledger-maintenance'):
        if not isinstance(request_id,str) or not 1<=len(request_id)<=128:
            raise ValueError('Invalid idempotency key')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM execution WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if old['kind']!=kind or old['config_sha']!=config_sha:
                    raise ValueError('Idempotency key belongs to another operation or configuration')
                return json.loads(old['body'])
            body={'request_id':request_id,'kind':kind,'state':'running','started_at':now(),'config_sha':config_sha,'steps':[],
                  'session_id':SESSION,'process_id':os.getpid(),'lease_scope':lease_scope}
            db.execute('INSERT INTO execution VALUES(?,?,?,?,?)',(request_id,kind,config_sha,'running',json.dumps(body)))
            return None

    def checkpoint(self,body):
        with self.connect() as db:
            changed=db.execute("UPDATE execution SET body=? WHERE request_id=? AND state='running'",
                (json.dumps(body,ensure_ascii=False,allow_nan=False),body['request_id'])).rowcount
            if changed!=1:raise Busy('Execution was interrupted or already finalized')

    def reconcile_interrupted(self,grace_seconds=30):
        """Never infer a successful mutation after a crash. Retain and escalate it."""
        with self.connect() as db:
            rows=[json.loads(r[0]) for r in db.execute("SELECT body FROM execution WHERE state='running'")]
        output=[]
        for body in rows:
            if body.get('session_id')==SESSION:continue
            try:age=time.time()-datetime.fromisoformat(body['started_at']).timestamp()
            except (ValueError,KeyError):age=grace_seconds+1
            if age<grace_seconds:continue
            with self.connect() as db:
                held=db.execute('SELECT expires FROM lease WHERE scope=?',(body.get('lease_scope','ledger-maintenance'),)).fetchone()
            if held and held['expires']>time.time():continue
            body.update(state='interrupted',reason='前一进程退出且维护租约已失效，不能推断恢复或发布已成功',
                required_action='检查已留档步骤及实际版本；使用配置的固定回滚/恢复动作取得新回执')
            try:output.append(self.finish(body))
            except Busy:pass
        return output

    def finish(self,body):
        body={**body,'finished_at':now()}
        body['receipt_sha']=digest(body)
        with self.connect() as db:
            changed=db.execute("UPDATE execution SET state=?,body=? WHERE request_id=? AND state='running'",
                (body['state'],json.dumps(body,ensure_ascii=False),body['request_id'])).rowcount
            if changed!=1:raise Busy('Execution was already finalized')
            self.event(body['kind'],body['request_id'],body['state'],body,db=db)
        return body

    @contextmanager
    def acquire(self,scope,ttl=120):
        if not isinstance(ttl,(int,float)) or not math.isfinite(ttl) or ttl<=0:raise ValueError('Invalid lease lifetime')
        owner=uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT owner,expires FROM lease WHERE scope=?',(scope,)).fetchone()
            if old and old['expires']>time.time():raise Busy('Another operation holds the maintenance lease')
            db.execute('INSERT INTO lease VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET owner=excluded.owner,expires=excluded.expires',
                (scope,owner,time.time()+ttl))
        try:
            yield lambda:self.assert_lease(scope,owner)
        finally:
            with self.connect() as db:db.execute('DELETE FROM lease WHERE scope=? AND owner=?',(scope,owner))

    def assert_lease(self,scope,owner):
        with self.connect() as db:
            row=db.execute('SELECT expires FROM lease WHERE scope=? AND owner=?',(scope,owner)).fetchone()
        if not row or row['expires']<=time.time():raise Busy('Maintenance lease expired; stop mutation')

    def observations(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM observation ORDER BY id')]
