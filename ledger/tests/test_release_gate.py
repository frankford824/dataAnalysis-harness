import copy
from pathlib import Path

import pytest

from ledger.release_gate import REQUIRED_CHECKS, digest, file_sha, validate


def receipt(tmp_path):
    (tmp_path / 'report.xml').write_text('<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="synthetic_receipt_contract"/></testsuite></testsuites>')
    inputs = [{'path': 'source.xlsx', 'sha256': 'b' * 64}]
    value = dict(schema=1, code_sha='a' * 40, model_sha='c' * 64,
                 inputs=inputs, input_sha=digest(inputs), unresolved_differences=0, inputs_unchanged=True,
                 checks=[dict(name=name, exit_code=0, tests=1, failures=0, errors=0, skipped=0,
                              report='report.xml', report_sha256=file_sha(tmp_path / 'report.xml'))
                         for name in sorted(REQUIRED_CHECKS)])
    source=[{'path':'ledger/ledger/api.py','sha256':'d'*64}]
    value.update(source_code_manifest=source,source_code_sha=digest(source))
    return seal(value)


def seal(value):
    value['receipt_sha'] = digest({k: v for k, v in value.items() if k != 'receipt_sha'})
    return value


def test_accepts_complete_matching_receipt(tmp_path):
    validate(receipt(tmp_path), version='a' * 40, model_sha='c' * 64, evidence_root=tmp_path)


@pytest.mark.parametrize('mutation', ['version', 'model', 'inputs', 'missing_check', 'skip',
                                      'failed', 'zero_tests', 'unresolved', 'tampered', 'duplicate'])
def test_refuses_unqualified_release(tmp_path, mutation):
    value = receipt(tmp_path)
    if mutation == 'version': value['code_sha'] = 'b' * 40
    elif mutation == 'model': value['model_sha'] = 'd' * 64
    elif mutation == 'inputs': value['inputs'][0]['sha256'] = 'e' * 64
    elif mutation == 'missing_check': value['checks'].pop()
    elif mutation == 'skip': value['checks'][0]['skipped'] = 1
    elif mutation == 'failed': value['checks'][0]['exit_code'] = 1
    elif mutation == 'zero_tests': value['checks'][0]['tests'] = 0
    elif mutation == 'unresolved': value['unresolved_differences'] = 1
    elif mutation == 'tampered': value['checks'][0]['report_sha256'] = 'e' * 64
    elif mutation == 'duplicate': value['checks'].append(copy.deepcopy(value['checks'][0]))
    if mutation != 'tampered': seal(value)
    with pytest.raises(ValueError):
        validate(value, version='a' * 40, model_sha='c' * 64, evidence_root=tmp_path)


def test_report_content_and_path_are_protected(tmp_path):
    value = receipt(tmp_path)
    (tmp_path / 'report.xml').write_text('altered')
    with pytest.raises(ValueError, match='report changed'):
        validate(value, version='a' * 40, evidence_root=tmp_path)


@pytest.mark.parametrize('report',['<testsuites/>',
    '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="hidden_failure"><failure>failed</failure></testcase></testsuite></testsuites>'])
def test_resealed_report_cannot_claim_empty_or_failed_cases_passed(tmp_path,report):
    value=receipt(tmp_path);(tmp_path/'report.xml').write_text(report)
    for check in value['checks']:check['report_sha256']=file_sha(tmp_path/'report.xml')
    seal(value)
    with pytest.raises(ValueError,match='statistics'):
        validate(value,version='a'*40,evidence_root=tmp_path)
    value['checks'][0]['report'] = '../outside.xml'
    seal(value)
    with pytest.raises(ValueError, match='outside'):
        validate(value, version='a' * 40, evidence_root=tmp_path)
