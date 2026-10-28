"""Full source/quote close bridge from raw files and actual counted run proof.

All inputs and the loopback HTTP provider are declared synthetic. No money
expected values are recorded; no production source or financial writer is used.
"""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import hashlib,io,json,threading,urllib.parse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ledger import automation_actions as actions,automation_checker as checker,automation_store as journal
from ledger import automation_source_cost_v3 as v3,source_recovery,service,cost_lines
from ledger.commission_registry import Registry
from ledger.commission_manager import Manager
from ledger.model.schema import Model,Platform,Store,SourceContract,Template,ParseOptions,ColumnBinding,Metric,StatementNode,Overhead,Check
from ledger.upstream_history import HistoryClient
from ledger.worker_control_api import install
from ledger.workspace import Workspace
from test_jst_source_members import fixture,HEADERS
from test_publication_scope import configure

PERIOD='2026-05'
HUMAN={'id':'integration-finance','kind':'human','authenticated':True}


@contextmanager
def http_quotes():
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed=urllib.parse.urlsplit(self.path);q=urllib.parse.parse_qs(parsed.query)
            if parsed.path!='/api/cost/history' or self.headers.get('X-ERP-Bridge-Cost-Token')!='integration-local-token':
                self.send_response(403);self.end_headers();return
            calls.append(q)
            raw=json.dumps({'data':[{'sku_id':q['sku_ids'][0],'wms_co_id':q['wms_co_ids'][0],
                'begin_date':'2026-05-01','end_date':'2026-05-31','cost_price':'3.335'}]}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def log_message(self,*_):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port),calls
    finally:server.shutdown();server.server_close();thread.join(timeout=5)



def raw_xlsx(headers,values):
    import openpyxl
    book=openpyxl.Workbook();sheet=book.active;sheet.title='证据'
    sheet.append(['Declared synthetic source preamble']);sheet.append(headers)
    sheet.append([float(v) if isinstance(v,Decimal) else v for v in values])
    output=io.BytesIO();book.save(output);return output.getvalue()

def capture(c,task_id):
    response=c.client.post('/api/v1/agent-jobs/claim',json={'capabilities':['recompute','source_recovery']})
    assert response.status_code==200,response.text
    job=response.json();assert job['payload']['source_task']['task_id']==task_id
    response=c.client.get('/api/automation/source-recovery/requests/'+task_id+'/plan',params={'job_id':job['id'],'lease_token':job['payload']['lease_token']})
    assert response.status_code==200,response.text
    plan=response.json()['plan'];folder=c.ws.root/'source-recovery'/task_id/job['id'];folder.mkdir(parents=True)
    _,data,blobs,*_=c.original
    for ref in (*data.nodes,*data.relations):
        raw=blobs[ref.sha256];assert hashlib.sha256(raw).hexdigest()==ref.sha256
        (folder/(ref.sha256+'.raw')).write_bytes(raw)
    collector=Path(source_recovery.__file__).resolve().parents[2]/'agent-worker/ledger_agent/jst_source_cli.mjs'
    receipt={'schema':source_recovery.CAPABILITY,'task_id':task_id,'job_id':job['id'],'plan_sha256':journal.digest(plan),
        'input_sha':plan['input_sha'],'parser_sha256':source_recovery.PARSER_SHA,'collector_sha256':hashlib.sha256(collector.read_bytes()).hexdigest(),
        'state':'raw_ready','reason':None,'nodes':[{'order_id':n.order_id,'sha256':n.sha256} for n in data.nodes],
        'relations':[{'order_id':n.order_id,'sha256':n.sha256} for n in data.relations],'responses':[]}
    sha=source_recovery.durable(folder/'receipt.json',receipt)
    response=c.client.post('/api/automation/source-recovery/requests/'+task_id+'/finish',json={'job_id':job['id'],
        'lease_token':job['payload']['lease_token'],'input_sha':plan['input_sha'],'receipt_sha256':sha})
    assert response.status_code==200,response.text
    assert response.json()['task']['state']=='source_raw_ready'
    return plan


