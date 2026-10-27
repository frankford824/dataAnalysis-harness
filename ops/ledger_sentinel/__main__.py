import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

from .sentinel import Sentinel
from .readiness import Readiness


def main(argv=None):
    parser=argparse.ArgumentParser(description='Ledger运维哨兵；只读探测与固定脚本执行，财务操作不在此入口')
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--home',type=Path,required=True)
    sub=parser.add_subparsers(dest='operation',required=True)
    sub.add_parser('check')
    monitor=sub.add_parser('monitor');monitor.add_argument('--interval',type=float,default=30)
    action=sub.add_parser('execute');action.add_argument('action_id');action.add_argument('--request-id',required=True)
    action.add_argument('--receipt',type=Path);action.add_argument('--candidate-version',default='');action.add_argument('--expected-current',default='')
    record=sub.add_parser('record-observation');record.add_argument('evidence',type=Path)
    capture=sub.add_parser('capture-observation');capture.add_argument('evidence',type=Path)
    assess=sub.add_parser('readiness');assess.add_argument('phase',choices=['shadow','canary']);assess.add_argument('--code-sha',required=True);assess.add_argument('--model-sha',required=True)
    promote=sub.add_parser('promote');promote.add_argument('phase',choices=['shadow','canary','production'])
    promote.add_argument('--action-id',required=True);promote.add_argument('--receipt',type=Path,required=True);promote.add_argument('--version',required=True)
    args=parser.parse_args(argv)
    try:
        config=json.loads(args.config.read_text(encoding='utf-8'));sentinel=Sentinel(args.home,config)
        readiness=Readiness(sentinel.state,sentinel.executor,**config.get('readiness',{}))
        if args.operation in {'check','monitor'}:
            if args.operation=='monitor' and args.interval<1:raise ValueError('Monitor interval must be at least one second')
            while True:
                result=sentinel.once();print(json.dumps(result,ensure_ascii=False),flush=True)
                if args.operation=='check':return 0 if result['status']=='ok' else 1
                time.sleep(args.interval)
        elif args.operation=='execute':
            result=sentinel.executor.execute(args.action_id,args.request_id,
                receipt=json.loads(args.receipt.read_text(encoding='utf-8')) if args.receipt else None,
                candidate_version=args.candidate_version,expected_current=args.expected_current)
        elif args.operation=='capture-observation':result={'sample_id':readiness.capture(args.evidence)}
        elif args.operation=='record-observation':result={'observation_id':readiness.record(args.evidence)}
        elif args.operation=='readiness':result=readiness.assess(args.phase,args.code_sha,args.model_sha)
        else:result=readiness.promote(args.phase,args.action_id,json.loads(args.receipt.read_text(encoding='utf-8')),args.version)
        print(json.dumps(result,ensure_ascii=False))
        return 1 if result.get('state') in {'failed','blocked','rollback_failed','rolled_back'} or result.get('status')=='blocked' else 0
    except (ValueError,OSError,KeyError,sqlite3.Error) as exc:
        print(json.dumps({'status':'failed','error_type':type(exc).__name__,'reason':str(exc)},ensure_ascii=False))
        return 2


if __name__=='__main__':raise SystemExit(main())
