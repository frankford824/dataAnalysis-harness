
"""Real XLSX -> ingest -> original engine ownership regressions, synthetic amounts."""
from pathlib import Path
import polars as pl
import pytest
from conftest import MODELS,write_xlsx
from ledger.model.loader import load_model
from ledger.model.schema import Metric
from ledger.engine.runtime import ingest,run
from test_after_sales_cost import ORDER_HEADER,COST_HEADER,AFTER_HEADER


def calculate(tmp_path,*,receipt_owner='一店',both=False,unknown_cost=False,ambiguous_file=False,missing_cost_store=False,receipt_file='原始售后单.xlsx'):
    base=load_model(MODELS/'cn-ecommerce')
    store1=base.store('taobao_xibishun').model_copy(update={'name':'一店','aliases':('甲店别名',),'cost_return_posting':'transaction'})
    store2=base.store('taobao_mt9sno8d').model_copy(update={'name':'二店','aliases':('乙店别名',),'cost_return_posting':'transaction'})
    model=base.model_copy(update={'stores':(store1,store2),'platforms':tuple(p.model_copy(update={'cost_pricing':'provided'})
        if p.id=='taobao' else p for p in base.platforms)})
    # A declared synthetic native-income control checks the actual XLSX paid
    # value through the same original native-source linking path. Formal
    # platform cash income remains a separate reconciliation source.
    native_income=Metric(id='native_income_control',name='原始实付控制',source='order_detail',
        value={'op':'sum','of':['buyer_paid']},time_basis='order_date',
        link={'key':'sub_order_id','to':'order.sub_order_id','grain':'order'})
    model=model.model_copy(update={'metrics':(*model.metrics,native_income)})
    paths=[]
    for name,price,product,income in [('一店',4,'P1',100),*([('二店',7,'P2',200)] if both else [])]:
        filename='一店+二店-订单明细.xlsx' if ambiguous_file else name+'-订单明细.xlsx'
        paths.append(write_xlsx(tmp_path/filename,[['说明'],ORDER_HEADER,
            ['S1','O1',income,0,product,'T1','2026-06-01','2026-06-01','']]))
        paths.append(write_xlsx(tmp_path/(name+'-聚水潭成本.xlsx'),[['说明'],COST_HEADER,
            ['I1','O1',None if missing_cost_store else '未配置成本店' if unknown_cost else name,'2026-06-01','Sent','销售订单','S1','O1','SKU1','SKU1',3,price,3*price]]))
    rows=[['A1','I1',receipt_owner,'O1','已确认','退款成功','卖家已收到退货','SKU1','S1',3,3,'2026-07-09']]
    if both:rows.append(['A1','I1','二店','O1','已确认','退款成功','卖家已收到退货','SKU1','S1',3,3,'2026-07-09'])
    paths.append(write_xlsx(tmp_path/receipt_file,[AFTER_HEADER+['实退数量','进仓时间'],*rows]))
    ingestion=ingest(paths,model,['一店','二店'])
    return run(ingestion,'taobao'),model,ingestion


def amounts(result,metric):
    rows=result.facts.filter((pl.col('metric_id')==metric)&pl.col('counted'))
    return {r['store']:r['contribution'] for r in rows.group_by('store').agg(pl.col('contribution').sum()).to_dicts()}


def test_same_store_original_xlsx_return_keeps_income_cost_and_credit(tmp_path):
    result,_,_=calculate(tmp_path)
    assert amounts(result,'native_income_control')=={'一店':100}
    assert amounts(result,'goods_cost')=={'一店':-12}
    assert amounts(result,'goods_return_cost')=={'一店':12}
    assert not result.eval_errors.get('cost_return')


@pytest.mark.parametrize('owner',['二店','未配置外店',None,''])
def test_foreign_unknown_and_missing_receipt_store_never_borrow_same_number_cost(tmp_path,owner):
    result,_,_=calculate(tmp_path,receipt_owner=owner)
    assert amounts(result,'goods_return_cost')=={}
    assert result.eval_errors['cost_return']
    assert any(f.check_id=='cost_return_evidence' and f.blocking for sl in result.slices.values() for f in sl.audit.findings)
    assert all(sl.nodes['net_profit'].value is None for sl in result.slices.values())


def test_confirmed_store_alias_resolves_to_canonical_owner(tmp_path):
    result,_,_=calculate(tmp_path,receipt_owner='甲店别名')
    assert amounts(result,'goods_return_cost')=={'一店':12}
    assert not result.eval_errors.get('cost_return')


def test_unknown_original_cost_owner_is_pending_not_missing_or_reassigned(tmp_path):
    result,_,_=calculate(tmp_path,unknown_cost=True)
    assert amounts(result,'goods_return_cost')=={}
    assert result.eval_errors['cost_return']


def test_same_ids_in_two_stores_keep_each_income_cost_return_and_quantity_cap(tmp_path):
    result,_,_=calculate(tmp_path,both=True)
    assert amounts(result,'native_income_control')=={'一店':100,'二店':200}
    assert amounts(result,'goods_cost')=={'一店':-12,'二店':-21}
    assert amounts(result,'goods_return_cost')=={'一店':12,'二店':21}
    assert not result.eval_errors.get('cost_return')


def test_ambiguous_two_store_filename_cannot_supply_first_matching_owner(tmp_path):
    result,_,_=calculate(tmp_path,ambiguous_file=True)
    assert amounts(result,'native_income_control')=={}
    assert result.eval_errors.get('order_detail')
    assert all(not sl.can_close for sl in result.slices.values())

def test_missing_row_store_can_use_a_unique_proven_file_hint(tmp_path):
    result,_,_=calculate(tmp_path,missing_cost_store=True,receipt_owner=None,receipt_file='一店-售后单.xlsx')
    assert amounts(result,'goods_return_cost')=={'一店':12}
    assert not result.eval_errors.get('cost_return')


def test_explicit_foreign_store_is_not_overridden_by_known_file_hint(tmp_path):
    result,_,_=calculate(tmp_path,receipt_owner='二店',receipt_file='一店-售后单.xlsx')
    assert amounts(result,'goods_return_cost')=={}
    assert result.eval_errors['cost_return']