@pytest.fixture
def source_close_case(tmp_path,monkeypatch):
    ws=Workspace(tmp_path/'workspace');Registry(ws.root)
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    monkeypatch.setenv('LEDGER_SOURCE_RECOVERY_PERIODS',PERIOD)
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','1')
    monkeypatch.setenv('LEDGER_SOURCE_COST_V3_ENABLED','1')
    original=fixture()
    native_headers=['主订单编号','子订单编号','订单创建时间','买家实付','店铺名称','宝贝ID','物流单号','数量']
    native=raw_xlsx(native_headers,['202600001','C1','2026-05-15',Decimal('20'),'测试店','123456789001','W1',Decimal('1.25')])
    ws.keep('native.xlsx',io.BytesIO(native),'s1')
    ws.keep('cost.xlsx',io.BytesIO(raw_xlsx([*HEADERS,'单位成本'],['100','101','202600001','C1','A',Decimal('1.25'),'2026-05-15','测试店','普通订单',None])),'s1')
    def template(name,source,columns):
        return Template(id=name,name=name,source=source,parse=ParseOptions(header_row=1),
            match_columns=tuple(label for _,label,_ in columns if label!='单位成本'),
            time_slots={'order_date':'order_time'},bindings=tuple(ColumnBinding(role=key,columns=(label,),kind=kind) for key,label,kind in columns))
    nt=template('native-integration','order_detail',[('order_id','主订单编号','text'),('sub_order_id','子订单编号','text'),('order_time','订单创建时间','time'),
        ('buyer_paid','买家实付','number'),('store_name','店铺名称','text'),('product_id','宝贝ID','text'),('tracking_no','物流单号','text'),('quantity','数量','number')])
    ct=template('cost-integration','order_cost',[('internal_order_id','内部订单号','text'),('internal_sub_order_id','子订单编号','text'),('order_id','原始线上订单号','text'),
        ('original_order_id','原始线上订单号','text'),('sub_order_id','线上子订单编号','text'),('sku','商品编码','text'),('quantity','数量','number'),
        ('order_time','下单时间','time'),('store_name','店铺名称','text'),('order_type','订单类型','text'),('unit_cost','单位成本','number')])
    model=Model(id='source-close-integration',name='Declared synthetic raw close integration',
        platforms=(Platform(id='taobao',name='淘宝',cost_pricing='historical'),),stores=(Store(id='s1',name='测试店',platform='taobao'),),
        sources=(SourceContract(id='order_detail',name='原始订单',is_spine=True,owner_role='shop_owner',cadence='monthly'),
            SourceContract(id='order_cost',name='原始成本',owner_role='warehouse',cadence='monthly')),
        templates=(nt,ct),metrics=(Metric(id='income',name='销售',source='order_detail',value={'op':'sum','of':['buyer_paid']},time_basis='order_date',
            link={'key':'sub_order_id','to':'order.sub_order_id','grain':'order'}),
            Metric(id='goods_cost',name='商品成本',source='order_cost',value={'op':'sum_product','of':['quantity','unit_cost']},sign='abs_negate',time_basis='order_date',
                link={'key':'sub_order_id','to':'order.sub_order_id','grain':'order'})),
        statement=(StatementNode(id='n_receipt',name='销售收入',headline='revenue',formula={'op':'add','of':['income']}),
            StatementNode(id='gross_profit',name='商品毛利',commission_base=True,formula={'op':'add','of':['income','goods_cost']}),
            StatementNode(id='net_profit',name='利润',headline='profit',formula={'op':'add','of':['gross_profit']})),
        overheads=(Overhead(period=PERIOD,amount=0,name='Declared synthetic confirmed zero overhead'),),
        checks=(Check(id='goods_coverage',name='成本覆盖',kind='spine_coverage',metric='goods_cost',threshold=1.,blocking=True),))
    configure(monkeypatch,ws.root,(('s1',PERIOD),))
    result=service.recompute(ws,model,model.store('s1'));assert result.failure is None,result.failure
    stamp=datetime.now(timezone.utc)
    grant=actions.decision_preview(ws,model,request_id='integration-grant-preview',period=PERIOD,store_id='s1',kind='rule_grant',
        values={'rule_id':v3.RULE,'rule_sha':v3.RULE_SHA,'enabled':True,'valid_from':(stamp-timedelta(days=1)).isoformat(),
                'valid_until':(stamp+timedelta(days=1)).isoformat()},reason='Declared synthetic authenticated source grant',actor=HUMAN)
    actions.decision_apply(ws,model,request_id='integration-grant-confirm',proposal_id=grant['proposal_id'],proposal_sha=grant['proposal_sha'],input_sha=grant['input_sha'],actor=HUMAN)
    actions.sync_month(ws,model,PERIOD)
    run=ws.latest_run('s1',PERIOD);targets=cost_lines.current(ws,run['id'],'s1',PERIOD)['items']
    assert len(targets)==1,targets
    target=targets[0];case=ws.conn.execute('SELECT * FROM automation_case').fetchone()
    actor={'id':'integration-source-worker','kind':'service','authenticated':True,'scopes':['business'],'capabilities':['approved_action']}
    app=FastAPI();clock=[1000];install(app,lambda:ws,lambda:model,lambda _:actor,clock=lambda:clock[0])
    class Mapping:
        def get(self,path):
            assert path=='stores'
            return {'stores':[{'ledger_store_id':'s1','order_store_id':'77','mapping_status':'confirmed','consumable':True}]}
    monkeypatch.setattr('ledger.order_feed.Client',Mapping)
    with TestClient(app) as client,http_quotes() as (url,calls):
        monkeypatch.setenv('LEDGER_HISTORY_COST_API_BASE',url);monkeypatch.setenv('LEDGER_HISTORY_COST_API_TOKEN','integration-local-token')
        c=SimpleNamespace(ws=ws,model=model,client=client,clock=clock,original=original,actor=actor,calls=calls)
        body={'case_id':case['id'],'run_id':run['id'],'input_sha':case['input_sha'],'coverage_key':target['coverage_key'],'context_sha':target['context_sha']}
        response=client.post('/api/automation/source-recovery/requests',json=body);assert response.status_code==200,response.text
        task=response.json();assert task['state']=='source_queued',task
        capture(c,task['task_id'])
        current=checker.check(ws,'s1',PERIOD,model=model)
        adopted=v3.apply(ws,model,{**body,'store_id':'s1','period':PERIOD,'request_id':'integration-adopt',
            'rule_id':v3.RULE,'expected_line_revision':0,'input_sha':current['input_sha']},actor)
        assert adopted['status']=='adopted',adopted.get('reason_codes')
        manager=Manager(lambda:ws,lambda:model)
        for _ in range(8):
            manager.once()
            with Registry(ws.root).connect() as db:pending=db.execute('SELECT count(*) FROM pending').fetchone()[0]
            if not pending:break
        used=v3.used(ws,'s1',PERIOD)
        if not used:
            from ledger.engine.runtime import ingest,run as engine_run
            real=ingest(ws.active_files('s1'),model,['测试店'],default_store='测试店')
            source_recovery.capture_originals(real)
            again=v3.consume(ws,model,model.store('s1'),real,engine_run(real,'taobao'))
            assert used,[(f.check_id,f.detail) for f in again.slices[('测试店',PERIOD)].audit.findings]
        assert len(used)==1,json.dumps({'active':len(v3.active(ws.conn,'s1',PERIOD)),'findings':json.loads(ws.latest_run('s1',PERIOD)['result']).get('findings'),'sources':json.loads(ws.conn.execute('SELECT manifest_json FROM automation_run_proof ORDER BY run_id DESC LIMIT 1').fetchone()[0]).get('approved_cost_sources')},ensure_ascii=False)
        assert used[0]['decision_id']==adopted['decision_id']
        assert used[0]['source_binding']
        c.manager=manager;c.used=used;c.target=target
        yield c
    ws.close()


