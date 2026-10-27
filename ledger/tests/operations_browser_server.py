"""Real temporary HTTP service for the Vue browser gate; never opens production."""
from pathlib import Path
import argparse
import shutil
import hashlib
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from fastapi import FastAPI,HTTPException,Request
import uvicorn

from ledger import automation_api, operations_api, commission_api, telemetry_api, worker_control_api
from operations_fixture import create_fixture


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--port',required=True,type=int)
    args=parser.parse_args()
    ws,model,registry,runs=create_fixture(Path(args.root),overhead=None,engine_can_close=True)
    registry.operator_add('browser','fixture-password-2030',admin=True)
    app=FastAPI()
    operations_api.install(app,lambda:ws,lambda:model)
    commission_api.install(app,lambda:ws,lambda:model)
    def actor(request):
        found=registry.actor(request.cookies.get('commission_session',''))
        return {**found,'kind':'human','authenticated':True} if found else None
    automation_api.install(app,lambda:ws,lambda:model,actor)
    telemetry_api.install(app,lambda:ws,actor)
    def worker_actor(request):
        return {'id':'browser-worker','kind':'service','authenticated':True,'scopes':['business']} if request.headers.get('authorization')=='Bearer browser-fixture-local-worker' else None
    worker_control_api.install(app,lambda:ws,lambda:model,worker_actor)
    # Exercise the legacy nullable response through its actual implementation.
    from ledger import api as legacy_api
    legacy_api.workspace=lambda:ws
    legacy_api._model=lambda:model
    legacy_api._snapshot=lambda:SimpleNamespace(model=model,revision=hashlib.sha256(model.model_dump_json().encode()).hexdigest())
    app.get('/api/stores/{store_id}')(legacy_api.store_detail)
    app.get('/api/order-feed/status')(legacy_api.order_feed_status)
    app.get('/api/stores/{store_id}/pricing-status')(legacy_api.pricing_progress)
    app.get('/api/stores/{store_id}/periods/{period}')(legacy_api.period_detail)
    legacy_api.DEFAULT_MODEL=Path(args.root)/'legacy-model'
    legacy_api.DEFAULT_MODEL.mkdir()
    @app.get('/api/commission')
    def legacy_commission(period:str=''):
        return legacy_api.commission_summary(period)
    @app.post('/api/_browser/prepare-close-case')
    def prepare_close_case(request:Request):
        nonlocal ws,model,registry
        if not actor(request):raise HTTPException(403,'Test setup requires the temporary operator')
        from test_replay_service import bundle,seed
        from ledger.replay_service import Session
        from ledger.workspace import Workspace
        from ledger.commission_registry import Registry
        from ledger.automation_checker import check
        source=Path(args.root)/'original-service-inputs';source.mkdir()
        path,missing=bundle.__wrapped__(source)
        target=Path(args.root)/'service-workspace'
        with Session(path,seed()) as session:
            session.drain_pending()
            for scope,order in missing.items():
                sid,period=scope.split('/')
                session.supplement(sid,period,amount='2.00',reason='合成原采购凭据已逐单审核',order_id=order)
            session.drain_pending()
            session.ws.conn.execute('PRAGMA wal_checkpoint(FULL)')
            shutil.copytree(session.root,target)
            next_model=session.model
        next_ws=Workspace(target);next_registry=Registry(target)
        next_registry.operator_add('browser','fixture-password-2030',admin=True)
        for sid in ('s1','s2'):
            result=check(next_ws,sid,'2026-07',model=next_model)
            if not result['allowed']:raise HTTPException(409,{'synthetic_original_service_not_ready':result['issues']})
        ws.close();ws,model,registry=next_ws,next_model,next_registry
        return {'period':'2026-07','stores':['s1','s2'],'synthetic_originals':True}
    @app.post('/api/_browser/prepare-subcent-case')
    def prepare_subcent_case(request:Request):
        nonlocal ws,model,registry
        if not actor(request):raise HTTPException(403,'Test setup requires the temporary operator')
        from test_operations_precision import subcent_fixture
        from ledger.commission_registry import Registry
        next_ws,next_model,unused_client=subcent_fixture(Path(args.root)/'subcent-workspace')
        unused_client.close()
        ws.close();ws,model=next_ws,next_model
        registry=Registry(ws.root)
        registry.operator_add('browser','fixture-password-2030',admin=True)
        return {'period':'2030-01','orders':100,'freight_each':'0.004000','freight_total':'0.40'}
    @app.post('/api/_browser/arrive-waiting-source')
    def arrive_waiting_source(request:Request):
        if not actor(request):raise HTTPException(403,'Test source arrival requires the temporary operator')
        from ledger import automation_actions
        before=ws.state('s1','2030-01')
        source=Path(args.root)/'原始日成本凭据-经营测试店甲.csv'
        source.write_text('原始日期,SKU,原始单价\n2030-01-01,SYNTHETIC,1.00\n',encoding='utf-8')
        file_sha=hashlib.sha256(source.read_bytes()).hexdigest()
        ws.keep(source.name,source,'s1')
        registry.enqueue_files({'s1'},'nas:'+file_sha)
        automation_actions.sync_month(ws,model,'2030-01')
        after=ws.state('s1','2030-01')
        return {'source_sha':file_sha,'unchanged_financial_run':getattr(before,'run_id',None)==getattr(after,'run_id',None),
            'unchanged_financial_payload':getattr(before,'result',None)==getattr(after,'result',None),
            'month':automation_actions.month(ws,model,'2030-01')}
    @app.post('/api/_browser/prepare-source-wait-case')
    def prepare_source_wait_case(request:Request):
        nonlocal ws,registry
        if not actor(request):raise HTTPException(403,'Test source case requires the temporary operator')
        from ledger.workspace import Workspace
        from ledger.commission_registry import Registry
        from ledger import automation_checker
        # This scope genuinely has no delivered originals or calculated run.
        # Do not turn the complete subcent result into a pretend source gap.
        next_ws=Workspace(Path(args.root)/'source-wait-workspace')
        target=next_ws.root/'commission/registry.db';target.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(registry.path.resolve().as_uri()+'?mode=ro',uri=True)) as origin:
            with closing(sqlite3.connect(target)) as destination:origin.backup(destination)
        next_registry=Registry(next_ws.root)
        ws.close();ws,registry=next_ws,next_registry
        automation_checker.bind_model(ws,model)
        assert ws.state('s1','2030-01') is None
        return {'period':'2030-01','store_id':'s1','source_materials':0,'financial_runs':0}
    @app.get('/api/_browser/rule-authority')
    def rule_authority(period:str,store_id:str,grant_id:str,request:Request):
        if not actor(request):raise HTTPException(403,'Test evidence requires the temporary operator')
        from ledger.automation_deterministic import approved_rule
        try:
            result=approved_rule(ws,store_id,period,grant_id)
            return {'approved':True,'decision_id':result['id']}
        except PermissionError as error:return {'approved':False,'reason':str(error)}
    @app.post('/api/_browser/prepare-company-finance-case')
    def prepare_company_finance_case(request:Request,mixed:bool=False):
        nonlocal ws,model,registry
        if not actor(request):raise HTTPException(403,'Test setup requires the temporary operator')
        import json
        from test_replay_service import bundle,seed,changed_bundle
        from ledger.replay_service import Session
        from ledger.workspace import Workspace
        from ledger.commission_registry import Registry
        from ledger import automation_actions
        source=Path(args.root)/('company-finance-mixed' if mixed else 'company-finance-only');source.mkdir()
        original,missing=bundle.__wrapped__(source)
        def total_ten(body):
            for row in body['overheads']:row['amount']='10.00'
        fixed=changed_bundle((original,missing),source/'approved-inputs',total_ten)
        target=source/'workspace'
        with Session(fixed,seed()) as session:
            session.drain_pending()
            for scope,order in missing.items():
                sid,period=scope.split('/')
                session.supplement(sid,period,amount='2.00',reason='合成原件成本批准',order_id=order)
            session.drain_pending()
            session.close_approved('s1','2026-07',approval_reference='synthetic-original-close',reason='合成原件完整审批后的首店结账')
            before=session.ws.state('s1','2026-07')
            saved={'run_id':before.run_id,'labor_cut':before.labor_cut,'result_sha':hashlib.sha256(json.dumps(before.result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
            # A later explicit model input is a conflict to review, never an
            # instruction to rewrite the already frozen store or its payout.
            session.model=session.model.model_copy(update={'overheads':tuple(row.model_copy(update={'amount':20}) if row.period=='2026-07' else row for row in session.model.overheads)})
            session.recompute('s2');session.drain_pending()
            next_model=session.model
            if mixed:
                newcomer=next_model.stores[0].model_copy(update={'id':'s3','name':'待取证测试店','aliases':()})
                next_model=next_model.model_copy(update={'stores':(*next_model.stores,newcomer)})
            session.ws.conn.execute('PRAGMA wal_checkpoint(FULL)');shutil.copytree(session.root,target)
        next_ws=Workspace(target);next_registry=Registry(target)
        next_registry.operator_add('browser','fixture-password-2030',admin=True)
        ws.close();ws,model,registry=next_ws,next_model,next_registry
        value=automation_actions.month(ws,model,'2026-07')
        groups=[row for row in value['decision_groups'] if row['kind']=='company_finance']
        if len(groups)!=1:raise HTTPException(409,{'missing_company_finance_fixture':value})
        return {'period':'2026-07','synthetic_originals':True,'mixed':mixed,'closed_reference':saved,'month':value}
    @app.get('/api/_browser/company-frozen-record')
    def company_frozen_record(request:Request):
        if not actor(request):raise HTTPException(403,'Test evidence requires the temporary operator')
        import json
        state=ws.state('s1','2026-07')
        return {'run_id':state.run_id,'labor_cut':state.labor_cut,'result_sha':hashlib.sha256(json.dumps(state.result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
    @app.get('/api/navigation')
    def navigation():
        periods={state.period for state in ws.overview()}|{item.period for item in model.overheads}
        return {'stores':[{'id':s.id,'name':s.name,'platform':s.platform} for s in model.stores],
                'platforms':[{'id':'taobao','name':'淘宝天猫'}],'periods':sorted(periods,reverse=True),
                'default_period':max(periods),'ingest_mode':'api','data_revision':'browser-fixture'}
    app.get('/api/overview')(legacy_api.overview)
    @app.get('/api/recompute/progress')
    def progress():return {'items':[]}
    @app.get('/api/version')
    def version():return {'version':'browser-fixture'}
    uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
