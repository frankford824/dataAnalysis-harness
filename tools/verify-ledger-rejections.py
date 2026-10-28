"""Verify explicitly incomplete/refused source cases; never issue release credit.

Manifest: schema_version=1, kind=source_rejection_suite, approval_reference,
target_scopes=[store/YYYY-MM], cases=[{path,sha256}]. Each real case is a
source_rejection with source_evidence={kind:original_source_capture or
historical_export, reference, input_sha256:[all original slot/file hashes],
registry_sha256:<canonical seed digest>, independent_controls:[{path,sha256}],
engine_outputs_used_as_expected:false}. Configuration/source references are
reverified before and after execution. A provenance attestation is not proof
that its author supplied truthful evidence; independent review is still needed.

Scope-only baselines are separate from executed source refusals. Synthetic
contracts require --allow-fixtures and never contribute real-source coverage.
This command has no record/baseline mode, no production writer, no receipt
issuer and cannot satisfy the existing positive financial release gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import xml.etree.ElementTree as ET

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'ledger'))
from ledger import freight_experiments_inputs as frozen,replay_service
from ledger.freight_trial_inputs import digest
from ledger.model.loader import load_model
from ledger.model.schema import Model
from ledger.version import engine_version,source_code_sha

SHA=re.compile('[a-f0-9]{64}')
SYNTHETIC=re.compile('fixture|synthetic|合成|夹具',re.I)


class RejectionBlocked(ValueError):
    pass


def _sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def _read(path):
    if not Path(path).is_file() or Path(path).is_symlink():raise RejectionBlocked('固定材料缺失或是符号链接')
    if Path(path).stat().st_size>256*1024*1024:raise RejectionBlocked('固定材料超过读取预算')
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value,dict):raise RejectionBlocked('材料顶层必须为对象')
    return value


def _text(value):return isinstance(value,str) and bool(value.strip())


def _resolve(value,base):
    p=Path(str(value));return p.resolve() if p.is_absolute() else (base/p).resolve()


def _reference(ref,base):
    if not isinstance(ref,dict) or not _text(ref.get('path')) or not SHA.fullmatch(str(ref.get('sha256',''))):
        raise RejectionBlocked('来源材料必须绑定路径和SHA')
    p=_resolve(ref['path'],base)
    if p.is_symlink() or not p.is_file() or _sha(p)!=ref['sha256']:raise RejectionBlocked('来源材料SHA不符')
    return {**ref,'path':str(p)}


def _model(path):
    return load_model(path) if path.is_dir() else Model.model_validate_json(path.read_text(encoding='utf-8'))


def _model_digest(value):return digest(json.loads(value.model_dump_json()))


def _scope(key,model):
    if not isinstance(key,str) or key.count('/')!=1:raise RejectionBlocked('批准范围缺少明确店月')
    sid,month=key.split('/')
    if sid not in {s.id for s in model.stores} or not re.fullmatch(r'[1-9]\d{3}-(0[1-9]|1[0-2])',month):
        raise RejectionBlocked('批准范围有未知店铺或无效月份')


def _case(ref,base,candidate,candidate_digest,*,allow_fixtures=False):
    row={'status':'blocked','case_id':None,'issues':[],'positive_scope_count':0,
         'eligible_for_financial_release':False,'real_source_rejection_scope_count':0}
    try:
        bound=_reference(ref,base);p=Path(bound['path']);row['case_path']=str(p);row['case_sha256']=bound['sha256'];case=_read(p)
        row['case_id']=case.get('case_id');fixture=case.get('kind')=='service_rejection_fixture'
        if case.get('kind') not in {'source_rejection','service_rejection_fixture'}:raise RejectionBlocked('只接受显式拒绝案例')
        if fixture and not allow_fixtures:raise RejectionBlocked('合成拒绝合同不能冒充真实来源')
        if not _text(case.get('approval_reference')):raise RejectionBlocked('案例缺少独立审查依据')
        expected=case.get('expected')
        if not isinstance(expected,dict) or not isinstance(expected.get('scopes'),dict) or not expected['scopes']:
            raise RejectionBlocked('缺少非空批准拒绝范围')
        for key in expected['scopes']:_scope(key,candidate)
        row['declared_scopes']=sorted(expected['scopes']);row['category']=expected.get('category');row['synthetic_contract']=fixture
        bundle=_resolve(case.get('bundle',''),p.parent);case['bundle']=str(bundle)
        inputs,fingerprint=frozen.verify(bundle);row['bundle_fingerprint']=fingerprint
        bundle_sha=_sha(bundle/'bundle.json');bundle_model=_model(bundle/'model.json')
        if inputs.get('model_hash')!=candidate_digest or _model_digest(bundle_model)!=candidate_digest:
            raise RejectionBlocked('拒绝案例执行Model与当前候选身份不一致')
        refs=[]
        evidence=case.get('source_evidence') or {}
        if not fixture:
            labels=[case.get('case_id'),case.get('approval_reference'),bundle_model.id,bundle_model.name,
                    inputs.get('data_kind'),_read(bundle/'classification.json').get('version')]
            if any(SYNTHETIC.search(str(v or '')) for v in labels):raise RejectionBlocked('已声明合成材料不能改kind冒充真实来源')
            if (evidence.get('kind') not in {'historical_export','original_source_capture'}
                or not _text(evidence.get('reference')) or evidence.get('engine_outputs_used_as_expected') is not False):
                raise RejectionBlocked('缺少明确独立原始来源控制声明')
            hashes=evidence.get('input_sha256');slots={s['sha'] for s in inputs.get('slots',[])};files={f['sha'] for f in inputs.get('files',[])}
            if (not isinstance(hashes,list) or len(hashes)!=len(set(hashes))
                or any(not isinstance(h,str) or not SHA.fullmatch(h) for h in hashes)
                or set(hashes)!=slots or slots!=files):raise RejectionBlocked('原件SHA清单必须完整对应固定输入，不能缩范围')
            if evidence.get('registry_sha256')!=digest(case.get('registry')):raise RejectionBlocked('固定配置种子身份未绑定')
            controls=evidence.get('independent_controls')
            if not isinstance(controls,list) or not controls:raise RejectionBlocked('真实拒绝案例缺少独立控制材料引用')
            refs.extend(_reference(r,p.parent) for r in controls)
            snapshot=(case.get('registry') or {}).get('source_snapshot',{}).get('reference')
            if snapshot is not None:refs.append(_reference(snapshot,p.parent))
        workspace=case.get('workspace_decisions')
        if workspace is not None:
            workspace=_reference(workspace,p.parent);case['workspace_decisions']=workspace
            replay_service.load_workspace_decisions(workspace,fixture=fixture);refs.append(workspace)
            if not fixture and evidence.get('workspace_decisions_sha256')!=workspace['sha256']:
                raise RejectionBlocked('工作区决定未与来源控制绑定同一SHA')
        result=replay_service.execute(case);row['result']=result
        if (result.get('assertion_kind')!='rejection' or result.get('eligible_for_financial_release') is not False
            or result.get('positive_scope_count')!=0 or result.get('ready_scope_count')!=0 or result.get('closed_scope_count')!=0):
            raise RejectionBlocked('拒绝结果不得获得ready/closed/正向金额或发布资格')
        if set(result.get('checked_scopes') or [])!=set(expected['scopes']):raise RejectionBlocked('没有完整观测所有批准scope')
        _,after=frozen.verify(bundle)
        if after!=fingerprint or _sha(bundle/'bundle.json')!=bundle_sha or _sha(p)!=bound['sha256']:
            raise RejectionBlocked('案例或输入在执行期间变化')
        for r in refs:
            if _sha(r['path'])!=r['sha256']:raise RejectionBlocked('独立来源/配置材料在执行期间变化')
        row['source_references']=refs;row['inputs_unchanged']=True
        row['status']=result['status']
        if not fixture and result['status']=='passed':row['real_source_rejection_scope_count']=result['source_rejection_scope_count']
    except Exception as error:
        # A programmer/IO/identity error is a failed attempt, never a successful
        # rejection merely because some exception happened.
        row['status']='blocked' if isinstance(error,(RejectionBlocked,OSError)) else 'failed'
        row['issues'].append({'type':type(error).__name__,'message':str(error)})
    return row


def evaluate(manifest,candidate_model,*,allow_fixtures=False):
    manifest=Path(manifest).resolve();candidate_model=Path(candidate_model).resolve()
    report={'kind':'source_rejection_assertions','status':'blocked','positive_scope_count':0,
        'ready_scope_count':0,'closed_scope_count':0,'eligible_for_financial_release':False,
        'release_receipt_issued':False,'cases':[],'issues':[]}
    try:
        body=_read(manifest);manifest_sha=_sha(manifest);candidate=_model(candidate_model);model_sha=_model_digest(candidate);code_before=source_code_sha()
        if (body.get('schema_version')!=1 or body.get('kind')!='source_rejection_suite'
            or not _text(body.get('approval_reference'))):raise RejectionBlocked('拒绝suite缺明确版本与独立批准依据')
        targets=body.get('target_scopes');cases=body.get('cases')
        if (not isinstance(targets,list) or not targets or len(targets)!=len(set(targets))
            or not isinstance(cases,list) or not cases):raise RejectionBlocked('没有范围或案例不能算验收通过')
        for key in targets:_scope(key,candidate)
        for ref in cases:report['cases'].append(_case(ref,manifest.parent,candidate,model_sha,allow_fixtures=allow_fixtures))
        covered={key for c in report['cases'] for key in c.get('declared_scopes',[])}
        if covered!=set(targets):raise RejectionBlocked('suite目标与逐案例批准scope不一致')
        if (_sha(manifest)!=manifest_sha or _model_digest(_model(candidate_model))!=model_sha
            or source_code_sha()!=code_before):raise RejectionBlocked('suite/候选Model/核算源码在执行期间变化')
        report.update(manifest_sha256=manifest_sha,candidate_model_digest=model_sha,source_code_sha256=code_before,
            code_version=engine_version(),observed_scope_count=len(covered),
            real_source_rejection_scope_count=sum(c['real_source_rejection_scope_count'] for c in report['cases']),
            scope_coverage_baseline_count=sum(len(c.get('declared_scopes',[])) for c in report['cases'] if c.get('category')=='scope_coverage' and c['status']=='passed'),
            status='passed' if all(c['status']=='passed' for c in report['cases']) else 'failed')
    except Exception as error:
        report['issues'].append({'type':type(error).__name__,'message':str(error)})
    return report


def _atomic(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
        if os.name!='nt':
            directory=os.open(path.parent,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path);parser.add_argument('--candidate-model',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path);parser.add_argument('--junit',type=Path)
    parser.add_argument('--allow-fixtures',action='store_true',help='显式运行合成合同；永不计入真实来源覆盖')
    args=parser.parse_args(argv);report=evaluate(args.manifest,args.candidate_model,allow_fixtures=args.allow_fixtures)
    _atomic(args.output,json.dumps(report,ensure_ascii=False,indent=2,default=str).encode())
    if args.junit:
        rows=list(report['cases'])
        if not rows or (report['status']!='passed' and all(r['status']=='passed' for r in rows)):
            rows.append({'status':report['status']})
        suite=ET.Element('testsuite',name='source_rejection_assertions',tests=str(len(rows)),failures=str(sum(r['status']!='passed' for r in rows)))
        for index,row in enumerate(rows):
            test=ET.SubElement(suite,'testcase',name='rejection_'+str(index))
            if row['status']!='passed':ET.SubElement(test,'failure',message='拒绝断言或来源身份未通过；详本地报告')
        _atomic(args.junit,ET.tostring(suite,encoding='utf-8',xml_declaration=True))
    print(json.dumps({k:report.get(k) for k in ['status','observed_scope_count','real_source_rejection_scope_count','positive_scope_count','eligible_for_financial_release']}))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