def test_actual_raw_adoption_is_selected_by_the_actual_counted_engine_run(source_close_case):
    c=source_close_case
    assert len(c.calls)==1
    assert len(v3.used(c.ws,'s1',PERIOD))==1
    assert c.ws.state('s1',PERIOD).state=='open'
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()


def bridge(c):
    run_id=c.ws.latest_run('s1',PERIOD)['id']
    input_sha=checker.check(c.ws,'s1',PERIOD,model=c.model,verify_artifacts=True)['input_sha']
    kwargs={'request_id':'real-source-quote-bridge','period':PERIOD,'store_ids':['s1'],'actor':HUMAN}
    before_quotes=len(c.calls)
    first=actions.close_preview(c.ws,c.model,**kwargs)
    assert first['status']=='processing' and first['proposal_id'] is None
    assert first['source_refresh']['required']
    assert not c.ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state LIKE 'physical_quote_refresh_%'").fetchone()
    assert actions.close_preview(c.ws,c.model,**kwargs)==first
    assert c.ws.latest_run('s1',PERIOD)['id']==run_id
    assert checker.check(c.ws,'s1',PERIOD,model=c.model,verify_artifacts=True)['input_sha']==input_sha
    task=c.ws.conn.execute("SELECT id FROM automation_attempt WHERE state='source_queued'").fetchone()
    assert task
    capture(c,task['id'])
    assert c.ws.latest_run('s1',PERIOD)['id']==run_id
    assert checker.check(c.ws,'s1',PERIOD,model=c.model,verify_artifacts=True)['input_sha']==input_sha
    second=actions.close_preview(c.ws,c.model,**kwargs)
    assert second['status']=='processing' and second['proposal_id'] is None
    assert second['source_refresh']['status']=='verified' and second['source_quote_refresh']['status']=='processing'
    assert len(c.calls)==before_quotes, 'queue creation is not a provider fetch'
    assert actions.close_preview(c.ws,c.model,**kwargs)==second
    c.manager.once()
    assert len(c.calls)==before_quotes+1
    assert v3.quote_refresh_view(c.ws,second['source_quote_refresh']['refresh_id'])['status']=='verified'
    assert c.ws.latest_run('s1',PERIOD)['id']==run_id, 'technical quote task must not recompute finance'
    assert checker.check(c.ws,'s1',PERIOD,model=c.model,verify_artifacts=True)['input_sha']==input_sha
    final=actions.close_preview(c.ws,c.model,**kwargs)
    assert final.get('proposal_id'),final
    assert len(final['eligible'])+len(final['blocked'])==1
    assert actions.close_preview(c.ws,c.model,**kwargs)==final
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='source_close_refresh'").fetchone()[0]==1
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='physical_quote_refresh_ready'").fetchone()[0]==1
    assert not c.ws.state('s1',PERIOD).closed
    assert not c.ws.conn.execute('SELECT 1 FROM manual_finance').fetchone()
    with Registry(c.ws.root).connect() as db:assert not db.execute('SELECT 1 FROM payout_confirmation').fetchone()
    return final,run_id,input_sha


