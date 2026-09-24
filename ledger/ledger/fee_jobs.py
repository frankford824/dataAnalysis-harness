"""Durable fee-rule batches on the existing registry job queue.

The journal is prepared before the atomic model write. Recovery reconciles the
actual rule revision; it never replays a stale rule set over a newer one.
"""
import hashlib
import json
import threading
import time
import uuid

from .commission_registry import Registry, json_text, now
from .model.loader import load_model
from .model.config import replace_fee_rules
from .model.transaction import model_lock
from . import service

KIND = 'fee_rules'
ACTIVE = ('preparing', 'queued', 'running')
lock = threading.RLock()


class Conflict(ValueError):
    pass


def rows(rules):
    return [r.model_dump(mode='json') for r in rules]


def revision(rules):
    return hashlib.sha256(json_text(rows(rules)).encode()).hexdigest()


def affected(before, after, platforms):
    def effective(rules, platform):
        return [{k:v for k,v in r.items() if k not in ('at','by','note')}
                for r in rows(rules) if r['platform'] in ('*', platform)]
    return {p for p in platforms if effective(before,p) != effective(after,p)}


def _write(registry, jid, status, payload, result, error=''):
    with registry.transaction() as conn:
        conn.execute('UPDATE job SET status=?,payload=?,result=?,error=? WHERE id=?',
                     (status,json_text(payload),json_text(result),error,jid))


def _audit(ws, payload, jid):
    summary = payload['note'] + f' [任务 {jid}]'
    if not ws.conn.execute("SELECT 1 FROM config_log WHERE kind='fee-rules' AND summary=?",(summary,)).fetchone():
        # Financial rule changes require an audit; Workspace.log_config is
        # deliberately best-effort for other UI operations, so do not use it.
        with ws.conn as conn:
            conn.execute('INSERT INTO config_log(at,by,kind,summary,before_json,after_json) VALUES(?,?,?,?,?,?)',
                         (now(),payload['actor'],'fee-rules',summary,json_text(payload['before']),json_text(payload['after'])))


def recover(ws, model_fn):
    registry=Registry(ws.root)
    with lock:
        with registry.connect() as conn:
            pending=conn.execute("SELECT * FROM job WHERE kind=? AND status='preparing'",(KIND,)).fetchall()
        for row in pending:
            p=json.loads(row['payload']); result=json.loads(row['result'])
            current=revision(model_fn().fee_rules)
            if current == p['revision']:
                p['saved']=True
                _audit(ws,p,row['id'])
                _write(registry,row['id'],'queued' if p['stores'] else 'done',p,result)
            else:
                _write(registry,row['id'],'failed',p,result,'规则保存未完成或已被其他修改替代；未重新覆盖配置，请刷新核对')


def restart(registry):
    with registry.transaction() as conn:
        for row in conn.execute("SELECT id,result FROM job WHERE kind=? AND status='running'",(KIND,)).fetchall():
            result=json.loads(row['result'])
            for item in result.get('stores',{}).values():
                if item['state']=='running':item.update(state='queued',phase='服务重启后等待继续',percent=0)
            conn.execute("UPDATE job SET status='queued',result=? WHERE id=?",(json_text(result),row['id']))


