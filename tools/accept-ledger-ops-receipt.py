"""Trusted CI handoff: bind the generated receipt to a frozen repair automatically.

This does not run candidate code, invent a check, invoke a model or deploy. The
receipt directory must be owned by the approved gate controller and must never
be mounted writable in a candidate sandbox.
"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'ledger'),str(ROOT/'tools')]
from ledger.release_gate import file_sha,validate
from ledger_acceptance_inputs import verify as verify_inputs
from ops.ledger_sentinel.financial_oracle import FinancialOracle
from ops.ledger_sentinel.repair import Repair


def accept(config,repair_id,receipts_root,*,expected_gate_sha):
    if not isinstance(expected_gate_sha,str) or not re.fullmatch('[0-9a-f]{64}',expected_gate_sha):
        raise ValueError('A fixed approved gate implementation SHA is required')
    repo=Path(config.get('repo_root',ROOT)).resolve();setting=config['repair']
    repair=Repair(repo,Path(setting['home']),setting['executor'])
    _job,tree,body=repair._read(repair_id)
    candidate=body.get('candidate_code_sha')
    if not isinstance(candidate,str) or not re.fullmatch('[0-9a-f]{40}',candidate) or body['phase']=='editing':
        raise ValueError('A frozen candidate is required')
    receipts_root=Path(receipts_root).resolve()
    if (receipts_root.is_relative_to(repo) or receipts_root.is_relative_to(repair.home)
            or receipts_root.is_relative_to(tree)):
        raise ValueError('Trusted CI receipts must be outside model/code/repair result directories')
    path=receipts_root/candidate/'acceptance-receipt.json'
    if path.is_symlink() or not path.resolve().is_relative_to(receipts_root) or not path.is_file():
        raise ValueError('Trusted CI has not issued this candidate receipt')
    receipt=json.loads(path.read_text(encoding='utf-8'))
    validate(receipt,version=candidate,evidence_root=path.parent)
    verify_inputs(receipt)
    if (receipt.get('gate_runner',{}).get('kind')!='controlled_full_gate.v1'
            or receipt.get('gate_runner',{}).get('sha256')!=expected_gate_sha):
        raise ValueError('Receipt came from another or unknown gate implementation')
    spec=dict(setting['executor'].get('financial_oracle') or {})
    if spec.pop('kind',None)!='external_service_cases.v1':raise ValueError('Independent financial authority is not configured')
    # The host computes the per-candidate SHA from its private CI artifact. No
    # human/model must hand-edit a new approval hash for every generated patch.
    spec['trusted_release']={'path':str(path),'sha256':file_sha(path)}
    repair.oracle=FinancialOracle(**spec)
    return repair.accept(repair_id)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--repair-id',required=True)
    parser.add_argument('--receipts-root',type=Path,required=True)
    parser.add_argument('--expected-gate-sha',required=True)
    args=parser.parse_args()
    result=accept(json.loads(args.config.read_text(encoding='utf-8')),args.repair_id,args.receipts_root,
        expected_gate_sha=args.expected_gate_sha)
    print(json.dumps({key:result.get(key) for key in ('repair_id','acceptance_state','acceptance_reason','candidate_code_sha')},ensure_ascii=False))
    raise SystemExit(0 if result.get('acceptance_state')=='passed' else 1)


if __name__=='__main__':main()
