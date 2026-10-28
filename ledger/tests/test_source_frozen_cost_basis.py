
"""Frozen source restoration uses original return engine, never a new calculator.

These are declared synthetic financial archives. They do not certify production
amounts or replace full-service historical/manual golden acceptance.
"""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from types import SimpleNamespace
import hashlib,io,json
import polars as pl
import pytest

from ledger import automation_source_cost_v3 as v3,source_recovery,automation_period_proof as proof,automation_store as journal,service
from ledger.engine.runtime import Ingestion,run
from ledger.engine.types import ANCHOR_SHA,ANCHOR_SHEET,ANCHOR_ROW,ANCHOR_FILE
from ledger.model.schema import SourceContract,Template,Predicate
from ledger.model.loader import load_model
from ledger.workspace import Workspace
from ledger.storage_integrity import seal,digest
from ledger.upstream_history import HistoryClient
from test_source_cost_posting import sample,make_item
from test_jst_source_members import fixture,prove
from conftest import MODELS


@pytest.fixture
def frozen_case(tmp_path,monkeypatch,request):
    options=getattr(request,'param',{})
    ing,baseline,store,target,members,quotes=sample(priced_a=options.get('priced_a',False))
    store=store.model_copy(update={'cost_return_posting':'transaction'})
    full=load_model(MODELS/'cn-ecommerce')
    returned=full.metric('goods_return_cost').model_copy(update={'by_platform':()})
    model=ing.model.model_copy(update={'stores':(store,),'sources':(*ing.model.sources,
        SourceContract(id='after_sales',name='Synthetic actual return receipts',owner_role='warehouse',cadence='monthly',required_for_close=False),
        SourceContract(id='cost_return',name='Original return adapter',owner_role='warehouse',cadence='monthly',required_for_close=False)),
        'metrics':(*ing.model.metrics,returned),'statement':tuple(n.model_copy(update={
            'formula':n.formula.model_copy(update={'of':('income','goods_cost','goods_return_cost')})}) for n in ing.model.statement)})
    ing.model=model;baseline=run(ing,store.platform)
    ws=Workspace(tmp_path/'workspace')
    f=ing.test_source_fixture
    raw_refs=[]
    for name,anchor in [('original-cost.xlsx',f[0].cost_anchors[0]),('original-native.xlsx',f[0].native_anchors[0])]:
        kept=ws.keep(name,io.BytesIO(f[2][anchor.sha256]),store.id)
        raw_refs.append({'path':str(ws.path_of(kept.sha)),'sha256':kept.sha})
    class Quotes(HistoryClient):
        def _fetch(self,sku,company,day):
            value={'data':[{'sku_id':sku,'wms_co_id':company,'begin_date':'2026-05-01','end_date':'2026-05-31','cost_price':'1'}]}
            return json.dumps(value).encode(),value['data']
    client=Quotes(ws.root);captured=[client.quote(sku,'10','2026-05-15') for sku in ('A','B')]
    raw_refs.extend(r for q in captured for r in q['artifact_refs'])
    stamp=datetime.now(timezone.utc);started=(stamp-timedelta(days=1)).isoformat()
    grant={'enabled':True,'rule_id':v3.RULE,'rule_sha':v3.RULE_SHA,'valid_from':started,
        'valid_until':(stamp+timedelta(days=1)).isoformat(),'period_from':'2026-05','period_until':'2026-05'}
    fact={'schema':v3.SCHEMA,'rule_id':v3.RULE,'rule_sha':v3.RULE_SHA,'store_id':store.id,'period':'2026-05',
        'target':target,'members':members,'source_binding':{},'quotes':captured,'source_refs':raw_refs}
    with ws.conn:
        ws.conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            ('human-auth','grant','finance','human','decision.apply','input','p','{}',started))
        ws.conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('grant-id','grant','rule_grant','2026-05',store.id,'apply','',journal.encoded(grant),'synthetic grant','input','p','finance',started))
        ws.conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            ('fact-auth','adopt','service','delegated_service',v3.RULE,'input',journal.digest(fact),
             journal.encoded({'approved_rule_grant':'grant-id','rule_sha':v3.RULE_SHA}),stamp.isoformat()))
        ws.conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('source-fact','adopt',v3.RULE,'2026-05',store.id,'apply','',journal.encoded({'rule_sha':v3.RULE_SHA,'fact':fact}),
             'synthetic prior adoption','input',journal.digest(fact),'service',stamp.isoformat()))
    verified={'source_proof':prove(f),'plan':{'platform_order_id':'202600001','shop_ids':['77']},
        'source_refs':raw_refs,'source_binding':{}}
    monkeypatch.setattr(source_recovery,'verified_fact_source',lambda *args,**kwargs:verified)
    result=v3.consume(ws,model,store,ing,baseline)
    sl=result.slices[(store.name,'2026-05')]
    assert sl.facts.filter((pl.col('metric_id')=='goods_cost')&pl.col('counted'))['contribution'].sum()==-5,[(f.check_id,f.detail) for f in sl.audit.findings]
    payload=service.slice_dict(sl,store,model)
    rid=ws.record(store.id,'2026-05',payload,[i.ref.sha256 for i in ing.items])
    sl.facts.write_parquet(ws.facts_path(rid));seal(ws.facts_path(rid))
    p=proof.prepare(ws,model,store,ing,sl,payload,None);proof.save(ws,rid,p)
    assert v3.used(ws,store.id,'2026-05')[0]['decision_id']=='source-fact'
    source=json.loads(ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone()[0])['approved_cost_sources'][0]
    assert len(source['source_inputs'])==(3 if options.get('priced_a') else 2)
    if not getattr(request,'param',{}).get('open'):
        with ws.conn:ws.conn.execute("UPDATE period SET state='closed',run_id=? WHERE store_id=? AND period='2026-05'",(rid,store.id))
    def frozen_hash():
        row=ws.conn.execute('SELECT * FROM run WHERE id=?',(rid,)).fetchone()
        state=ws.conn.execute("SELECT * FROM period WHERE store_id=? AND period='2026-05'",(store.id,)).fetchone()
        saved=ws.conn.execute('SELECT * FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone()
        return journal.digest([dict(row),dict(state),dict(saved),digest(ws.facts_path(rid))])
    def next_ing(receipts,*,changed_policy=False):
        current=model
        if changed_policy:
            goods=model.metric('goods_cost').model_copy(update={'where':(Predicate(field='business_kind',op='eq',value='new-only'),)})
            current=model.model_copy(update={'metrics':tuple(goods if m.id=='goods_cost' else m for m in model.metrics)})
        native=deepcopy(ing.items[0]);native.frame=native.frame.with_columns(pl.lit('202600002').alias('order_id'),
            pl.lit('C2').alias('sub_order_id'),pl.lit(datetime(2026,6,1)).alias('order_date'),
            pl.lit(datetime(2026,6,1)).alias('order_time'))
        template=Template(id='synthetic_actual_returns',name='Actual receipts',source='after_sales',match_columns=('return',),
            time_slots={'settle_date':'return_time'},bindings=({'role':'return_time','columns':('return_time',)},))
        rows=[]
        for index,(member,quantity) in enumerate(receipts):
            rows.append({'internal_order_id':'200','internal_sub_order_id':member,'sub_order_id':'C1',
                'sku':'A' if member=='201' else 'B','order_id':'202600001','store_name':store.name,
                'refund_status':'退款成功','goods_status':'卖家已收到退货','returned_quantity':quantity,
                'after_sale_id':'E'+str(index),'settle_date':datetime(2026,6,20),
                'return_time':datetime(2026,6,20),ANCHOR_SHA:'c'*64,ANCHOR_FILE:'returns.xlsx',ANCHOR_SHEET:'证据',ANCHOR_ROW:index+3})
        fresh=Ingestion(current,[native,make_item(template,rows,'c'*64)])
        return fresh,run(fresh,store.platform)
    yield SimpleNamespace(ws=ws,model=model,store=store,run_id=rid,source=source,hash=frozen_hash,
        next=next_ing,original=ing,fact=v3.active(ws.conn,store.id,'2026-05')[0])
    ws.close()


def return_total(result):
    return result.facts.filter((pl.col('metric_id')=='goods_return_cost')&pl.col('counted'))['contribution'].sum()


def test_frozen_members_feed_new_month_return_without_touching_old_hash(frozen_case,monkeypatch):
    c=frozen_case;before=c.hash()
    def forbidden(*args,**kwargs):raise AssertionError('frozen origin must not request current JST or history quotes')
    monkeypatch.setattr(source_recovery,'verified_fact_source',forbidden)
    monkeypatch.setattr(HistoryClient,'from_env',forbidden)
    ing,baseline=c.next([('201',1),('202',1)])
    scope=SimpleNamespace(allows=lambda sid,period:period=='2026-06',periods_for_store=lambda sid:['2026-06'])
    result=v3.consume(c.ws,ing.model,c.store,ing,baseline,scope)
    assert return_total(result)==2,result.facts.select('metric_id','template_id','period','counted','contribution','amount','linked','link_key','source_note').to_dicts()
    assert c.hash()==before
    assert {i.template.id for i in ing.known if i.template.source=='order_cost'}=={'jst_source_cost_frozen_v3'}
    assert all(e['frozen_origin_run_id']==c.run_id for e in ing.physical_cost_evidence)


def test_frozen_same_parent_members_and_over_return_use_original_quantity_caps(frozen_case):
    c=frozen_case;before=c.hash()
    ing,baseline=c.next([('201',100),('201',100),('202',100)])
    result=v3.consume(c.ws,ing.model,c.store,ing,baseline)
    assert return_total(result)==5  # A has 2 pieces; B has 3; same-parent totals cannot bleed.
    assert c.hash()==before


def test_current_sale_where_change_does_not_rewrite_frozen_return_basis(frozen_case):
    c=frozen_case;ing,baseline=c.next([('202',1)],changed_policy=True)
    result=v3.consume(c.ws,ing.model,c.store,ing,baseline)
    assert return_total(result)==1


def test_missing_frozen_member_archive_is_pending_not_current_quote_fallback(frozen_case):
    c=frozen_case;before=c.hash()
    from pathlib import Path
    Path(c.source['source_inputs'][0]['path']).unlink()
    ing,baseline=c.next([('202',1)])
    result=v3.consume(c.ws,ing.model,c.store,ing,baseline)
    assert return_total(result)==0
    assert any(f.check_id=='physical_source_fact_pending' for f in result.slices[(c.store.name,'2026-06')].audit.findings)
    assert c.hash()==before


def test_whole_period_manual_total_cannot_be_split_into_frozen_member_prices(frozen_case):
    c=frozen_case
    c.ws.conn.execute('INSERT INTO manual_finance(run_id,store_id,period,decision_json,result_json,original_sha,at,by) VALUES(?,?,?,?,?,?,?,?)',
        (c.run_id,c.store.id,'2026-05','{}',json.dumps({'manual_cost':True,'goods_cost':100}),'synthetic original',journal.now(),'declared synthetic finance'))
    ing,baseline=c.next([('202',1)])
    result=v3.consume(c.ws,ing.model,c.store,ing,baseline)
    assert return_total(result)==0
    finding=next(f for f in result.slices[(c.store.name,'2026-06')].audit.findings if f.check_id=='physical_source_fact_pending')
    assert finding.detail['reason_code']=='whole_period_manual_total_has_no_member_price_basis'


@pytest.mark.parametrize('frozen_case',[{'open':True}],indirect=True)
def test_current_run_proof_selects_used_facts_and_ignores_noncounted_old_fact(frozen_case):
    c=frozen_case
    assert [f['decision_id'] for f in v3.used(c.ws,c.store.id,'2026-05')]==['source-fact']
    fresh,_,_,_,_,_=sample()
    goods=c.model.metric('goods_cost').model_copy(update={'where':(Predicate(field='business_kind',op='eq',value='new-only'),)})
    current=c.model.model_copy(update={'metrics':tuple(goods if m.id=='goods_cost' else m for m in c.model.metrics)})
    fresh.model=current
    result=v3.consume(c.ws,current,c.store,fresh,run(fresh,c.store.platform))
    sl=result.slices[(c.store.name,'2026-05')]
    assert not fresh.physical_cost_evidence
    payload=service.slice_dict(sl,c.store,current)
    rid=c.ws.record(c.store.id,'2026-05',payload,[i.ref.sha256 for i in fresh.items])
    sl.facts.write_parquet(c.ws.facts_path(rid));seal(c.ws.facts_path(rid))
    p=proof.prepare(c.ws,current,c.store,fresh,sl,payload,None);proof.save(c.ws,rid,p)
    assert len(v3.active(c.ws.conn,c.store.id,'2026-05'))==1
    assert v3.used(c.ws,c.store.id,'2026-05')==[]
    refreshed=v3.ensure_quote_refresh(c.ws,current,'2026-05',[c.store.id],'no-unused-refresh',
        {'id':'finance','kind':'human','authenticated':True})
    assert refreshed['required'] is False


def test_frozen_store_owner_id_survives_a_display_name_change(frozen_case):
    c=frozen_case;before=c.hash();ing,baseline=c.next([('202',1)])
    renamed=c.store.model_copy(update={'name':'新店名','aliases':()})
    ing.model=ing.model.model_copy(update={'stores':(renamed,)})
    for item in ing.items:
        item.frame=item.frame.with_columns(pl.lit(renamed.name).alias('store_name'))
        if '__hint_store__' in item.frame.columns:item.frame=item.frame.with_columns(pl.lit(renamed.name).alias('__hint_store__'))
    result=v3.consume(c.ws,ing.model,renamed,ing,run(ing,renamed.platform))
    assert return_total(result)==1
    assert c.hash()==before


@pytest.mark.parametrize('frozen_case',[{'priced_a':True}],indirect=True)
def test_preexisting_counted_sibling_keeps_its_frozen_price_for_later_return(frozen_case):
    c=frozen_case;before=c.hash()
    origins={r.get('origin') for r in c.source['source_inputs'] if r['role']=='counted_cost'}
    assert origins=={'v3','existing_counted_source'}
    assert c.source['close_quote_keys']==[['10','B','2026-05-15']]
    ing,baseline=c.next([('201',1),('202',1)])
    result=v3.consume(c.ws,ing.model,c.store,ing,baseline)
    assert return_total(result)==2
    assert c.hash()==before


def test_container_only_return_stays_pending_instead_of_disappearing(frozen_case):
    c=frozen_case;before=c.hash();ing,baseline=c.next([('202',1)])
    receipt=ing.frames_of('after_sales')[0]
    receipt.frame=receipt.frame.with_columns(pl.lit('100').alias('internal_order_id'),
        pl.lit('101').alias('internal_sub_order_id'),pl.lit('KIT').alias('sku'))
    result=v3.consume(c.ws,ing.model,c.store,ing,run(ing,c.store.platform))
    assert return_total(result)==0
    finding=next(f for f in result.slices[(c.store.name,'2026-06')].audit.findings if f.check_id=='physical_source_fact_pending')
    assert finding.detail['reason_code']=='physical_source_after_sale_identity_unproved'
    assert c.hash()==before


@pytest.mark.parametrize('receipt_store',['外店','未配置外店',None])
def test_frozen_basis_cannot_erase_explicit_or_unknown_receipt_owner(frozen_case,receipt_store):
    from ledger.model.schema import Store
    c=frozen_case;before=c.hash();ing,baseline=c.next([('202',1)])
    ing.model=ing.model.model_copy(update={'stores':(*ing.model.stores,
        Store(id='s2',name='外店',platform=c.store.platform,cost_return_posting='transaction'))})
    receipt=ing.frames_of('after_sales')[0]
    receipt.frame=receipt.frame.with_columns(pl.lit(receipt_store,dtype=pl.String).alias('store_name'))
    result=v3.consume(c.ws,ing.model,c.store,ing,run(ing,c.store.platform))
    assert return_total(result)==0
    assert result.eval_errors['cost_return']
    assert any(f.check_id=='cost_return_evidence' and f.blocking for sl in result.slices.values() for f in sl.audit.findings)
    assert c.hash()==before
