"""Live-provider ops calibration on varied synthetic Git repos and actual bwrap checks."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .ops_runtime import ROOT,ops_identity,execute_repair,file_sha,digest
from .runtime import Config,ModelSpec,runtime_identity
from .state import State

# The expected tests are original trusted fixture files. The model may read
# them, but Repair refuses edits and the frozen verification hashes them again.
CASES=(
 ('inclusive_total','Return the sum of integers from zero through n inclusive; n is nonnegative.',
  'def solve(n):\n    return sum(range(n))\n',
  'for n in (0,1,2,7,31): assert solve(n)==n*(n+1)//2'),
 ('clamp','Clamp x to the inclusive [low, high] interval, with low <= high.',
  'def solve(x,low,high):\n    return min(low,max(high,x))\n',
  'for x in (-8,0,3,9): assert solve(x,0,5)==max(0,min(5,x))'),
 ('stable_unique','Return first occurrences in input order, without duplicates.',
  'def solve(values):\n    return sorted(set(values))\n',
  "assert solve(['z','a','z','b','a'])==['z','a','b']; assert solve([3,1,3,2])==[3,1,2]"),
 ('mean_empty','Return arithmetic mean, or None for an empty sequence.',
  'def solve(values):\n    return sum(values)/(len(values)+1) if values else 0\n',
  'assert solve([]) is None; assert solve([2,4,9])==5; assert solve([-2,2])==0'),
 ('chunk_tail','Return consecutive chunks including the final short chunk. Reject nonpositive size.',
  'def solve(values,size):\n    return [values[i:i+size] for i in range(0,len(values)-size+1,size)]\n',
  'assert solve([1,2,3,4,5],2)==[[1,2],[3,4],[5]]; assert solve([],2)==[]\n    import pytest\n    with pytest.raises(ValueError): solve([1],0)'),
 ('normalize_key','Strip outer whitespace and Unicode-casefold a string key.',
  'def solve(value):\n    return value.lower()\n',
  "assert solve(' A_B ')== 'a_b'; assert solve('Straße')=='strasse'; assert solve('')==''"),
 ('count_groups','Count each hashable item and return a dictionary.',
  'def solve(values):\n    result={}\n    for value in values: result[value]=1\n    return result\n',
  "assert solve(['a','b','a'])=={'a':2,'b':1}; assert solve([])=={}"),
 ('parse_boolean','Parse true/yes/1 and false/no/0 case-insensitively after strip; reject other values.',
  'def solve(value):\n    return bool(value)\n',
  "assert solve(' FALSE ') is False; assert solve('yes') is True; assert solve('0') is False\n    import pytest\n    with pytest.raises(ValueError): solve('maybe')"),
 ('flatten_once','Flatten one level of a sequence of lists, preserving order.',
  'def solve(values):\n    result=[]\n    for value in values: result.append(value)\n    return result\n',
  'assert solve([[1,2],[],[3]])==[1,2,3]; assert solve([])==[]'),
 ('adjacent_pairs','Return all consecutive pairs; fewer than two inputs give an empty list.',
  'def solve(values):\n    return list(zip(values,values))\n',
  'assert solve([1,2,3])==[(1,2),(2,3)]; assert solve([1])==[]; assert solve([])==[]'),
 ('binary_index','Return the zero-based position of target in a sorted unique list, or -1.',
  'def solve(values,target):\n    low,high=0,len(values)-1\n    while low<=high:\n        mid=(low+high)//2\n        if values[mid]==target: return mid+1\n        if values[mid]<target: low=mid+1\n        else: high=mid-1\n    return -1\n',
  'assert solve([1,4,7,9],7)==2; assert solve([1],1)==0; assert solve([],1)==-1; assert solve([1,4],3)==-1'),
 ('balanced_parentheses','Return whether parentheses are balanced; other characters are ignored.',
  "def solve(text):\n    return text.count('(')==text.count(')')\n",
  "assert solve('(a())') is True; assert solve(')(') is False; assert solve('(()') is False; assert solve('') is True"),
 ('leap_year','Return Gregorian leap-year truth including century exceptions.',
  'def solve(year):\n    return year%4==0\n',
  'assert solve(2000) is True; assert solve(1900) is False; assert solve(2024) is True; assert solve(2023) is False'),
 ('relative_change','Return (new-old)/old for a positive old base, otherwise None.',
  'def solve(new,old):\n    return (new-old)/abs(old) if old else 0\n',
  'assert solve(15,10)==.5; assert solve(2,-2) is None; assert solve(2,0) is None'),
 ('reverse_words','Reverse word order and normalize whitespace to one space.',
  'def solve(text):\n    return text[::-1]\n',
  "assert solve('  red  green blue ')== 'blue green red'; assert solve('')==''; assert solve('one')=='one'"),
 ('ceiling_multiple','Return the smallest multiple of positive step that is >= n, including negative n.',
  'def solve(n,step):\n    return (n//step)*step\n',
  'assert solve(5,3)==6; assert solve(-5,3)==-3; assert solve(0,3)==0; assert solve(6,3)==6'),
 ('duration_parts','Split nonnegative whole seconds into (hours, minutes, seconds), with minute/second below sixty.',
  'def solve(seconds):\n    return seconds//3600,seconds//60,seconds%60\n',
  'assert solve(3661)==(1,1,1); assert solve(59)==(0,0,59); assert solve(7200)==(2,0,0)'),
 ('first_nonempty','Return the first nonempty string, or None when none exists.',
  'def solve(values):\n    return values[-1] if values else None\n',
  "assert solve(['','alpha','beta'])=='alpha'; assert solve(['','']) is None; assert solve([]) is None"),
 ('unique_intersection','Return the sorted distinct items common to both sequences.',
  'def solve(left,right):\n    return sorted(set(left)|set(right))\n',
  'assert solve([3,1,1,2],[2,3,3,4])==[2,3]; assert solve([],[])==[]; assert solve([1],[2])==[]'),
 ('matrix_transpose','Transpose a rectangular list of rows; [] gives []; reject ragged input.',
  'def solve(rows):\n    return [list(row) for row in zip(*rows)]\n',
  'assert solve([[1,2],[3,4]])==[[1,3],[2,4]]; assert solve([])==[]\n    import pytest\n    with pytest.raises(ValueError): solve([[1,2],[3]])'),
)


def create_case(output,item,runtime):
    from ops.ledger_sentinel.repair import Repair
    name,contract,bug,assertions=item
    repo=output/'repository';folder=repo/'ledger/ledger';folder.mkdir(parents=True)
    tests=repo/'ledger/tests';tests.mkdir()
    (folder/'__init__.py').write_text('')
    module='ledger/ledger/synthetic_case.py';test='ledger/tests/test_original_contract.py'
    (repo/module).write_text('"""'+contract+'"""\n'+bug)
    (tests/'conftest.py').write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).resolve().parents[1]))\n')
    (repo/test).write_text('from ledger.synthetic_case import solve\ndef test_original_contract():\n    '+assertions+'\n')
    (repo/'synthetic-fixture.json').write_text(json.dumps({'kind':'synthetic_ops_case','business_rows':False,'case':name}))
    subprocess.run(['git','init','-q',str(repo)],check=True,capture_output=True)
    subprocess.run(['git','-C',str(repo),'add','.'],check=True,capture_output=True)
    subprocess.run(['git','-c','core.hooksPath='+str(output/'disabled-hooks'),'-c','commit.gpgsign=false',
        '-c','user.name=Synthetic Ops Evaluation','-c','user.email=fixture@localhost','-C',str(repo),'commit','-qm','synthetic original counterexample'],check=True,capture_output=True)
    base=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
    (repo/'existing-user-note.txt').write_text('Existing synthetic work must not change.\n')
    config={'code_prefixes':['ledger/ledger/'],'max_files':1,
        'independent_oracle':{'kind':'synthetic_pure_python.v1','case':name,'module':module,'runtime':str(runtime)},'code_entry_points':{name:[module,test]},
        'runtime_mounts':[{'source':str(Path(runtime).resolve()),'target':'/runtime'}],
        'checks':[{'id':'original_contract','argv':['/runtime/bin/python','-m','pytest','-q',test,
            '--junitxml=/out/original.xml','-p','no:cacheprovider'],'report':'original.xml','timeout_seconds':30}]}
    repair=Repair(repo,output/'repair-state',config)
    prepared=repair.prepare({'id':name,'status':'failed','reason':'synthetic_contract_violation'},base)
    return repair,prepared['repair_id']


def model_calls(state,identifier):
    with state.connect() as db:return db.execute('SELECT count(*) FROM model_call WHERE case_id=?',(identifier,)).fetchone()[0]


async def evaluate(config,output,*,runtime,rounds=5,concurrency=2,selection=None,live_provider=False):
    if not config.evaluation:raise ValueError('Use the dedicated synthetic calibration configuration')
    if rounds<1 or concurrency<1:raise ValueError('Invalid calibration dimensions')
    output=Path(output).resolve()
    if output.is_relative_to(ROOT):raise ValueError('Evaluation state must be outside the source checkout')
    cases=[item for item in CASES if selection is None or item[0] in selection]
    if not cases:raise ValueError('No synthetic ops cases selected')
    output.mkdir(parents=True,exist_ok=False);state=State(output/'private-audit')
    identity_before=ops_identity();core_before=runtime_identity();semaphore=asyncio.Semaphore(concurrency)
    spec=config.select(('ops',))
    async def one(item,iteration):
        async with semaphore:
            started=time.monotonic();trial=output/'trials'/(item[0]+'-'+str(iteration));trial.mkdir(parents=True)
            repair,identifier=await asyncio.to_thread(create_case,trial,item,runtime)
            result=await execute_repair(repair,identifier,config,state,spec,run_acceptance=False)
            from .ops_oracle import configured_oracle
            independent=await asyncio.to_thread(configured_oracle(repair,identifier))
            claimed=result.get('code_repair_status')=='verified'
            actual_verified=independent.get('passed') is True and independent.get('executed') is True
            result['evaluation_oracle']=independent
            calls=model_calls(state,identifier);result.update(model_calls=calls,case=item[0],iteration=iteration,case_spec_sha=digest(item),ops_runtime_sha=identity_before)
            for record in [result.get('baseline_oracle'),result.get('frozen_oracle'),result.get('evaluation_oracle')]:
                if record is not None:record['receipt_path']=Path(record['receipt_path']).resolve().relative_to(output).as_posix()
            if result.get('frozen_code_file'):
                result['frozen_code_file']['path']=Path(result['frozen_code_file']['path']).resolve().relative_to(output).as_posix()
            for record in result.get('frozen_original_tests',[]):
                record['path']=Path(record['path']).resolve().relative_to(output).as_posix()
            proof=trial/'independent-code-proof.json';proof.write_text(json.dumps(result,ensure_ascii=False,indent=2))
            return {'case':item[0],'iteration':iteration,'correct':claimed and actual_verified and calls>0,
                'code_repair_status':result.get('code_repair_status'),'state':result.get('state'),
                'false_completion':claimed and not actual_verified,'independent_oracle_passed':actual_verified,'failure_category':None if claimed and actual_verified else independent.get('failure_category') or 'harness_or_patch_unverified',
                'model_calls':calls,'proof_path':proof.relative_to(output).as_posix(),'proof_sha256':file_sha(proof),
                'duration_seconds':round(time.monotonic()-started,3),'model_error':result.get('model_error')}
    rows=await asyncio.gather(*(one(item,iteration) for item in cases for iteration in range(rounds)))
    summary=state.summary()
    result={'schema':1,'kind':'synthetic_ops_evaluation','dataset':'synthetic-code-contracts.v1',
        'sdk':'agentscope==2.0.11','live_provider':live_provider,'models':[spec.name],'skills':['ops'],
        'runtime_sha':core_before,'ops_runtime_sha':identity_before,
        'inputs_unchanged':identity_before==ops_identity() and core_before==runtime_identity(),
        'provider_sha':hashlib.sha256(config.base_url.rstrip('/').encode()).hexdigest(),
        'case_sha':digest(CASES),'cases':[item[0] for item in cases],'case_count':len(cases),'normal_case_count':len(cases),
        'rounds':rounds,'trials':len(rows),'normal_trials':len(rows),'correct':sum(row['correct'] for row in rows),
        'normal_verified':sum(row['correct'] for row in rows),'false_completions':sum(row['false_completion'] for row in rows),
        'trusted_release_approved':False,'production_corpus_verified':False,'financial_gate_status':'not_run_in_synthetic_trials',
        'audit':{'path':state.path.relative_to(output).as_posix(),'sha256':file_sha(state.path)},
        'usage':summary,'usage_complete':summary['usage_unknown_calls']==0,'results':rows}
    result['passed']=(live_provider and len(cases)>=20 and rounds>=5 and result['normal_verified']==len(rows)
        and result['false_completions']==0 and result['inputs_unchanged'] and summary['calls']>=len(rows))
    report=output/'ops-evaluation.json';report.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    if result['passed']:
        profile={'name':spec.name,'skills':['ops'],'evaluation_path':str(report),'evaluation_sha':file_sha(report),
            'price':None,'cost_per_correct':None}
        (output/'ops-model-profile.json').write_text(json.dumps(profile,ensure_ascii=False,indent=2))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=5);parser.add_argument('--concurrency',type=int,default=2)
    parser.add_argument('--case',action='append');parser.add_argument('--model',default='deepseek-v4-flash-0731')
    args=parser.parse_args()
    sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'ledger'))
    from ledger.assist import load_config
    saved=load_config()
    if not saved.ready:raise SystemExit('Existing ledger gateway is not configured')
    config=Config(saved.base_url,saved.api_key,(ModelSpec(args.model,skills=('ops',)),),evaluation=True,timeout_seconds=180,max_iters=16)
    result=asyncio.run(evaluate(config,args.output,runtime=args.runtime,rounds=args.rounds,concurrency=args.concurrency,selection=args.case,live_provider=True))
    print(json.dumps({key:value for key,value in result.items() if key!='results'},ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
