"""Host-agent transport with fenced leases and independently accepted results."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sys
import threading
from dataclasses import asdict,is_dataclass

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'host-agent'))
from finance_agent.control_plane import ControlPlaneClient,ControlPlaneError
from finance_agent.models import JobStage


class Control(ControlPlaneClient):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        # AgentState owns a thread-affine SQLite connection. Renewal uses
        # cached credentials and HTTP, never that connection.
        self.worker_id=self.state.get_secret('agent_id')
        self.service_token=self.config.access_token or self.state.get_secret('access_token')
        self.lease=None
        self.job_key=None
        self.accepted_terminal=set()
        self.accepted_handoffs={}
        self.status='online'
        self.configuration_sha=None
        self.configuration=None
        self.state.connection.execute('CREATE TABLE IF NOT EXISTS protocol_rejections(event_key TEXT PRIMARY KEY,endpoint TEXT NOT NULL,status INTEGER NOT NULL,detail TEXT NOT NULL)')
        self.state.connection.commit()

    def _headers(self,enrollment=False):
        if enrollment or not self.service_token:
            raise ControlPlaneError('A dedicated enrolled service identity is required')
        return {'Authorization':'Bearer '+self.service_token}

    def heartbeat(self,status=None):
        if status is not None:self.status=status
        lease=dict(self.lease) if self.lease else None
        payload={'agent_id':self.worker_id,'status':self.status,'leases':[lease] if lease else []}
        if self.configuration_sha:payload['configuration_sha']=self.configuration_sha
        if self.configuration:payload['configuration']=self.configuration
        response=self.client.post('/api/v1/agents/heartbeat',headers=self._headers(),json=payload)
        self._raise(response);body=response.json()
        if lease and lease['job_id'] in body.get('rejected',[]):
            raise ControlPlaneError('The server rejected this worker lease')
        return body

    def queue_progress(self,job_id,sequence,stage,message,detail=None):
        if not self.lease or self.lease['job_id']!=job_id:
            raise ControlPlaneError('No active lease for progress')
        return super().queue_progress(job_id,sequence,stage,message,
            {**(detail or {}),'lease_token':self.lease['lease_token']})

    def queue_terminal(self,job_id,success,payload):
        if not self.lease or self.lease['job_id']!=job_id:
            raise ControlPlaneError('No active lease for terminal receipt')
        return super().queue_terminal(job_id,success,{**payload,**self.lease,
            'worker_idempotency_key':self.job_key})

    def flush_events(self):
        sent=0
        for event in self.state.pending_events():
            response=self.client.post(event['endpoint'],headers={**self._headers(),
                'Idempotency-Key':event['event_key']},json=event['payload'])
            if response.status_code==409:
                # A stale delivery is rejected, never a completed business job.
                # Persist the rejection so one old event cannot wedge all jobs.
                try:detail=response.json().get('detail')
                except ValueError:detail='unreadable rejection'
                self.state.connection.execute('INSERT OR IGNORE INTO protocol_rejections VALUES(?,?,?,?)',
                    (event['event_key'],event['endpoint'],409,json.dumps(detail,ensure_ascii=False)))
                self.state.mark_event_sent(event['id'])
                if event['endpoint'].endswith(('/complete','/fail')):self.lease=None
                if event['endpoint'].endswith('/complete') and isinstance(detail,dict) and detail.get('issues'):
                    payload=event['payload'];job_id=payload.get('job_id')
                    if job_id:
                        self.state.queue_event(job_id+':checker-rejected',event['endpoint'].removesuffix('/complete')+'/fail',
                            {'lease_token':payload['lease_token'],'input_sha':payload['input_sha'],
                             'error_code':'CheckerRejectedCompletion','message':'Independent verification did not pass',
                             'retryable':True,'result':{}})
                continue
            self._raise(response)
            if event['payload'].get('tool_outcomes'):
                accepted=response.json()
                if accepted.get('ok') is not True or (accepted.get('job') or {}).get('state') not in {'waiting_source','waiting_maintenance'}:
                    raise ControlPlaneError('Missing server source-handoff receipt')
                self.accepted_handoffs[event['payload'].get('job_id')]=accepted['job']
            if event['endpoint'].endswith('/complete'):
                try:accepted=response.json()
                except ValueError:accepted={}
                if accepted.get('ok') is not True or (accepted.get('job') or {}).get('verification',{}).get('allowed') is not True:
                    raise ControlPlaneError('Missing independently verified completion receipt')
            self.state.mark_event_sent(event['id']);sent+=1
            if event['endpoint'].endswith(('/complete','/fail')):self.lease=None
            if event['endpoint'].endswith('/complete'):
                payload=event['payload'];key=payload.get('worker_idempotency_key')
                job_id=payload.get('job_id')
                if key and job_id:
                    self.state.mark_completed(key,job_id,payload.get('result') or {})
                    self.accepted_terminal.add(job_id)
        return sent


class Worker:
    def __init__(self,runtime,control,catalog,*,renew_seconds=30):
        self.runtime,self.control,self.catalog=runtime,control,catalog
        self.renew_seconds=renew_seconds
        config=getattr(runtime,'config',None)
        provider=getattr(catalog,'configuration_sha',None)
        # Only a one-way digest leaves this process; never transmit configured
        # credentials. Same config across restarts retains the same epoch.
        runtime_raw=json.dumps(asdict(config) if is_dataclass(config) else None,sort_keys=True,default=str).encode()
        control.configuration={'runtime_sha':hashlib.sha256(runtime_raw).hexdigest(),
            'source_sha':provider() if callable(provider) else hashlib.sha256(b'unknown-source-profile').hexdigest()}
        raw=json.dumps(control.configuration,sort_keys=True,separators=(',',':')).encode()
        control.configuration_sha=hashlib.sha256(raw).hexdigest()

    def _diagnostic(self,case,error_code):
        control=self.control;folder=control.state.state_dir/'worker-diagnostics'
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        raw=json.dumps({'schema':'ledger-worker-diagnostic.v1','case_id':case['id'],'store_id':case['store_id'],
            'period':case['period'],'input_sha':case['input_sha'],'source_context_sha':case.get('source_context_sha'),
            'error_code':error_code,'financial_applied':False},sort_keys=True).encode()
        sha=hashlib.sha256(raw).hexdigest();path=folder/(sha+'.json')
        if not path.exists():path.write_bytes(raw);path.chmod(0o600)
        return {'tool_id':'worker_runtime','source_kind':'system','reason_code':'technical_failure',
            'artifact_refs':[{'path':str(path),'sha256':sha}]}

    def once(self):
        control=self.control
        control.heartbeat();control.flush_events()
        job=control.claim_job()
        if job is None:return None
        captured=job.payload.get('automation_case')
        if not isinstance(captured,dict) or not all(captured.get(k) for k in ('id','store_id','period','input_sha')):
            raise ControlPlaneError('The server returned an incomplete case scope')
        case={**captured,'scope':job.payload.get('scope','business'),'skills':job.payload.get('skills',['evidence'])}
        control.lease={'job_id':job.id,'lease_token':job.payload['lease_token'],'input_sha':case['input_sha']}
        control.job_key=job.idempotency_key
        control.status='busy'
        stop=threading.Event();lease_failed=threading.Event()
        def renew():
            while not stop.wait(self.renew_seconds):
                try:control.heartbeat()
                except Exception:lease_failed.set();return
        thread=threading.Thread(target=renew,daemon=True);thread.start()
        try:
            def checker():
                if lease_failed.is_set():return {'verified':False,'lease_active':False,'status':'lease_lost'}
                response=control.client.get('/api/automation/months/'+case['period'],headers=control._headers())
                control._raise(response);data=response.json()
                row=next((r for r in data.get('stores',[]) if r.get('store_id')==case['store_id']),{})
                verification=row.get('verification') or {}
                source_current=not case.get('source_context_sha') or row.get('source_context_sha')==case['source_context_sha']
                return {'verified':verification.get('allowed') is True and row.get('input_sha')==case['input_sha'] and source_current,'lease_active':source_current,
                    'input_sha':row.get('input_sha'),'status':row.get('status','unknown'),
                    'stage':row.get('stage','unknown')}
            def trusted_outcomes():
                provider=getattr(self.catalog,'trusted_outcomes',None)
                if not callable(provider):return []
                value=provider(case)
                return value if isinstance(value,list) else []
            outcomes=trusted_outcomes()
            cached=control.state.completed_result(job.idempotency_key)
            if outcomes and not checker()['verified']:
                # A prior trusted local receipt can survive a process restart
                # before terminal delivery. Do not buy another model run merely
                # to rediscover the same unavailable/login/missing source.
                result={'state':'waiting_source_receipt','model_used':False}
            elif cached is not None and checker()['verified']:
                result=cached
            else:
                control.queue_progress(job.id,1,JobStage.CLAIMED,'已认领案件，正在自动取证')
                control.flush_events()
                result=asyncio.run(self.runtime.run(case,self.catalog.tools(checker),checker))
            outcomes=trusted_outcomes()
            from .runtime import Runtime as TrustedRuntime
            if not outcomes and isinstance(self.runtime,TrustedRuntime) and result.get('state') in {'maintenance','waiting_configuration'}:
                # These states are emitted by the frozen Python SDK wrapper,
                # not parsed from the model's explanation or response text.
                outcomes=[self._diagnostic(case,'SDKFrameworkFailure' if result['state']=='maintenance' else 'SDKConfigurationUnavailable')]
            stop.set();thread.join(timeout=2)
            if lease_failed.is_set():result={'state':'maintenance','error_code':'LeaseRenewalFailed'}
            success=result.get('state')=='verified' and checker()['verified']
            envelope={'result':result}
            if not success:
                envelope.update(error_code=result.get('error_code') or result.get('reason') or result.get('state','unverified'),
                    message='独立Checker尚未证明任务完成，保留案件与证据等待后续处理',
                    retryable=not lease_failed.is_set())
                if outcomes and not lease_failed.is_set():
                    envelope.update(result={},message='',error_code='TrustedSourceHandoff',tool_outcomes=outcomes,retryable=False)
            control.queue_terminal(job.id,success,envelope);control.flush_events()
            accepted=control.accepted_handoffs.get(job.id)
            if accepted:result={'state':accepted['state'],'handoff':accepted['waiting'],'model_used':result.get('model_used')}
            control.heartbeat('degraded' if result.get('state') in {'maintenance','waiting_configuration','waiting_maintenance'} else 'online')
            return result
        except Exception as exc:
            try:
                # This diagnostic is produced by the local Python executor,
                # never copied from a model narrative or its claimed outcome.
                outcome=self._diagnostic(case,type(exc).__name__)
                control.queue_terminal(job.id,False,{'result':{},
                    'error_code':'TrustedSourceHandoff','message':'',
                    'retryable':False,'tool_outcomes':[outcome] if not lease_failed.is_set() else []})
                control.flush_events()
            except Exception:
                pass  # Durable protocol outbox is retried on the next run.
            try:control.heartbeat('degraded')
            except Exception:pass
            raise
        finally:
            stop.set();thread.join(timeout=2);control.lease=None;control.job_key=None
