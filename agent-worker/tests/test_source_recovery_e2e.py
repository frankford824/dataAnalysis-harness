"""Actual fixed Node child/HTTP protocol over declared synthetic raw fixtures."""
from pathlib import Path
import sys,shutil,hashlib,json
import httpx
import pytest
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'ledger'))
sys.path.insert(0,str(ROOT/'ledger/tests'))
from test_source_recovery import source_case,request_task,PERIOD
from ledger import automation_checker as checker
from ledger_agent.source_recovery import Config,Executor
from ledger_agent.upstream import Collector,Config as UpstreamConfig
from ledger_agent.worker import Control,Worker
from finance_agent.config import AgentConfig
from finance_agent.state import AgentState

def fixture_executor(c,monkeypatch,*,node_missing=False):
    directory=c.tmp_path/'reader-fixture';directory.mkdir()
    data={'100':c.original[3],'200':c.original[4]}
    payload=directory/'source-data.json';payload.write_text(json.dumps(data,ensure_ascii=False))
    module=directory/'jst.mjs'
    module.write_text('''import fs from 'node:fs';
const data=JSON.parse(fs.readFileSync(process.env.SOURCE_FIXTURE_PAYLOAD,'utf8'));
export class Jst{constructor(){this.cookie='fixture';this.ua='fixture';this.coid='10'}
 async callback(path,method,args){const where=JSON.parse(args[1]);const id=where[0].v;
 const rows=where[0].k==='so_id'?Object.values(data).filter(d=>d.so_id===id):data[id]?[data[id]]:[];
 return {datas:rows.map(d=>({...d,o_id_en:'enc-'+d.o_id,src_status:d.status}))}}}
''')
    preload=directory/'transport.mjs'
    preload.write_text('''import fs from 'node:fs';
const data=JSON.parse(fs.readFileSync(process.env.SOURCE_FIXTURE_PAYLOAD,'utf8'));
globalThis.fetch=async input=>{const url=new URL(input);let raw;
 if(url.pathname.endsWith('OrderEditorNew.aspx')){const id=url.searchParams.get('order_id').replace('enc-','');raw='<html><script>var _OrderData='+JSON.stringify(data[id])+';</script></html>'}
 else if(url.pathname.endsWith('MergeOrder.aspx'))raw='<span onclick="ShowOrder(100)">x</span><span onclick="ShowOrder(200)">y</span>';
 else throw Error('Unexpected fixed fixture route');return new Response(raw,{status:200,headers:{'Content-Type':'text/html'}})};
''')
    import ledger_agent.source_recovery as worker_source
    real=worker_source.subprocess.Popen
    def injected(argv,**kwargs):
        if len(argv)<2 or Path(str(argv[1])).name!='jst_source_cli.mjs':return real(argv,**kwargs)
        env={**kwargs['env'],'NODE_OPTIONS':'--import='+preload.as_uri(),'SOURCE_FIXTURE_PAYLOAD':str(payload)}
        return real(argv,**{**kwargs,'env':env})
    monkeypatch.setattr(worker_source.subprocess,'Popen',injected)
    config=Config(workspace_root=str(c.ws.root),node_binary=str(directory/'missing-node') if node_missing else shutil.which('node'),
                  jst_module=str(module),jst_sha256=hashlib.sha256(module.read_bytes()).hexdigest(),enabled=True,evaluation=True)
    return Executor(config)


def worker(c,executor):
    def transport(request):
        response=c.client.request(request.method,request.url.path+('?' + request.url.query.decode() if request.url.query else ''),
                                  content=request.content,headers=dict(request.headers))
        return httpx.Response(response.status_code,content=response.content,headers=response.headers)
    state=AgentState(c.tmp_path/'protocol');state.set_secret('agent_id',c.actor['id']);state.set_secret('access_token','synthetic')
    control=Control(AgentConfig(control_plane_url='http://ledger',state_dir=c.tmp_path),state,transport=httpx.MockTransport(transport))
    class NoModel:
        async def run(self,*args):raise AssertionError('Fixed source task must not occupy a model loop')
    class Catalog:
        def configuration_sha(self):return 'synthetic-source-catalog'
    return Worker(NoModel(),control,Catalog(),source_executor=executor,renew_seconds=.05)


def test_actual_subprocess_fixed_reader_and_independent_backend_proof(source_case,monkeypatch):
    c=source_case;task=request_task(c);executor=fixture_executor(c,monkeypatch)
    value=worker(c,executor).once()
    assert value['state']=='source_raw_ready' and value['business_complete'] is False and value['model_used'] is False
    stored=json.loads(c.ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(task['task_id'],)).fetchone()[0])
    path=c.ws.root/'source-recovery'/task['task_id']/stored['source_attempt_id']/'independent-proof.json'
    proof=json.loads(path.read_text())
    assert proof['source_proof']['status']=='proved',proof
    assert len(proof['source_proof']['members'])==1
    assert proof['financial_applied'] is False
    assert not checker.check(c.ws,'s1',PERIOD,model=c.model)['allowed']
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()
    assert not c.ws.conn.execute("SELECT 1 FROM period WHERE state='closed'").fetchone()


