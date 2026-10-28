"""Synthetic real-byte source replay, financial CAS and date dependency tests."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import hashlib,json
import polars as pl
import pytest

from ledger import source_recovery as source, source_dates, automation_store as journal
from ledger.engine.runtime import Ingestion
from test_source_recovery import source_case,request_task,source_claim
from test_publication_scope import configure


def raw_receipt(c,task,job):
    row=c.ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(task['task_id'],)).fetchone()
    plan=json.loads(row[0])['plan'];_,capture,blobs,*_=c.original
    folder=c.ws.root/'source-recovery'/task['task_id']/job['id'];folder.mkdir(parents=True)
    for sha,raw in blobs.items():(folder/(sha+'.raw')).write_bytes(raw)
    collector=Path(source.__file__).resolve().parents[2]/'agent-worker/ledger_agent/jst_source_cli.mjs'
    receipt={'schema':source.CAPABILITY,'task_id':task['task_id'],'job_id':job['id'],
        'plan_sha256':journal.digest(plan),'input_sha':plan['input_sha'],'parser_sha256':source.PARSER_SHA,
        'collector_sha256':hashlib.sha256(collector.read_bytes()).hexdigest(),
        'nodes':[{'order_id':n.order_id,'sha256':n.sha256} for n in capture.nodes],
        'relations':[{'order_id':n.order_id,'sha256':n.sha256} for n in capture.relations],
        'state':'raw_ready','reason':None}
    sha=source.durable(folder/'receipt.json',receipt)
    return {'job_id':job['id'],'lease_token':job['payload']['lease_token'],
        'input_sha':plan['input_sha'],'receipt_sha256':sha}


def completed(c):
    task=request_task(c);job=source_claim(c);body=raw_receipt(c,task,job)
    response=c.client.post('/api/automation/source-recovery/requests/'+task['task_id']+'/finish',json=body)
    assert response.status_code==200,response.text
    return task,job,body


def test_real_bytes_rechecked_and_adoption_does_not_need_old_run_nonce(source_case,monkeypatch):
    c=source_case;configure(monkeypatch,c.ws.root,(('s1',c.period),))
    task,job,_=completed(c)
    value=source.verified_source(c.ws,c.model,c.body)
    assert value['source_proof']['status']=='proved'
    assert value['source_binding']['source_attempt_id']==job['id']
    with c.ws.conn:c.ws.conn.execute("UPDATE automation_case SET input_sha='new-after-adoption'")
    adopted=source.verified_fact_source(c.ws,c.model,{'source_binding':value['source_binding']})
    assert adopted['source_proof']==value['source_proof']
    with pytest.raises(source.Conflict):source.verified_source(c.ws,c.model,c.body)
    raw=Path(value['source_refs'][0]['path']);raw.write_bytes(raw.read_bytes()+b'changed')
    with pytest.raises(source.SourcePending,match='source_raw_bytes_unverified'):
        source.verified_fact_source(c.ws,c.model,{'source_binding':value['source_binding']})


def test_adopted_binding_rejects_replaced_original_and_worker_json(source_case,monkeypatch):
    c=source_case;configure(monkeypatch,c.ws.root,(('s1',c.period),));completed(c)
    value=source.verified_source(c.ws,c.model,c.body);binding=value['source_binding']
    forged={**binding,'receipt_sha256':'f'*64}
    with pytest.raises(source.SourcePending,match='source_binding_journal_changed'):
        source.verified_fact_source(c.ws,c.model,{'source_binding':forged})
    with c.ws.conn:c.ws.conn.execute("DELETE FROM slot WHERE name='synthetic-cost.xlsx'")
    with pytest.raises(source.SourcePending,match='original_membership_changed'):
        source.verified_fact_source(c.ws,c.model,{'source_binding':binding})


def test_finishing_changed_current_proof_returns_controlled_409(source_case,monkeypatch):
    c=source_case;task=request_task(c);job=source_claim(c);body=raw_receipt(c,task,job)
    def conflict(*args,**kwargs):raise source.Conflict('declared current input changed')
    monkeypatch.setattr(source,'_current',conflict)
    response=c.client.post('/api/automation/source-recovery/requests/'+task['task_id']+'/finish',json=body)
    assert response.status_code==409
    assert 'declared current input changed' in response.json()['detail']


def test_current_new_financial_attempt_reuses_only_same_verified_target_without_raw_get(source_case,monkeypatch):
    c=source_case;task,job,_=completed(c)
    old=source.verified_source(c.ws,c.model,c.body)
    case=dict(c.ws.conn.execute('SELECT * FROM automation_case').fetchone())
    case['input_sha']='current-financial-input'
    # Isolate financial-current validation, whose old-CAS rejection is tested
    # above. Source plan itself still reopens originals and checks shop mapping.
    original_current=source._current
    monkeypatch.setattr(source,'_current',lambda *args:case)
    body={**c.body,'input_sha':case['input_sha']}
    new=source.submit(c.ws,c.model,body)
    assert new['task_id']!=task['task_id'] and new['state']=='source_raw_ready'
    payload=json.loads(c.ws.conn.execute('SELECT result_json FROM automation_attempt WHERE id=?',(new['task_id'],)).fetchone()[0])
    assert payload['cache_revalidated'] and payload['source_binding']==old['source_binding']
    # Same PID but a changed native original day is a real target change.
    original_plan=source._plan
    def changed(*args,**kwargs):
        result=original_plan(*args,**kwargs);result['original_day']='2026-05-16';return result
    monkeypatch.setattr(source,'_plan',changed);case['input_sha']='changed-original'
    different=source.submit(c.ws,c.model,{**body,'input_sha':case['input_sha']})
    assert different['state']=='source_queued'


def test_raw_ready_dispatches_recompute_once_when_scope_is_authorized(source_case,monkeypatch):
    c=source_case;configure(monkeypatch,c.ws.root,(('s1',c.period),))
    task,job,body=completed(c)
    endpoint='/api/automation/source-recovery/requests/'+task['task_id']+'/finish'
    assert c.client.post(endpoint,json=body).status_code==200
    rows=c.ws.conn.execute("SELECT result_json FROM automation_attempt WHERE state='dispatch_pending'").fetchall()
    assert len(rows)==1 and json.loads(rows[0][0])['target_period']==c.period
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()


def test_september_missing_order_feed_retains_only_readonly_original_anchors(tmp_path):
    from test_order_feed_cost_source_window import setup_feed,source_item,retained,STORE
    from ledger.model.schema import Model,SourceContract,Metric,ValueExpr
    feed,_=setup_feed(tmp_path)
    original=source_item(feed,order_time='2026-09-14 10:00:00',order_id='202600001',
        original_order_id='202600001',internal_order_id='100',internal_sub_order_id='101',
        __sheet__='商品',__row__=82)
    original.ref=original.ref.__class__('a'*64,'unchanged-original.xlsx','商品')
    original.frame=original.frame.with_columns(pl.lit('a'*64).alias('__anchor_sha__'))
    from ledger.model.loader import load_model
    from conftest import MODELS
    model=load_model(MODELS/'cn-ecommerce').model_copy(update={'stores':(STORE,)})
    ingestion=Ingestion(model=model,items=[original]);before=original.frame.clone()
    source.capture_originals(ingestion);feed.append_to(ingestion,STORE)
    assert not retained(ingestion), 'inside-window old price must never return to financial inputs'
    gap=pl.DataFrame({'period':['2026-09'],'order_id':['202600001']})
    index=source.anchor_index(ingestion,STORE,'2026-09',None,gap)
    assert index['orders']['202600001']['internal_order_ids']==['100']
    assert index['orders']['202600001']['cost']==[{'sha256':'a'*64,'sheet':'商品','row':82}]
    assert ingestion.source_recovery_originals[0].frame.equals(before)
    assert all(item is not ingestion.source_recovery_originals[0] for item in ingestion.items)


def date_fact(ws,sid,pid,day):
    value={'schema':source_dates.VERSION,'store_id':sid,'platform_order_id':pid,'original_day':day}
    ws.conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
        (journal.ident(),'synthetic-case',journal.ident(),'synthetic-input','date_source_fact',journal.encoded(value),journal.now()))


def test_date_epoch_is_pid_scoped_and_changes_before_any_recompute(source_case):
    c=source_case;deps={'potential_parent_ids':['202600001']}
    first=source_dates.fingerprint(c.ws,'s1','2026-09',deps)
    with c.ws.conn:date_fact(c.ws,'s1','202600002','2026-07-15');date_fact(c.ws,'s2','202600001','2026-07-15')
    assert source_dates.fingerprint(c.ws,'s1','2026-09',deps)==first
    with c.ws.conn:date_fact(c.ws,'s1','202600001','2026-07-15')
    second=source_dates.fingerprint(c.ws,'s1','2026-09',deps);assert second!=first
    with c.ws.conn:date_fact(c.ws,'s1','202600001','2026-07-16')
    assert source_dates.fingerprint(c.ws,'s1','2026-09',deps)!=second


def test_unlinked_refund_input_is_a_potential_date_dependency():
    from ledger.model.loader import load_model
    from conftest import MODELS
    model=load_model(MODELS/'cn-ecommerce');store=model.store('taobao_mt9s7n9x')
    metric=next(m for m in model.metrics if m.id=='trade_refund').for_platform('taobao')
    frame=pl.DataFrame({metric.link.key:['202600001','202600002','202600003'],
        str(metric.orderless_time_basis or metric.time_basis):['2026-09-15','2026-08-15',None]})
    ingestion=SimpleNamespace(known=[SimpleNamespace(template=SimpleNamespace(source=metric.source,time_slots={}),frame=frame)])
    result=source_dates.potential_parents(ingestion,model,store,'2026-09',facts=pl.DataFrame())
    assert result['potential_parent_ids']==['202600001','202600003']


@pytest.mark.parametrize('source_case',[{'date_only':True}],indirect=True)
def test_date_fact_retires_actual_financial_input_immediately_not_only_after_manager(source_case):
    from ledger.automation_checker import check
    c=source_case
    before=check(c.ws,'s1','2026-09',model=c.model)['input_sha']
    with c.ws.conn:date_fact(c.ws,'s1','202600002','2026-07-15')
    assert check(c.ws,'s1','2026-09',model=c.model)['input_sha']==before
    with c.ws.conn:date_fact(c.ws,'s1','202600001','2026-07-15')
    after=check(c.ws,'s1','2026-09',model=c.model)['input_sha']
    assert after!=before
    with c.ws.conn:date_fact(c.ws,'s1','202600001','2026-07-15')
    assert check(c.ws,'s1','2026-09',model=c.model)['input_sha']==after


def test_human_close_refresh_is_persistent_force_capture_and_old_generation_is_fenced(source_case,monkeypatch):
    c=source_case;configure(monkeypatch,c.ws.root,(('s1',c.period),));completed(c)
    value=source.verified_source(c.ws,c.model,c.body)
    fact={'decision_id':'synthetic-source-fact','store_id':'s1','period':c.period,'source_binding':value['source_binding']}
    monkeypatch.setattr('ledger.automation_source_cost_v3.used',lambda *args:[fact])
    actor={'id':'finance-synthetic','kind':'human','authenticated':True}
    kwargs={'period':c.period,'store_ids':['s1'],'request_id':'close-refresh-synthetic-one','actor':actor}
    first=source.ensure_close_refresh(c.ws,c.model,**kwargs)
    assert first['status']=='processing' and first['required']
    assert source.ensure_close_refresh(c.ws,c.model,**kwargs)==first
    with pytest.raises(source.SourcePending,match='source_capture_generation_not_ready'):
        source.verified_fact_source(c.ws,c.model,fact)
    prior=source.verified_fact_source(c.ws,c.model,fact,allow_pending_refresh=True)
    assert prior['source_proof']==value['source_proof'] and prior['close_eligible'] is False
    assert prior['basis_status']=='previously_verified_awaiting_close_refresh'
    row=c.ws.conn.execute("SELECT * FROM automation_attempt WHERE state='source_queued'").fetchone()
    payload=json.loads(row['result_json']);assert payload['cache_reuse_allowed'] is False
    task={'task_id':row['id']};job=source_claim(c);body=raw_receipt(c,task,job)
    response=c.client.post('/api/automation/source-recovery/requests/'+row['id']+'/finish',json=body)
    assert response.status_code==200,response.text
    final=source.ensure_close_refresh(c.ws,c.model,**kwargs)
    assert final['status']=='verified' and final['generation_sha']
    assert source.verify_close_refresh(c.ws,c.model,refresh_id=final['refresh_id'],generation_sha=final['generation_sha'])==final
    assert source.verified_fact_source(c.ws,c.model,fact)['task_id']==row['id']
    next_refresh=source.ensure_close_refresh(c.ws,c.model,**{**kwargs,'request_id':'close-refresh-synthetic-two'})
    assert next_refresh['status']=='processing'
    with pytest.raises(source.Conflict,match='后续任务替代'):
        source.verify_close_refresh(c.ws,c.model,refresh_id=final['refresh_id'],generation_sha=final['generation_sha'])


def test_empty_close_request_is_sealed_and_cannot_be_silently_widened(source_case,monkeypatch):
    c=source_case;actor={'id':'finance-synthetic','kind':'human','authenticated':True}
    current=[];monkeypatch.setattr('ledger.automation_source_cost_v3.used',lambda *args:current)
    kwargs={'period':c.period,'store_ids':['s1'],'request_id':'close-empty-synthetic','actor':actor}
    first=source.ensure_close_refresh(c.ws,c.model,**kwargs)
    assert first['status']=='verified' and not first['required']
    current.append({'decision_id':'new-fact'})
    with pytest.raises(source.Conflict,match='事实'):
        source.ensure_close_refresh(c.ws,c.model,**kwargs)


def test_same_plan_new_raw_quantity_cannot_pass_close_or_keep_old_fact(source_case,monkeypatch):
    from test_jst_source_members import rebuild
    c=source_case;configure(monkeypatch,c.ws.root,(('s1',c.period),));completed(c)
    value=source.verified_source(c.ws,c.model,c.body)
    fact={'decision_id':'synthetic-source-fact','store_id':'s1','period':c.period,'source_binding':value['source_binding']}
    monkeypatch.setattr('ledger.automation_source_cost_v3.used',lambda *args:[fact])
    actor={'id':'finance-synthetic','kind':'human','authenticated':True}
    kwargs={'period':c.period,'store_ids':['s1'],'request_id':'changed-raw-close','actor':actor}
    source.ensure_close_refresh(c.ws,c.model,**kwargs)
    row=c.ws.conn.execute("SELECT * FROM automation_attempt WHERE state='source_queued'").fetchone()
    job=source_claim(c)
    changed=deepcopy(c.original[4]);changed['items'][0]['qty']='2'
    c.original=rebuild(c.original,target=changed)
    body=raw_receipt(c,{'task_id':row['id']},job)
    response=c.client.post('/api/automation/source-recovery/requests/'+row['id']+'/finish',json=body)
    assert response.status_code==200,response.text
    result=source.ensure_close_refresh(c.ws,c.model,**kwargs)
    assert result['status']=='pending' and result['generation_sha'] is None
    assert result['pending'][0]['state']=='physical_source_changed'
    with pytest.raises(source.SourcePending,match='physical_facts_changed'):
        source.verified_fact_source(c.ws,c.model,fact)
    with pytest.raises(PermissionError):
        source.ensure_close_refresh(c.ws,c.model,**{**kwargs,'actor':c.actor})


def test_superseded_capture_worker_cannot_publish_even_if_financial_input_unchanged(source_case,monkeypatch):
    c=source_case;configure(monkeypatch,c.ws.root,(('s1',c.period),));completed(c)
    value=source.verified_source(c.ws,c.model,c.body)
    fact={'decision_id':'synthetic-source-fact','store_id':'s1','period':c.period,'source_binding':value['source_binding']}
    monkeypatch.setattr('ledger.automation_source_cost_v3.used',lambda *args:[fact])
    actor={'id':'finance-synthetic','kind':'human','authenticated':True}
    kwargs={'period':c.period,'store_ids':['s1'],'request_id':'supersede-one','actor':actor}
    source.ensure_close_refresh(c.ws,c.model,**kwargs)
    row=c.ws.conn.execute("SELECT * FROM automation_attempt WHERE state='source_queued'").fetchone()
    job=source_claim(c);body=raw_receipt(c,{'task_id':row['id']},job)
    source.ensure_close_refresh(c.ws,c.model,**{**kwargs,'request_id':'supersede-two'})
    response=c.client.post('/api/automation/source-recovery/requests/'+row['id']+'/finish',json=body)
    assert response.status_code==409 and '代际已被替代' in response.json()['detail']
    assert c.ws.conn.execute('SELECT state FROM automation_attempt WHERE id=?',(row['id'],)).fetchone()[0]=='source_queued'


def test_september_window_404_plan_reopens_real_xlsx_and_normalizes_new_raw(tmp_path,monkeypatch):
    from decimal import Decimal
    import io
    from test_order_feed_cost_source_window import setup_feed,source_item,retained,STORE
    from test_jst_source_members import HEADERS,xlsx,order,item,html
    from ledger.model.loader import load_model
    from ledger.engine.runtime import Ingested
    from ledger.engine.types import FileRef,Recognition
    from ledger import automation_period_proof as pp
    from ledger.workspace import Workspace
    from ledger.storage_integrity import seal
    from conftest import MODELS
    monkeypatch.setenv('LEDGER_SOURCE_RECOVERY_PERIODS','2026-09')
    feed,_=setup_feed(tmp_path);ws=Workspace(feed.workspace_root)
    model=load_model(MODELS/'cn-ecommerce').model_copy(update={'stores':(STORE,)})
    cost=xlsx(HEADERS,['100','101','202600001','C1','A',Decimal('1.25'),'2026-09-15',STORE.name,'普通订单'])
    native=xlsx(['主订单编号','子订单编号','订单创建时间'],['202600001','C1','2026-09-15'])
    rc=ws.keep('source-cost-202609.xlsx',io.BytesIO(cost),STORE.id)
    rn=ws.keep('source-native-202609.xlsx',io.BytesIO(native),STORE.id)
    original=source_item(feed,order_time='2026-09-15',order_id='202600001',original_order_id='202600001',
        sub_order_id='C1',internal_order_id='100',internal_sub_order_id='101',quantity=1.25,sku='A',
        __sheet__='证据',__row__=3,__anchor_row__=3,__anchor_sha__=rc.sha)
    original.ref=FileRef(rc.sha,'source-cost-202609.xlsx','证据')
    spine=next(x.id for x in model.sources if x.is_spine and x.platform in ('*','any',STORE.platform))
    nt=next(t for t in model.templates if t.source==spine)
    nr=FileRef(rn.sha,'source-native-202609.xlsx','证据')
    nf=pl.DataFrame({'order_id':['202600001'],'sub_order_id':['C1'],'store_name':[STORE.name],
        'order_time':['2026-09-15'],'__sheet__':['证据'],'__row__':[3]})
    ni=Ingested(nr,Recognition(nr,'source',12,nt.id,spine),1,nf,nt)
    ing=Ingestion(model=model,items=[original,ni]);source.capture_originals(ing)
    feed.append_to(ing,STORE)
    assert not retained(ing), 'September source missing from OrderOS cannot restore uploaded old price'
    gap=pl.DataFrame({'coverage_key':['sept-404'],'context_sha':['a'*64],'order_id':['202600001'],
        'order_count':[1],'goods_scope_version':[1],'partial_cost':[False]})
    facts=pl.DataFrame({'metric_id':['trade_income'],'amount':[100.],'source_id':[spine],
        'template_id':[nt.id],'store':[STORE.name],'period':['2026-09'],'file_sha':[rn.sha],
        'sheet':['证据'],'row_no':[3],'order_id':['202600001']})
    payload={'can_close':False,'missing_sources':[],'findings':[],
        'statement':[{'id':'revenue','value':100.,'available':True}]}
    rid=ws.record(STORE.id,'2026-09',payload,[rc.sha,rn.sha])
    gap.write_parquet(ws.coverage_gaps_path(rid));seal(ws.coverage_gaps_path(rid))
    facts.write_parquet(ws.facts_path(rid));seal(ws.facts_path(rid))
    sl=SimpleNamespace(period='2026-09',facts=facts,pricing_gaps=pl.DataFrame(),coverage_gap_rows=gap)
    pp.save(ws,rid,pp.prepare(ws,model,STORE,ing,sl,payload,None))
    case={'id':'synthetic-sept-case','store_id':STORE.id,'period':'2026-09','run_id':rid,'input_sha':'declared-synthetic-cas'}
    class Mapping:
        def get(self,path):
            assert path=='stores'
            return {'stores':[{'ledger_store_id':STORE.id,'order_store_id':'77','mapping_status':'confirmed','consumable':True}]}
    plan=source._plan(ws,model,case,{'coverage_key':'sept-404','context_sha':'a'*64},Mapping())
    assert plan['source_order_id']=='100' and plan['original_day']=='2026-09-15'
    assert plan['cost_anchors']==[{'sha256':rc.sha,'sheet':'证据','row':3}]
    tid='source-'+'1'*40;jid='2'*32;folder=ws.root/'source-recovery'/tid/jid;folder.mkdir(parents=True)
    source_node=order('100',[item(created='2026-09-15 12:00:00')],status='Split',order_date='2026-09-15 10:00:00')
    physical=order('200',[item('200','201',created='2026-09-15 12:00:00')],link_o_id='100',is_split=True,is_merge=True,order_date='2026-09-15 10:00:00')
    nodes=[]
    for data in (source_node,physical):
        raw=html(data);sha=hashlib.sha256(raw).hexdigest();(folder/(sha+'.raw')).write_bytes(raw)
        nodes.append({'order_id':data['o_id'],'sha256':sha})
    raw=b'<span onclick="ShowOrder(100)">x</span><span onclick="ShowOrder(200)">y</span>'
    rel_sha=hashlib.sha256(raw).hexdigest();(folder/(rel_sha+'.raw')).write_bytes(raw)
    collector=Path(source.__file__).resolve().parents[2]/'agent-worker/ledger_agent/jst_source_cli.mjs'
    receipt={'schema':source.CAPABILITY,'task_id':tid,'job_id':jid,'plan_sha256':journal.digest(plan),
        'input_sha':plan['input_sha'],'parser_sha256':source.PARSER_SHA,
        'collector_sha256':hashlib.sha256(collector.read_bytes()).hexdigest(),'state':'raw_ready','reason':None,
        'nodes':nodes,'relations':[{'order_id':'100','sha256':rel_sha}]}
    receipt_sha=source.durable(folder/'receipt.json',receipt)
    proved=source.verify_receipt(ws,plan,tid,jid,receipt_sha)
    assert proved['source_proof']['status']=='proved',proved
    assert proved['source_proof']['members'][0]['pricing_day']=='2026-09-15'
    # Same parsed locators cannot hide a later original-byte change.
    ws.path_of(rc.sha).write_bytes(cost+b'changed')
    with pytest.raises(source.SourcePending,match='source_original_bytes_changed'):
        source.verify_receipt(ws,plan,tid,jid,receipt_sha)
    ws.close()


def test_new_raw_metadata_changes_lineage_but_not_proved_financial_members(source_case,monkeypatch):
    from test_jst_source_members import rebuild
    c=source_case;configure(monkeypatch,c.ws.root,(('s1',c.period),));completed(c)
    value=source.verified_source(c.ws,c.model,c.body)
    fact={'decision_id':'synthetic-source-fact','store_id':'s1','period':c.period,'source_binding':value['source_binding']}
    monkeypatch.setattr('ledger.automation_source_cost_v3.used',lambda *args:[fact])
    kwargs={'period':c.period,'store_ids':['s1'],'request_id':'metadata-only-close',
        'actor':{'id':'finance-synthetic','kind':'human','authenticated':True}}
    source.ensure_close_refresh(c.ws,c.model,**kwargs)
    row=c.ws.conn.execute("SELECT * FROM automation_attempt WHERE state='source_queued'").fetchone();job=source_claim(c)
    changed=deepcopy(c.original[4]);changed['irrelevant_capture_note']='new source metadata'
    changed['items'][0]['qty']='1.2500'
    c.original=rebuild(c.original,target=changed)
    body=raw_receipt(c,{'task_id':row['id']},job)
    response=c.client.post('/api/automation/source-recovery/requests/'+row['id']+'/finish',json=body)
    assert response.status_code==200,response.text
    result=source.ensure_close_refresh(c.ws,c.model,**kwargs)
    assert result['status']=='verified' and result['generation_sha']
    fresh=source.verified_fact_source(c.ws,c.model,fact)
    assert journal.digest(fresh['source_proof'])!=journal.digest(value['source_proof'])
    assert source.proof_semantic(fresh['source_proof'])==source.proof_semantic(value['source_proof'])
    assert fresh['receipt_sha256']!=value['receipt_sha256']
    assert any(r['source']=='fixed_jst_source_receipt' and r['sha256']==fresh['receipt_sha256'] for r in fresh['source_refs'])


def gate_first_writer(ws,monkeypatch):
    """Force both optimistic reads to finish even when no sync is needed."""
    from contextlib import contextmanager
    import threading
    barrier=threading.Barrier(2);local=threading.local();original=ws.transaction
    @contextmanager
    def synchronized():
        if not getattr(local,'passed',False):
            local.passed=True;barrier.wait(timeout=15)
        with original() as conn:yield conn
    monkeypatch.setattr(ws,'transaction',synchronized)


@pytest.mark.parametrize('physical',[False,True])
def test_concurrent_same_close_request_rechecks_under_writer_and_creates_one_generation(source_case,monkeypatch,physical):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from ledger import automation_actions as actions
    c=source_case;configure(monkeypatch,c.ws.root,(('s1',c.period),))
    if physical:
        completed(c);value=source.verified_source(c.ws,c.model,c.body)
        fact={'decision_id':'synthetic-source-fact','store_id':'s1','period':c.period,'source_binding':value['source_binding']}
        monkeypatch.setattr('ledger.automation_source_cost_v3.used',lambda *args:[fact])
    else:monkeypatch.setattr('ledger.automation_source_cost_v3.used',lambda *args:[])
    if physical:
        barrier=threading.Barrier(2);original=actions.sync_month
        def synchronized(*args,**kwargs):
            result=original(*args,**kwargs);barrier.wait(timeout=15);return result
        monkeypatch.setattr(actions,'sync_month',synchronized)
    else:
        gate_first_writer(c.ws,monkeypatch)
    kwargs={'period':c.period,'store_ids':['s1'],'request_id':'concurrent-same-source-preview',
        'actor':{'id':'finance-synthetic','kind':'human','authenticated':True}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls=[pool.submit(source.ensure_close_refresh,c.ws,c.model,**kwargs) for _ in range(2)]
        results=[call.result(timeout=30) for call in calls]
    assert results[0]==results[1]
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='source_close_refresh'").fetchone()[0]==1
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='source_queued'").fetchone()[0]==int(physical)
    if physical:
        head=source.generation_head(c.ws,c.body['case_id'],'202600001')
        assert head['generation']==2 # Initial proved source plus exactly one refresh.


@pytest.mark.parametrize('change',['actor','scope'])
def test_concurrent_same_request_with_different_identity_is_conflict_not_integrity_error(source_case,monkeypatch,change):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from ledger import automation_actions as actions
    c=source_case;monkeypatch.setattr('ledger.automation_source_cost_v3.used',lambda *args:[])
    gate_first_writer(c.ws,monkeypatch)
    first={'period':c.period,'store_ids':['s1'],'request_id':'concurrent-different-source-preview',
        'actor':{'id':'finance-synthetic','kind':'human','authenticated':True}}
    second=deepcopy(first)
    if change=='actor':second['actor']['id']='finance-other'
    else:second['store_ids']=['s1','s2']
    def invoke(kwargs):
        try:return {'result':source.ensure_close_refresh(c.ws,c.model,**kwargs)}
        except source.Conflict:return {'controlled_conflict':True}
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls=[pool.submit(invoke,k) for k in (first,second)]
        results=[call.result(timeout=30) for call in calls]
    assert sum('result' in r for r in results)==1 and sum(r.get('controlled_conflict',False) for r in results)==1
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='source_close_refresh'").fetchone()[0]==1
