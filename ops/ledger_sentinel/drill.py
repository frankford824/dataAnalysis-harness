"""Local fault injection in a NEW sandbox directory. No service or production access."""
import argparse
import json
from pathlib import Path
import sys

from .executor import Executor, atomic_json
from .probes import Probes
from .state import State


def run(output):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    script=output/'fixed_recovery.py'
    script.write_text('''import json,sys,time
from pathlib import Path
mode,path=sys.argv[1:];target=Path(path)
if mode=='fail':raise SystemExit(23)
if mode=='timeout':time.sleep(10)
target.write_text(json.dumps({'ok':True}))
''',encoding='utf-8')
    health=output/'fixture-health.json';atomic_json(health,{'ok':True})
    probes=Probes([{'id':'healthy','kind':'json','path':str(health),'assertions':[{'field':'ok','value':True}]}])
    state=State(output/'state')
    def fixed(mode,timeout=5):return {'argv':[sys.executable,str(script),mode,str(health)],'timeout_seconds':timeout}
    results={}
    for label,mode,rollback in [('recovery_failure','fail',None),('timeout_rollback','timeout','recover'),('rollback_failure','fail','fail')]:
        plan={'id':label,'kind':'recovery','capability':'technical','preconditions':['healthy'],'postconditions':['healthy'],
            'command':fixed(mode,.1 if mode=='timeout' else 5)}
        if rollback:plan.update(rollback=fixed(rollback),rollback_conditions=['healthy'])
        results[label]=Executor(state,probes,[plan]).execute(label,label)
    expected={'recovery_failure':'failed','timeout_rollback':'rolled_back','rollback_failure':'rollback_failed'}
    receipt={'kind':'local_fault_drill','production_observed':False,
        'passed':all(results[k]['state']==v for k,v in expected.items()),'results':results}
    atomic_json(output/'drill-receipt.json',receipt)
    return receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    try:result=run(args.output)
    except (OSError,ValueError) as exc:
        print(json.dumps({'passed':False,'reason':str(exc)},ensure_ascii=False));return 2
    print(json.dumps(result,ensure_ascii=False));return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