def test_missing_node_is_persistent_pending_and_never_false_complete(source_case,monkeypatch):
    c=source_case;request_task(c)
    value=worker(c,fixture_executor(c,monkeypatch,node_missing=True)).once()
    assert value['state']=='source_pending' and value['reason']=='source_node_runtime_unavailable'
    assert value['business_complete'] is False and value['financial_applied'] is False


def test_restored_node_retries_same_task_on_host_epoch_without_model(source_case,monkeypatch):
    c=source_case;task=request_task(c)
    executor=fixture_executor(c,monkeypatch,node_missing=True)
    running=worker(c,executor)
    first=running.once();assert first['state']=='source_pending'
    old=json.loads(c.ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(task['task_id'],)).fetchone()[0])
    Path(executor.config.node_binary).symlink_to(shutil.which('node'))
    second=running.once()
    assert second['state']=='source_raw_ready' and second['task_id']==task['task_id']
    new=json.loads(c.ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(task['task_id'],)).fetchone()[0])
    assert old['source_attempt_id']!=new['source_attempt_id'] and len(new['prior_source_receipts'])==1
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()


def test_fixed_source_recovery_remains_available_when_model_is_disabled(source_case,monkeypatch):
    c=source_case;request_task(c);executor=fixture_executor(c,monkeypatch)
    monkeypatch.setattr(executor,'model_qualified_for',lambda config:False)
    value=worker(c,executor).once()
    assert value['state']=='source_raw_ready' and value['model_used'] is False


def test_missing_404_reaches_real_queue_fixed_child_and_independent_proof(source_case,monkeypatch):
    c=source_case;seen=[]
    def wire(request):
        path=request.url.path;seen.append((request.method,path))
        if path.startswith('/api/automation/source-recovery/'):
            response=c.client.request(request.method,path,content=request.content,headers=dict(request.headers))
            return httpx.Response(response.status_code,content=response.content)
        if path.startswith('/api/automation/months/'):
            from ledger.automation_actions import month
            return httpx.Response(200,json=month(c.ws,c.model,PERIOD))
        if path.endswith('/pricing-gaps'):
            return httpx.Response(200,json={'items':[{'metric_id':'goods_cost','coverage_key':'synthetic-one',
                'order_id':'202600001','internal_order_id':'100','store':'测试店','period':PERIOD}],
                'matching':1})
        if path.endswith('/coverage-gaps'):
            return httpx.Response(200,json={'items':[{'coverage_key':'synthetic-one','context_sha':'a'*64,
                'order_id':'202600001','order_count':1,'line_revision':0,'store':'测试店','period':PERIOD}],
                'matching':1})
        if path.endswith('/stores'):
            return httpx.Response(200,json={'stores':[{'ledger_store_id':'s1','order_store_id':'77',
                'mapping_status':'confirmed','consumable':True}]})
        return httpx.Response(404,json={'detail':'original absent from synthetic OrderOS ingestion'})
    collector=Collector(UpstreamConfig(ledger_base_url='http://ledger',order_base_url='http://order',
        cost_base_url='http://cost',ledger_token='synthetic',order_token='synthetic',source_recovery_enabled=True),c.tmp_path/'upstream',
        client=httpx.Client(transport=httpx.MockTransport(wire)))
    row=next(r for r in __import__('ledger.automation_actions',fromlist=['month']).month(c.ws,c.model,PERIOD)['stores'] if r['store_id']=='s1')
    case={'id':row['case_id'],'store_id':'s1','period':PERIOD,'input_sha':row['input_sha'],
          'run_id':row['run_id'],'source_context_sha':row['source_context_sha']}
    outcome=collector.tools(lambda:{'verified':False,'lease_active':True,'input_sha':case['input_sha']})['collect_case_evidence'](case,{})
    assert outcome['status']=='pending' and outcome['accounting_ready'] is False,(outcome.get('reason'),outcome.get('error_code'),seen)
    request=outcome['items'][0]['request']
    assert request['source_task_state']=='source_queued' and request['business_complete'] is False
    assert ('GET','/api/integration/ledger/v1/orders/100') in seen
    task_id=request['task_id']
    result=worker(c,fixture_executor(c,monkeypatch)).once()
    assert result['state']=='source_raw_ready' and result['task_id']==task_id
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()


