"""Read-only probes. Missing evidence is unknown, never healthy by default."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import time
import urllib.request

from .state import now


def file_sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def timestamp(value):
    stamp=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if stamp.tzinfo is None:raise ValueError('Timestamp has no timezone')
    return stamp.timestamp()


def field(value,path):
    for part in path.split('.') if path else []:
        value=value[int(part)] if isinstance(value,list) else value[part]
    return value


def backup_manifest(path,*,expected_code=None,not_before=None,max_age=86400):
    path=Path(path).resolve();body=json.loads(path.read_text(encoding='utf-8'))
    completed=timestamp(body['completed_at'])
    if completed>time.time()+30 or time.time()-completed>max_age or (not_before is not None and completed<not_before):
        raise ValueError('Backup is stale or predates this operation')
    if expected_code is not None and body.get('code_sha')!=expected_code:
        raise ValueError('Backup version mismatch')
    files=body.get('files')
    if not isinstance(files,list) or not files:raise ValueError('Backup manifest has no artifacts')
    seen=set()
    for row in files:
        artifact=(path.parent/row['path']).resolve()
        if artifact in seen or not artifact.is_relative_to(path.parent) or not artifact.is_file():
            raise ValueError('Backup artifact missing, duplicated or outside backup directory')
        seen.add(artifact)
        if file_sha(artifact)!=row['sha256']:raise ValueError('Backup checksum failed')
    return body


class Probes:
    def __init__(self,config,fetch=None):
        self.config={row['id']:row for row in config}
        if len(self.config)!=len(config):raise ValueError('Duplicate probe IDs')
        self.fetch=fetch or self._fetch

    @staticmethod
    def _fetch(spec):
        import os
        headers={'Accept':'application/json'}
        for key,variable in spec.get('header_env',{}).items():
            if variable not in os.environ:raise ValueError('Missing configured service credential')
            headers[key]=os.environ[variable]
        request=urllib.request.Request(spec['url'],headers=headers,method='GET')
        with urllib.request.urlopen(request,timeout=spec.get('timeout_seconds',5)) as response:
            return json.loads(response.read(4*1024*1024).decode())

    def read(self,identifier,variables=None):
        stamp=now();variables=variables or {};metrics={}
        try:
            spec=self.config[identifier];kind=spec['kind']
            status,reason='unknown','missing_required_evidence'
            if kind in {'http_json','manager','feed','jst_session','worker'}:
                data=self.fetch(spec)
            elif kind=='json':
                data=json.loads(Path(spec['path']).read_text(encoding='utf-8'))
            if kind in {'http_json','json'}:
                assertions=spec.get('assertions') or []
                if not assertions:raise ValueError('Probe requires explicit assertions')
                for rule in assertions:
                    actual=field(data,rule['field']);wanted=rule.get('value')
                    if isinstance(wanted,str) and wanted.startswith('$'):
                        wanted=variables[wanted[1:]]
                        if wanted is None:raise ValueError('Expected probe identity is unavailable')
                    operation=rule.get('operator','eq')
                    if operation=='eq':passed=type(actual) is type(wanted) and actual==wanted
                    elif operation in {'gte','lte'}:
                        if isinstance(actual,bool) or not isinstance(actual,(int,float)) or not math.isfinite(actual):
                            raise ValueError('Probe metric is not finite numeric evidence')
                        passed=actual>=wanted if operation=='gte' else actual<=wanted
                    else:raise ValueError('Unknown assertion operator')
                    if not passed:
                        return {'id':identifier,'status':'failed','reason':'assertion_failed:'+rule['field'],'at':stamp,'metrics':metrics,'required_action':spec.get('required_action','')}
                if spec.get('timestamp_field'):
                    age=time.time()-timestamp(field(data,spec['timestamp_field']))
                    metrics['age_seconds']=age
                    if age< -30 or age>spec['max_age_seconds']:
                        raise ValueError('Probe observation is stale or from the future')
                status,reason='ok','assertions_passed'
            elif kind=='manager':
                pending=field(data,spec.get('pending_field','pending'))
                if spec.get('worker_probe'):
                    dependency=self.read(spec['worker_probe'],variables)
                    if dependency['status']!='ok':raise ValueError('Manager worker heartbeat is not verified')
                    alive=True
                else:alive=field(data,spec.get('alive_field','worker_alive'))
                if not isinstance(pending,list) or type(alive) is not bool:raise ValueError('Incomplete manager evidence')
                metrics={'pending':len(pending),'errors':sum(bool(r.get('error')) for r in pending)}
                status,reason=('failed','manager_worker_down') if not alive else ('failed','manager_errors') if metrics['errors'] else ('degraded','manager_backlog') if len(pending)>spec.get('max_pending',100) else ('ok','manager_running')
            elif kind=='feed':
                data=field(data,spec.get('field',''))
                consumed,latest=data['consumed_seq'],data['source_latest_seq']
                if not data['snapshot_id'] or type(consumed) is not int or type(latest) is not int:raise ValueError('Incomplete feed watermark')
                metrics={'consumed_seq':consumed,'source_latest_seq':latest,'lag':latest-consumed}
                status,reason=('failed','feed_error') if data.get('last_error') else ('degraded','feed_behind') if consumed<latest else ('ok','feed_caught_up')
                if consumed>latest:status,reason='failed','feed_watermark_invalid'
                age=time.time()-timestamp(data[spec.get('timestamp_field','last_success')])
                metrics['age_seconds']=age
                if age< -30 or age>spec.get('max_age_seconds',1800):raise ValueError('Feed observation is stale or from the future')
            elif kind in {'worker','jst_session'}:
                data=field(data,spec.get('field',''))
                observed=data[spec.get('timestamp_field','checked_at')]
                observed=float(observed) if spec.get('timestamp_unit')=='epoch_seconds' else timestamp(observed)
                if not math.isfinite(observed):raise ValueError('Heartbeat timestamp is not finite')
                age=time.time()-observed
                metrics['age_seconds']=age
                if age< -30 or age>spec.get('max_age_seconds',120):raise ValueError('Heartbeat is stale or from the future')
                if kind=='worker':
                    alive=data['alive']
                    if type(alive) is not bool:raise ValueError('Worker state is not explicit')
                    status,reason=('failed','worker_error') if not alive or data.get('last_error') or data.get('status') in {'error','failed','dead'} else ('ok','worker_alive')
                else:
                    authenticated=data['authenticated']
                    if type(authenticated) is not bool:raise ValueError('Authentication was not verified')
                    remaining=timestamp(data['expires_at'])-time.time();metrics['expires_in_seconds']=remaining
                    status,reason=('failed','jst_login_required') if not authenticated or remaining<=0 else ('degraded','jst_session_expiring') if remaining<spec.get('warn_before_seconds',1800) else ('ok','jst_authenticated')
            elif kind=='disk':
                usage=shutil.disk_usage(spec['path']);metrics={'free_bytes':usage.free,'total_bytes':usage.total}
                status,reason=('ok','disk_space_sufficient') if usage.free>=spec['min_free_bytes'] else ('failed','disk_space_low')
            elif kind=='sqlite':
                path=Path(spec['path']).resolve()
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=spec.get('timeout_seconds',3))) as db:
                    deadline=time.monotonic()+spec.get('timeout_seconds',3)
                    db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
                    rows=[r[0] for r in db.execute('PRAGMA quick_check')]
                    if rows!=['ok']:raise ValueError('Database quick_check failed')
                status,reason='ok','sqlite_quick_check_passed'
            elif kind=='backup':
                body=backup_manifest(spec['path'],max_age=spec.get('max_age_seconds',86400))
                metrics={'artifacts':len(body['files']),'completed_at':body['completed_at']}
                status,reason='ok','backup_verified'
            else:raise ValueError('Unknown probe kind')
            return {'id':identifier,'status':status,'reason':reason,'at':stamp,'metrics':metrics,'required_action':spec.get('required_action','')}
        except Exception as exc:
            # No raw HTTP bodies, headers, cookies or DSNs enter notifications.
            return {'id':identifier,'status':'unknown','reason':'probe_evidence_unavailable',
                'at':stamp,'metrics':metrics,'error_type':type(exc).__name__,
                'required_action':self.config.get(identifier,{}).get('required_action','补齐探针证据或检查只读连接')}

    def require(self,identifiers,variables=None):
        results=[self.read(identifier,variables) for identifier in identifiers]
        if not results or any(r['status']!='ok' for r in results):
            raise ValueError('Required operational conditions not verified: '+','.join(r['id']+':'+r['status'] for r in results))
        return results
