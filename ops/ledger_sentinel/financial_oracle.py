"""Host-owned service oracle; candidate outputs never sign their own acceptance.

Only code, frozen input bundles, approved seed/steps and an empty output volume
enter the child namespace. Expected financial values, the manifest and release authority
remain in the parent. Only approved scope keys cross the execution boundary. A financial proof is distinct from the eight release gates.
"""
from contextlib import closing
from decimal import Decimal
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zlib


ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).with_name('financial_oracle_child.py')
CHECKER = ROOT/'tools/verify-ledger-service-cases.py'
SCOPE_FIELDS = {'state','statement','commission_total','commission_people','confirmed_total',
    'confirmed_people','cost_line_total','allocation_pending_count','commission_complete','can_close','source_sync_pending'}
IMMUTABLE_TABLES = ('automation_authorization','automation_receipt','automation_decision','period_cost_input','manual_finance','run_labor','automation_overhead_close','payout_confirmation','settlement')
SERVICE_OPERATIONS=frozenset({'recompute','supplement','confirm','replace_scheme','drain_pending','close_approved','restore_approved_manual_finance'})
SHA = re.compile('[0-9a-f]{64}')


class OracleBlocked(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()


def file_sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def implementation_sha():
    # Parent comparator helpers and candidate protocol are version-bound too.
    paths=[Path(__file__),CHILD,CHECKER,*sorted((ROOT/'ledger/ledger').rglob('*.py'))]
    return digest([{'path':p.relative_to(ROOT).as_posix(),'sha256':file_sha(p)} for p in paths])


def _read(path, limit=16*1024*1024):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:raise OracleBlocked('oracle_material_invalid')
    return json.loads(path.read_bytes(),parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))


def _reference(base, reference):
    if not isinstance(reference,dict) or not SHA.fullmatch(str(reference.get('sha256',''))):raise OracleBlocked('oracle_reference_missing')
    path=Path(reference.get('path',''));path=path if path.is_absolute() else base/path
    if path.is_symlink() or not path.is_file() or file_sha(path)!=reference['sha256']:raise OracleBlocked('oracle_reference_changed')
    return path.resolve()


