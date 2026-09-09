from types import SimpleNamespace
import polars as pl
import pytest
from ledger.engine.preship_refunds import apply


def fixture(*,qty=2,ref_qty=1,when="2026/6/2 10:30:00",ship="2026-06-02 12:00:00",internal="PARENT",status="买家未收到货",kind="销售订单"):
    cost=SimpleNamespace(frame=pl.DataFrame([dict(internal_order_id="CHILD",parent_internal_order_id="PARENT",order_id="MAIN",sub_order_id="SUB",sku="SKU",ship_time=ship,quantity=float(qty),unit_cost=3.5,total_cost=qty*3.5,order_type=kind)]),notes=[])
    after=SimpleNamespace(frame=pl.DataFrame([dict(after_sale_id="AF",internal_order_id=internal,order_id="MAIN",sub_order_id="SUB",sku="SKU",quantity=float(ref_qty),refund_status="退款成功",goods_status=status,refund_confirm_time=when)]),template=SimpleNamespace(id="upload"))
    return cost,after


def test_parent_refund_reduces_only_cancelled_quantity_and_is_idempotent():
    cost,after=fixture()
    changes=apply(cost,[after])
    assert cost.frame['quantity'].to_list()==[1]
    assert cost.frame['total_cost'].to_list()==[3.5]
    assert changes[0]['removed_cost']=='3.50'
    assert after.frame['__preship_handled'].to_list()==[True]
    assert after.frame['__preship_skip_exclusion'].to_list()==[False]
    apply(cost,[after])
    assert cost.frame['quantity'].to_list()==[1]


def test_full_refund_removes_cost_without_exceeding_original_quantity():
    cost,after=fixture(ref_qty=10)
    changes=apply(cost,[after])
    assert cost.frame['quantity'].to_list()==[0]
    assert float(changes[0]['removed_cost'])==7


@pytest.mark.parametrize('kwargs',[{'when':'2026-06-02 12:00:00'},{'when':'2026-06-02 12:01:00'}, {'when':'2026-06-02'}, {'when':None}, {'ship':None}, {'status':'买家已收到货'}, {'internal':'UNRELATED'}, {'kind':'补发订单'}, {'ref_qty':0}])
def test_unproven_postship_or_reship_cost_is_preserved(kwargs):
    cost,after=fixture(**kwargs)
    before=cost.frame.clone()
    assert apply(cost,[after])==[]
    assert cost.frame.equals(before)


def test_ambiguous_cost_lines_are_not_spread_or_all_removed():
    cost,after=fixture()
    cost.frame=pl.concat([cost.frame,cost.frame])
    assert apply(cost,[after])==[]
    assert cost.frame['quantity'].sum()==4


def test_repeated_identical_source_does_not_cancel_twice():
    cost,after=fixture()
    second=SimpleNamespace(frame=after.frame.clone(),template=SimpleNamespace(id='upload-copy'))
    apply(cost,[after,second])
    assert cost.frame['quantity'].item()==1
    assert second.frame['__preship_handled'].item()


def test_partial_refund_is_not_zeroed_again_by_existing_exclusion():
    from conftest import MODELS
    from ledger.model.loader import load_model
    model=load_model(MODELS/"cn-ecommerce")
    from ledger.engine.runtime import Ingestion,Ingested,_exclude_linked
    from ledger.engine.types import FileRef,Recognition
    from ledger.order_feed import OrderFeed
    cost,after=fixture(internal='CHILD')
    apply(cost,[after])
    ref=FileRef('test','after.xlsx','Sheet1');template=OrderFeed._after_template()
    item=Ingested(ref=ref,frame=after.frame,template=template,recognition=Recognition(ref,template.signature,len(after.frame.columns),source_id='after_sales',template_id=template.id))
    ing=Ingestion(model=model,items=[item])
    retained=_exclude_linked(cost.frame,model.metric('goods_cost'),ing,[],'test')
    assert retained['quantity'].sum()==1


def test_split_cancellation_does_not_restore_original_parent_cost():
    from conftest import MODELS
    from ledger.model.loader import load_model
    from ledger.engine.runtime import Ingestion,Ingested,_exclude_linked
    from ledger.engine.types import FileRef,Recognition
    from ledger.order_feed import OrderFeed
    cost,after=fixture(ref_qty=2)
    apply(cost,[after])
    model=load_model(MODELS/'cn-ecommerce')
    ref=FileRef('test','after.xlsx','Sheet1');template=OrderFeed._after_template()
    item=Ingested(ref=ref,frame=after.frame,template=template,recognition=Recognition(ref,template.signature,len(after.frame.columns),source_id='after_sales',template_id=template.id))
    ing=Ingestion(model=model,items=[item])
    parent=cost.frame.with_columns(pl.lit('PARENT').alias('internal_order_id'),pl.lit(2.0).alias('quantity'))
    assert _exclude_linked(parent,model.metric('goods_cost'),ing,[],'parent').height==0


def test_same_sku_on_another_platform_child_keeps_its_cost():
    cost,after=fixture(ref_qty=2)
    other=cost.frame.with_columns(pl.lit('OTHER-SUB').alias('sub_order_id'))
    cost.frame=pl.concat([cost.frame,other])
    apply(cost,[after])
    assert cost.frame['quantity'].to_list()==[0,2]
    assert cost.frame['total_cost'].to_list()==[0,7]