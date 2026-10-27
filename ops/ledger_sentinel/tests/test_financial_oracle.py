"""Real bwrap + Session, with expected scopes kept outside candidate namespace."""
import json
import importlib.util
from pathlib import Path
import shutil
import sys
import sqlite3
import hashlib

import pytest

from ops.ledger_sentinel.financial_oracle import FinancialOracle, OracleBlocked, file_sha, implementation_sha

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'ledger/tests'))
_prior_conftest=sys.modules.get('conftest')
_spec=importlib.util.spec_from_file_location('ledger_oracle_fixture_conftest',ROOT/'ledger/tests/conftest.py')
_fixture_conftest=importlib.util.module_from_spec(_spec);_spec.loader.exec_module(_fixture_conftest)
try:
    sys.modules['conftest']=_fixture_conftest
    from test_replay_service import bundle as service_bundle, seed, all_expected
finally:
    if _prior_conftest is None:sys.modules.pop('conftest',None)
    else:sys.modules['conftest']=_prior_conftest


@pytest.fixture
def oracle_case(tmp_path):
    bundle,missing=service_bundle.__wrapped__(tmp_path)
    path=tmp_path/'approved-case.json'
    case={'schema_version':1,'kind':'service_fixture','case_id':'external-session',
        'approval_reference':'synthetic engineering fixture only','bundle':str(bundle),
        'registry':seed(),'steps':[{'operation':'recompute','store_id':sid} for sid in ('s1','s2')],
        'expected':all_expected()}
    path.write_text(json.dumps(case))
    manifest=tmp_path/'approved-manifest.json'
    body={'schema_version':1,'kind':'service_fixture','approval_reference':case['approval_reference'],
        'target_scopes':sorted(case['expected']['scopes']),'cases':[{'path':path.name,'sha256':file_sha(path)}]}
    manifest.write_text(json.dumps(body))
    return manifest,path,case,missing


def make_oracle(manifest):
    return FinancialOracle(manifest=manifest,manifest_sha256=file_sha(manifest),
        runtime=ROOT/'ledger/.venv',implementation_sha256=implementation_sha(),allow_fixtures=True)


def test_real_session_external_parent_compares_all_four_scopes(oracle_case,tmp_path):
    manifest,_path,_case,_missing=oracle_case
    report=make_oracle(manifest).verify(candidate_root=ROOT,candidate_code_sha='a'*40,output=tmp_path/'authority')
    assert report['status']=='passed',report
    assert report['candidate_execution_isolated'] is True
    assert report['cases'][0]['scope_count']==4
    assert report['financial_approval_granted'] is False
    assert not (tmp_path/'authority/acceptance-receipt.json').exists()


def test_expected_amount_difference_cannot_become_junit_success(oracle_case,tmp_path):
    manifest,path,case,_missing=oracle_case
    case['expected']['scopes']['s1/2026-06']['statement']['net_profit']['value']=999
    path.write_text(json.dumps(case));body=json.loads(manifest.read_text());body['cases'][0]['sha256']=file_sha(path);manifest.write_text(json.dumps(body))
    report=make_oracle(manifest).verify(candidate_root=ROOT,candidate_code_sha='b'*40,output=tmp_path/'authority')
    assert report['status']=='blocked'
    assert report['reason_code']=='financial_expected_difference'


@pytest.mark.parametrize('payload',[
    "import os\nos._exit(0)\n",
    "from pathlib import Path\nimport os\nPath('/out').mkdir(exist_ok=True)\nPath('/out/fake.xml').write_text('<testsuite tests=\"1\"/>')\nos._exit(0)\n",
])
def test_candidate_exit_or_fake_out_never_signs_authority(oracle_case,tmp_path,payload):
    manifest,_path,_case,_missing=oracle_case
    candidate=tmp_path/'candidate';shutil.copytree(ROOT/'ledger/ledger',candidate/'ledger/ledger',ignore=shutil.ignore_patterns('__pycache__'))
    (candidate/'ledger/ledger/__init__.py').write_text(payload)
    report=make_oracle(manifest).verify(candidate_root=candidate,candidate_code_sha='c'*40,output=tmp_path/'authority')
    assert report['status']=='blocked'
    assert report['reason_code'] in {'financial_transport_incomplete','financial_oracle_execution_failed'}
    assert not (tmp_path/'authority/acceptance-receipt.json').exists()


