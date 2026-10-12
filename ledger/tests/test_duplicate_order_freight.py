"""Overlapping platform exports must not multiply a real freight charge."""
import polars as pl
import pytest
from conftest import MODELS,write_xlsx
from ledger.model.loader import load_model
from ledger.engine.runtime import ingest,_build_spine,_project_scoped_live,_mark_counted
from ledger.engine.link import Spine
from ledger.engine.project import project
from ledger.engine.order_identity import canonical_exports


def orders(tmp_path,conflict=False):
    model=load_model(MODELS/'cn-ecommerce');store=model.store('douyin_luckywish')
    header=['主订单编号','子订单编号','商品ID','选购商品','订单应付金额','订单提交时间','商品数量']
    a=['A','A1','P1','商品1',10,'2026-07-10 12:00:00',1]
    b=['B','B1','P2','商品2',20,'2026-08-10 12:00:00',1]
    c=['C','C1','P3','商品3',30,'2026-06-10 12:00:00',1]
    paths=[write_xlsx(tmp_path/'6-8月订单明细.xlsx',[header,a,b,c]),
           write_xlsx(tmp_path/'7月订单明细.xlsx',[header,[*a[:4],11 if conflict else 10,*a[5:]]])]
    original=[p.read_bytes() for p in paths]
    ing=ingest(paths,model,default_store=store.name)
    spine=_build_spine(ing,[],{store.name:store.name})
    assert [p.read_bytes() for p in paths]==original
    assert sum(i.frame.height for i in ing.frames_of('order_detail'))==4
    return model,store,ing,spine


def source(store):
    return pl.DataFrame({'metric_id':['freight_cost']*3,'source_id':['freight']*3,
        'store':[store.name]*3,'period':['2026-07','2026-07','2026-08'],
        'link_key':['A1','A1','B1'],'amount':[-109.2,-12.,-8.],'major':[None]*3})


def test_overlapping_exports_keep_months_and_real_multiple_charges(tmp_path):
    m,store,ing,spine=orders(tmp_path)
    assert spine.frame.height==3
    assert ing.deduplication[-1]['events']==1
    metric=m.metric('freight_cost').for_platform('douyin')
    facts=source(store)
    projected=_project_scoped_live(facts,metric,spine)
    assert not projected.allocation_pending
    assert projected.facts['amount'].sum()==pytest.approx(-129.2)
    marked=_mark_counted(facts,projected.facts,[metric])
    assert marked.height==3
    assert marked['contribution'].to_list()==[-109.2,-12.,-8.]
    assert marked['counted'].all()
    assert set(projected.facts['allocation_method'])=={'direct_single_target'}


def test_conflicting_export_is_visible_and_money_is_retained_once(tmp_path):
    m,store,ing,spine=orders(tmp_path,conflict=True)
    assert spine.frame.height==4
    metric=m.metric('freight_cost').for_platform('douyin')
    facts=source(store)
    projected=_project_scoped_live(facts,metric,spine)
    assert projected.allocation_pending[0]['reason']=='direct_order_identity_conflict'
    july=projected.facts.filter(pl.col('period')=='2026-07')
    assert july['spine_row'].to_list()==[None]
    assert july['amount'].sum()==pytest.approx(-121.2)
    marked=_mark_counted(facts,projected.facts,[metric])
    assert marked['contribution'].to_list()==[-109.2,-12.,-8.]


@pytest.mark.parametrize('change',['erp','missing_child','missing_product','master_only','different_quantity','different_store','different_month'])
def test_canonicalization_never_drops_unproven_or_different_orders(change):
    rows=[dict(store='s',period='2026-07',order_id='A',sub_order_id='A1',product_id='P',quantity=1.,__spine_origin__='order_detail_file')]*2
    frame=pl.DataFrame(rows)
    if change=='erp':frame=frame.with_columns(pl.lit('order_console').alias('__spine_origin__'))
    if change=='missing_child':frame=frame.with_columns(pl.lit(None,dtype=pl.String).alias('sub_order_id'))
    if change=='missing_product':frame=frame.with_columns(pl.lit('').alias('product_id'))
    if change=='master_only':frame=frame.drop('sub_order_id')
    if change=='different_quantity':frame=frame.with_columns(pl.Series('quantity',[1.,2.]))
    if change=='different_store':frame=frame.with_columns(pl.Series('store',['s','other']))
    if change=='different_month':frame=frame.with_columns(pl.Series('period',['2026-07','2026-08']))
    out,n=canonical_exports(frame)
    assert n==0 and out.equals(frame)


@pytest.mark.parametrize('field,values',[('store',['s','other']),('period',['2026-07','2026-08'])])
def test_direct_conflict_does_not_choose_an_arbitrary_store_or_month(field,values):
    from test_allocation import _metric,_facts
    frame=pl.DataFrame({'order_id':['A','A'],'store':['s','s'],'period':['2026-07']*2}).with_columns(pl.Series(field,values))
    with pytest.raises(ValueError,match='跨店或跨期'):
        project(_facts([('A',-10.)]),_metric(None),Spine(frame))


def test_final_guard_rejects_duplicate_internal_row_identity():
    from test_allocation import _metric,_facts
    frame=pl.DataFrame({'order_id':['A','B'],'store':['s','s'],'period':['2026-07']*2,'spine_row':[0,0]})
    with pytest.raises(ValueError,match='重复关联'):
        project(_facts([('A',-10.),('B',-20.)]),_metric(None),Spine(frame))
