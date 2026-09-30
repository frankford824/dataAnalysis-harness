import csv,io
from decimal import Decimal
import polars as pl
import pytest
from ledger.fee_display import describe,SOURCE_AMOUNT,BOOKED_AMOUNT,IDENTIFIER
from ledger import view
from conftest import MODELS
from ledger.model.loader import load_model


@pytest.mark.parametrize('route,counted,linked,status',[
    ('direct',True,True,'本行已计入'),
    ('direct',True,False,'本行已计入'),
    ('store_wide',True,False,'全店分摊已计入'),
    ('allocated',False,False,'汇总分摊已计入'),
    ('allocated',False,True,'汇总分摊已计入'),
    ('unposted',False,True,'本行未计入'),
    ('unposted',False,False,'本行未计入'),
])
def test_status_explains_accounting_not_link_boolean(route,counted,linked,status):
    row=dict(booking_status=route,counted=counted,linked=linked,contribution=-10 if counted else 0)
    out=describe(row)
    assert out['accounting_status']==status
    assert out['order_match_status'] not in ('是','否')
    if route=='allocated':assert '不是漏记' in out['accounting_explanation']


def test_old_missing_markers_and_legitimate_zero_are_distinct():
    assert describe({'amount':10})['accounting_status'].startswith('待核对')
    assert describe({'counted':True,'contribution':10},graded=False)['accounting_status'].startswith('待核对')
    assert describe({'counted':True,'contribution':None})['accounting_status'].startswith('待核对')
    assert describe({'counted':True,'contribution':0})['accounting_status']=='本行已计入（金额为0）'


def test_rule_zero_cost_is_explicit_without_certifying_missing_evidence():
    row = {'metric_id':'goods_cost','counted':True,'contribution':0,
           'source_note':'蓝色旗帜且卖家备注含 by，按刷单规则不计商品成本'}
    assert describe(row)['accounting_status'] == '刷单规则确认零成本'
    assert '无需补录' in describe(row)['accounting_hint']
    assert describe(row, graded=False)['accounting_status'].startswith('待核对')
    assert describe({**row, 'contribution':None})['accounting_status'].startswith('待核对')


def test_export_and_drill_share_labels_and_keep_money_unchanged(tmp_path):
    m=load_model(MODELS/'cn-ecommerce')
    rows=[]
    for i,(key,amount,contribution,counted,linked,status) in enumerate([
        ('P',-300.,-300.,True,True,'direct'),
        ('__store_wide__',-1000.,-700.,True,False,'direct'),
        ('U',-50.,0.,False,False,'allocated'),
        ('V',-10.,0.,False,True,'unposted'),
        ('X',-20.,-20.,True,False,'store_wide'),
    ]):
        rows.append(dict(metric_id='ad_cost',major=None,minor=None,subject=None,link_key=key,amount=amount,
            contribution=contribution,counted=counted,linked=linked,booking_status=status,file_name='source.xlsx',file_sha='a',sheet='s',row_no=i+2,
            source_note='原始证据说明',allocation_control='source.xlsx 第9行',source_period='2026-07'))
    facts=pl.DataFrame(rows);original=facts.clone()
    path=tmp_path/'facts.parquet';facts.write_parquet(path)
    exported=list(csv.DictReader(io.StringIO(view.fees_csv(path,m))))
    assert not {'是否进账','已挂钩','进账','金额','入账路径'} & set(exported[0])
    assert sum(Decimal(r[BOOKED_AMOUNT]) for r in exported)==-1020
    indirect=next(r for r in exported if r[IDENTIFIER]=='U')
    assert indirect['核算状态']=='汇总分摊已计入' and Decimal(indirect[BOOKED_AMOUNT])==0
    assert '不是漏记' in indirect['核算说明'] and Decimal(indirect[SOURCE_AMOUNT])==-50
    assert any(r[IDENTIFIER]=='全店分摊汇总（非订单号）' for r in exported)
    for mode in ['counted','uncounted','allocated','all']:
        d=view.drill(path,m,'n_ad',only=mode)
        for r in d['sample']:
            e=next(x for x in exported if x['原表行号']==str(r['row_no']))
            assert r['accounting_status']==e['核算状态']
            assert r['accounting_explanation']==e['核算说明']
    assert facts.equals(original) and pl.read_parquet(path).equals(original)


def test_export_empty_header_and_missing_amount_do_not_invent_zero():
    m=load_model(MODELS/'cn-ecommerce')
    assert '核算状态' in view.fees_csv(pl.DataFrame(),m).splitlines()[0]
    frame=pl.DataFrame({'metric_id':['ad_cost'],'amount':[-5.],'link_key':['P']})
    row=next(csv.DictReader(io.StringIO(view.fees_csv(frame,m))))
    assert row[BOOKED_AMOUNT]=='' and row['核算状态'].startswith('待核对')