def test_manifest_and_implementation_pins_fail_before_child(oracle_case,tmp_path):
    manifest,_path,_case,_missing=oracle_case
    oracle=make_oracle(manifest);manifest.write_text('{}')
    report=oracle.verify(candidate_root=ROOT,candidate_code_sha='d'*40,output=tmp_path/'authority')
    assert report['reason_code']=='approved_service_cases_missing_or_changed'
    assert report['candidate_execution_isolated'] is False


def test_existing_authority_is_never_reused_or_overwritten(oracle_case,tmp_path):
    manifest,_path,_case,_missing=oracle_case
    output=tmp_path/'authority';output.mkdir();old=output/'acceptance-receipt.json';old.write_text('old-private-evidence')
    with pytest.raises(OracleBlocked,match='oracle_output_not_empty'):
        make_oracle(manifest).verify(candidate_root=ROOT,candidate_code_sha='d'*40,output=output)
    assert old.read_text()=='old-private-evidence'


def test_fixture_financial_pass_never_supplies_release_receipt(oracle_case,tmp_path):
    manifest,_path,_case,_missing=oracle_case
    result=make_oracle(manifest)(candidate_root=ROOT,candidate_code_sha='e'*40,diagnostic_root=tmp_path/'candidate-reports',output=tmp_path/'authority')
    assert result['status']=='blocked'
    assert result['reason_code']=='independent_release_checks_missing'
    assert json.loads((tmp_path/'authority/financial-oracle.json').read_text())['status']=='passed'


def rewrite_case(manifest,path,case):
    path.write_text(json.dumps(case))
    body=json.loads(manifest.read_text());body['cases'][0]['sha256']=file_sha(path)
    body['target_scopes']=sorted(case['expected']['scopes']);manifest.write_text(json.dumps(body))


def prepare_closed_case(oracle_case):
    from test_replay_service import expected_scope
    manifest,path,case,missing=oracle_case
    case['steps']=[{'operation':'drain_pending'},*[
        {'operation':'supplement','store_id':key.split('/')[0],'period':key.split('/')[1],
         'amount':'2.00','reason':'explicit synthetic original-cost approval','order_id':order}
        for key,order in sorted(missing.items())],{'operation':'drain_pending'},
        {'operation':'confirm','store_id':'s1','period':'2026-06','payouts':[{'person_id':'p1','amount':'8.00'}],
         'reason':'explicit synthetic payout fixture'},
        {'operation':'close_approved','store_id':'s1','period':'2026-06',
         'approval_reference':case['approval_reference'],'reason':'explicit synthetic close fixture'},
        {'operation':'recompute','store_id':'s1'}]
    case['expected']=all_expected(**{key:expected_scope(160,8,cost=2,can_close=True) for key in missing})
    case['expected']['scopes']['s1/2026-06'].update(state='closed',can_close=False,confirmed_total=8.0,confirmed_people={'p1':8.0})
    rewrite_case(manifest,path,case)
    return manifest


def test_actual_approved_close_stays_frozen_after_further_recompute(oracle_case,tmp_path):
    manifest=prepare_closed_case(oracle_case)
    report=make_oracle(manifest).verify(candidate_root=ROOT,candidate_code_sha='f'*40,output=tmp_path/'authority')
    assert report['status']=='passed',report.get('reason_code')
    assert report['cases'][0]['frozen_count']==1
    assert report['cases'][0]['immutable_count']>=2


def test_closed_result_tampering_is_rejected_outside_candidate(oracle_case,tmp_path):
    manifest=prepare_closed_case(oracle_case)
    candidate=tmp_path/'candidate';shutil.copytree(ROOT/'ledger/ledger',candidate/'ledger/ledger',ignore=shutil.ignore_patterns('__pycache__'))
    target=candidate/'ledger/ledger/replay_service.py'
    target.write_text(target.read_text()+'''\n
_before_oracle_recompute=Session.recompute
def _tampered_recompute(self,store_id):
    result=_before_oracle_recompute(self,store_id)
    closed=self.ws.conn.execute("SELECT run_id FROM period WHERE state='closed' LIMIT 1").fetchone()
    if closed:
        row=self.ws.conn.execute('SELECT result FROM run WHERE id=?',(closed[0],)).fetchone()
        payload=json.loads(row[0]);payload['statement'][0]['value']=999
        self.ws.conn.execute('UPDATE run SET result=? WHERE id=?',(json.dumps(payload),closed[0]))
        self.ws.conn.commit()
    return result
Session.recompute=_tampered_recompute
''')
    report=make_oracle(manifest).verify(candidate_root=candidate,candidate_code_sha='4'*40,output=tmp_path/'authority')
    assert report['status']=='blocked'
    assert report['reason_code']=='financial_result_proof_changed'
    assert not (tmp_path/'authority/acceptance-receipt.json').exists()


