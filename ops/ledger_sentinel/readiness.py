"""Collected, identity-bound closing-cycle observations. Flags do not count."""
from contextlib import closing
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import time

from .executor import atomic_json
from .probes import file_sha, timestamp
from .state import digest, now

REQUIRED_OBSERVATIONS = frozenset({'amount_conservation','frozen_history_unchanged','scope_isolation',
    'required_outputs','public_readback','rollback_drill','worker_health'})
IDENTITY_FIELDS=('phase','period','code_sha','source_code_sha','model_sha','input_sha','acceptance_receipt_sha')


def _identity(body):
    if body.get('phase') not in {'shadow','canary'} or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',body.get('period','')):
        raise ValueError('A shadow/canary business period is required')
    for field,size in [('code_sha',40),('source_code_sha',64),('model_sha',64),('input_sha',64),('acceptance_receipt_sha',64)]:
        if not re.fullmatch('[0-9a-f]{'+str(size)+'}',body.get(field,'')):
            raise ValueError('Observation lacks immutable identity: '+field)
    return {key:body[key] for key in IDENTITY_FIELDS}


def _month(value):
    year,month=map(int,value.split('-'));return year*12+month


class Readiness:
    def __init__(self,state,executor,*,minimum_seconds=86400,minimum_cycles=2,workspace_root=None,input_bundle_root=None):
        if (not isinstance(minimum_seconds,(int,float)) or not math.isfinite(minimum_seconds)
                or minimum_seconds<60 or type(minimum_cycles) is not int or minimum_cycles<2):
            raise ValueError('Observation gates cannot be disabled')
        self.state=state;self.executor=executor
        self.minimum_seconds=minimum_seconds;self.minimum_cycles=minimum_cycles
        self.workspace_root=Path(workspace_root).resolve() if workspace_root else None
        self.input_bundle_root=Path(input_bundle_root).resolve() if input_bundle_root else None

    def _inputs(self,reference,identity,semantic_model_sha):
        if not self.input_bundle_root or not isinstance(reference,dict):
            raise ValueError('Virtual feed sources require a captured original input bundle')
        name=reference.get('ref','')
        if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,95}',name):raise ValueError('Invalid input evidence reference')
        bundle=(self.input_bundle_root/name).resolve()
        if not bundle.is_relative_to(self.input_bundle_root):raise ValueError('Input evidence reference escapes its trusted directory')
        from ledger import freight_experiments_inputs as frozen
        try:inputs,fingerprint=frozen.verify(bundle)
        except frozen.InputError as exc:raise ValueError('Captured input bundle verification failed') from exc
        model=json.loads((bundle/'model.json').read_text())
        if (fingerprint!=reference.get('fingerprint') or inputs.get('period')!=identity['period']
                or frozen.digest(model)!=inputs.get('model_hash')):raise ValueError('Captured input bundle scope/model/fingerprint mismatch')
        if digest(model)!=semantic_model_sha:
            raise ValueError('Captured input model bytes do not match their semantic identity')
        objects=inputs.get('feed_manifest',{}).get('objects',{})
        if (frozen.REQUIRED_OBJECTS-set(objects) or inputs.get('snapshot_id')!=inputs.get('feed_manifest',{}).get('snapshot_id')
                or type(inputs.get('consumed_seq')) is not int or inputs['consumed_seq']!=inputs.get('source_latest_seq')
                or type(inputs['feed_manifest'].get('through_seq')) is not int
                or not 0<=inputs['feed_manifest']['through_seq']<=inputs['consumed_seq']):
            raise ValueError('Captured input bundle lacks a complete feed snapshot/watermark')
        import polars as pl
        for name,meta in objects.items():
            if not re.fullmatch(r'[A-Za-z0-9_.-]+\.parquet',name):raise ValueError('Invalid captured upstream object path')
            file=bundle/'feed'/name
            if (not file.is_file() or file_sha(file)!=meta.get('sha256') or file.stat().st_size!=meta.get('bytes')
                    or pl.scan_parquet(file).select(pl.len()).collect().item()!=meta.get('rows')):
                raise ValueError('Captured upstream original object is missing or changed')
        deltas=json.loads((bundle/'deltas.json').read_text())
        if (frozen.digest(deltas)!=inputs.get('delta_hash')
                or any(type(row.get('seq')) is not int or not inputs['feed_manifest']['through_seq']<row['seq']<=inputs['consumed_seq'] for row in deltas)):
            raise ValueError('Captured upstream delta identity mismatch')
        return inputs,{'ref':reference['ref'],'fingerprint':fingerprint,'bundle_manifest_sha256':file_sha(bundle/'bundle.json')}

    def _close(self,identifier,identity,first_at,last_at,semantic_model_sha,input_evidence=None):
        """Read existing human authorization and sealed sources; never close a ledger."""
        if self.workspace_root is None:raise ValueError('Trusted local workspace_root is required for actual close evidence')
        root=self.workspace_root;database=root/'workspace.db'
        if not database.is_file():raise ValueError('Actual close evidence is unavailable')
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            db.row_factory=sqlite3.Row
            receipt=db.execute('SELECT * FROM automation_receipt WHERE id=?',(identifier,)).fetchone()
            if not receipt or receipt['action']!='close.confirm':raise ValueError('An actual close.confirm receipt is required')
            authorization=db.execute('SELECT * FROM automation_authorization WHERE id=?',(receipt['authorization_id'],)).fetchone()
            if (not authorization or authorization['identity_kind']!='human' or not authorization['actor']
                    or authorization['action']!='close.confirm' or authorization['input_sha']!=identity['input_sha']):
                raise ValueError('Close authorization does not match this human-approved input')
            result=json.loads(receipt['result_json']);scope=json.loads(authorization['scope_json'])
            if result.get('receipt_id')!=identifier or authorization['request_id']!=receipt['request_id']:
                raise ValueError('Close receipt/authorization request identity mismatch')
            if (not first_at<=timestamp(receipt['at'])<=last_at or scope.get('period')!=identity['period']
                    or result.get('period')!=identity['period']):
                raise ValueError('Close receipt is outside the collected observation window/period')
            completed=result.get('completed') or []
            if result.get('status')!='completed' or result.get('unfinished') or not completed:
                raise ValueError('A partial or empty close batch cannot complete an observed cycle')
            if {r['store_id'] for r in completed}!=set(scope.get('store_ids') or []):
                raise ValueError('Close batch does not cover the authorized scope')
            proposal=db.execute('SELECT * FROM automation_proposal WHERE id=?',(result.get('batch_id'),)).fetchone()
            if (not proposal or proposal['kind']!='close' or proposal['proposal_sha']!=authorization['proposal_sha']
                    or proposal['input_sha']!=identity['input_sha'] or proposal['actor']!=authorization['actor']):
                raise ValueError('Close preview/authorization binding is invalid')
            preview=json.loads(proposal['payload_json'])
            if (proposal['period']!=identity['period'] or not first_at<=timestamp(proposal['at'])<=timestamp(receipt['at'])
                    or not first_at<=timestamp(authorization['at'])<=timestamp(receipt['at'])):
                raise ValueError('Human close preview/authorization was not collected within this cycle')
            company_scope={row['store_id'] for row in (preview.get('overhead') or {}).get('basis',[])}
            if not company_scope or company_scope!={r['store_id'] for r in completed} or preview.get('blocked'):
                raise ValueError('Observed close must cover the complete approved company operating scope')
            refs=[];proofs=[];captured=None;captured_identity=None
            from ledger.storage_integrity import digest as artifact_digest, verified, registered_archive_link
            for row in completed:
                period=db.execute('SELECT state,run_id FROM period WHERE store_id=? AND period=?',(row['store_id'],identity['period'])).fetchone()
                proof=db.execute('SELECT * FROM automation_run_proof WHERE run_id=?',(row['run_id'],)).fetchone()
                if not period or period['state']!='closed' or period['run_id']!=row['run_id'] or not proof:
                    raise ValueError('Current closed state/run proof does not match the close receipt')
                manifest=json.loads(proof['manifest_json'])
                if (manifest.get('period')!=identity['period'] or manifest.get('store_id')!=row['store_id']
                        or manifest.get('engine')!=identity['code_sha'] or manifest.get('source_code_sha')!=identity['source_code_sha']
                        or manifest.get('model_sha')!=semantic_model_sha):
                    raise ValueError('Close run code/model/scope does not match this observation')
                artifacts=manifest.get('artifacts') or [];sources=set(manifest.get('source_refs') or [])
                slots=manifest.get('slots')
                if not artifacts or not sources or not isinstance(slots,list):raise ValueError('Close lacks sealed run/original source evidence')
                originals={slot[2] for slot in slots};virtual=sources-originals
                if virtual:
                    if captured is None:captured,captured_identity=self._inputs(input_evidence,identity,semantic_model_sha)
                    feed=manifest.get('feed') or {}
                    if (feed.get('snapshot_id')!=captured['snapshot_id'] or feed.get('consumed_seq')!=captured['consumed_seq']
                            or feed.get('source_latest_seq')!=captured['source_latest_seq']
                            or not {tuple(s) for s in slots}<={tuple(s[k] for k in ('store_id','name','sha')) for s in captured['slots']}):
                        raise ValueError('Closed feed identity/slots differ from captured original inputs')
                    refs.append({'kind':'derived_feed','source_shas':sorted(virtual),
                        'snapshot_id':feed['snapshot_id'],'consumed_seq':feed['consumed_seq']})
                for artifact in artifacts:
                    path=root/artifact['path'];expected=artifact['sha']
                    allowed=path.resolve().is_relative_to(root) or registered_archive_link(root,path,expected)
                    if (not allowed or not path.is_file() or artifact_digest(path)!=expected
                            or (artifact.get('sealed',True) and not verified(path))):
                        raise ValueError('Close sealed evidence is missing or changed')
                    refs.append({'path':artifact['path'],'sha256':expected,'kind':'sealed_run'})
                for sha in originals:
                    if not re.fullmatch('[0-9a-f]{64}',sha):raise ValueError('Invalid original evidence SHA')
                    path=root/'files'/sha[:2]/sha
                    if not path.is_file() or file_sha(path)!=sha:raise ValueError('Close original source is missing or changed')
                    refs.append({'path':path.relative_to(root).as_posix(),'sha256':sha,'kind':'original'})
                proofs.append({'run_id':row['run_id'],'sha256':digest(dict(proof))})
        return {'receipt_id':identifier,'receipt_sha256':digest(dict(receipt)),
            'authorization_sha256':digest(dict(authorization)),'proposal_sha256':digest(dict(proposal)),
            'closed_at':receipt['at'],'run_proofs':proofs,'source_refs':refs,'input_evidence':captured_identity}

    def capture(self,evidence_path):
        """Retain one sample now. Sentinel owns its collection clock."""
        path=Path(evidence_path).resolve();body=json.loads(path.read_text(encoding='utf-8'))
        if body.get('schema')!=2:raise ValueError('Collected observation sample schema 2 is required')
        identity=_identity(body);kind=body.get('measurement_kind')
        phase_started=None
        if kind not in {'actual','synthetic'}:raise ValueError('Explicit actual/synthetic measurement_kind is required')
        if kind=='actual' and (body.get('synthetic') is True or body.get('fixture_kind')):
            raise ValueError('Synthetic fixture metadata cannot count as an actual observation')
        if kind=='actual':
            plan=self.executor.plans.get(body.get('action_id'))
            if not plan or plan.get('kind')!='deployment':raise ValueError('Actual sample requires configured acceptance context')
            receipt_path=(path.parent/body['acceptance_receipt']).resolve()
            receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
            self.executor._accept(plan,receipt,identity['code_sha'])
            if (receipt['receipt_sha']!=identity['acceptance_receipt_sha'] or receipt['model_sha']!=identity['model_sha']
                    or receipt.get('source_code_sha')!=identity['source_code_sha']):
                raise ValueError('Sample acceptance receipt/model identity mismatch')
            from ledger.model.loader import load_model
            semantic=digest(load_model(Path(plan['model_root'])).model_dump(mode='json'))
            if body.get('semantic_model_sha')!=semantic:raise ValueError('Sample semantic model differs from accepted model')
            flag=json.loads((self.state.home/'rollout.json').read_text(encoding='utf-8'))
            if (flag.get('phase')!=identity['phase'] or flag.get(identity['phase']+'_enabled') is not True
                    or any(flag.get(key)!=identity[key] for key in ('code_sha','source_code_sha','model_sha','acceptance_receipt_sha'))):
                raise ValueError('Sample does not match the actually enabled rollout phase/identity')
            phase_started=timestamp(flag['changed_at'])
        checks=body.get('checks') or []
        if len(checks)!=len(REQUIRED_OBSERVATIONS) or {c.get('name') for c in checks}!=REQUIRED_OBSERVATIONS:
            raise ValueError('Sample is missing required checks')
        collected=time.time();stamp=now();retained=[];source_ids=[]
        for check in checks:
            artifact=(path.parent/check['artifact']).resolve()
            if not artifact.is_relative_to(path.parent) or not artifact.is_file() or file_sha(artifact)!=check['sha256']:
                raise ValueError('Sample artifact is missing, outside the evidence directory or changed')
            evidence=json.loads(artifact.read_text(encoding='utf-8'))
            if (evidence.get('schema')!=2 or evidence.get('name')!=check['name']
                    or evidence.get('measurement_kind')!=kind or _identity(evidence)!=identity):
                raise ValueError('Structured check identity differs from its observation')
            if kind=='actual' and (evidence.get('synthetic') is True or evidence.get('fixture_kind')):
                raise ValueError('Synthetic check evidence cannot count as an actual observation')
            if abs(timestamp(evidence['observed_at'])-collected)>120:
                raise ValueError('Sample evidence is stale; historical replay is not an observed cycle')
            if phase_started is not None and timestamp(evidence['observed_at'])<phase_started:
                raise ValueError('Sample evidence predates the enabled rollout phase')
            source=evidence.get('source_observation_id')
            if not isinstance(source,str) or not 1<=len(source)<=128:raise ValueError('Check lacks a source observation identity')
            result=evidence.get('result') or {}
            if (result.get('status')!='passed' or type(result.get('checked')) is not int or result['checked']<1
                    or type(result.get('failures')) is not int or result['failures']!=0
                    or type(result.get('unknown')) is not int or result['unknown']!=0):
                raise ValueError('A failed, empty or unknown sample does not pass')
            if any(type(result.get(key)) is not int or result[key]!=0 for key in ('errors','skipped')):
                raise ValueError('Errored or skipped evidence cannot pass an observation check')
            if check['name']=='amount_conservation':
                try:zero=Decimal(str(result['difference']))==0
                except (KeyError,InvalidOperation):zero=False
                if not zero:raise ValueError('Amount difference must be exactly zero')
            retained.append({'name':check['name'],'sha256':check['sha256'],'source':artifact,'source_observation_id':source})
            source_ids.append(source)
        sample={**identity,'schema':2,'measurement_kind':kind,'collected_at':stamp,
            'semantic_model_sha':body.get('semantic_model_sha'),'close_receipt_id':body.get('close_receipt_id'),
            'input_evidence':body.get('input_evidence'),
            'source_observation_ids':sorted(set(source_ids)),'checks':[{k:v for k,v in c.items() if k!='source'} for c in retained]}
        identifier=digest(sample);destination=self.state.home/'rollout-samples'/identifier
        destination.mkdir(parents=True,exist_ok=True)
        for index,check in enumerate(retained):
            target=destination/f'{index:02d}.json'
            if not target.exists():shutil.copyfile(check['source'],target)
            if file_sha(target)!=check['sha256']:raise ValueError('Sample changed during retention')
            sample['checks'][index]['path']=target.relative_to(self.state.home).as_posix()
        sample['sample_sha']=digest(sample)
        with self.state.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT body FROM readiness_sample'):
                prior=json.loads(row[0])
                if prior['period']!=identity['period'] and (prior['input_sha']==identity['input_sha']
                        or set(prior['source_observation_ids'])&set(source_ids)):
                    raise ValueError('The same input/source evidence cannot count as another period')
            db.execute('INSERT OR IGNORE INTO readiness_sample VALUES(?,?,?)',(identifier,stamp,json.dumps(sample,ensure_ascii=False)))
        self.state.event('rollout_sample',identifier,'captured',{'phase':identity['phase'],'period':identity['period'],'measurement_kind':kind})
        return identifier

    def _sample(self,identifier):
        with self.state.connect() as db:row=db.execute('SELECT body FROM readiness_sample WHERE id=?',(identifier,)).fetchone()
        if not row:raise ValueError('Unknown collected sample')
        sample=json.loads(row[0])
        if digest({k:v for k,v in sample.items() if k!='sample_sha'})!=sample.get('sample_sha'):
            raise ValueError('Collected sample changed')
        for check in sample['checks']:
            path=(self.state.home/check['path']).resolve()
            if not path.is_relative_to(self.state.home.resolve()) or not path.is_file() or file_sha(path)!=check['sha256']:
                raise ValueError('Retained check evidence is missing or changed')
        return sample

    def record(self,evidence_path):
        body=json.loads(Path(evidence_path).read_text(encoding='utf-8'))
        if body.get('schema')!=2:raise ValueError('Legacy self-reported cycles are not collected observation evidence')
        ids=body.get('sample_ids') or []
        if len(set(ids))!=len(ids) or len(ids)<2:raise ValueError('At least two distinct collected samples are required')
        samples=sorted((self._sample(i) for i in ids),key=lambda s:timestamp(s['collected_at']))
        first,last=samples[0],samples[-1];identity=_identity(first);seen=set()
        if any(_identity(s)!=identity or s['measurement_kind']!=first['measurement_kind'] for s in samples):
            raise ValueError('Collected samples do not share one scope/identity')
        for sample in samples:
            sources=set(sample['source_observation_ids'])
            if sources&seen:raise ValueError('Repeated source events are not independent observation samples')
            seen.update(sources)
        start,finish=timestamp(first['collected_at']),timestamp(last['collected_at'])
        if finish-start<self.minimum_seconds:raise ValueError('Collected observation duration is incomplete')
        actual=first['measurement_kind']=='actual';close=None
        if actual:
            if not last.get('close_receipt_id'):raise ValueError('Actual observation requires the collected human close receipt')
            close=self._close(last['close_receipt_id'],identity,start,finish,last['semantic_model_sha'],last.get('input_evidence'))
        observation={**identity,'schema':2,'measurement_kind':first['measurement_kind'],'sample_ids':ids,
            'started_at':first['collected_at'],'finished_at':last['collected_at'],'sample_count':len(ids),
            'close_evidence':close,'semantic_model_sha':last.get('semantic_model_sha'),
            'input_evidence':last.get('input_evidence'),
            'source_observation_ids':sorted(seen)}
        identifier=digest(observation);observation['source_receipt_sha']=identifier
        with self.state.connect() as db:
            db.execute('INSERT OR IGNORE INTO readiness_observation VALUES(?,?,?,?,?,?)',
                (identifier,identity['phase'],identity['period'],digest([identity['code_sha'],identity['model_sha']]),now(),json.dumps(observation,ensure_ascii=False)))
        self.state.event('rollout_observation',identifier,'recorded' if actual else 'synthetic_only',
            {'phase':identity['phase'],'period':identity['period'],'measurement_kind':first['measurement_kind']})
        return identifier

    def assess(self,phase,code_sha,model_sha,*,include_synthetic=False,acceptance_receipt_sha=None):
        if phase not in {'shadow','canary'}:raise ValueError('Only observed rollout phases may be assessed')
        with self.state.connect() as db:
            rows=[json.loads(r[0]) for r in db.execute('SELECT body FROM readiness_observation WHERE identity_sha=?',
                (digest([code_sha,model_sha]),))]
        valid=[];invalid=0;synthetic=0
        for row in rows:
            try:
                if row.get('schema')!=2 or digest({k:v for k,v in row.items() if k!='source_receipt_sha'})!=row['source_receipt_sha']:
                    raise ValueError('Observation identity changed')
                for identifier in row['sample_ids']:self._sample(identifier)
                if acceptance_receipt_sha and row['acceptance_receipt_sha']!=acceptance_receipt_sha:continue
                if row['measurement_kind']=='synthetic':
                    synthetic+=1
                    if not include_synthetic:continue
                else:
                    proof=self._close(row['close_evidence']['receipt_id'],_identity(row),timestamp(row['started_at']),
                        timestamp(row['finished_at']),row['semantic_model_sha'],row.get('input_evidence'))
                    if proof!=row['close_evidence']:raise ValueError('Close evidence changed')
                valid.append(row)
            except (KeyError,OSError,ValueError,sqlite3.Error):invalid+=1
        if valid:
            latest=max(valid,key=lambda row:timestamp(row['finished_at']))
            valid=[r for r in valid if (r['source_code_sha'],r['acceptance_receipt_sha'])==
                (latest['source_code_sha'],latest['acceptance_receipt_sha'])]
        selected=[r for r in valid if phase=='canary' or r['phase']=='shadow']
        periods=sorted({r['period'] for r in selected});required=1 if phase=='shadow' else self.minimum_cycles
        tail=periods[-required:]
        adjacent=len(tail)>=required and all(_month(b)-_month(a)==1 for a,b in zip(tail,tail[1:]))
        latest_canary=bool(periods and any(r['period']==periods[-1] and r['phase']=='canary' for r in selected))
        ready=adjacent and (phase=='shadow' or latest_canary)
        return {'status':('simulated_ready' if include_synthetic else 'ready') if ready else 'blocked',
            'phase':phase,'code_sha':code_sha,'model_sha':model_sha,'observed_periods':periods,'required_cycles':required,
            'invalid_observation_count':invalid,'synthetic_observation_count':synthetic,
            'missing_cycles':max(0,required-len(periods)),'latest_period_has_canary':latest_canary,
            'notice':'仅实际采集且含本轮人工结账凭据的相邻周期计入；合成、旧闭账回放及开关均不计入。'}

    def promote(self,phase,action_id,receipt,version):
        if phase not in {'shadow','canary','production'}:raise ValueError('Invalid rollout phase')
        plan=self.executor.plans.get(action_id)
        if not plan or plan.get('kind')!='deployment':raise ValueError('A configured deployment acceptance context is required')
        self.executor._accept(plan,receipt,version)
        if phase!='shadow':
            gate=self.assess('shadow' if phase=='canary' else 'canary',version,receipt['model_sha'],
                acceptance_receipt_sha=receipt['receipt_sha'])
            if gate['status']!='ready':raise ValueError('Actual observation cycles are incomplete')
        with self.state.acquire('ledger-maintenance',30):
            value={'phase':phase,'shadow_enabled':phase=='shadow','canary_enabled':phase=='canary',
                'production_enabled':phase=='production','code_sha':version,'model_sha':receipt['model_sha'],
                'source_code_sha':receipt.get('source_code_sha'),
                'acceptance_receipt_sha':receipt['receipt_sha'],'changed_at':now(),
                'financial_policy_changed':False,'observation_claimed_by_switch':False}
            atomic_json(self.state.home/'rollout.json',value)
            self.state.event('rollout_switch',phase,'enabled',value)
        return value