@pytest.mark.parametrize('invalidate',['generation','input'])
def test_actual_source_then_real_http_quote_then_manager_rejects_stale_confirm_without_close(source_close_case,invalidate):
    c=source_close_case;final,run_id,input_sha=bridge(c)
    if invalidate=='generation':
        newer=actions.close_preview(c.ws,c.model,request_id='new-source-close-generation',period=PERIOD,store_ids=['s1'],actor=HUMAN)
        assert newer['status']=='processing'
    else:
        headers=['主订单编号','子订单编号','订单创建时间','买家实付','店铺名称','宝贝ID','物流单号','数量']
        c.ws.keep('native.xlsx',io.BytesIO(raw_xlsx(headers,['202600001','C1','2026-05-15',Decimal('21'),'测试店','123456789001','W1',Decimal('1.25')])),'s1')
    with pytest.raises(actions.Conflict,match='代际|输入'):
        actions.close_confirm(c.ws,c.model,request_id='reject-stale-confirm',proposal_id=final['proposal_id'],
            proposal_sha=final['proposal_sha'],input_sha=final['input_sha'],actor=HUMAN)
    assert not c.ws.state('s1',PERIOD).closed
    assert c.ws.latest_run('s1',PERIOD)['id']==run_id
    assert not c.ws.conn.execute('SELECT 1 FROM manual_finance').fetchone()


def test_finished_source_refresh_has_no_later_financial_recompute_obligation(source_close_case):
    c=source_close_case;_,run_id,input_sha=bridge(c)
    for _ in range(3):c.manager.once()
    assert c.ws.latest_run('s1',PERIOD)['id']==run_id, 'a successful equivalent source refresh must not enqueue its own financial recompute'
    assert checker.check(c.ws,'s1',PERIOD,model=c.model,verify_artifacts=True)['input_sha']==input_sha
    assert not c.ws.state('s1',PERIOD).closed


def test_actual_changed_physical_proof_is_pending_and_does_enqueue_financial_review(source_close_case):
    from copy import deepcopy
    from test_jst_source_members import rebuild
    c=source_close_case;run_id=c.ws.latest_run('s1',PERIOD)['id']
    first=actions.close_preview(c.ws,c.model,request_id='changed-physical-close-bridge',period=PERIOD,store_ids=['s1'],actor=HUMAN)
    assert first['status']=='processing'
    changed=deepcopy(c.original[4]);changed['items'][0]['qty']='2'
    c.original=rebuild(c.original,target=changed)
    task=c.ws.conn.execute("SELECT id FROM automation_attempt WHERE state='source_queued'").fetchone()
    capture(c,task['id'])
    status=actions.close_preview(c.ws,c.model,request_id='changed-physical-close-bridge',period=PERIOD,store_ids=['s1'],actor=HUMAN)
    assert status['status']=='pending' and status['proposal_id'] is None
    assert not c.ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state LIKE 'physical_quote_refresh_%'").fetchone()
    outbox=c.ws.conn.execute("SELECT result_json FROM automation_attempt WHERE state='dispatch_pending'").fetchall()
    assert any(json.loads(r[0]).get('purpose')=='independently_verify_source_cost_without_model' for r in outbox)
    c.manager.once()
    assert c.ws.latest_run('s1',PERIOD)['id']>run_id
    result=json.loads(c.ws.latest_run('s1',PERIOD)['result'])
    assert any(f['id']=='physical_source_fact_pending' and not f['passed'] for f in result['findings'])
    assert not c.ws.state('s1',PERIOD).closed
    assert not c.ws.conn.execute('SELECT 1 FROM manual_finance').fetchone()