def _manual_input(case, step, bundle, *, fixture=False):
    """Bind documentary approval to a sealed source, never parent expected data."""
    fields={'path','sha256','source_run_id','decision_sha256','original_result_sha256','decision_at'}
    reference=step.get('reference')
    if type(step.get('cost_input_only',False)) is not bool:raise OracleBlocked('manual_cost_input_mode_invalid')
    if (step.get('approval_reference')!=case['approval_reference'] or 'fixture' in step
        or not isinstance(reference,dict) or set(reference)!=fields
        or type(reference.get('source_run_id')) is not int or reference['source_run_id']<=0
        or any(not SHA.fullmatch(str(reference.get(k,''))) for k in ('sha256','decision_sha256','original_result_sha256'))
        or not isinstance(reference.get('decision_at'),str) or not reference['decision_at']):
        raise OracleBlocked('manual_finance_approval_invalid')
    path=_reference(bundle,reference)
    if not Path(reference['path']).is_absolute() or not path.is_relative_to(bundle.resolve()):
        raise OracleBlocked('manual_finance_source_outside_bundle')
    body=_read(path,limit=32*1024*1024)
    method='declared_synthetic_manual_decisions' if fixture else 'sqlite_mode_ro_query_only_transaction'
    if (body.get('method')!=method or body.get('no_engine_result_amounts_read') is not True
        or not isinstance(body.get('rows'),list) or any(not isinstance(row,dict) for row in body['rows'])):
        raise OracleBlocked('manual_finance_source_invalid')
    rows=[row for row in body['rows'] if row.get('store_id')==step.get('store_id') and row.get('period')==step.get('period')
          and type(row.get('run_id')) is int and row['run_id']==reference['source_run_id']]
    if len(rows)!=1:raise OracleBlocked('manual_finance_source_identity_mismatch')
    row=rows[0];raw=row.get('decision_json')
    if (not isinstance(raw,str) or hashlib.sha256(raw.encode()).hexdigest()!=reference['decision_sha256']
        or row.get('original_sha')!=reference['original_result_sha256'] or row.get('at')!=reference['decision_at']):
        raise OracleBlocked('manual_finance_source_identity_mismatch')
    decision=json.loads(raw,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    if (not isinstance(decision,dict) or decision.get('source_run_id')!=reference['source_run_id']
        or not isinstance(decision.get('confirmed'),dict) or set(decision['confirmed'])!={'goods','dropship','reshipment'}
        or not isinstance(decision.get('payouts'),list) or type(decision.get('no_payout')) is not bool
        or not isinstance(decision.get('reason'),str) or not decision['reason'].strip()):
        raise OracleBlocked('manual_finance_source_invalid')
    if not fixture and case.get('source_evidence',{}).get('manual_finance_decisions_sha256')!=reference['sha256']:
        raise OracleBlocked('manual_finance_source_provenance_missing')
    return path.relative_to(bundle.resolve()).as_posix()


def _inventory(root):
    root=Path(root)
    if root.is_symlink() or not root.is_dir():raise OracleBlocked('oracle_tree_invalid')
    output=[]
    for path in sorted(root.rglob('*')):
        if path.is_symlink():raise OracleBlocked('oracle_symbolic_path')
        if path.is_file():output.append({'path':path.relative_to(root).as_posix(),'sha256':file_sha(path),'bytes':path.stat().st_size})
    return output


def _bounded_child_tree(root):
    count=total=0
    for path in root.rglob('*'):
        info=path.lstat()
        if stat.S_ISDIR(info.st_mode):continue
        if not stat.S_ISREG(info.st_mode):raise OracleBlocked('child_output_nonregular')
        count+=1;total+=info.st_size
        if count>50000 or total>1024*1024*1024:raise OracleBlocked('child_output_limit')


def _bundle(root):
    manifest=_read(root/'bundle.json');inputs=_read(root/'inputs.json')
    bundle_digest=lambda value:hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
    if manifest.get('schema_version')!=1 or manifest.get('inventory_hash')!=bundle_digest(manifest.get('files')):raise OracleBlocked('bundle_manifest_invalid')
    if manifest.get('fingerprint')!=bundle_digest({k:v for k,v in inputs.items() if k not in {'captured_at','runs'}}):raise OracleBlocked('bundle_fingerprint_invalid')
    actual=[r for r in _inventory(root) if r['path']!='bundle.json']
    if actual!=manifest['files']:raise OracleBlocked('bundle_files_changed')
    model=_read(root/'model.json')
    if bundle_digest(model)!=inputs.get('model_hash'):raise OracleBlocked('bundle_model_changed')
    return inputs,model,manifest['fingerprint']


def _different(left,right):
    if isinstance(left,bool) or isinstance(right,bool):return type(left)!=type(right) or left!=right
    if isinstance(left,(int,float,Decimal)) and isinstance(right,(int,float,Decimal)):
        a,b=Decimal(str(left)),Decimal(str(right))
        return not a.is_finite() or not b.is_finite() or abs(a-b)>Decimal('.0000005')
    if isinstance(left,dict) and isinstance(right,dict):return set(left)!=set(right) or any(_different(v,right[k]) for k,v in left.items())
    if isinstance(left,list) and isinstance(right,list):return len(left)!=len(right) or any(_different(a,b) for a,b in zip(left,right))
    return type(left)!=type(right) or left!=right


def _resolve(db,text):
    if not isinstance(text,str):raise OracleBlocked('sqlite_json_invalid')
    value=json.loads(text)
    if isinstance(value,dict) and '$ledger_snapshot_v1' in value:
        if set(value)!={'$ledger_snapshot_v1'} or not SHA.fullmatch(str(value['$ledger_snapshot_v1'])):raise OracleBlocked('snapshot_reference_invalid')
        row=db.execute('SELECT codec,raw_bytes,payload FROM evidence_blob WHERE sha=?',(value['$ledger_snapshot_v1'],)).fetchone()
        if not row or row[0]!='zlib' or not 0<=row[1]<=256*1024*1024:raise OracleBlocked('snapshot_blob_invalid')
        decoder=zlib.decompressobj();raw=decoder.decompress(row[2],row[1]+1)
        if len(raw)!=row[1] or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail or hashlib.sha256(raw).hexdigest()!=value['$ledger_snapshot_v1']:raise OracleBlocked('snapshot_blob_changed')
        value=json.loads(raw)
    return value


def _open(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size>512*1024*1024:raise OracleBlocked('child_database_missing')
    connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    connection.row_factory=sqlite3.Row
    connection.execute('PRAGMA query_only=ON');connection.execute('PRAGMA trusted_schema=OFF')
    if connection.execute('PRAGMA quick_check').fetchone()[0]!='ok':connection.close();raise OracleBlocked('child_database_corrupt')
    return connection


def _real_tables(db,required):
    rows={row['name']:row for row in db.execute('SELECT name,type,sql FROM sqlite_master')}
    if any(name not in rows or rows[name]['type']!='table' or 'CREATE VIRTUAL TABLE' in str(rows[name]['sql']).upper() for name in required):raise OracleBlocked('child_financial_tables_not_real')


def _sum(values):
    values=list(values)
    if any(value is None for value in values):return None
    return sum((Decimal(str(value)) for value in values),Decimal(0))


def _logical(value):
    # Immutable run-proof protocol, not a financial calculation.
    ignored={'file_sha','__sha__','spine_row','registry_revision','rule_key','rule_version','calculation_id'}
    if isinstance(value,dict):return {k:_logical(v) for k,v in value.items() if k not in ignored}
    if isinstance(value,list):return [_logical(v) for v in value]
    if isinstance(value,str):return re.sub(r'order-feed:[^:;\s]+:\d+','order-feed:captured',value)
    return value


def inspect_checkpoint(folder, *, source_code_sha=None):
    """Read real databases and original proof-bound artifacts with stdlib only."""
    snapshot=_read(folder/'snapshot.json');scopes={};frozen={};immutable={};cost_inputs={};run_ids={}
    with closing(_open(folder/'workspace.db')) as ws, closing(_open(folder/'registry.db')) as registry:
        _real_tables(ws,{'period','run','automation_run_proof'})
        _real_tables(registry,{'payout_confirmation'})
        for period in ws.execute('SELECT * FROM period ORDER BY store_id,period'):
            run=(ws.execute('SELECT * FROM run WHERE id=?',(period['run_id'],)).fetchone() if period['state']=='closed' else
                ws.execute('SELECT * FROM run WHERE store_id=? AND period=? ORDER BY id DESC LIMIT 1',(period['store_id'],period['period'])).fetchone())
            if run is None or (run['store_id'],run['period'])!=(period['store_id'],period['period']) or not run['evidence_ready'] or run['evidence_error']:raise OracleBlocked('financial_run_unsealed')
            payload=_resolve(ws,run['result']);key=period['store_id']+'/'+period['period']
            run_ids[key]=run['id']
            proof=ws.execute('SELECT * FROM automation_run_proof WHERE run_id=?',(run['id'],)).fetchone()
            if proof is None:raise OracleBlocked('financial_run_proof_missing')
            if digest(_logical(payload))!=proof['result_sha']:raise OracleBlocked('financial_result_proof_changed')
            manifest=json.loads(proof['manifest_json']);artifacts=manifest.get('artifacts')
            if source_code_sha is not None and manifest.get('source_code_sha')!=source_code_sha:raise OracleBlocked('financial_source_identity_mismatch')
            if not isinstance(artifacts,list) or not artifacts:raise OracleBlocked('financial_artifacts_missing')
            for artifact in artifacts:
                path=folder/str(artifact.get('path',''))
                if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()) or not path.is_file() or file_sha(path)!=artifact.get('sha'):raise OracleBlocked('financial_artifact_changed')
            commission=payload.get('commission') or {};people={p['person_id']:{'base':p.get('base'),'amount':p.get('amount')} for p in commission.get('people',[])}
            if len(people)!=len(commission.get('people',[])):raise OracleBlocked('duplicate_commission_person')
            if commission.get('amount_complete') and _different(_sum(p['amount'] for p in people.values()),commission.get('total')):raise OracleBlocked('commission_conservation_failed')
            confirmation=registry.execute('SELECT * FROM payout_confirmation WHERE store_id=? AND period=? AND finance_run=? ORDER BY at DESC,id DESC LIMIT 1',(period['store_id'],period['period'],run['id'])).fetchone()
            payouts=_resolve(registry,confirmation['payouts_json']) if confirmation else []
            confirmed={p['person_id']:p['amount'] for p in payouts}
            if len(confirmed)!=len(payouts) or confirmation and _different(_sum(confirmed.values()),Decimal(confirmation['confirmed_total'])):raise OracleBlocked('payout_conservation_failed')
            rpc=(snapshot.get('scopes') or {}).get(key)
            if not isinstance(rpc,dict) or type(rpc.get('can_close')) is not bool:raise OracleBlocked('readiness_rpc_missing')
            scopes[key]={'state':period['state'],'statement':{r['id']:{'value':r.get('value'),'available':bool(r.get('available',True))} for r in payload.get('statement',[])},
                'commission_total':commission.get('total'),'commission_people':people,'commission_complete':bool(commission.get('amount_complete')),
                'can_close':rpc['can_close'],'source_sync_pending':bool(payload.get('source_sync_pending')),
                'confirmed_total':float(confirmation['confirmed_total']) if confirmation else None,'confirmed_people':confirmed,
                'cost_line_total':(payload.get('cost_line_posting') or {}).get('total',0),'allocation_pending_count':int(payload.get('allocation_pending_count') or 0)}
            if _different(scopes[key],rpc):raise OracleBlocked('rpc_sqlite_mismatch')
            if period['state']=='closed':
                if scopes[key]['can_close']:raise OracleBlocked('closed_scope_reported_open')
                frozen[key]={'run_id':run['id'],'result_sha':digest(payload),'proof_sha':digest(dict(proof)),'artifact_sha':digest(artifacts)}
        tables={r[0] for r in ws.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'period_cost_input' in tables:
            _real_tables(ws,{'period_cost_input','automation_authorization','automation_receipt','automation_decision'})
            for row in ws.execute('SELECT * FROM period_cost_input'):
                item=dict(row);item['costs']=json.loads(item.pop('costs_json'));item['evidence']=json.loads(item.pop('evidence_json'))
                auth=ws.execute('SELECT * FROM automation_authorization WHERE id=?',(item['authorization_id'],)).fetchone()
                if not auth:raise OracleBlocked('period_cost_authorization_missing')
                item['authorization']=dict(auth)
                receipts=ws.execute("SELECT * FROM automation_receipt WHERE authorization_id=? AND action='period_cost_input.apply'",(item['authorization_id'],)).fetchall()
                if len(receipts)!=1:raise OracleBlocked('period_cost_receipt_missing')
                item['receipt']=dict(receipts[0]);result=json.loads(receipts[0]['result_json'])
                decision=ws.execute("SELECT * FROM automation_decision WHERE id=? AND action='apply' AND kind='period_cost_input'",(result.get('decision_id'),)).fetchone()
                if not decision:raise OracleBlocked('period_cost_decision_missing')
                item['decision']=dict(decision)
                item['revoked']=bool(ws.execute("SELECT 1 FROM automation_decision WHERE action='revoke' AND ref_id=?",(decision['id'],)).fetchone())
                cost_inputs[item['id']]=item
        for name,db in [('workspace',ws),('registry',registry)]:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in IMMUTABLE_TABLES:
                if table in tables:
                    for row in db.execute('SELECT * FROM '+table):
                        item=dict(row);identity=str(item.get('id',item.get('run_id',digest(item))))
                        immutable[name+':'+table+':'+identity]=digest(item)
    if set(snapshot.get('scopes',{}))!=set(scopes):raise OracleBlocked('rpc_scope_mismatch')
    return {'scopes':scopes,'frozen':frozen,'immutable':immutable,'period_cost_inputs':cost_inputs,'run_ids':run_ids,'input_bundle_fingerprint':snapshot.get('input_bundle_fingerprint')}


def _approved_cost_control(case, step, bundle):
    ref=step['reference'];body=_read(Path(ref['path']),limit=32*1024*1024)
    rows=[r for r in body['rows'] if r.get('store_id')==step['store_id'] and r.get('period')==step['period'] and r.get('run_id')==ref['source_run_id']]
    if len(rows)!=1:raise OracleBlocked('period_cost_source_identity_mismatch')
    source=rows[0];decision=json.loads(source['decision_json'],parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    return {'store_id':step['store_id'],'period':step['period'],'costs':decision['confirmed'],
        'reference':{k:v for k,v in ref.items() if k!='path'},'capture_sha256':ref['sha256'],
        'replay_approval_reference':case['approval_reference'],'source_kind':'fixture' if case['kind']=='service_fixture' else 'real_history'}


def _verify_cost_inputs(checkpoint, observed, controls):
    """Validate original source authority outside candidate code and SQLite triggers."""
    for row in observed['period_cost_inputs'].values():
        evidence=row['evidence'];reference=evidence.get('reference') or {};auth=row['authorization']
        matching=[c for c in controls if c['store_id']==row['store_id'] and c['period']==row['period']
            and all(reference.get(k)==v for k,v in c['reference'].items())
            and evidence.get('replay_approval_reference')==c['replay_approval_reference']]
        if len(matching)!=1:raise OracleBlocked('period_cost_source_not_approved')
        control=matching[0]
        doc=evidence.get('document') or {}
        expected_doc={'capture_sha256':control['capture_sha256'],'decision_sha256':control['reference']['decision_sha256'],
            'historical_source_run_id':control['reference']['source_run_id'],'historical_result_sha256':control['reference']['original_result_sha256'],
            'decision_at':control['reference']['decision_at'],'historical_authenticated_actor_proved':False,'production_authority':False}
        if evidence.get('source_kind')!=control['source_kind'] or any(doc.get(k)!=v for k,v in expected_doc.items()):raise OracleBlocked('period_cost_documentary_lineage_invalid')
        def money(value):
            if isinstance(value,bool):raise OracleBlocked('period_cost_amount_invalid')
            try:result=Decimal(str(value))
            except Exception:raise OracleBlocked('period_cost_amount_invalid') from None
            if not result.is_finite() or abs(result)>Decimal('999999999999.99') or result!=result.quantize(Decimal('.01')):raise OracleBlocked('period_cost_amount_invalid')
            return result
        if set(row['costs'])!={'goods','dropship','reshipment'} or any(money(row['costs'][k])!=money(control['costs'][k]) for k in control['costs']):
            raise OracleBlocked('period_cost_approved_amount_difference')
        receipt=row['receipt'];decision=row['decision'];result=json.loads(receipt['result_json']);decision_value=json.loads(decision['values_json'])
        if (row['revoked'] or receipt['request_id']!=row['request_id'] or auth['request_id']!=row['request_id']
            or decision['request_id']!=row['request_id'] or decision['actor']!=row['actor']
            or decision['store_id']!=row['store_id'] or decision['period']!=row['period']
            or result.get('cost_input_id')!=row['id'] or result.get('payouts_confirmed') is not False or result.get('period_closed') is not False
            or decision_value.get('cost_input_id')!=row['id'] or decision_value.get('costs')!=row['costs']):
            raise OracleBlocked('period_cost_transaction_receipt_invalid')
        scope=json.loads(auth['scope_json'])
        if (row['identity_kind']!='documentary_history_replay' or auth['identity_kind']!='documentary_history_replay'
            or auth['actor']!=row['actor'] or auth['action']!='period_cost_input.apply'
            or scope.get('store_id')!=row['store_id'] or scope.get('period')!=row['period']
            or scope.get('historical_authenticated_actor_proved') is not False or scope.get('production_authority') is not False
            or evidence.get('historical_authenticated_actor_proved') is not False or evidence.get('production_authority') is not False):
            raise OracleBlocked('period_cost_documentary_authority_invalid')
        artifacts=evidence.get('artifacts')
        if not isinstance(artifacts,list) or len(artifacts)!=1 or artifacts[0].get('sha')!=control['capture_sha256']:
            raise OracleBlocked('period_cost_original_capture_missing')
        artifact=artifacts[0];relative=Path(str(artifact.get('path','')));path=checkpoint/relative
        if relative.is_absolute() or '..' in relative.parts or path.is_symlink() or not path.resolve().is_relative_to(checkpoint.resolve()) or not path.is_file() or file_sha(path)!=control['capture_sha256']:
            raise OracleBlocked('period_cost_original_capture_changed')
    return set(observed['period_cost_inputs'])


class FinancialOracle:
    def __init__(self, *, manifest, manifest_sha256, runtime, implementation_sha256,
                 allow_fixtures=False, timeout_seconds=600, trusted_release=None):
        self.manifest=Path(manifest).resolve();self.manifest_sha=manifest_sha256;self.runtime=Path(runtime).resolve()
        self.implementation_sha=implementation_sha256;self.allow_fixtures=allow_fixtures
        self.timeout=float(timeout_seconds);self.trusted_release=trusted_release
        if implementation_sha()!=implementation_sha256 or not 1<=self.timeout<=3600:raise OracleBlocked('financial_oracle_configuration_invalid')

    def _cases(self):
        if not self.manifest.is_file() or self.manifest.is_symlink() or file_sha(self.manifest)!=self.manifest_sha:raise OracleBlocked('approved_service_cases_missing_or_changed')
        body=_read(self.manifest);kind='service_fixture' if self.allow_fixtures else 'historical_golden'
        if body.get('schema_version')!=1 or body.get('kind')!=kind or not str(body.get('approval_reference','')).strip() or not body.get('cases'):raise OracleBlocked('approved_service_manifest_invalid')
        output=[];ids=set();targets=body.get('target_scopes')
        if not isinstance(targets,list) or not targets or len(targets)!=len(set(targets)):raise OracleBlocked('approved_service_scope_missing')
        union=set();inventory=[];models=set()
        for row in body['cases']:
            path=_reference(self.manifest.parent,row);case=_read(path)
            if case.get('schema_version')!=1 or case.get('kind')!=kind or not case.get('case_id') or case['case_id'] in ids or not str(case.get('approval_reference','')).strip():raise OracleBlocked('approved_service_case_invalid')
            ids.add(case['case_id']);bundle=Path(case.get('bundle',''));bundle=bundle if bundle.is_absolute() else path.parent/bundle
            inputs,model,fingerprint=_bundle(bundle);models.add(inputs['model_hash'])
            expected=case.get('expected',{}).get('scopes')
            if not isinstance(expected,dict) or not expected:raise OracleBlocked('approved_expected_missing')
            for key,value in expected.items():
                if not re.fullmatch(r'[^/]+/\d{4}-(0[1-9]|1[0-2])',key) or set(value)!=SCOPE_FIELDS or not {'n_receipt','gross_profit','net_profit'}<=set(value['statement']):raise OracleBlocked('approved_expected_incomplete')
                if not self.allow_fixtures:
                    for metric in ('n_receipt','gross_profit','net_profit'):
                        node=value['statement'][metric]
                        if (not isinstance(node,dict) or node.get('available') is not True
                            or isinstance(node.get('value'),bool) or not isinstance(node.get('value'),(int,float,Decimal))
                            or not Decimal(str(node['value'])).is_finite()):
                            raise OracleBlocked('historical_required_amount_unproven')
            union.update(expected)
            if not self.allow_fixtures:
                evidence=case.get('source_evidence') or {};slots={r['sha'] for r in inputs.get('slots',[])}
                if evidence.get('kind')!='historical_export' or not evidence.get('reference') or set(evidence.get('input_sha256',[]))!=slots or not slots:raise OracleBlocked('historical_source_provenance_missing')
                catalog=_read(bundle/'classification.json')
                declarations=[case['case_id'],case['approval_reference'],evidence['reference'],model.get('id'),model.get('name'),catalog.get('version'),inputs.get('data_kind')]
                if any(re.search('fixture|synthetic|test.only|合成|夹具',str(value),re.I) for value in declarations):raise OracleBlocked('synthetic_case_not_historical')
            reference=case.get('workspace_decisions');decision_path=None
            if reference:
                decision_path=_reference(path.parent,reference)
                if body.get('workspace_decisions',{}).get('sha256')!=reference.get('sha256'):raise OracleBlocked('workspace_approval_manifest_mismatch')
                if not self.allow_fixtures and case.get('source_evidence',{}).get('workspace_decisions_sha256')!=reference['sha256']:raise OracleBlocked('workspace_source_provenance_missing')
            elif body.get('workspace_decisions'):raise OracleBlocked('workspace_approval_case_missing')
            if not isinstance(case.get('steps'),list) or not case['steps']:raise OracleBlocked('service_steps_missing')
            manual_inputs={}
            for step_index,step in enumerate(case['steps']):
                if not isinstance(step,dict) or step.get('operation') not in SERVICE_OPERATIONS:raise OracleBlocked('service_operation_unsupported')
                if step.get('operation')=='close_approved' and step.get('approval_reference')!=case['approval_reference']:raise OracleBlocked('close_approval_mismatch')
                if step.get('operation')=='restore_approved_manual_finance':
                    manual_inputs[step_index]=_manual_input(case,step,bundle,fixture=self.allow_fixtures)
            inventory.append({'case_sha':file_sha(path),'bundle':str(bundle.resolve()),'inventory':_inventory(bundle),'decision_sha':file_sha(decision_path) if decision_path else None})
            output.append({'case':case,'bundle':bundle.resolve(),'fingerprint':fingerprint,'decisions':decision_path,'manual_inputs':manual_inputs})
        if len(models)!=1 or union!=set(targets):raise OracleBlocked('complete_approved_scope_mismatch')
        if not self.allow_fixtures:
            # Reuse the pinned host preflight's actual resolver for operating
            # scope. It never imports or executes a candidate Python module.
            for name,module in list(sys.modules.items()):
                if name=='ledger' or name.startswith('ledger.'):
                    origin=getattr(module,'__file__',None)
                    if origin and not Path(origin).resolve().is_relative_to(ROOT/'ledger/ledger'):raise OracleBlocked('parent_candidate_module_forbidden')
            spec=importlib.util.spec_from_file_location('trusted_financial_service_preflight',CHECKER)
            preflight=importlib.util.module_from_spec(spec);spec.loader.exec_module(preflight)
            latest=body.get('latest_period');periods={preflight._previous(latest),latest}
            model=preflight._model(output[0]['bundle']/'model.json');stores={s.id:s for s in model.stores}
            operating={period:sorted(s.id for s in model.stores if not s.archived) for period in periods}
            if body.get('workspace_decisions'):
                reference=preflight._workspace_reference(body['workspace_decisions'],self.manifest.parent)
                operating,_receipt=preflight._approved_operating_scope(model,reference,periods)
            platforms={stores[sid].platform for ids in operating.values() for sid in ids}
            for key in targets:
                sid,period=preflight._scope(key,stores)
                if period not in periods or sid not in operating[period]:raise OracleBlocked('historical_target_scope_not_operating')
            coverage=preflight._coverage(targets,stores,periods)
            if not platforms or platforms!=set(coverage['platforms']) or periods!=set(coverage['periods']) or not coverage['cross_period_stores']:raise OracleBlocked('historical_complete_coverage_missing')
        return body,output,inventory

    def _project(self,candidate_root,target):
        source=Path(candidate_root)/'ledger/ledger';manifest=[]
        for path in sorted(source.rglob('*')):
            if path.is_symlink():raise OracleBlocked('candidate_code_symbolic')
            if path.is_file() and path.suffix=='.py':
                relative=path.relative_to(source);dest=target/'ledger/ledger'/relative;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,dest)
                manifest.append({'path':'ledger/ledger/'+relative.as_posix(),'sha256':file_sha(dest)})
        if not manifest:raise OracleBlocked('candidate_code_missing')
        # API import registers a static mount even for an offline finance call.
        # No web content is served in this namespace; provide its directory.
        (target/'ledger/ledger/static').mkdir(exist_ok=True)
        # An isolated development checkout activates the existing source guard;
        # this is not a fabricated release acceptance or the user's checkout.
        for args in [('init','-q'),('add','ledger'),('-c','user.name=Offline Oracle','-c','user.email=oracle@localhost','commit','-qm','private code projection')]:
            subprocess.run(['git','-C',str(target),*args],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return manifest

    def verify(self, *, candidate_root, candidate_code_sha, output):
        output=Path(output).resolve()
        if output.exists() and any(output.iterdir()):raise OracleBlocked('oracle_output_not_empty')
        output.mkdir(parents=True,exist_ok=True);output.chmod(0o700)
        report={'schema':1,'kind':'external_financial_service_oracle.v1','candidate_code_sha':candidate_code_sha,
            'implementation_sha256':self.implementation_sha,'status':'blocked','financial_approval_granted':False,
            'candidate_execution_isolated':False,'cases':[],'release_ready':False}
        try:
            candidate_root=Path(candidate_root).resolve()
            if output.is_relative_to(candidate_root) or output.is_relative_to(self.runtime) or self.manifest.is_relative_to(self.runtime):raise OracleBlocked('oracle_authority_path_exposed')
            if not re.fullmatch('[0-9a-f]{40}',candidate_code_sha) or implementation_sha()!=self.implementation_sha:raise OracleBlocked('oracle_identity_changed')
            if not self.allow_fixtures:
                actual=subprocess.check_output(['git','-C',str(candidate_root),'rev-parse','HEAD'],stderr=subprocess.DEVNULL,text=True).strip()
                if actual!=candidate_code_sha:raise OracleBlocked('candidate_commit_identity_mismatch')
            manifest,cases,before=self._cases();bwrap=shutil.which('bwrap')
            if not bwrap or not (self.runtime/'bin/python').is_file():raise OracleBlocked('financial_sandbox_unavailable')
            with tempfile.TemporaryDirectory(prefix='ledger-financial-oracle-') as temporary:
                temp=Path(temporary);projection=temp/'code';projection.mkdir();code_manifest=self._project(candidate_root,projection)
                child=temp/'child';child.mkdir();request=[];mounts=[]
                for index,row in enumerate(cases):
                    case=row['case'];public={k:case[k] for k in ('kind','registry')}
                    public['steps']=[dict(step) for step in case['steps']]
                    for step_index,relative in row['manual_inputs'].items():
                        step=public['steps'][step_index]
                        step['reference']={**step['reference'],'path':'/inputs/'+str(index)+'/'+relative}
                    # Scope names are execution authority metadata, never the
                    # expected monetary results or the parent's manifest.
                    public['publication_scopes']=sorted(case['expected']['scopes'])
                    if row['decisions']:
                        public['workspace_decisions']={**case['workspace_decisions'],'path':'/decisions/'+str(index)+'.json'}
                        mounts+=['--ro-bind',str(row['decisions']),'/decisions/'+str(index)+'.json']
                    request.append(public);mounts+=['--ro-bind',str(row['bundle']),'/inputs/'+str(index)]
                request_path=temp/'request.json';request_path.write_text(json.dumps({'cases':request},ensure_ascii=False))
                argv=[str(Path(bwrap).resolve()),'--unshare-all','--unshare-net','--die-with-parent','--new-session','--clearenv',
                    '--setenv','PATH','/usr/bin:/bin','--setenv','HOME','/tmp','--setenv','POLARS_MAX_THREADS','2',
                    '--setenv','PYTHONDONTWRITEBYTECODE','1','--proc','/proc','--dev','/dev','--tmpfs','/tmp']
                for system in ('/usr','/bin','/lib','/lib64'):
                    if Path(system).exists():argv+=['--ro-bind',system,system]
                argv+=['--ro-bind',str(projection),'/candidate','--ro-bind',str(self.runtime),'/runtime',
                    '--ro-bind',str(CHILD),'/driver.py','--ro-bind',str(request_path),'/request.json',
                    '--bind',str(child),'/results',*mounts,'--chdir','/tmp','--','/runtime/bin/python','-I','-B','/driver.py']
                with (temp/'private.log').open('wb') as log:
                    process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                    try:
                        deadline=time.monotonic()+self.timeout
                        while process.poll() is None:
                            _bounded_child_tree(child)
                            if log.tell()>8*1024*1024:raise OracleBlocked('child_log_limit')
                            if time.monotonic()>deadline:raise OracleBlocked('financial_child_timeout')
                            time.sleep(.1)
                        exit_code=process.returncode
                    finally:
                        if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
                report.update(candidate_execution_isolated=True,child_exit_code=exit_code,child_log_sha256=file_sha(temp/'private.log'),source_code_manifest=code_manifest,source_code_sha=digest(code_manifest))
                shutil.copyfile(temp/'private.log',output/'child.private.log')
                (output/'child.private.log').chmod(0o600)
                report['child_log_path']='child.private.log'
                # Persist raw artifacts privately for an independent inspection.
                _bounded_child_tree(child)
                shutil.copytree(child,output/'child-artifacts')
                if exit_code!=0 or not (child/'transport-completed.json').is_file() or _read(child/'transport-completed.json')!={'cases':len(cases)}:raise OracleBlocked('financial_transport_incomplete')
                for index,row in enumerate(cases):
                    previous_frozen={};previous_immutable={};last=None;cost_controls=[];approved_cost_ids=set()
                    for step_index,_step in enumerate(row['case']['steps']):
                        observed=inspect_checkpoint(child/str(index)/str(step_index),source_code_sha=digest(code_manifest))
                        if _step.get('operation')=='restore_approved_manual_finance' and _step.get('cost_input_only') is True:
                            control=_approved_cost_control(row['case'],_step,row['bundle'])
                            if control not in cost_controls:cost_controls.append(control)
                            target=_step['store_id']+'/'+_step['period'];now_scope=observed['scopes'].get(target)
                            before_scope=last['scopes'].get(target) if last else None
                            if (not now_scope or now_scope['state']!='open' or before_scope and (
                                _different(before_scope['confirmed_people'],now_scope['confirmed_people'])
                                or _different(before_scope['confirmed_total'],now_scope['confirmed_total'])
                                or before_scope['commission_complete'] is False and now_scope['commission_complete'] is True)):
                                raise OracleBlocked('period_cost_input_conferred_financial_close')
                            ids=_verify_cost_inputs(child/str(index)/str(step_index),observed,cost_controls)
                            if len(ids-approved_cost_ids)>1 or not any(r['store_id']==control['store_id'] and r['period']==control['period'] for r in observed['period_cost_inputs'].values()):raise OracleBlocked('period_cost_source_input_missing')
                            for new_id in ids-approved_cost_ids:
                                cost_row=observed['period_cost_inputs'][new_id]
                                target_key=cost_row['store_id']+'/'+cost_row['period']
                                if not last or cost_row['source_run_id']!=last['run_ids'].get(target_key):raise OracleBlocked('period_cost_source_run_unverified')
                            approved_cost_ids=ids
                        else:
                            ids=_verify_cost_inputs(child/str(index)/str(step_index),observed,cost_controls)
                            if ids!=approved_cost_ids:raise OracleBlocked('period_cost_input_outside_approved_step')
                        if set(observed['scopes'])-set(row['case']['expected']['scopes']):raise OracleBlocked('financial_scope_outside_approved')
                        if any(observed['frozen'].get(k)!=v for k,v in previous_frozen.items()) or any(observed['immutable'].get(k)!=v for k,v in previous_immutable.items()):raise OracleBlocked('frozen_financial_layer_changed')
                        previous_frozen.update(observed['frozen']);previous_immutable.update(observed['immutable']);last=observed
                    if last['input_bundle_fingerprint']!=row['fingerprint'] or _different(row['case']['expected']['scopes'],last['scopes']):raise OracleBlocked('financial_expected_difference')
                    report['cases'].append({'case_id':row['case']['case_id'],'status':'passed','scope_count':len(last['scopes']),'scope_sha256':digest(last['scopes']),'frozen_count':len(previous_frozen),'immutable_count':len(previous_immutable)})
                _body,_cases,after=self._cases()
                if before!=after or self._project_identity(candidate_root)!=code_manifest or implementation_sha()!=self.implementation_sha:raise OracleBlocked('oracle_inputs_changed')
                report.update(status='passed',manifest_sha256=self.manifest_sha,model_sha256=_bundle(cases[0]['bundle'])[0]['model_hash'],input_inventory_sha256=digest(before),fixture_only=self.allow_fixtures,
                    original_input_shas=sorted({slot['sha'] for row in cases for slot in _bundle(row['bundle'])[0]['slots']}),target_period=manifest.get('latest_period'))
        except (OracleBlocked,OSError,ValueError,KeyError,TypeError,sqlite3.Error,subprocess.SubprocessError) as error:
            report['reason_code']=str(error) if isinstance(error,OracleBlocked) else 'financial_oracle_execution_failed'
        suite=ET.Element('testsuite',name='external_financial_oracle',tests='1',failures=str(int(report['status']!='passed')),errors='0',skipped='0')
        case=ET.SubElement(suite,'testcase',name='independent_service_and_frozen_invariants')
        if report['status']!='passed':ET.SubElement(case,'failure',message=report['reason_code'])
        ET.ElementTree(suite).write(output/'financial-oracle.xml',encoding='utf-8',xml_declaration=True)
        report['report_sha256']=file_sha(output/'financial-oracle.xml');report['proof_sha256']=digest(report)
        (output/'financial-oracle.json').write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2))
        return report

    @staticmethod
    def _project_identity(root):
        source=Path(root)/'ledger/ledger'
        if source.is_symlink() or any(p.is_symlink() for p in source.rglob('*')):raise OracleBlocked('candidate_code_symbolic')
        return [{'path':p.relative_to(root).as_posix(),'sha256':file_sha(p)} for p in sorted(source.rglob('*.py')) if p.is_file()]

    def __call__(self, *, candidate_code_sha, candidate_root, diagnostic_root, output):
        report=self.verify(candidate_root=candidate_root,candidate_code_sha=candidate_code_sha,output=output)
        result={'kind':'independent_financial_acceptance.v1','candidate_code_sha':candidate_code_sha,'status':'blocked',
            'reason_code':report.get('reason_code','independent_release_checks_missing'),'financial_oracle_report':'financial-oracle.json'}
        # Eight independent release gates cannot be invented from one financial
        # test. Only a separately configured trusted host attestation can supply
        # them; no candidate /out or diagnostic file is considered here.
        if report['status']!='passed' or self.allow_fixtures or not self.trusted_release:return result
        receipt_path=_reference(Path('/'),self.trusted_release)
        if any(receipt_path.is_relative_to(Path(root).resolve()) for root in (candidate_root,diagnostic_root,output)):
            raise OracleBlocked('release_receipt_untrusted_location')
        from ledger.release_gate import validate
        receipt=_read(receipt_path);validate(receipt,version=candidate_code_sha,evidence_root=receipt_path.parent)
        if receipt.get('source_code_sha')!=report['source_code_sha']:raise OracleBlocked('release_source_identity_mismatch')
        if (receipt.get('approved_service_cases',{}).get('sha256')!=self.manifest_sha or receipt.get('target_period')!=report['target_period']
            or not set(report['original_input_shas'])<={row['sha256'] for row in receipt['inputs']}):raise OracleBlocked('release_financial_input_identity_mismatch')
        for check in receipt['checks']:
            if check['name']=='service_replay':
                check.update(report='financial-oracle.xml',report_sha256=report['report_sha256'],tests=1,failures=0,errors=0,skipped=0,exit_code=0)
            else:
                source=(receipt_path.parent/check['report']).resolve();target=Path(output)/check['report'];target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
                if source.suffix=='.json':
                    log=source.parent/(check['name']+'.log');shutil.copyfile(log,target.parent/log.name)
        receipt['independent_financial_oracle']={'path':'financial-oracle.json','sha256':file_sha(Path(output)/'financial-oracle.json'),'implementation_sha256':self.implementation_sha}
        receipt['receipt_sha']=digest({k:v for k,v in receipt.items() if k!='receipt_sha'})
        validate(receipt,version=candidate_code_sha,evidence_root=Path(output))
        (Path(output)/'acceptance-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,sort_keys=True,indent=2))
        return {**result,'status':'passed','candidate_execution_isolated':True,'receipt':'acceptance-receipt.json'}


def configured_financial_oracle(config):
    spec=config.get('financial_oracle')
    if not spec:return None
    if not isinstance(spec,dict) or spec.get('kind')!='external_service_cases.v1':raise OracleBlocked('financial_oracle_configuration_invalid')
    return FinancialOracle(**{k:v for k,v in spec.items() if k!='kind'})