@pytest.mark.parametrize('source_case',[{'with_dates':True}],indirect=True)
def test_proved_dates_enter_existing_inputs_without_scope_origin_read_loss(source_case,monkeypatch):
    c=source_case
    class SyntheticScope:
        identity={'schema':'synthetic-verification-only','approval_reference':'test:date-input-no-financial-release'}
        def allows(self,store_id,period):return store_id=='s1' and period==PERIOD
        def require(self,store_id,period,**kwargs):assert self.allows(store_id,period)
        def verify(self,*args):return None
    monkeypatch.setattr('ledger.publication_scope.load_automatic',lambda *args,**kwargs:SyntheticScope())
    request_task(c);value=worker(c,fixture_executor(c,monkeypatch)).once()
    assert value['state']=='source_raw_ready'
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='date_source_fact'").fetchone()[0]==1
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='dispatch_pending'").fetchone()[0]==1
    from ledger.source_dates import attach_saved
    from ledger.engine.runtime import Ingestion
    from ledger.engine.refund_dates import lookup
    ingestion=Ingestion(model=c.model,items=[])
    result=attach_saved(c.ws,c.model,c.model.store('s1'),ingestion,None)
    assert result['attached']==1 and ingestion.known[0].template.source=='refund_order_dates'
    assert lookup(ingestion.items,{'测试店':'测试店'})['测试店','202600001'][0]=='2026-05'
    assert not c.ws.conn.in_transaction
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()
    assert not c.ws.conn.execute("SELECT 1 FROM period WHERE state='closed'").fetchone()
    # A different original parent's newly proved month must not change this
    # parent's derived input SHA, row citation or financial run identity.
    first_sha=ingestion.original_date_source_evidence[0]['input_ref']['sha256']
    first_note=lookup(ingestion.items,{'测试店':'测试店'})['测试店','202600001'][1]
    from test_jst_source_members import order,item,html
    from ledger.jst_source_members import Node
    from ledger.source_dates import prove_original_day
    from ledger import automation_store as journal
    import copy
    raw=html(order('300',[item('300','301',oi_so_id='202600002',raw_so_id='202600002',outer_so_id='202600002')],
                   so_id='202600002',order_date='2026-04-01 10:00:00'))
    sha=hashlib.sha256(raw).hexdigest();saved=c.ws.root/'source-recovery'/'extra-date.raw';saved.write_bytes(raw)
    old=json.loads(c.ws.conn.execute("SELECT result_json FROM automation_attempt WHERE state='date_source_fact'").fetchone()[0])
    other=copy.deepcopy(old);other.update(platform_order_id='202600002',original_day='2026-04-01',
        source_order_ids=['300'],proof=prove_original_day('202600002',['77'],[Node('300',sha)],{sha:raw}),
        source_refs=[{'path':str(saved),'sha256':sha,'order_id':'300'}])
    with c.ws.transaction() as conn:conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
        (journal.ident(),c.body['case_id'],'synthetic-other-parent',c.body['input_sha'],'date_source_fact',journal.encoded(other),journal.now()))
    again=Ingestion(model=c.model,items=[]);assert attach_saved(c.ws,c.model,c.model.store('s1'),again,None)['attached']==2
    own=next(x for x in again.original_date_source_evidence if x['source_fact']['platform_order_id']=='202600001')
    assert own['input_ref']['sha256']==first_sha
    assert lookup(again.items,{'测试店':'测试店'})['测试店','202600001'][1]==first_note


@pytest.mark.parametrize('source_case',[{'with_dates':True,'date_only':True}],indirect=True)
def test_sealed_refund_date_demand_reaches_input_without_model_or_cost_dag(source_case,monkeypatch):
    c=source_case
    class SyntheticScope:
        identity={'schema':'synthetic-nine-only','approval_reference':'test:source-nine-date-recovery'}
        def allows(self,sid,period):return sid=='s1' and period=='2026-09'
        def require(self,sid,period,**kwargs):assert self.allows(sid,period)
        def verify(self,*args):return None
    monkeypatch.setattr('ledger.publication_scope.load_automatic',lambda *args,**kwargs:SyntheticScope())
    # No caller supplies a task/identity/date: real claim discovers the sealed
    # financial source-row demand and uses the fixed original-day capability.
    value=worker(c,fixture_executor(c,monkeypatch)).once()
    assert value['state']=='source_raw_ready' and value['model_used'] is False
    fact=json.loads(c.ws.conn.execute("SELECT result_json FROM automation_attempt WHERE state='date_source_fact'").fetchone()[0])
    assert fact['case_period']=='2026-09' and fact['original_day']=='2026-05-15'
    from ledger.engine.runtime import Ingestion
    from ledger.source_dates import attach_saved
    ingestion=Ingestion(model=c.model,items=[])
    assert attach_saved(c.ws,c.model,c.model.store('s1'),ingestion,None)['attached']==1
    assert ingestion.known[0].template.source=='refund_order_dates'
    assert c.ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]==1
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()
