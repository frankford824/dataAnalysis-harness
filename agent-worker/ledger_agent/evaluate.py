"""Repeated live-provider evaluation on declared synthetic cases, never production data."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

from .runtime import Config,ModelSpec,Runtime,runtime_identity
from .state import State,cost_per_correct

CASES = (
    ('split_shipment','freight',True),('merged_parents','freight',True),
    ('reship_freight','freight',True),('cross_month_source','orders',True),
    ('partial_cost','costs',True),('historical_evidence','costs',True),
    ('zero_cost_proven','costs',True),('late_source','orders',True),
    ('unique_payment','orders',True),('duplicate_import','freight',True),
    ('creator_reference','orders',True),('managed_membership','orders',True),
    ('ambiguous_members','freight',False),('missing_amount','costs',False),
    ('login_expired','orders',False),('source_conflict','costs',False),
    ('stale_inputs','orders',False),('permission_denied','freight',False),
    ('collector_timeout','costs',False),('missing_original','orders',False),
)


async def evaluate(config,output:Path,*,rounds=5,concurrency=2,selection=None):
    if rounds<1 or concurrency<1:raise ValueError('Invalid evaluation dimensions')
    output.mkdir(parents=True,exist_ok=False)
    state=State(output/'private-audit')
    source_before=runtime_identity()
    semaphore=asyncio.Semaphore(concurrency)
    async def one(item,iteration):
        name,source,solvable=item
        async with semaphore:
            found=[]
            case={'id':name+'_'+str(iteration),'input_sha':hashlib.sha256(name.encode()).hexdigest(),
                  'reason_codes':[name],'skills':['evidence']}
            def collect(kind):
                def operation(case,evidence):
                    if kind!=source:return {'status':'not_found','input_sha':case['input_sha']}
                    if solvable:
                        found.append(True)
                        return {'status':'found','input_sha':case['input_sha'],'amount':123.45,
                                'order_id':'3300000000000000000','identity_proven':True}
                    status={'login_expired':'login_required','source_conflict':'conflict',
                            'stale_inputs':'stale','permission_denied':'denied'}.get(name,'pending')
                    if name=='collector_timeout':raise TimeoutError('synthetic collector timeout')
                    return {'status':status,'input_sha':case['input_sha']}
                return operation
            started=time.monotonic()
            result=await Runtime(config,state).run(case,{f'collect_{kind}':collect(kind) for kind in ('orders','costs','freight')},
                lambda:{'verified':bool(found) and solvable,'input_sha':case['input_sha']})
            expected='verified' if solvable else 'waiting_evidence'
            return {'case':name,'iteration':iteration,'expected':expected,'actual':result['state'],
                    'correct':result['state']==expected,'false_completion':not solvable and result['state']=='verified',
                    'duration_seconds':round(time.monotonic()-started,3),'error_code':result.get('error_code'),
                    'prose_guard':result.get('prose_guard')}
    cases=[item for item in CASES if selection is None or item[0] in selection]
    if not cases:raise ValueError('Empty evaluation selection')
    rows=await asyncio.gather(*(one(item,i) for item in cases for i in range(rounds)))
    result={'schema':1,'dataset':'synthetic-business-risk-fixtures.v1','live_provider':True,
            'sdk':'agentscope==2.0.11','models':[m.name for m in config.models],
            'skills':['evidence'],'runtime_sha':source_before,'inputs_unchanged':source_before==runtime_identity(),
            'provider_sha':hashlib.sha256(config.base_url.rstrip('/').encode()).hexdigest(),
            'case_sha':hashlib.sha256(json.dumps(CASES).encode()).hexdigest(),
            'case_count':len(cases),'rounds':rounds,'trials':len(rows),
            'correct':sum(r['correct'] for r in rows),'false_completions':sum(r['false_completion'] for r in rows),
            'production_corpus_verified':False,'usage':state.summary(),'results':rows}
    result['passed']=result['case_count']>=20 and rounds>=5 and result['correct']==len(rows) and result['false_completions']==0 and result['inputs_unchanged']
    report_path=output/'evaluation.json'
    report_path.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    if result['passed']:
        profile={'name':config.models[0].name,'skills':['evidence'],'evaluation_path':str(report_path.resolve()),
                 'evaluation_sha':hashlib.sha256(report_path.read_bytes()).hexdigest(),'price':config.models[0].price,
                 'cost_per_correct':cost_per_correct(result['usage'],result['correct'])}
        (output/'model-profile.json').write_text(json.dumps(profile,ensure_ascii=False,indent=2))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=5)
    parser.add_argument('--concurrency',type=int,default=2)
    parser.add_argument('--case',action='append')
    parser.add_argument('--model')
    parser.add_argument('--price',type=Path,required=True)
    args=parser.parse_args()
    from .state import load_price
    price=load_price(args.price)
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'ledger'))
    from ledger.assist import load_config
    saved=load_config()
    if not saved.ready:raise SystemExit('Existing ledger gateway is not configured')
    config=Config(saved.base_url,saved.api_key,(ModelSpec(args.model or saved.model,price=price),),evaluation=True)
    result=asyncio.run(evaluate(config,args.output,rounds=args.rounds,concurrency=args.concurrency,selection=args.case))
    print(json.dumps({k:v for k,v in result.items() if k!='results'},ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)


if __name__=='__main__':main()
