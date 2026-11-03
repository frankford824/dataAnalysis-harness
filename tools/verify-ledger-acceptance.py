"""Execute required checks and issue a receipt only for unchanged, committed inputs.

Does not connect to production, rebuild a financial baseline or waive a failure.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'ledger'))
sys.path.insert(0, str(REPO / 'tools'))
from ledger.release_gate import digest, file_sha, validate
from ledger.version import source_code_manifest
from ledger_acceptance_inputs import capture as capture_inputs,inventory as input_inventory


def git(*args):
    return subprocess.check_output(['git', '-C', str(REPO), *args], text=True).strip()


def identities(root, pattern='*'):
    return [{'path': p.relative_to(root).as_posix(), 'sha256': file_sha(p)}
            for p in sorted(root.rglob(pattern)) if p.is_file() and not p.name.startswith('.')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--python', default=sys.executable)
    parser.add_argument('--pnpm', default='pnpm')
    parser.add_argument('--agent-python', default=str(REPO/'agent-worker/.venv/bin/python'))
    parser.add_argument('--regression-manifest',type=Path)
    parser.add_argument('--service-cases',type=Path)
    parser.add_argument('--baseline-attestation',type=Path,
                        help='Owner attestation that no closed store-month has an engine baseline (instead of --service-cases)')
    parser.add_argument('--target-period',required=True)
    parser.add_argument('--runtime-model',type=Path,default=REPO/'models/cn-ecommerce')
    args = parser.parse_args()
    forbidden = ('PYTEST_ADDOPTS','PYTEST_PLUGINS','LEDGER_BROWSER_CASE','PYTHONOPTIMIZE')
    if sys.flags.optimize or any(os.environ.get(name) for name in forbidden):
        raise SystemExit('Refusing scope/optimization overrides: '+', '.join(name for name in forbidden if os.environ.get(name)))
    if git('status', '--porcelain', '--', 'ledger', 'models', 'tools', 'ops', 'agent-worker', 'host-agent'):
        raise SystemExit('Refusing release receipt: implementation has uncommitted changes')
    if not args.corpus.is_dir():
        raise SystemExit('Required real corpus is missing')
    version = git('rev-parse', 'HEAD')
    source_manifest=source_code_manifest(REPO)
    runtime_model=args.runtime_model.resolve()
    if args.service_cases and args.baseline_attestation:
        raise SystemExit('Use either approved service cases or a baseline attestation')
    service_manifest_sha=file_sha(args.service_cases) if args.service_cases and args.service_cases.is_file() else None
    attestation_sha=file_sha(args.baseline_attestation) if args.baseline_attestation and args.baseline_attestation.is_file() else None
    regressions=[]
    if args.regression_manifest:
        supplied=json.loads(args.regression_manifest.read_text(encoding='utf-8'))
        if supplied.get('schema')!=1 or supplied.get('code_sha')!=version or not supplied.get('files'):
            raise SystemExit('Invalid isolated regression manifest')
        for row in supplied['files']:
            path=(REPO/row['path']).resolve()
            relative=Path(row['path'])
            if (not path.is_relative_to((REPO/'ledger/tests/regressions').resolve()) or path.is_symlink()
                    or relative.parent.as_posix()!='ledger/tests/regressions' or not relative.name.startswith('test_')
                    or relative.suffix!='.py' or not path.is_file() or file_sha(path)!=row['sha256']):
                raise SystemExit('Untrusted regression path/hash is not allowed')
            regressions.append({'path':row['path'],'sha256':row['sha256']})
        if len({row['path'] for row in regressions})!=len(regressions):raise SystemExit('Duplicate regression path')
    input_roots,inputs = capture_inputs(args.corpus,args.service_cases,args.baseline_attestation)
    models = identities(runtime_model)
    if not inputs:
        raise SystemExit('Empty real corpus cannot certify a release')
    args.output.mkdir(parents=True, exist_ok=False)
    checks = []
    pytest = [args.python, '-m', 'pytest', '-q', '--strict-markers',
              *('--ignore='+str((REPO/row['path']).resolve()) for row in regressions)]
    commands = [
        ('backend', REPO / 'ledger', pytest + ['-m', 'not slow']),
        ('corpus', REPO / 'ledger', pytest + ['-m', 'slow']),
        ('baseline_attestation', REPO, [args.python,str(REPO/'tools/verify-baseline-attestation.py'),
            '--attestation',str(args.baseline_attestation.resolve()),'--target-period',args.target_period,
            '--junit',str((args.output/'baseline_attestation.xml').resolve())]) if args.baseline_attestation else
        ('service_replay', REPO, [args.python,str(REPO/'tools/verify-ledger-service-cases.py'),
            '--candidate-model',str(runtime_model),'--target-period',args.target_period,
            '--junit',str((args.output/'service_replay.xml').resolve()),
            '--output',str((args.output/'service_cases.json').resolve()),
            *(['--manifest',str(args.service_cases.resolve())] if args.service_cases else [])]),
        ('invariants', REPO / 'ledger', pytest + ['tests/test_accept_gate.py', 'tests/test_release_gate.py',
            'tests/test_frozen_costs.py', 'tests/test_evidence_guards.py', 'tests/test_historical_close.py',
            'tests/test_release_package.py','tests/test_release_readback.py','tests/test_write_auth.py',
            'tests/test_ledger_backup.py','tests/test_release_ship.py','tests/test_acceptance_inputs.py',
            'tests/test_ops_receipt_handoff.py']),
        ('browser', REPO / 'ledger/web', [args.pnpm, 'run', 'test']),
        ('frontend_build', REPO / 'ledger/web', [args.pnpm, 'run', 'build']),
        ('ops', REPO, [args.python,'-m','pytest','-q','ops/ledger_sentinel/tests']),
        ('agent_worker', REPO/'agent-worker', [args.agent_python,'-m','pytest','-q']),
    ]
    if regressions:
        commands.append(('repair_regressions',REPO/'ledger',[args.python,'-m','pytest','-q','--strict-markers',
                          *(str((REPO/row['path']).resolve()) for row in regressions)]))
    env = {**os.environ, 'LEDGER_NO_CORPUS': '0', 'LEDGER_ORDER_FEED_ENABLED': '0',
           'LEDGER_ACCEPTANCE_CORPUS': str(args.corpus.resolve()),'BUILD_SOURCEMAP':'0'}
    for name, cwd, command in commands:
        report = args.output / (name + ('.xml' if name not in {'browser', 'frontend_build'} else '.json'))
        argv = command + (['--junitxml=' + str(report.resolve())] if report.suffix == '.xml' and name not in {'service_replay','baseline_attestation'} else [])
        with (args.output / (name + '.log')).open('w', encoding='utf-8') as log:
            completed = subprocess.run(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
        if report.suffix == '.xml' and report.exists():
            root = ET.parse(report).getroot()
            suites = [root] if root.tag == 'testsuite' else root.findall('testsuite')
            stats = {k: sum(int(s.get(k, 0)) for s in suites) for k in ('tests', 'failures', 'errors', 'skipped')}
        else:
            stats = dict(tests=1, failures=int(completed.returncode != 0), errors=0, skipped=0)
            report.write_text(json.dumps({'command': argv, 'exit_code': completed.returncode,
                                         'log_sha256': file_sha(args.output / (name + '.log'))}))
        check = dict(name=name, command=argv, exit_code=completed.returncode,
                     report=report.name, report_sha256=file_sha(report), **stats)
        checks.append(check)
        print(json.dumps({k: v for k, v in check.items() if k not in {'command', 'report_sha256'}}, ensure_ascii=False), flush=True)
    unchanged = (version == git('rev-parse', 'HEAD') and not git('status', '--porcelain', '--', 'ledger', 'models', 'tools', 'ops', 'agent-worker', 'host-agent')
                 and source_manifest==source_code_manifest(REPO)
                 and inputs == input_inventory(input_roots) and models == identities(runtime_model)
                 and (not args.service_cases or (args.service_cases.is_file() and file_sha(args.service_cases)==service_manifest_sha))
                 and (not args.baseline_attestation or (args.baseline_attestation.is_file() and file_sha(args.baseline_attestation)==attestation_sha)))
    receipt = dict(schema=1, code_sha=version, model_sha=digest(models), model_manifest=models,
                   gate_runner={'sha256':file_sha(Path(__file__).resolve()),'kind':'controlled_full_gate.v1'},
                   source_code_manifest=source_manifest,source_code_sha=digest(source_manifest),
                   input_roots=input_roots,
                   model_root=str(runtime_model),model_source='runtime_snapshot',inputs=inputs,
                   corpus_root=str(args.corpus.resolve()),
                   frontend_artifacts=identities(REPO / 'ledger/ledger/static'),
                   input_sha=digest(inputs), checks=checks,
                   unresolved_differences=0 if all(c['exit_code']==0 and c['failures']==0 and c['errors']==0 and c['skipped']==0 for c in checks if c['name'] in {'backend','corpus','service_replay','baseline_attestation','invariants'}) else None,
                   target_period=args.target_period,
                   inputs_unchanged=unchanged)
    if args.service_cases:
        receipt['approved_service_cases']={'path':str(args.service_cases.resolve()),'sha256':service_manifest_sha}
    if args.baseline_attestation:
        attested=json.loads(args.baseline_attestation.read_text(encoding='utf-8'))
        receipt['baseline_attestation']={'kind':'no_engine_baseline','path':str(args.baseline_attestation.resolve()),
            'sha256':attestation_sha,'approval_reference':attested.get('approval_reference',''),
            'evidence_sha256':(attested.get('evidence') or {}).get('sha256')}
    if (args.output/'service_cases.json').is_file():
        receipt['service_case_attempt']={'path':'service_cases.json','sha256':file_sha(args.output/'service_cases.json')}
    if regressions:
        check=next(row for row in checks if row['name']=='repair_regressions')
        receipt['repair_regressions']={'code_sha':version,'files':regressions,
            'excluded_from_official_gates':True,'isolation':'separate_pytest_process',
            'check':{key:check[key] for key in ('exit_code','report','report_sha256')}}
    receipt['receipt_sha'] = digest(receipt)
    # A failed run is inspectable but is never named acceptance-receipt.json.
    (args.output / 'attempt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    if not unchanged:
        raise SystemExit('Code/model/input changed while checks ran')
    try:
        validate(receipt, version=version, model_sha=digest(models), evidence_root=args.output)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    (args.output / 'acceptance-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    print('Qualified acceptance receipt issued')


if __name__ == '__main__':
    main()
