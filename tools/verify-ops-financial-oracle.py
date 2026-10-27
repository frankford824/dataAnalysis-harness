"""Run the external service oracle; this command grants no financial approval."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'ledger'))
from ops.ledger_sentinel.financial_oracle import FinancialOracle


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,type=Path)
    parser.add_argument('--manifest-sha256',required=True)
    parser.add_argument('--implementation-sha256',required=True)
    parser.add_argument('--candidate',required=True,type=Path)
    parser.add_argument('--candidate-code-sha',required=True)
    parser.add_argument('--runtime',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    oracle=FinancialOracle(manifest=args.manifest,manifest_sha256=args.manifest_sha256,
        implementation_sha256=args.implementation_sha256,runtime=args.runtime)
    report=oracle.verify(candidate_root=args.candidate,candidate_code_sha=args.candidate_code_sha,output=args.output)
    print(json.dumps({key:report.get(key) for key in ('status','reason_code','proof_sha256','candidate_execution_isolated')}))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
