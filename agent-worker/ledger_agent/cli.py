from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import time
import asyncio
import subprocess
import sys

from .runtime import Config, ModelSpec, Runtime
from .state import State


def main():
    parser = argparse.ArgumentParser(description='Local AgentScope ledger worker')
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--usage',action='store_true')
    parser.add_argument('--repair-event',type=Path)
    args = parser.parse_args()
    raw = json.loads(args.config.read_text(encoding='utf-8'))
    state = State(Path(raw['state_dir']).expanduser())
    if args.usage:
        print(json.dumps(state.summary(),ensure_ascii=False));return
    from importlib.metadata import version
    if version('agentscope')!='2.0.11':raise SystemExit('Worker requires the tested AgentScope 2.0.11 environment')
    from .commands import Catalog
    from .worker import Control,Worker
    from finance_agent.config import AgentConfig
    from finance_agent.state import AgentState
    key = os.environ.get('FA_GATEWAY_API_KEY') or os.environ.get('LEDGER_LLM_API_KEY','')
    if not key and raw.get('api_key_file'):
        key = Path(raw['api_key_file']).expanduser().read_text().strip()
    base=raw.get('base_url') or os.environ.get('FA_GATEWAY_BASE_URL') or os.environ.get('LEDGER_LLM_BASE_URL','')
    if raw.get('inherit_ledger_gateway',True) and (not base or not key):
        from .worker import ROOT
        sys.path.insert(0,str(ROOT/'ledger'))
        from ledger.assist import load_config
        inherited=load_config()
        base,key=base or inherited.base_url,key or inherited.api_key
    config = Config(base_url=base,
                    api_key=key,models=tuple(ModelSpec(**row) for row in raw.get('models',[])))
    if raw.get('model_enabled',True) is False or os.environ.get('LEDGER_AGENT_MODEL_ENABLED')=='0':
        config=Config(base_url='',api_key='',models=config.models)
    if args.repair_event:
        from .ops_runtime import run_repair,validate_ops_model
        from .worker import ROOT
        sys.path.insert(0,str(ROOT))
        from ops.ledger_sentinel.repair import Repair
        # Do not create a worktree before the ops-specific model is validated.
        try:validate_ops_model(config)
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            print(json.dumps({'state':'waiting_configuration','reason':'ops_model_not_validated','worktree_created':False,'published':False}))
            raise SystemExit(2)
        repo=Path(raw.get('repo_root',ROOT)).resolve()
        repair_config=raw['repair']
        from ops.ledger_sentinel.financial_oracle import configured_financial_oracle
        repair=Repair(repo,Path(repair_config['home']),repair_config['executor'],
            oracle=configured_financial_oracle(repair_config['executor']))
        version=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
        body=repair.prepare(json.loads(args.repair_event.read_text(encoding='utf-8')),version)
        result=asyncio.run(run_repair(repair,body['repair_id'],config,state))
        print(json.dumps(result,ensure_ascii=False))
        raise SystemExit(0 if result['state']=='reviewable' else 1)
    token = os.environ.get('FINANCE_AGENT_ACCESS_TOKEN')
    if not token and raw.get('service_token_file'):
        token = Path(raw['service_token_file']).expanduser().read_text().strip()
    if not token: raise SystemExit('Missing dedicated ledger worker service token')
    host_state = AgentState(state.root/'protocol')
    host_state.set_secret('access_token',token)
    host_state.set_secret('agent_id',raw['worker_id'])
    control = Control(AgentConfig(control_plane_url=raw['control_plane_url'],state_dir=state.root),host_state)
    from .upstream import Collector,Config as UpstreamConfig
    options=dict(raw.get('upstream',{}))
    for name in ('order_token','cost_token'):
        filename=options.pop(name+'_file',None)
        if filename:options[name]=Path(filename).expanduser().read_text().strip()
    options.setdefault('ledger_base_url',raw['control_plane_url'])
    options['ledger_token']=token
    options.setdefault('order_token',os.environ.get('LEDGER_ORDER_FEED_TOKEN',''))
    options.setdefault('cost_token',os.environ.get('ERP_BRIDGE_COST_API_TOKEN',''))
    # Keep operational upstream writes independently disabled until rollout config enables them.
    options.setdefault('member_requests_enabled',False)
    collector=Collector(UpstreamConfig(**options),state.root/'upstream')
    configured=Catalog(raw.get('tools',{}),state.root/'tools')
    class Combined:
        def tools(self,checker):
            standard=collector.tools(checker);extra=configured.tools(checker)
            if set(standard)&set(extra):raise ValueError('Configured tool shadows a trusted upstream collector')
            return {**standard,**extra}
        def trusted_outcomes(self,case):
            return collector.trusted_outcomes(case)
        def configuration_sha(self):
            return collector.configuration_sha()
    worker = Worker(Runtime(config,state),control,Combined())
    try:
        while True:
            try: result = worker.once()
            except Exception as exc: result = {'state':'maintenance','error_code':type(exc).__name__}
            if result: print(json.dumps(result,ensure_ascii=False),flush=True)
            if args.once:break
            time.sleep(max(1,float(raw.get('poll_seconds',5))))
    finally:
        collector.close();control.close()
        host_state.connection.close()


if __name__=='__main__': main()
