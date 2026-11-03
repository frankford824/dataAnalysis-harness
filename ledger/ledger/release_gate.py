"""Hash-bound acceptance receipts. This module never changes financial data."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

COMMON_CHECKS = frozenset({'backend', 'corpus', 'invariants', 'browser', 'frontend_build', 'agent_worker', 'ops'})
REQUIRED_CHECKS = COMMON_CHECKS | {'service_replay'}
# When no closed store-month was produced by the engine, the owner attests that
# fact instead of approving replay cases; the verifier rejects the attestation
# as soon as any closed month could have been replayed.
ATTESTED_CHECKS = COMMON_CHECKS | {'baseline_attestation'}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def validate(receipt: dict, *, version: str, model_sha: str | None = None,
             evidence_root: Path | None = None) -> dict:
    if receipt.get('schema') != 1 or not re.fullmatch('[0-9a-f]{40}', version):
        raise ValueError('Invalid receipt schema or full commit identity')
    if receipt.get('code_sha') != version or type(receipt.get('unresolved_differences')) is not int or receipt['unresolved_differences'] != 0:
        raise ValueError('Receipt version mismatch or unresolved business differences')
    if receipt.get('inputs_unchanged') is not True:
        raise ValueError('Inputs changed while checks executed')
    if 'source_code_sha' in receipt or 'source_code_manifest' in receipt:
        source=receipt.get('source_code_manifest')
        if (not isinstance(source,list) or not source or any(not isinstance(row,dict) for row in source)
                or receipt.get('source_code_sha')!=digest(source)
                or len({row.get('path') for row in source})!=len(source)
                or any(not isinstance(row.get('path'),str) or not row['path'].startswith('ledger/ledger/')
                    or not row['path'].endswith('.py') or not re.fullmatch('[0-9a-f]{64}',row.get('sha256','')) for row in source)):
            raise ValueError('Missing or altered complete source code identity')
    if not re.fullmatch('[0-9a-f]{64}', receipt.get('model_sha', '')):
        raise ValueError('Missing rule/model identity')
    if model_sha is not None and receipt['model_sha'] != model_sha:
        raise ValueError('Receipt model identity mismatch')
    inputs = receipt.get('inputs', [])
    if not inputs or receipt.get('input_sha') != digest(inputs):
        raise ValueError('Missing or altered input manifest')
    if len({row.get('path') for row in inputs}) != len(inputs):
        raise ValueError('Duplicate input identities')
    for row in inputs:
        if not row.get('path') or not re.fullmatch('[0-9a-f]{64}', row.get('sha256', '')):
            raise ValueError('Invalid input identity')
    checks = receipt.get('checks', [])
    if len({c.get('name') for c in checks}) != len(checks):
        raise ValueError('Duplicate check name')
    names = {c.get('name') for c in checks}
    if 'baseline_attestation' in names:
        attestation = receipt.get('baseline_attestation')
        if 'service_replay' in names or not ATTESTED_CHECKS.issubset(names):
            raise ValueError('Required checks were not run')
        if (not isinstance(attestation, dict) or attestation.get('kind') != 'no_engine_baseline'
                or not str(attestation.get('approval_reference') or '').strip()
                or not re.fullmatch('[0-9a-f]{64}', str(attestation.get('sha256', '')))
                or attestation['sha256'] not in {row.get('sha256') for row in inputs}):
            raise ValueError('Baseline attestation is missing or not bound to the inputs')
    elif not REQUIRED_CHECKS.issubset(names):
        raise ValueError('Required checks were not run')
    for check in checks:
        if (any(type(check.get(k)) is not int for k in ('exit_code','tests','failures','errors','skipped'))
                or check.get('exit_code') != 0 or check.get('tests', 0) <= 0
                or any(check.get(k) != 0 for k in ('failures', 'errors', 'skipped'))
                or not re.fullmatch('[0-9a-f]{64}', check.get('report_sha256', ''))):
            raise ValueError(f"Check did not pass completely: {check.get('name')}")
        if evidence_root is not None:
            report = (evidence_root / check.get('report', '')).resolve()
            if not report.is_relative_to(evidence_root.resolve()) or not report.is_file():
                raise ValueError('Check report outside evidence directory or missing')
            if file_sha(report) != check['report_sha256']:
                raise ValueError('Check report changed after execution')
            if report.suffix=='.xml':
                try:
                    root=ET.parse(report).getroot()
                    suites=[root] if root.tag=='testsuite' else root.findall('testsuite')
                    stats={key:sum(int(suite.get(key,0)) for suite in suites) for key in ('tests','failures','errors','skipped')}
                    cases=list(root.iter('testcase'))
                    actual={'tests':len(cases),**{key:sum(case.find(tag) is not None for case in cases)
                        for key,tag in [('failures','failure'),('errors','error'),('skipped','skipped')]}}
                    if stats!=actual or any(stats[key]!=check[key] for key in stats):raise ValueError('Check statistics differ from actual test cases')
                except (ET.ParseError,TypeError) as error:raise ValueError('Invalid actual test report') from error
            elif report.suffix=='.json':
                try:record=json.loads(report.read_text(encoding='utf-8'))
                except (ValueError,OSError) as error:raise ValueError('Invalid command report') from error
                log=report.parent/(check['name']+'.log')
                if (record.get('command')!=check.get('command') or record.get('exit_code')!=check['exit_code']
                        or not log.is_file() or record.get('log_sha256')!=file_sha(log)):
                    raise ValueError('Command execution or log identity differs from its report')
            else:raise ValueError('Unsupported actual check report')
    if receipt.get('receipt_sha') != digest({k: v for k, v in receipt.items() if k != 'receipt_sha'}):
        raise ValueError('Acceptance receipt content changed')
    return receipt


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--receipt', type=Path, required=True)
    ap.add_argument('--version', required=True)
    ap.add_argument('--model-sha')
    ap.add_argument('--model-root', type=Path)
    ap.add_argument('--evidence-root', type=Path)
    args = ap.parse_args()
    receipt = validate(json.loads(args.receipt.read_text(encoding='utf-8')), version=args.version,
                       model_sha=args.model_sha, evidence_root=args.evidence_root)
    if args.model_root is not None:
        actual = [{'path': p.relative_to(args.model_root).as_posix(), 'sha256': file_sha(p)}
                  for p in sorted(args.model_root.rglob('*')) if p.is_file() and not p.name.startswith('.')]
        if digest(actual) != receipt['model_sha']:
            raise ValueError('Deployed rules differ from the accepted model')
    print('Acceptance receipt verified')


if __name__ == '__main__':
    main()
