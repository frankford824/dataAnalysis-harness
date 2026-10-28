
"""Actual original-engine source consumption, using synthetic immutable sources."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import json

import polars as pl
import pytest

from ledger import automation_source_cost_v3 as v3,source_cost_posting as posting,automation_store as journal
from ledger.engine.runtime import Ingested,Ingestion,run
from ledger.engine.types import FileRef,Recognition,ANCHOR_SHA,ANCHOR_SHEET,ANCHOR_ROW,ANCHOR_FILE
from ledger.model.schema import Model,Platform,Store,SourceContract,Template,ColumnBinding,Metric,StatementNode
from ledger.automation_cost_actions import Pending
from test_jst_source_members import fixture,prove


def make_item(template,rows,sha):
    frame=pl.from_dicts(rows,infer_schema_length=None,strict=False)
    ref=FileRef(sha,'synthetic.xlsx','证据')
    return Ingested(ref=ref,template=template,frame=frame,rows=frame.height,
        recognition=Recognition(ref=ref,signature=template.signature,header_count=len(frame.columns),
            source_id=template.source,template_id=template.id))


def sample(*,priced_a=False,exclude=None):
    f=fixture(qty='1',bundle=True);proof=prove(f)
    members=proof['members'];anchor=members[0]['source_anchors'][0]
    raw=Template(id='synthetic_native',name='原订单',source='order_detail',match_columns=('synthetic',),
        time_slots={'order_date':'order_time'},bindings=tuple(ColumnBinding(role=k,columns=(k,)) for k in
            ('order_id','sub_order_id','product_id','buyer_paid','order_time','store_name')))
    cost=Template(id='synthetic_cost',name='原成本',source='order_cost',match_columns=('cost',),
        time_slots={'order_date':'order_time'},bindings=tuple(ColumnBinding(role=k,columns=(k,)) for k in
            ('order_id','sub_order_id','original_order_id','internal_order_id','internal_sub_order_id','sku','quantity',
             'unit_cost','order_time','store_name','order_state','order_type','business_kind')))
    metric=Metric(id='goods_cost',name='商品成本',source='order_cost',value={'op':'sum_product','of':['quantity','unit_cost']},
        sign='abs_negate',time_basis='order_date',link={'key':'order_id','to':'order.order_id','grain':'order'},
        where=({'field':'business_kind','op':'eq','value':'allowed'},) if exclude else ())
    model=Model(id='v3-fixture',name='Synthetic physical source fixture',
        platforms=(Platform(id='alibaba1688',name='1688',cost_pricing='historical'),),
        stores=(Store(id='s',name='测试店',platform='alibaba1688'),),
        sources=(SourceContract(id='order_detail',name='原订单',is_spine=True,owner_role='shop_owner',cadence='monthly'),
                 SourceContract(id='order_cost',name='成本',owner_role='warehouse',cadence='monthly')),
        templates=(raw,cost),metrics=(Metric(id='income',name='销售',source='order_detail',
            value={'op':'sum','of':['buyer_paid']},time_basis='order_date',link={'key':'order_id','to':'order.order_id','grain':'order'}),metric),
        statement=(StatementNode(id='net',name='利润',formula={'op':'add','of':['income','goods_cost']}),))
    native={'order_id':'202600001','sub_order_id':'C1','product_id':'P','buyer_paid':20.,
        'order_time':datetime(2026,5,15),'order_date':datetime(2026,5,15),'store_name':'测试店','__hint_store__':'测试店',
        ANCHOR_SHA:'a'*64,ANCHOR_SHEET:'证据',ANCHOR_ROW:3,ANCHOR_FILE:'native.xlsx'}
    row={'order_id':'202600001','sub_order_id':'C1','original_order_id':'202600001',
        'internal_order_id':'100','internal_sub_order_id':'101','sku':'KIT','quantity':1.,
        'unit_cost':None,'order_time':datetime(2026,5,15),'order_date':datetime(2026,5,15),'store_name':'测试店',
        'order_state':'Split','order_type':'普通订单','__hint_store__':'测试店',
        'business_kind':'blocked' if exclude else 'allowed',
        ANCHOR_SHA:anchor['sha256'],ANCHOR_SHEET:anchor['sheet'],ANCHOR_ROW:anchor['row'],ANCHOR_FILE:'cost.xlsx'}
    original=make_item(cost,[row],anchor['sha256'])
    items=[make_item(raw,[native],'a'*64),original]
    if priced_a:
        known={**row,'sku':'A','internal_order_id':'200','internal_sub_order_id':'201','quantity':2.,'unit_cost':1.,
            'cost_source':'history','cost_status':'priced','cost_as_of':'2026-05-15',
            'pricing_evidence':json.dumps({'order_date':'2026-05-15'}),'order_state':'Sent',
            ANCHOR_SHA:'b'*64,ANCHOR_ROW:5}
        items.append(make_item(cost,[known],'b'*64))
    ing=Ingestion(model,items)
    ing.test_source_fixture=f
    ing.source_recovery_originals=(original,)
    baseline=run(ing,'alibaba1688')
    target={'coverage_key':'202600001','order_id':'202600001','sub_order_id':None}
    quotes={(m['company'],m['sku'],m['pricing_day']):{'sku':m['sku'],'company':m['company'],
        'pricing_day':m['pricing_day'],'unit_cost':'1','quote_sha':m['sku']+'-quote'} for m in members}
    return ing,baseline,model.store('s'),target,members,quotes


def cost_sum(result):
    return result.facts.filter(pl.col('metric_id')=='goods_cost').filter(pl.col('counted'))['contribution'].sum()


def test_known_a_two_plus_missing_b_three_is_five_not_seven():
    ing,baseline,store,target,members,quotes=sample(priced_a=True)
    assert cost_sum(baseline)==-2
    made,anchors,physical=posting.render_items(ing,baseline,store,'2026-05',target,members,quotes,fact_id='fact')
    assert len(made)==1 and made[0].frame['sku'].to_list()==['B']
    assert physical=={('200','202')}
    posting.replace_inputs(ing,made,anchors,physical)
    result=run(ing,store.platform)
    assert cost_sum(result)==-5
    assert result.slices[(store.name,'2026-05')].nodes['net'].value==15
    assert sum(i.frame.height for i in ing.items if i.template.source=='order_cost')==2
    assert ing.source_recovery_originals[0].frame['order_state'].to_list()==['Split']


def test_original_metric_where_is_not_bypassed_by_a_proved_source():
    ing,baseline,store,target,members,quotes=sample(exclude=True)
    made,anchors,physical=posting.render_items(ing,baseline,store,'2026-05',target,members,quotes,fact_id='fact')
    posting.replace_inputs(ing,made,anchors,physical)
    result=run(ing,store.platform)
    assert result.facts.filter(pl.col('metric_id')=='goods_cost').height==0
    assert all(i.frame['business_kind'].to_list()==['blocked','blocked'] for i in made)


def test_original_full_container_amount_owns_all_children_and_is_not_replaced():
    ing,baseline,store,target,members,quotes=sample()
    original=ing.items[1]
    original.frame=original.frame.with_columns(pl.lit('Sent').alias('order_state'),pl.lit(5.).alias('unit_cost'),
        pl.lit('history').alias('cost_source'),pl.lit('priced').alias('cost_status'),
        pl.lit('2026-05-15').alias('cost_as_of'),pl.lit(json.dumps({'order_date':'2026-05-15'})).alias('pricing_evidence'))
    ing.source_recovery_originals=(original,)
    baseline=run(ing,store.platform)
    assert cost_sum(baseline)==-5
    made,anchors,physical=posting.render_items(ing,baseline,store,'2026-05',target,members,quotes,fact_id='fact')
    assert made==[] and anchors==set() and physical==set()


def test_unattributed_existing_component_is_pending_instead_of_guessing_delta():
    ing,baseline,store,target,members,quotes=sample(priced_a=True)
    key=(store.name,'2026-05');sl=baseline.slices[key]
    sl.facts=sl.facts.with_columns(pl.when(pl.col('metric_id')=='goods_cost').then(pl.lit('unknown')).otherwise(pl.col('file_sha')).alias('file_sha'))
    with pytest.raises(Pending,match='existing_cost_component_ownership_unverified'):
        posting.render_items(ing,baseline,store,'2026-05',target,members,quotes,fact_id='fact')


def test_retained_quote_is_reparsed_and_semantic_tamper_is_rejected(tmp_path):
    from ledger.upstream_history import HistoryClient
    class Source(HistoryClient):
        def _fetch(self,*_):
            body={'data':[{'sku_id':'A','wms_co_id':'10','begin_date':'2026-05-01','end_date':'2026-05-31','cost_price':'3.335'}]}
            return json.dumps(body).encode(),body['data']
    quote=Source(tmp_path).quote('A','10','2026-05-15')
    ws=SimpleNamespace(root=tmp_path)
    fact={'quotes':[quote],'source_refs':quote['artifact_refs']}
    assert v3.retained_quotes(ws,fact)[('10','A','2026-05-15')]['unit_cost']=='3.335'
    bad=deepcopy(fact);bad['quotes'][0]['unit_cost']='4'
    with pytest.raises(Pending,match='physical_quote_semantics_changed'):v3.retained_quotes(ws,bad)
    path=quote['artifact_refs'][0]['path']
    from pathlib import Path
    Path(path).write_text('{}')
    with pytest.raises(Pending,match='physical_source_artifact_missing_or_changed'):v3.retained_quotes(ws,fact)

def test_adopted_facts_are_stable_when_kill_switch_or_grant_naturally_expires(tmp_path,monkeypatch):
    from ledger.workspace import Workspace
    from ledger import source_recovery
    ing,baseline,store,target,members,quotes=sample(priced_a=True)
    ws=Workspace(tmp_path/'ws');stamp=datetime.now(timezone.utc)
    grant_id='human-grant';key='human-grant-apply'
    # The adoption occurred during its approved interval, but the interval has
    # naturally expired now. Persisted financial evidence must not disappear.
    started=stamp-timedelta(days=2);ended=stamp-timedelta(days=1)
    values={'enabled':True,'rule_id':v3.RULE,'rule_sha':v3.RULE_SHA,'valid_from':started.isoformat(),'valid_until':ended.isoformat(),
        'period_from':'2026-05','period_until':'2026-05'}
    with ws.conn:
        ws.conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            ('human-auth',key,'finance','human','decision.apply','before','proposal','{}',started.isoformat()))
        ws.conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (grant_id,key,'rule_grant','2026-05','s','apply','',journal.encoded(values),'synthetic human grant','before','proposal','finance',started.isoformat()))
    adopted_at=(started+timedelta(hours=1)).isoformat()
    fact={'schema':v3.SCHEMA,'rule_id':v3.RULE,'rule_sha':v3.RULE_SHA,'store_id':'s','period':'2026-05',
        'target':target,'members':members,'source_binding':{},'quotes':list(quotes.values()),'source_refs':[]}
    with ws.conn:
        ws.conn.execute('INSERT INTO automation_authorization VALUES(?,?,?,?,?,?,?,?,?)',
            ('source-auth','adopt','service','delegated_service',v3.RULE,'input',journal.digest(fact),
             journal.encoded({'approved_rule_grant':grant_id,'rule_sha':v3.RULE_SHA}),adopted_at))
        ws.conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('source-fact','adopt',v3.RULE,'2026-05','s','apply','',journal.encoded({'rule_sha':v3.RULE_SHA,'fact':fact}),
             'synthetic adoption','input',journal.digest(fact),'service',adopted_at))
    monkeypatch.setenv('LEDGER_SOURCE_COST_V3_ENABLED','0')
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','0')
    monkeypatch.setattr(source_recovery,'verified_fact_source',lambda *args,**kwargs:{'source_proof':{'schema':'jst_source_members.v1','status':'proved','members':members},
        'plan':{'platform_order_id':'202600001','shop_ids':['77']}})
    monkeypatch.setattr(v3,'retained_quotes',lambda *args:quotes)
    result=v3.consume(ws,ing.model,store,ing,baseline)
    assert cost_sum(result)==-5
    assert ing.physical_cost_evidence[0]['close_quote_keys']==[('10','B','2026-05-15')]
    assert len(v3.active(ws.conn,'s','2026-05'))==1
    before=v3.fingerprint(ws,'s','2026-05')
    with ws.conn:
        ws.conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('grant-revoke','revoke-key','rule_grant','2026-05','s','revoke',grant_id,'{}','explicit withdrawal','now','','finance',stamp.isoformat()))
    assert v3.fingerprint(ws,'s','2026-05')!=before
    fresh_ing,fresh_baseline,*_=sample(priced_a=True)
    result=v3.consume(ws,fresh_ing.model,store,fresh_ing,fresh_baseline)
    assert cost_sum(result)==-2
    assert any(f.check_id=='physical_source_fact_pending' for f in result.slices[(store.name,'2026-05')].audit.findings)
    assert len(v3.active(ws.conn,'s','2026-05'))==1  # Original adoption/audit retained.
    ws.close()


def test_fractional_stock_pieces_keep_original_precision_until_engine_aggregation():
    ing,baseline,store,target,members,quotes=sample()
    for quote in quotes.values():quote['unit_cost']='0.0042'
    made,anchors,physical=posting.render_items(ing,baseline,store,'2026-05',target,members,quotes,fact_id='fact')
    assert sorted(made[0].frame['total_cost'].to_list())==pytest.approx([.0084,.0126])
    posting.replace_inputs(ing,made,anchors,physical);result=run(ing,store.platform)
    assert cost_sum(result)==pytest.approx(-.021)
    assert result.slices[(store.name,'2026-05')].nodes['net'].value==19.98


def test_container_only_actual_return_receipt_is_not_silently_lost():
    ing,baseline,store,target,members,quotes=sample()
    store=store.model_copy(update={'cost_return_posting':'transaction'})
    template=Template(id='synthetic_return',name='原始实际退货',source='after_sales',match_columns=('after-sales',))
    ing.items.append(make_item(template,[{'internal_order_id':'100','sku':'KIT','refund_status':'退款成功',
        'goods_status':'卖家已收到退货','returned_quantity':.5}], 'r'*64))
    with pytest.raises(Pending,match='physical_source_after_sale_identity_unproved'):
        posting.render_items(ing,baseline,store,'2026-05',target,members,quotes,fact_id='fact')


def test_parsed_iso_order_time_does_not_mask_normalized_original_date():
    ing,baseline,store,target,members,quotes=sample(priced_a=True)
    original=ing.source_recovery_originals[0]
    original.frame=original.frame.with_columns(pl.lit('2026-05-15T10:00:00').alias('order_time'))
    made,anchors,physical=posting.render_items(ing,baseline,store,'2026-05',target,members,quotes,fact_id='fact')
    assert made[0].frame['sku'].to_list()==['B']
    assert physical=={('200','202')}