def submit(ws, model_dir, rules, *, expected, request_id, note='', actor='', recompute=True):
    registry=Registry(ws.root)
    try:
        jid='fee:'+str(uuid.UUID(request_id))
    except (ValueError,TypeError,AttributeError):
        raise Conflict('请刷新页面后重新试算并提交（缺少有效请求编号）')
    desired=revision(rules)
    with lock, model_lock(model_dir):
        base=load_model(model_dir)
        with registry.connect() as conn:
            previous=conn.execute('SELECT * FROM job WHERE id=?',(jid,)).fetchone()
            active=conn.execute("SELECT id FROM job WHERE kind=? AND status IN ('preparing','queued','running') LIMIT 1",(KIND,)).fetchone()
            latest=conn.execute('SELECT * FROM job WHERE kind=? ORDER BY at DESC,rowid DESC LIMIT 1',(KIND,)).fetchone()
        if previous:
            old_payload=json.loads(previous['payload'])
            if old_payload.get('request_revision',old_payload['revision']) != desired:
                raise Conflict('同一请求编号不能提交不同规则，请重新试算')
            if old_payload.get('saved') and revision(base.fee_rules)!=old_payload['revision']:
                raise Conflict('该请求已执行，但当前规则已更新；未覆盖新配置，请刷新后重新试算')
            return status(ws,jid)
        current=revision(base.fee_rules)
        if desired == current and latest and json.loads(latest['payload'])['revision']==desired:
            return status(ws,latest['id'])
        if expected != current:
            raise Conflict('费项规则已变化或页面版本过旧；本次未保存，请刷新并重新试算')
        if active:
            raise Conflict('上一批费项规则正在重算，本次尚未保存；请等待该任务完成再提交新规则')
        request_revision=desired
        rules=[r.model_copy(update={'at':r.at or now()}) for r in rules] if desired!=current else rules
        desired=revision(rules)
        platforms=affected(base.fee_rules,rules,{s.platform for s in base.stores})
        known=set(ws.store_ids())
        # Existing periods include order-feed-only stores, not only uploaded files.
        targets=[s.id for s in base.stores if not s.archived and s.id in known and s.platform in platforms]
        if latest and latest['status']=='failed':
            old=json.loads(latest['payload']); result=json.loads(latest['result'])
            targets=list(dict.fromkeys(targets+[s for s in old['stores'] if result.get('stores',{}).get(s,{}).get('state')!='done']))
        if not recompute:targets=[]
        payload=dict(before=rows(base.fee_rules),after=rows(rules),revision=desired,
                     request_revision=request_revision,
                     saved=False,
                     stores=targets,platforms=sorted(platforms),actor=actor,
                     note=note.strip() or f'界面改费项规则，共 {len(rules)} 条')
        result={'stores':{},'started_at':now()}
        with registry.transaction() as conn:
            conn.execute('INSERT INTO job VALUES(?,?,?,?,?,?,?,?)',
                         (jid,KIND,actor,now(),'preparing',json_text(payload),json_text(result),''))
        try:
            if desired != current:
                replace_fee_rules(model_dir,rules)
                _audit(ws,payload,jid)
            payload['saved']=True
            _write(registry,jid,'queued' if targets else 'done',payload,result)
        except Exception:
            # A committed model with a failed audit/queue acknowledgement is
            # recovered from the durable preparing record, never overwritten.
            if revision(load_model(model_dir).fee_rules) != desired:
                _write(registry,jid,'failed',payload,result,'保存失败，规则未生效')
            raise
    return status(ws,jid)


def status(ws, jid=None):
    registry=Registry(ws.root)
    with registry.connect() as conn:
        row=conn.execute('SELECT * FROM job WHERE kind=? '+('AND id=?' if jid else 'ORDER BY at DESC,rowid DESC LIMIT 1'),
                         (KIND,jid) if jid else (KIND,)).fetchone()
    if not row:
        return None
    p=json.loads(row['payload']); result=json.loads(row['result']); stores=[]
    for sid in p['stores']:
        detail=result.get('stores',{}).get(sid,{'state':'queued','phase':'等待调度','percent':0})
        # Use this task's own durable reporter, never another same-store run.
        stores.append({'store_id':sid,**detail})
    done=sum(s['state']=='done' for s in stores); failed=sum(s['state']=='failed' for s in stores)
    completed=done+failed
    fraction=sum((s.get('percent',0)/100) for s in stores if s['state']=='running')
    percent=round(100*(completed+fraction)/len(stores)) if stores else (100 if row['status']=='done' else 0)
    if row['status'] in ACTIVE: percent=min(99,percent)
    return dict(id=row['id'],status=row['status'],revision=p['revision'],at=row['at'],
                count=len(p['after']),total=len(stores),completed=completed,succeeded=done,failed=failed,
                percent=percent,platforms=p['platforms'],stores=stores,error=row['error'],
                saved=bool(p.get('saved')))


