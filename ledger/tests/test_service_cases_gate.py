"""Gate contract tests never label the real-service synthetic fixture as history."""
import copy
import importlib.util
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from ledger import freight_experiments_inputs as frozen
from ledger import replay_service
from ledger.freight_trial_inputs import digest
from ledger.model.schema import Model, Platform, Store
from test_replay_service import all_expected, bundle, seed


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('service_cases_gate', ROOT / 'tools/verify-ledger-service-cases.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


@pytest.fixture
def declared_materials(tmp_path):
    """Hash-valid small material to test rejection, never real historical evidence."""
    model = Model(id='declared-model', name='待核来源模型',
        platforms=(Platform(id='taobao', name='淘宝'), Platform(id='douyin', name='抖音')),
        stores=(Store(id='t1', name='甲', platform='taobao'), Store(id='d1', name='乙', platform='douyin')))
    directory = tmp_path / 'declared-bundle'
    directory.mkdir()
    source = b'input material for gate contract tests only'
    import hashlib
    source_sha = hashlib.sha256(source).hexdigest()
    source_file = directory / 'files' / source_sha[:2] / source_sha
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(source)
    write(directory / 'model.json', json.loads(model.model_dump_json()))
    write(directory / 'classification.json', {'version': 'declared-v1'})
    write(directory / 'inputs.json', {'period': '2026-07', 'model_hash': gate.model_digest(model),
        'slots': [{'store_id': 't1', 'name': '订单原表.xlsx', 'sha': source_sha}],
        'files': [{'sha': source_sha, 'name': '订单原表.xlsx'}]})
    frozen.seal(directory)
    scopes = {sid + '/' + period: {} for sid in ('t1', 'd1') for period in ('2026-06', '2026-07')}
    case = {'schema_version': 1, 'case_id': 'declared-202607', 'kind': 'historical_golden',
        'approval_reference': 'approval/declared-case', 'bundle': directory.name,
        'registry': {'people': [], 'members': [], 'schemes': []},
        'steps': [{'operation': 'recompute', 'store_id': 't1'}], 'expected': {'scopes': scopes},
        'source_evidence': {'kind': 'historical_export', 'reference': 'source-review/declared',
                            'input_sha256': [source_sha]}}
    case_path = write(tmp_path / 'case.json', case)
    manifest = {'schema_version': 1, 'kind': 'historical_golden',
        'approval_reference': 'approval/declared-scope', 'latest_period': '2026-07',
        'target_scopes': sorted(scopes), 'cases': [{'path': case_path.name, 'sha256': gate._sha(case_path)}]}
    manifest_path = write(tmp_path / 'approved.json', manifest)
    return directory, case_path, manifest_path, model


def change_case(materials, mutate):
    _directory, case_path, manifest_path, _model = materials
    case = json.loads(case_path.read_text())
    mutate(case)
    write(case_path, case)
    manifest = json.loads(manifest_path.read_text())
    manifest['cases'][0]['sha256'] = gate._sha(case_path)
    write(manifest_path, manifest)


def never_execute(_case):
    pytest.fail('invalid historical material must not reach replay_service.execute')


def failed_service(directory, observed):
    """A contract-only double: deliberately cannot qualify historical evidence."""
    def execute(case):
        observed.append(case)
        scopes = copy.deepcopy(case['expected']['scopes'])
        return {'case_id': case['case_id'], 'kind': case['kind'], 'status': 'failed',
            'differences': [{'path': 'scopes', 'kind': 'changed'}],
            'checked_scopes': sorted(scopes), 'actual': {'scopes': scopes,
                'input_bundle_fingerprint': frozen.verify(directory)[1]}}
    return execute


def test_no_manifest_or_missing_materials_leaves_failed_junit_and_blocked_attempt(tmp_path):
    output, junit = tmp_path / 'attempt.json', tmp_path / 'service.xml'
    assert gate.main(['--candidate-model', str(ROOT / 'models/cn-ecommerce'), '--target-period', '2026-07',
                      '--output', str(output), '--junit', str(junit)]) == 2
    attempt = json.loads(output.read_text())
    assert attempt['status'] == 'blocked'
    assert attempt['model_digest'] and attempt['required_platforms']
    assert attempt['unverified_platforms'] == attempt['required_platforms']
    suite = ET.parse(junit).getroot()
    assert suite.get('failures') == '1' and suite.get('skipped') == '0'
    assert suite.find('properties/property[@name="target_period"]').get('value') == '2026-07'
    assert suite.find('testcase/failure') is not None
    assert not (tmp_path / 'acceptance-receipt.json').exists()
    attempt = gate.evaluate(ROOT / 'models/cn-ecommerce', tmp_path / 'missing.json', target_period='2026-07')
    assert attempt['status'] == 'blocked'


@pytest.mark.parametrize('mutate', [
    lambda case: case.update(kind='service_fixture'),
    lambda case: case.update(approval_reference=''),
    lambda case: case.update(expected={'scopes': {}}),
    lambda case: case.pop('source_evidence'),
    lambda case: case['source_evidence'].update(input_sha256=[]),
    lambda case: case['source_evidence'].update(input_sha256=['0' * 64]),
    lambda case: case.update(case_id='service-fixture-relabelled'),
    lambda case: case['source_evidence'].update(reference='synthetic-source'),
])
def test_missing_approval_expected_sources_or_fixture_labels_fail_closed(declared_materials, monkeypatch, mutate):
    directory, _case_path, manifest_path, _model = declared_materials
    change_case(declared_materials, mutate)
    monkeypatch.setattr(replay_service, 'execute', never_execute)
    attempt = gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')
    assert attempt['status'] == 'blocked'
    assert attempt['verified_scopes'] == []
    assert attempt['cases'][0]['status'] == 'blocked'


def test_case_sha_cannot_be_replaced_by_a_new_unapproved_file(declared_materials, monkeypatch):
    directory, case_path, manifest_path, _model = declared_materials
    case_path.write_text(case_path.read_text() + '\n')
    monkeypatch.setattr(replay_service, 'execute', never_execute)
    attempt = gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')
    assert attempt['cases'][0]['declared_sha256'] != attempt['cases'][0]['case_sha256']
    assert 'SHA256' in attempt['cases'][0]['issues'][0]['message']


def test_old_bundle_cannot_certify_new_candidate_semantic_model(declared_materials, monkeypatch, tmp_path):
    directory, _case_path, manifest_path, model = declared_materials
    changed = model.model_copy(update={'name': '新版候选模型'})
    candidate_path = write(tmp_path / 'new-model.json', json.loads(changed.model_dump_json()))
    monkeypatch.setattr(replay_service, 'execute', never_execute)
    attempt = gate.evaluate(candidate_path, manifest_path, target_period='2026-07')
    assert attempt['status'] == 'blocked'
    assert attempt['model_digest'] != attempt['cases'][0]['model_digest']
    assert 'model_digest' in attempt['cases'][0]['issues'][0]['message']
    inputs = json.loads((directory / 'inputs.json').read_text())
    inputs['model_hash'] = attempt['model_digest']
    write(directory / 'inputs.json', inputs)
    frozen.seal(directory)
    assert gate.evaluate(candidate_path, manifest_path, target_period='2026-07')['status'] == 'blocked'


def test_platform_and_adjacent_month_coverage_cannot_be_shrunk(declared_materials, monkeypatch):
    directory, _case_path, manifest_path, _model = declared_materials
    manifest = json.loads(manifest_path.read_text())
    manifest['target_scopes'] = ['t1/2026-06', 't1/2026-07']
    write(manifest_path, manifest)
    monkeypatch.setattr(replay_service, 'execute', lambda case: {
        'case_id': case['case_id'], 'kind': case['kind'], 'status': 'failed',
        'differences': [{'path': 'scopes', 'kind': 'changed'}],
        'checked_scopes': [], 'actual': {'scopes': {},
            'input_bundle_fingerprint': frozen.verify(directory)[1]}})
    attempt = gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')
    assert attempt['required_platforms'] == ['douyin', 'taobao']
    assert attempt['unverified_platforms'] == ['douyin', 'taobao']
    assert any(issue['code'] == 'target_platform_coverage' and 'douyin' in issue['message'] for issue in attempt['issues'])
    assert attempt['cases'][0]['result']['status'] == 'failed'
    manifest['target_scopes'] = ['t1/2026-05', 't1/2026-07']
    write(manifest_path, manifest)
    assert '最近相邻两月' in gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')['issues'][0]['message']


def test_catalog_only_and_archived_store_platforms_do_not_require_history(declared_materials, monkeypatch):
    directory, _case_path, manifest_path, model = declared_materials
    model = model.model_copy(update={
        'platforms': (*model.platforms, Platform(id='future', name='未来平台'), Platform(id='closed', name='停业平台')),
        'stores': (*model.stores, Store(id='closed-store', name='停业店', platform='closed', archived=True))})
    write(directory / 'model.json', json.loads(model.model_dump_json()))
    inputs = json.loads((directory / 'inputs.json').read_text())
    inputs['model_hash'] = gate.model_digest(model)
    write(directory / 'inputs.json', inputs)
    frozen.seal(directory)
    observed = []
    monkeypatch.setattr(replay_service, 'execute', failed_service(directory, observed))
    attempt = gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')
    assert attempt['required_platforms'] == ['douyin', 'taobao']
    assert attempt['catalog_only_platforms'] == ['closed', 'future']
    assert attempt['target_platforms'] == ['douyin', 'taobao']
    assert not any(issue['code'].startswith('target_') for issue in attempt['issues'])
    assert len(observed) == 1 and attempt['status'] != 'passed'


def test_platform_operating_one_month_does_not_need_fabricated_zero_month(declared_materials, monkeypatch):
    directory, _case_path, manifest_path, _model = declared_materials
    change_case(declared_materials, lambda case: case['expected']['scopes'].pop('d1/2026-06'))
    manifest = json.loads(manifest_path.read_text())
    manifest['target_scopes'] = ['t1/2026-06', 't1/2026-07', 'd1/2026-07']
    write(manifest_path, manifest)
    observed = []
    monkeypatch.setattr(replay_service, 'execute', failed_service(directory, observed))
    attempt = gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')
    assert attempt['target_platforms'] == ['douyin', 'taobao']
    assert attempt['target_periods'] == ['2026-06', '2026-07']
    assert attempt['target_cross_period_stores'] == ['t1']
    assert not any(issue['code'].startswith('target_') for issue in attempt['issues'])
    assert len(observed) == 1 and attempt['status'] != 'passed'


def test_two_months_in_different_stores_do_not_prove_same_store_replay(declared_materials, monkeypatch):
    directory, _case_path, manifest_path, _model = declared_materials
    change_case(declared_materials, lambda case: case['expected'].update(scopes={
        key: value for key, value in case['expected']['scopes'].items()
        if key in {'t1/2026-06', 'd1/2026-07'}}))
    manifest = json.loads(manifest_path.read_text())
    manifest['target_scopes'] = ['t1/2026-06', 'd1/2026-07']
    write(manifest_path, manifest)
    monkeypatch.setattr(replay_service, 'execute', failed_service(directory, []))
    attempt = gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')
    assert attempt['target_platforms'] == ['douyin', 'taobao']
    assert attempt['target_periods'] == ['2026-06', '2026-07']
    assert attempt['target_cross_period_stores'] == []
    assert any(issue['code'] == 'target_same_store_coverage' for issue in attempt['issues'])
    assert attempt['status'] == 'blocked'


def test_explicit_target_anchor_cannot_silently_fall_back_to_manifest_month(declared_materials, monkeypatch):
    directory, _case_path, manifest_path, _model = declared_materials
    manifest = json.loads(manifest_path.read_text())
    manifest['latest_period'] = '2026-06'
    write(manifest_path, manifest)
    monkeypatch.setattr(replay_service, 'execute', never_execute)
    attempt = gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')
    assert attempt['status'] == 'blocked'
    assert attempt['target_period'] == '2026-07' and attempt['manifest_latest_period'] == '2026-06'
    assert '禁止静默回退' in attempt['issues'][0]['message']


def test_target_period_is_required_and_validated(tmp_path):
    args = ['--candidate-model', str(ROOT / 'models/cn-ecommerce'),
            '--output', str(tmp_path / 'attempt.json'), '--junit', str(tmp_path / 'service.xml')]
    with pytest.raises(SystemExit) as rejected:
        gate.main(args)
    assert rejected.value.code == 2
    attempt = gate.evaluate(ROOT / 'models/cn-ecommerce', target_period='2026-00')
    assert attempt['status'] == 'blocked' and '目标月份' in attempt['issues'][0]['message']


@pytest.mark.parametrize('key,value', [('target_scopes', []), ('cases', []), ('latest_period', '2026-00')])
def test_empty_or_invalid_manifest_scope_is_blocked(declared_materials, key, value):
    directory, _case_path, manifest_path, _model = declared_materials
    manifest = json.loads(manifest_path.read_text())
    manifest[key] = value
    write(manifest_path, manifest)
    assert gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')['status'] == 'blocked'


def test_service_is_called_and_output_scopes_cannot_be_suppressed(declared_materials, monkeypatch, tmp_path):
    directory, _case_path, manifest_path, _model = declared_materials
    observed = []

    def output_with_missing_scope(case):
        observed.append(case)
        scopes = copy.deepcopy(case['expected']['scopes'])
        scopes.pop('d1/2026-07')
        return {'case_id': case['case_id'], 'kind': case['kind'], 'status': 'passed', 'differences': [],
            'checked_scopes': sorted(scopes), 'actual': {'scopes': scopes,
                'input_bundle_fingerprint': frozen.verify(directory)[1]}}

    monkeypatch.setattr(replay_service, 'execute', output_with_missing_scope)
    attempt = gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')
    assert len(observed) == 1
    assert observed[0]['bundle'] == str(directory)
    assert attempt['status'] != 'passed'
    case = attempt['cases'][0]
    assert case['case_sha256'] and case['bundle_fingerprint'] and case['model_digest']
    assert case['output_scopes'] == case['checked_scopes']
    assert any(issue['code'] == 'scope_mismatch' for issue in case['issues'])
    assert attempt['missing_scopes'] == sorted(observed[0]['expected']['scopes'])
    gate.write_reports(attempt, tmp_path / 'attempt.json', tmp_path / 'junit.xml')
    assert int(ET.parse(tmp_path / 'junit.xml').getroot().get('failures')) > 0


def test_case_mutation_during_execute_is_detected(declared_materials, monkeypatch):
    directory, case_path, manifest_path, _model = declared_materials

    def mutate_during_service(case):
        case_path.write_text(case_path.read_text() + '\n')
        scopes = case['expected']['scopes']
        return {'case_id': case['case_id'], 'kind': case['kind'], 'status': 'passed', 'differences': [],
            'checked_scopes': sorted(scopes), 'actual': {'scopes': scopes,
                'input_bundle_fingerprint': frozen.verify(directory)[1]}}

    monkeypatch.setattr(replay_service, 'execute', mutate_during_service)
    attempt = gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')
    assert attempt['status'] == 'blocked' and not attempt['verified_scopes']
    assert '发生变化' in attempt['cases'][0]['issues'][0]['message']


def test_reports_cannot_overwrite_approved_inputs_or_candidate_model(declared_materials, tmp_path):
    directory, case_path, manifest_path, _model = declared_materials
    attempt = gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')
    before = gate._sha(case_path), gate._sha(manifest_path), gate._sha(directory / 'model.json')
    for destination in (case_path, manifest_path, directory / 'model.json', directory / 'report.json'):
        with pytest.raises(ValueError, match='不能覆盖'):
            gate.write_reports(attempt, destination, tmp_path / 'service.xml')
    with pytest.raises(ValueError, match='不同文件'):
        gate.write_reports(attempt, tmp_path / 'same.json', tmp_path / 'same.json')
    assert before == (gate._sha(case_path), gate._sha(manifest_path), gate._sha(directory / 'model.json'))


def test_positive_actual_service_fixture_executes_but_never_qualifies_as_real_history(bundle, tmp_path):
    """Real files/feed/registry/recompute; hand-authored fixture expected is unchanged."""
    directory, _missing = bundle
    case = {'schema_version': 1, 'case_id': 'service-fixture-two-months', 'kind': 'service_fixture',
        'bundle': str(directory), 'registry': seed(), 'steps': [
            {'operation': 'recompute', 'store_id': 's1'}, {'operation': 'recompute', 'store_id': 's2'}],
        'expected': all_expected()}
    result = replay_service.execute(case)
    assert result['status'] == 'passed', result.get('differences')
    assert len(result['checked_scopes']) == 4
    inputs, fingerprint = frozen.verify(directory)
    assert result['actual']['input_bundle_fingerprint'] == fingerprint
    case.update(kind='historical_golden', approval_reference='approval/forged-history',
        source_evidence={'kind': 'historical_export', 'reference': 'source/forged-history',
                         'input_sha256': sorted({slot['sha'] for slot in inputs['slots']})})
    # Even a relabeled case cannot wash the synthetic bundle's provenance markers.
    case['case_id'] = 'declared-two-months'
    case_path = write(tmp_path / 'relabelled.json', case)
    manifest_path = write(tmp_path / 'approved.json', {'schema_version': 1, 'kind': 'historical_golden',
        'approval_reference': 'approval/forged-scope', 'latest_period': '2026-07',
        'target_scopes': sorted(case['expected']['scopes']),
        'cases': [{'path': case_path.name, 'sha256': gate._sha(case_path)}]})
    attempt = gate.evaluate(directory / 'model.json', manifest_path, target_period='2026-07')
    assert attempt['status'] == 'blocked'
    assert attempt['verified_scopes'] == []
    assert '禁止冒充真实历史' in attempt['cases'][0]['issues'][0]['message']
    assert attempt['cases'][0]['bundle_fingerprint'] == fingerprint


def approved_scope_material(materials,*,attach_case=True):
    directory,case_path,manifest_path,_model=materials
    source=write(manifest_path.parent/'approved-workspace.json',{'schema_version':1,
        'kind':'approved_workspace_decisions','approval_reference':'approval/operating-ledger',
        'decisions':[{'period':'2026-06','store_id':'d1','kind':'store_scope',
            'values':{'included':False,'valid_from':'2026-06','valid_until':'2026-07'},
            'reason':'已核对该店两个月没有营业，排除依据留档'}]})
    reference={'path':source.name,'sha256':gate._sha(source),'approval_reference':'approval/operating-ledger'}
    manifest=json.loads(manifest_path.read_text());manifest['workspace_decisions']=reference
    manifest['target_scopes']=['t1/2026-06','t1/2026-07']
    write(manifest_path,manifest)
    def update(case):
        case['expected']['scopes']={k:v for k,v in case['expected']['scopes'].items() if k.startswith('t1/')}
        if attach_case:
            case['workspace_decisions']=reference
            case['source_evidence']['workspace_decisions_sha256']=reference['sha256']
    change_case(materials,update)
    return reference,source


def failed_service_with_scope(directory,observed):
    execute=failed_service(directory,observed)
    def run(case):
        result=execute(case);reference=case['workspace_decisions']
        result['actual']['workspace_decisions']={'source_sha256':reference['sha256'],
            'approval_reference':reference['approval_reference'],'kind':'approved_workspace_decisions','offline_only':True}
        return result  # Financial result remains failed; never a fabricated historical green.
    return run


def test_only_bound_approved_decisions_can_change_operating_platform_scope(declared_materials,monkeypatch):
    directory,_case,manifest_path,_model=declared_materials
    reference,_source=approved_scope_material(declared_materials)
    observed=[];monkeypatch.setattr(replay_service,'execute',failed_service_with_scope(directory,observed))
    attempt=gate.evaluate(directory/'model.json',manifest_path,target_period='2026-07')
    assert attempt['required_platforms']==['taobao'] and attempt['unverified_platforms']==['taobao']
    assert attempt['approved_operating_stores']=={'2026-06':['t1'],'2026-07':['t1']}
    assert attempt['workspace_decision_receipt']['source_sha256']==reference['sha256']
    assert not any(issue['code'].startswith('target_') for issue in attempt['issues'])
    assert len(observed)==1 and attempt['status']!='passed'
    assert attempt['cases'][0]['status']=='failed',attempt['cases'][0]['issues']
    assert Path(observed[0]['workspace_decisions']['path']).is_absolute()


@pytest.mark.parametrize('mode',['missing_case_reference','missing_source_sha','different_case_source'])
def test_scope_cannot_be_reduced_with_unbound_or_conflicting_case_decisions(declared_materials,monkeypatch,mode):
    directory,_case,manifest_path,_model=declared_materials
    reference,source=approved_scope_material(declared_materials,attach_case=mode!='missing_case_reference')
    if mode=='missing_source_sha':change_case(declared_materials,lambda case:case['source_evidence'].pop('workspace_decisions_sha256'))
    if mode=='different_case_source':
        body=json.loads(source.read_text());body['decisions'][0]['reason']='另一份未经本manifest批准的范围版本'
        other=write(source.with_name('different-workspace.json'),body)
        change_case(declared_materials,lambda case:case['workspace_decisions'].update(path=other.name,sha256=gate._sha(other)))
    monkeypatch.setattr(replay_service,'execute',never_execute)
    attempt=gate.evaluate(directory/'model.json',manifest_path,target_period='2026-07')
    assert attempt['status']=='blocked' and not attempt['verified_scopes']
    assert attempt['cases'][0]['status']=='blocked'


def test_service_must_actually_restore_same_scope_instead_of_echoing_declaration(declared_materials,monkeypatch):
    directory,_case,manifest_path,_model=declared_materials
    approved_scope_material(declared_materials)
    monkeypatch.setattr(replay_service,'execute',failed_service(directory,[]))
    attempt=gate.evaluate(directory/'model.json',manifest_path,target_period='2026-07')
    assert attempt['status']=='blocked'
    assert '没有恢复' in attempt['cases'][0]['issues'][0]['message']


def test_scope_artifact_mutation_or_fixture_kind_cannot_reuse_approval(declared_materials,monkeypatch):
    directory,_case,manifest_path,_model=declared_materials
    _reference,source=approved_scope_material(declared_materials)
    source.write_text(source.read_text()+'\n')
    monkeypatch.setattr(replay_service,'execute',never_execute)
    attempt=gate.evaluate(directory/'model.json',manifest_path,target_period='2026-07')
    assert attempt['status']=='blocked' and not attempt['cases']
    # Updating a hash does not convert a declared fixture into approved history.
    body=json.loads(source.read_text());body['kind']='service_fixture_workspace_decisions';write(source,body)
    manifest=json.loads(manifest_path.read_text());manifest['workspace_decisions']['sha256']=gate._sha(source);write(manifest_path,manifest)
    attempt=gate.evaluate(directory/'model.json',manifest_path,target_period='2026-07')
    assert attempt['status']=='blocked' and not attempt['cases']
