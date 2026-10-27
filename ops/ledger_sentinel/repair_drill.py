"""Actual networkless planted-defect exercise; never a production observation."""
from __future__ import annotations
import argparse
import ast
import hashlib
import os
import json
from pathlib import Path
import shutil
import subprocess

from .executor import atomic_json
from .probes import file_sha
from .repair import Repair
from .state import digest


def planted_oracle(path):
    """Tiny independent AST oracle for this synthetic arithmetic fixture only."""
    tree=ast.parse(Path(path).read_text())
    if len(tree.body)!=1 or not isinstance(tree.body[0],ast.FunctionDef):raise ValueError('Non-pure planted candidate')
    function=tree.body[0]
    if (function.name!='successor' or function.decorator_list or function.returns or function.type_comment
            or len(function.body)!=1 or not isinstance(function.body[0],ast.Return)
            or len(function.args.args)!=1 or function.args.args[0].arg!='value' or function.args.args[0].annotation
            or function.args.defaults or function.args.kw_defaults or function.args.posonlyargs or function.args.kwonlyargs
            or function.args.vararg or function.args.kwarg):
        raise ValueError('Non-pure planted candidate')
    expression=function.body[0].value
    if (not isinstance(expression,ast.BinOp) or not isinstance(expression.op,ast.Add)
            or not isinstance(expression.left,ast.Name) or expression.left.id!='value'
            or not isinstance(expression.right,ast.Constant) or type(expression.right.value) is not int
            or abs(expression.right.value)>10000):
        raise ValueError('Unsupported planted candidate grammar')
    values=(-1,0,10,101)
    return {'kind':'synthetic_independent_ast_oracle','passed':all(v+expression.right.value==v+1 for v in values),
        'cases':len(values),'source_sha256':file_sha(path),'financial_acceptance':False}