def step(ws, model_fn, job):
    registry=Registry(ws.root); p=json.loads(job['payload']); result=json.loads(job['result'])
    model=model_fn()
    if revision(model.fee_rules)!=p['revision']:
        _write(registry,job['id'],'failed',p,result,'当前规则版本已变化，旧任务停止；请按新版本重新提交')
        return
    entries=result.setdefault('stores',{})
    sid=next((sid for sid in p['stores'] if entries.get(sid,{}).get('state') not in ('done','failed')),None)
    if sid:
        started=time.monotonic(); last=[0.0]
        entries[sid]={'state':'running','phase':'等待核算资源','percent':0}
        _write(registry,job['id'],'running',p,result)
        def report(phase,done=0,total=0):
            from .progress import work_percent
            if time.monotonic()-last[0]<.75:return
            last[0]=time.monotonic()
            entries[sid].update(phase=phase,percent=max(entries[sid].get('percent',0),work_percent(phase,done,total)))
            _write(registry,job['id'],'running',p,result)
        try:
            store=model.store(sid)
            if store.archived:raise ValueError('店铺已归档，未自动重算')
            output=service.recompute(ws,model,store,report=report,note='费项规则更新')
            if output.failure:raise ValueError(output.failure.get('why') or str(output.failure))
            if revision(model_fn().fee_rules)!=p['revision']:raise ValueError('核算期间规则已变化，请按新版本重新提交')
            entries[sid]={'state':'done','phase':'已完成核算','percent':100,
                          'run_ids':[r['run_id'] for r in output.periods],
                          'review_required':any(not r.get('can_close') for r in output.periods)}
        except Exception as exc:
            entries[sid]={'state':'failed','phase':'核算失败','percent':100,'error':str(exc)[:1500]}
        entries[sid]['seconds']=round(time.monotonic()-started,2)
    finished=all(entries.get(s,{}).get('state') in ('done','failed') for s in p['stores'])
    failures=any(s.get('state')=='failed' for s in entries.values())
    _write(registry,job['id'],('failed' if failures else 'done') if finished else 'queued',p,result,
           '部分店铺核算失败，可仅重试失败项' if finished and failures else '')


def retry(ws, model, jid):
    registry=Registry(ws.root)
    with lock, registry.transaction() as conn:
        row=conn.execute('SELECT * FROM job WHERE id=? AND kind=?',(jid,KIND)).fetchone()
        if not row:raise Conflict('任务不存在')
        if row['status']!='failed':raise Conflict('只有失败任务可以重试')
        p=json.loads(row['payload']); result=json.loads(row['result'])
        if revision(model.fee_rules)!=p['revision']:raise Conflict('规则已更新，不能重试旧版本')
        if conn.execute("SELECT 1 FROM job WHERE kind=? AND status IN ('preparing','queued','running')",(KIND,)).fetchone():
            raise Conflict('已有费项重算任务，请等待完成')
        result.setdefault('attempts',[]).append({'at':now(),'stores':result.get('stores',{}),'error':row['error']})
        result['stores']={sid:r for sid,r in result.get('stores',{}).items() if r['state']=='done'}
        conn.execute("UPDATE job SET status='queued',result=?,error='' WHERE id=?",(json_text(result),jid))
    return status(ws,jid)


def adopt_legacy(ws, model, config_id):
    """Explicit release recovery of a saved old synchronous request; no rule write."""
    from .model.schema import FeeRule
    registry=Registry(ws.root)
    with lock:
        record=ws.conn.execute("SELECT * FROM config_log WHERE kind='fee-rules' ORDER BY id DESC LIMIT 1").fetchone()
        if not record or record['id']!=config_id:raise Conflict('费项改动记录已变化，拒绝接管旧请求')
        def parsed(value):
            return tuple(FeeRule(**{k:v for k,v in r.items() if k in FeeRule.model_fields}) for r in json.loads(value))
        before,after=parsed(record['before_json']),parsed(record['after_json'])
        if revision(after)!=revision(model.fee_rules):raise Conflict('当前规则与旧请求不一致，未创建重算任务')
        jid=f'fee-legacy:{config_id}:{revision(after)[:12]}'
        with registry.connect() as conn:
            if conn.execute('SELECT 1 FROM job WHERE id=?',(jid,)).fetchone():return status(ws,jid)
            if conn.execute("SELECT 1 FROM job WHERE kind=? AND status IN ('preparing','queued','running')",(KIND,)).fetchone():raise Conflict('已有费项后台任务，未重复接管')
        platforms=affected(before,after,{s.platform for s in model.stores})
        known=set(ws.store_ids())
        targets=[s.id for s in model.stores if s.id in known and not s.archived and s.platform in platforms]
        payload=dict(before=rows(before),after=rows(after),revision=revision(after),saved=True,
                     legacy_config_id=config_id,stores=targets,platforms=sorted(platforms),actor='release-recovery',
                     note=f'接管已保存的费项改动 {config_id}，不重新写规则')
        with registry.transaction() as conn:
            conn.execute('INSERT INTO job VALUES(?,?,?,?,?,?,?,?)',
                         (jid,KIND,'release-recovery',now(),'queued' if targets else 'done',json_text(payload),json_text({'stores':{}}),''))
    return status(ws,jid)
