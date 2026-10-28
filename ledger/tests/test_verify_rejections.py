"""The rejection runner cannot issue positive/release credit or hide IO drift."""
import hashlib
import importlib.util
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from test_replay_rejections import gap_bundle,observation

TOOL=Path(__file__).resolve().parents[2]/'tools'/'verify-ledger-rejections.py'
spec=importlib.util.spec_from_file_location('verify_ledger_rejections',TOOL)
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)


def saved(path,value):
    path.write_text(json.dumps(value),encoding='utf-8')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def suite(tmp_path):
    bundle,_=gap_bundle(tmp_path)
    expected={'schema_version':1,'category':'scope_coverage','scopes':{'s1/2026-09':{
        'observation':observation(codes={'no_result','company_revenue_unknown'},run=False,
            outcome='not_started',source='not_attempted'),'proved':{},'unknown':{}}}}
    case=tmp_path/'case.json';case_sha=saved(case,{'schema_version':1,'case_id':'synthetic-explicit-rejection-contract',
        'kind':'service_rejection_fixture','approval_reference':'synthetic-reviewed-contract','bundle':str(bundle),
        'registry':{'people':[],'members':[],'schemes':[]},'expected':expected,'steps':[]})
    manifest=tmp_path/'rejections.json';saved(manifest,{'schema_version':1,'kind':'source_rejection_suite',
        'approval_reference':'synthetic-contract-suite','target_scopes':['s1/2026-09'],
        'cases':[{'path':case.name,'sha256':case_sha}]})
    return manifest,bundle/'model.json',case


def test_fixture_rejection_is_explicit_and_never_real_or_financial_coverage(tmp_path):
    manifest,model,_=suite(tmp_path)
    assert runner.evaluate(manifest,model)['status']!='passed'
    result=runner.evaluate(manifest,model,allow_fixtures=True)
    assert result['status']=='passed',result
    assert result['real_source_rejection_scope_count']==result['positive_scope_count']==0
    assert result['eligible_for_financial_release'] is False and not result['release_receipt_issued']
    assert result['cases'][0]['inputs_unchanged'] is True


def test_fixture_label_cannot_be_laundered_by_kind_even_with_fixture_option(tmp_path):
    manifest,model,case=suite(tmp_path);body=json.loads(case.read_text());body['kind']='source_rejection'
    h=saved(case,body);m=json.loads(manifest.read_text());m['cases'][0]['sha256']=h;saved(manifest,m)
    result=runner.evaluate(manifest,model,allow_fixtures=True)
    assert result['status']!='passed'
    assert result['cases'][0]['real_source_rejection_scope_count']==0


def test_unexpected_exception_and_case_mutation_are_not_successful_rejection(tmp_path,monkeypatch):
    manifest,model,case=suite(tmp_path)
    def unexpected(_case):raise RuntimeError('unexpected evaluator bug')
    monkeypatch.setattr(runner.replay_service,'execute',unexpected)
    result=runner.evaluate(manifest,model,allow_fixtures=True)
    assert result['status']!='passed' and result['cases'][0]['status']=='failed'
    def mutate(_case):
        case.write_text(case.read_text()+'\n',encoding='utf-8')
        return {'status':'passed','assertion_kind':'rejection','category':'scope_coverage',
            'eligible_for_financial_release':False,'positive_scope_count':0,'ready_scope_count':0,
            'closed_scope_count':0,'source_rejection_scope_count':0,'checked_scopes':['s1/2026-09']}
    monkeypatch.setattr(runner.replay_service,'execute',mutate)
    result=runner.evaluate(manifest,model,allow_fixtures=True)
    assert result['status']!='passed' and result['cases'][0]['status']=='blocked'


def test_empty_suite_is_failure_and_junit_agrees_with_manifest_failure(tmp_path,monkeypatch):
    manifest,model,_=suite(tmp_path);m=json.loads(manifest.read_text());m['cases']=[];saved(manifest,m)
    output=tmp_path/'attempt.json';junit=tmp_path/'negative.xml'
    assert runner.main(['--manifest',str(manifest),'--candidate-model',str(model),
        '--output',str(output),'--junit',str(junit),'--allow-fixtures'])==1
    assert int(ET.parse(junit).getroot().get('failures'))>0
    # A suite-level identity failure after passed cases still produces a
    # failing JUnit test, rather than an all-green XML beside a nonzero CLI.
    monkeypatch.setattr(runner,'evaluate',lambda *_a,**_k:{'status':'blocked',
        'cases':[{'status':'passed'}],'issues':[{'type':'identity_changed'}]})
    assert runner.main(['--manifest',str(manifest),'--candidate-model',str(model),
        '--output',str(output),'--junit',str(junit)])==1
    assert int(ET.parse(junit).getroot().get('failures'))==1
    assert not (tmp_path/'acceptance-receipt.json').exists()