def run(output,*,repo,runtime,model_root,corpus_root,target_period):
    output=Path(output).resolve();repo=Path(repo).resolve();runtime=Path(runtime).resolve()
    if output.exists():raise ValueError('Use a fresh isolated drill directory')
    if output.is_relative_to(repo):raise ValueError('Drill must stay outside the source checkout')
    output.mkdir(parents=True)
    synthetic=output/'synthetic-repo';(synthetic/'ledger/ledger').mkdir(parents=True)
    (synthetic/'ledger/tests').mkdir(parents=True)
    module='ledger/ledger/planted_counter.py';test='ledger/tests/test_planted_counter.py'
    (synthetic/'ledger/ledger/__init__.py').write_text('')
    (synthetic/module).write_text('def successor(value):\n    return value + 2\n')
    (synthetic/'ledger/tests/conftest.py').write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parents[1]))\n')
    (synthetic/test).write_text('from ledger.planted_counter import successor\ndef test_successor():\n    assert successor(10) == 11\n')
    git=shutil.which('git')
    if not git:raise ValueError('Git is required')
    subprocess.run([git,'init',str(synthetic)],check=True,capture_output=True)
    subprocess.run([git,'-C',str(synthetic),'add','.'],check=True,capture_output=True)
    subprocess.run([git,'-c','core.hooksPath='+str(output/'disabled-hooks'),'-c','commit.gpgsign=false',
        '-c','user.name=Synthetic Fixture','-c','user.email=fixture@localhost','-C',str(synthetic),'commit','-m','synthetic planted defect'],check=True,capture_output=True)
    version=subprocess.check_output([git,'-C',str(synthetic),'rev-parse','HEAD'],text=True).strip()
    (synthetic/'existing-user-note.txt').write_text('Synthetic existing untracked work, must remain untouched.\n')
    original_test_sha=file_sha(synthetic/test)
    trusted=output/'trusted';(trusted/'tools').mkdir(parents=True)
    (trusted/'ledger').mkdir()
    shutil.copytree(repo/'ledger/ledger',trusted/'ledger/ledger',ignore=shutil.ignore_patterns('__pycache__','static'))
    shutil.copytree(Path(model_root).resolve(),trusted/'models/cn-ecommerce')
    entry=trusted/'tools/verify-ledger-service-cases.py'
    shutil.copyfile(repo/'tools/verify-ledger-service-cases.py',entry)
    checks=[{'id':'planted','argv':['/runtime/bin/python','-m','pytest','-q',test,
        '--junitxml=/out/planted.xml','-p','no:cacheprovider'],'report':'planted.xml','timeout_seconds':60}]
    config={'code_prefixes':['ledger/ledger/'],'code_entry_points':{'planted_counter':[module,test]},
        'runtime_mounts':[{'source':str(runtime),'target':'/runtime'}],
        'checks':checks,'corpus_root':str(Path(corpus_root).resolve()),
        'trusted_root':str(trusted),'trusted_entry':'tools/verify-ledger-service-cases.py',
        'trusted_entry_sha256':file_sha(entry),'trusted_acceptance':{'argv':['/runtime/bin/python',
            '/trusted/tools/verify-ledger-service-cases.py','--candidate-model','/trusted/models/cn-ecommerce',
            '--target-period',target_period,'--junit','/out/service_cases.xml','--output','/out/service_cases.json'],
            'receipt':'acceptance-receipt.json','timeout_seconds':120}}
    repair=Repair(synthetic,output/'repair-state',config)
    before=repair.source_identity()
    body=repair.prepare({'id':'planted_counter','status':'failed','reason':'synthetic_off_by_one'},version)
    identifier=body['repair_id'];inventory=repair.list_code(identifier)
    worktree=Path(body['worktree'])
    original_oracle=planted_oracle(worktree/module)
    reproduced=repair.run_check(identifier,'planted')
    if reproduced['state']!='reported_failure' or original_oracle['passed']:
        result={'kind':'synthetic_actual_sandbox_drill','production_observed':False,'state':'blocked',
            'reason':'planted_failure_not_reproduced','check':reproduced}
        atomic_json(output/'drill.json',result);return result
    try:
        repair.write_code(identifier,test,'def test_successor(): assert True\n')
    except ValueError:immutable=True
    else:raise ValueError('Existing test rewrite was not blocked')
    repair.write_code(identifier,module,'def successor(value):\n    return value + 1\n')
    corrected_oracle=planted_oracle(worktree/module)
    corrected=repair.run_check(identifier,'planted')
    if corrected['state']!='reported_pass' or not corrected_oracle['passed']:raise ValueError('Independent synthetic oracle did not verify the scoped repair')
    candidate=repair.freeze_patch(identifier)
    accepted=repair.accept(identifier)
    if accepted.get('acceptance_state')=='passed':raise ValueError('Missing approved real cases unexpectedly qualified this drill')
    if accepted.get('acceptance_reason')!='independent_financial_oracle_unconfigured':
        raise ValueError('Missing independent financial authority must block production repair acceptance')
    # This pinned preflight executes trusted source only, never candidate code.
    # It proves the present real-case material gap, not the candidate's finances.
    preflight=output/'financial-preflight.json';junit=output/'financial-preflight.xml'
    environment={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(trusted/'ledger')}
    process=subprocess.run([str(runtime/'bin/python'),str(entry),'--candidate-model',str(trusted/'models/cn-ecommerce'),
        '--target-period',target_period,'--junit',str(junit),'--output',str(preflight)],
        cwd=trusted,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
    financial_gate=json.loads(preflight.read_text())
    if process.returncode==0 or financial_gate.get('status')!='blocked':
        raise ValueError('Missing approved real cases unexpectedly qualified the independent preflight')
    worktree=Path(candidate['worktree'])
    if file_sha(worktree/test)!=original_test_sha or repair.source_identity()!=before:
        raise ValueError('Drill altered original expected tests or original user work')
    if repair.model_tools(identifier)['read_code'](module).get('state')!='blocked':
        raise ValueError('Frozen model read remained open after trusted execution')
    artifacts=repair.artifacts(identifier)
    result={'kind':'synthetic_actual_sandbox_drill','production_observed':False,'state':'scoped_repair_verified_financial_release_blocked',
        'network_isolation':corrected.get('network'),'host_fallback':corrected.get('host_fallback'),
        'planted_reproduced':original_oracle,'corrected':corrected_oracle,
        'candidate_report_authoritative':False,'independent_financial_oracle_configured':False,
        'existing_test_immutable':immutable,'original_checkout_unchanged':True,
        'candidate_code_sha':candidate['candidate_code_sha'],'acceptance_state':accepted['acceptance_state'],
        'real_case_gate_status':financial_gate['status'],'real_case_gate_approved_manifest_provided':False,
        'unverified_platform_count':len(financial_gate.get('unverified_platforms',[])),
        'financial_preflight_sha256':file_sha(preflight),'financial_preflight_candidate_executed':False,
        'financial_preflight_entry_sha256':file_sha(entry),
        'code_inventory_sha':inventory['manifest_sha'],'artifacts':artifacts,
        'notice':'A real bwrap test over synthetic code; not a model evaluation, financial acceptance or production observation.'}
    atomic_json(output/'drill.json',result)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('output','repo','runtime','model-root','corpus-root'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--target-period',required=True)
    args=parser.parse_args(argv)
    result=run(args.output,repo=args.repo,runtime=args.runtime,model_root=args.model_root,
               corpus_root=args.corpus_root,target_period=args.target_period)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['state']=='scoped_repair_verified_financial_release_blocked' else 2


if __name__=='__main__':raise SystemExit(main())
