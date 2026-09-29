import json
from copy import deepcopy
import hashlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import polars as pl
import pytest
from conftest import MODELS
from ledger.model.loader import load_model
from ledger.engine.normalize import NormalizeError
from ledger.engine.runtime import Ingestion,_assert_reconciled,_promotion_booking_evidence
from ledger.engine.calculate import CalculateError
from ledger.engine.payment_evidence import resolve_supplemental_payments
from test_promotion_dates import ingested
from test_statement_event_dedupe import item,douyin_row


@pytest.mark.parametrize('template_id,amount_col',[
    ('promotion_pdd_v1','总花费(元)'),('promotion_v5','总营销花费(元)'),
    ('promotion_douyin_v1','整体消耗'),('promotion_taobao_v1','花费')])
def test_every_daily_promotion_layout_preserves_row_month(template_id,amount_col):
    template=load_model(MODELS/'cn-ecommerce').template(template_id)
    product='主体ID' if template_id=='promotion_taobao_v1' else '商品ID'
    headers=[product,amount_col,'日期']+(['主体类型'] if template_id=='promotion_taobao_v1' else [])
    rows=[['p',10,'2026-06-01']+(['商品'] if len(headers)==4 else []),
          ['p',20,'2026-07-01']+(['商品'] if len(headers)==4 else [])]
    entry=ingested(template,headers,rows)
    assert entry.frame['source_period'].to_list()==['2026-06','2026-07']


def test_onboarded_template_cannot_silently_discard_date():
    template=load_model(MODELS/'cn-ecommerce').template('promotion_v5')
    broken=template.model_copy(update={'time_slots':{},'bindings':tuple(b for b in template.bindings if b.role!='spend_time')})
    with pytest.raises(NormalizeError,match='未使用推广表'):
        ingested(broken,['商品ID','商品名称','总营销花费(元)','日期'],[['p','商品',10,'2026-06-01']])


@pytest.mark.parametrize('value',['20260723','2026-07-23','20260701~20260731','20260701至20260731'])
def test_jd_daily_and_monthly_range_exports_have_explicit_scope(value):
    template=load_model(MODELS/'cn-ecommerce').template('promotion_jd_v1')
    entry=ingested(template,['跟单SKU ID','跟单SKU名称','花费','日期'],[['p','商品',10,value]])
    assert entry.frame['source_period'][0]=='2026-07'


def test_jd_cross_month_range_requires_monthly_breakdown():
    template=load_model(MODELS/'cn-ecommerce').template('promotion_jd_v1')
    with pytest.raises(NormalizeError,match='跨月'):
        ingested(template,['跟单SKU ID','跟单SKU名称','花费','日期'],[['p','商品',10,'20260601~20260731']])


def test_onboarded_promotion_footer_in_date_column_is_not_a_second_charge():
    template=load_model(MODELS/'cn-ecommerce').template('promotion_v5')
    entry=ingested(template,['商品ID','商品名称','总营销花费(元)','日期'],
                   [['p','商品',10,'2026-07-01'],['-','-',15,'总计']])
    assert entry.frame['product_id'].to_list()==['p','__store_wide__']
    assert entry.frame['promotion_scope'].to_list()==[None,'2026-07']


def test_hosted_rows_without_public_spend_still_constrain_control_months():
    template=load_model(MODELS/'cn-ecommerce').template('promotion_pdd_v1')
    entry=ingested(template,['商品ID','总花费(元)','日期'],
        [['p',10,'2026-06-01'],['hosted','-','2026-07-01'],['-',20,'总计']])
    assert entry.frame.filter(pl.col('product_id')=='__store_wide__')['promotion_scope'].item()=='pending'


def test_equal_totals_cannot_hide_wrong_source_month():
    facts=pl.DataFrame({'store':['s'],'period':['2026-07'],'metric_id':['ad_cost'],
        'source_id':['promotion'],'source_period':['2026-06'],'counted':[True],'contribution':[-10.]})
    projected=facts.with_columns(pl.lit(-10.).alias('amount'))
    with pytest.raises(CalculateError,match='原表发生月份'):_assert_reconciled(facts,projected)