def test_expected_and_authority_are_absent_from_child_filesystem_and_proc(oracle_case,tmp_path):
    manifest,_path,_case,_missing=oracle_case
    secret=tmp_path/'hidden-expected.json';secret.write_text('{"expected":"parent-only"}')
    candidate=tmp_path/'candidate';shutil.copytree(ROOT/'ledger/ledger',candidate/'ledger/ledger',ignore=shutil.ignore_patterns('__pycache__'))
    probe=("from pathlib import Path\n"
        +f"assert not Path({str(secret)!r}).exists()\n"
        +f"assert not Path({('/proc/1/root'+str(secret))!r}).exists()\n"
        +"assert not Path('/work').exists()\nassert not Path('/out').exists()\n"
        +"assert not Path('/authority').exists()\n")
    target=candidate/'ledger/ledger/__init__.py';target.write_text(probe+target.read_text())
    report=make_oracle(manifest).verify(candidate_root=candidate,candidate_code_sha='1'*40,output=tmp_path/'authority')
    assert report['status']=='passed',report.get('reason_code')


def test_candidate_symlink_is_not_followed_by_host_export(oracle_case,tmp_path):
    manifest,_path,_case,_missing=oracle_case
    candidate=tmp_path/'candidate';shutil.copytree(ROOT/'ledger/ledger',candidate/'ledger/ledger',ignore=shutil.ignore_patterns('__pycache__'))
    (candidate/'ledger/ledger/__init__.py').write_text("import os\nos.symlink('/etc/passwd','/results/host-read-trap')\nos._exit(0)\n")
    report=make_oracle(manifest).verify(candidate_root=candidate,candidate_code_sha='2'*40,output=tmp_path/'authority')
    assert report['status']=='blocked' and report['reason_code']=='child_output_nonregular'
    assert not (tmp_path/'authority/child-artifacts/host-read-trap').exists()


def test_missing_original_costs_are_verified_pending_without_zero_profit(oracle_case,tmp_path):
    import polars as pl
    from ledger import freight_experiments_inputs as frozen
    manifest,path,case,_missing=oracle_case
    bundle=Path(case['bundle']);costs=bundle/'feed/order_costs.parquet'
    frame=pl.read_parquet(costs);missing=pl.col('order_id').cast(pl.Int64)<=20
    frame=frame.with_columns(
        pl.when(missing).then(None).otherwise(pl.col('unit_cost')).alias('unit_cost'),
        pl.when(missing).then(None).otherwise(pl.col('cost_amount')).alias('cost_amount'),
        pl.when(missing).then(pl.lit('missing_price')).otherwise(pl.col('cost_status')).alias('cost_status'))
    frame.write_parquet(costs)
    inputs=json.loads((bundle/'inputs.json').read_text())
    meta=inputs['feed_manifest']['objects']['order_costs.parquet']
    meta.update(sha256=file_sha(costs),bytes=costs.stat().st_size,rows=frame.height)
    (bundle/'inputs.json').write_text(json.dumps(inputs,ensure_ascii=False))
    state=json.loads((bundle/'feed-state.json').read_text());feed_manifest=json.loads(state['manifest_json'])
    feed_manifest['objects']['order_costs.parquet']=meta;state['manifest_json']=json.dumps(feed_manifest)
    (bundle/'feed-state.json').write_text(json.dumps(state));frozen.seal(bundle)
    expected=case['expected']['scopes']['s1/2026-06']
    expected['statement']['gross_profit']={'value':None,'available':False}
    expected['statement']['net_profit']={'value':None,'available':False}
    # The declared partial preview uses known income 20*10 and no recognized
    # cost, at the fixed 5% scheme. It is explicitly incomplete, never payable.
    expected.update(commission_total=10.0,commission_people={'p1':{'base':200.0,'amount':10.0}},commission_complete=False)
    rewrite_case(manifest,path,case)
    report=make_oracle(manifest).verify(candidate_root=ROOT,candidate_code_sha='3'*40,output=tmp_path/'authority')
    assert report['status']=='passed',report.get('reason_code')
    assert report['financial_approval_granted'] is False
