"""A trusted gate handoff never upgrades an absent financial oracle to success."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))

from ledger.release_gate import digest
from ops.ledger_sentinel.financial_oracle import implementation_sha
from ops.ledger_sentinel.repair import Repair
from test_release_package import package_case,sources,seal

GATE='1'*64


def module():
    spec=importlib.util.spec_from_file_location('ops_handoff_test',REPO/'tools/accept-ledger-ops-receipt.py')
    tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool);return tool


@pytest.fixture
def handoff(package_case,tmp_path):
    case=package_case
    config={'repo_root':str(case['repo']),'repair':{'home':str(tmp_path/'repair'),
        'executor':{'checks':[],'financial_oracle':{'kind':'external_service_cases.v1',
            'manifest':str(tmp_path/'missing-approved-cases.json'),'manifest_sha256':'f'*64,
            'runtime':sys.prefix,'implementation_sha256':implementation_sha()}}}}
    repair=Repair(case['repo'],Path(config['repair']['home']),config['repair']['executor'])
    job=repair.prepare({'status':'failed'},case['version']);identifier=job['repair_id']
    repair.write_code(identifier,'ledger/ledger/example.py','VALUE = 3\n')
    frozen=repair.freeze_patch(identifier);candidate=frozen['candidate_code_sha']
    root=tmp_path/'trusted-ci';target=root/candidate;shutil.copytree(case['evidence'],target)
    value=deepcopy(case['receipt']);manifest=sources({'repo':Path(job['worktree']),'version':candidate})
    value.update(code_sha=candidate,source_code_manifest=manifest,source_code_sha=digest(manifest),
        gate_runner={'kind':'controlled_full_gate.v1','sha256':GATE});seal(value)
    path=target/'acceptance-receipt.json';path.write_text(json.dumps(value))
    return config,identifier,root,path,value,case,Path(job['worktree'])


def test_qualified_mechanical_receipt_still_cannot_replace_missing_real_financial_cases(handoff):
    config,identifier,root,_path,_value,_case,_tree=handoff
    result=module().accept(config,identifier,root,expected_gate_sha=GATE)
    assert result['acceptance_state']=='blocked'
    assert result.get('acceptance_receipt_sha') is None


@pytest.mark.parametrize('kind',['missing_receipt','unknown_runner','changed_original','candidate_output','bad_fixed_sha'])
def test_handoff_refuses_untrusted_or_changed_receipts_before_financial_approval(handoff,kind):
    config,identifier,root,path,value,case,tree=handoff
    gate=GATE
    if kind=='missing_receipt':path.unlink()
    elif kind=='unknown_runner':
        value['gate_runner']['sha256']='2'*64;seal(value);path.write_text(json.dumps(value))
    elif kind=='changed_original':(case['corpus']/'source.xlsx').write_bytes(b'changed input')
    elif kind=='candidate_output':root=tree/'out'
    else:gate=''
    with pytest.raises(ValueError):module().accept(config,identifier,root,expected_gate_sha=gate)