def test_indirect_rows_are_disclosed_without_being_counted_twice(tmp_path):
    from ledger.view import drill,fees_csv
    from test_promotion_dates import calculate
    result=calculate([['P1',10,'2026-07-01'],['P2',20,'2026-07-01'],['-',40,'总计']],platform='pdd')
    sl=result.slice('shop','2026-07')
    facts=sl.facts.filter(pl.col('metric_id')=='ad_cost')
    assert facts['contribution'].sum()==-40.
    assert facts.filter(pl.col('booking_status')=='allocated')['amount'].sum()==-20.
    shown=drill(facts,result.model,'ad',only='allocated')
    assert shown['rows']==1 and shown['allocated']['amount']==-20.
    assert shown['uncounted']['rows']==0
    assert shown['sample'][0]['allocation_control']
    assert '汇总分摊已计入' in fees_csv(facts,result.model)
    path=tmp_path/'archived-facts.parquet'
    facts.write_parquet(path)
    for mode in ('counted','allocated','uncounted','all'):
        assert drill(path,result.model,'ad',only=mode)==drill(facts,result.model,'ad',only=mode)
    assert fees_csv(path,result.model)==fees_csv(facts,result.model)


def payment_fixture(changes=None,original=True,legacy_order=False,competing=False):
    from ledger.engine.runtime import _dedupe_across_files
    from test_reship_period_and_cost_drill import item as order_item
    model=load_model(MODELS/'cn-ecommerce')
    new=douyin_row(payment_title='用户向商家打款',payment_case_id='SS12345678')
    if changes:new.update(changes)
    old=douyin_row(txn_id='2.02608020914033e+27',base_order_id='other-payment-reference')
    entries=[item([old],'old','douyin_settlement_v2'),item([new],'monthly','douyin_settlement_v1')]
    if competing:entries.append(item([douyin_row(txn_id='2026080209140335073439088962',payment_title='普通商品')],'other','douyin_settlement_v1'))
    if original:
        rows=[dict(order_id=douyin_row()['base_order_id'],product_id='p',store_name='shop',order_time=datetime(2026,7,1),buyer_paid=128.2)]
        if legacy_order:rows.append({**rows[0],'order_id':'other-payment-reference'})
        exported=order_item('order_detail',rows,rows[0].keys())
        exported.template=exported.template.model_copy(update={'id':'douyin_order_detail_v1'})
        entries.append(exported)
    ing=Ingestion(model=model,items=entries)
    resolve_supplemental_payments(ing)
    _dedupe_across_files(ing,model)
    return ing


def test_supplemental_payment_requires_original_order_and_case_evidence():
    ing=payment_fixture()
    assert ing.items[0].frame.height==0
    assert ing.items[1].frame.height==1
    assert '补款关联互证' in ing.items[1].frame['source_note'][0]
    assert not ing.validation_errors


@pytest.mark.parametrize('changes',[
    {'payment_title':'普通商品'},{'payment_case_id':None},{'income':129.2},
    {'settle_time':'2026-08-02 09:14:07'},{'__hint_store__':'another'},])
def test_supplemental_payment_does_not_relax_missing_or_conflicting_evidence(changes):
    ing=payment_fixture(changes)
    assert ing.items[0].frame.height==1
    assert ing.validation_errors


@pytest.mark.parametrize('original,legacy_order',[(False,False),(True,True)])
def test_unknown_or_two_real_orders_require_review(original,legacy_order):
    ing=payment_fixture(original=original,legacy_order=legacy_order)
    assert ing.items[0].frame.height==1
    assert ing.validation_errors


def test_scientific_payment_does_not_choose_between_supplement_and_normal_event():
    ing=payment_fixture(competing=True)
    assert ing.items[0].frame.height==1
    assert ing.validation_errors


@pytest.mark.parametrize('code',['promotion_integrity','promotion_scope_evidence'])
def test_promotion_evidence_guard_cannot_be_ignored_on_close(tmp_path,code):
    from ledger.workspace import Workspace,WorkspaceError
    ws=Workspace(tmp_path/'book')
    rid=ws.record('s','2026-07',{'can_close':True,'statement':[],
        'findings':[{'id':code,'passed':False,'blocking':True,'message':'推广证据待核对'}]},[])
    with pytest.raises(WorkspaceError,match='不能人工忽略'):
        ws.close_period('s','2026-07',ignored_blockers=(code,),expected_run_id=rid)


def test_overlapping_daily_promotion_files_are_not_silently_added():
    from ledger.engine.promotion_guard import overlaps
    model=load_model(MODELS/'cn-ecommerce');template=model.template('promotion_v5')
    a=ingested(template,['商品ID','总营销花费(元)','日期'],[['p',10,'2026-07-01']])
    b=deepcopy(a)
    from ledger.engine.types import FileRef
    a.ref=FileRef('a','a.xlsx','sheet');b.ref=FileRef('b','b.xlsx','sheet')
    for entry in [a,b]:entry.frame=entry.frame.with_columns(pl.lit('shop').alias('__hint_store__'))
    ing=Ingestion(model=model,items=[a,b]);overlaps(ing)
    assert 'promotion' in ing.validation_errors
    assert a.frame['source_note'][0].startswith('推广证据待核对：')


def test_same_promotion_content_ignores_only_campaign_deletion_status(tmp_path):
    from conftest import write_xlsx
    from ledger.engine.runtime import ingest
    model=load_model(MODELS/'cn-ecommerce')
    headers=['日期','商品ID','商品名称','总营销花费(元)','是否已删除','推广名称']
    a=write_xlsx(tmp_path/'a.xlsx',[headers,['2026-07-01','p','商品',10,'','计划甲'],['总计','总计','',10,'','']])
    b=write_xlsx(tmp_path/'b.xlsx',[headers,['2026-07-01','p','商品',10,'已删除','计划甲'],['总计','总计','',10,'','']])
    result=ingest([a,b],model,default_store='shop')
    assert not result.validation_errors
    assert result.items[1].frame.is_empty()
    assert result.deduplication[0]['namespace']=='promotion_business_table'
    # A different campaign is not a proven duplicate even at the same amount.
    c=write_xlsx(tmp_path/'c.xlsx',[headers,['2026-07-01','p','商品',10,'已删除','计划乙'],['总计','总计','',10,'','']])
    result=ingest([a,c],model,default_store='shop')
    assert 'promotion' in result.validation_errors


def test_promotion_labelled_order_table_is_not_zero_spend_evidence():
    from ledger.engine.promotion_guard import overlaps
    from ledger.engine.types import FileRef
    model=load_model(MODELS/'cn-ecommerce')
    entry=item([douyin_row()])
    entry.ref=FileRef(entry.ref.sha256,'推广-错放.xlsx','sheet')
    ing=Ingestion(model=model,items=[entry]);overlaps(ing)
    assert '上传错表' in ing.validation_errors['promotion'][0]


def test_failed_promotion_parse_is_a_financial_integrity_error(tmp_path):
    from conftest import write_xlsx
    from ledger.engine.runtime import ingest
    model=load_model(MODELS/'cn-ecommerce')
    original=model.template('promotion_v5')
    broken=original.model_copy(update={'time_slots':{},'bindings':tuple(b for b in original.bindings if b.role!='spend_time')})
    model=model.model_copy(update={'templates':tuple(broken if t.id==broken.id else t for t in model.templates)})
    file=write_xlsx(tmp_path/'promotion.xlsx',[['商品ID','商品名称','总营销花费(元)','日期'],['p','商品',10,'2026-07-01']])
    ing=ingest([file],model,default_store='shop')
    assert not ing.items[0].ok
    assert ing.validation_errors['promotion'][0].startswith('推广证据待核对：')
