from datetime import date
import polars as pl
import pytest
from conftest import MODELS, write_xlsx
from ledger.model.loader import load_model
from ledger.engine.runtime import ingest, run
from test_after_sales_cost import ORDER_HEADER, COST_HEADER, AFTER_HEADER


def calculate(tmp_path, returns, *, quantity=3, unit=4, policy="transaction"):
    model=load_model(MODELS/"cn-ecommerce")
    stores=tuple(s.model_copy(update={"cost_return_posting":policy}) if s.id=="taobao_xibishun" else s for s in model.stores)
    model=model.model_copy(update={"stores":stores})
    orders=write_xlsx(tmp_path/"淘宝喜必顺-订单明细.xlsx",[["说明"],ORDER_HEADER,
        ["S1","O1",100,0,"P1","T1","2026-06-01","2026-06-01",""]])
    costs=write_xlsx(tmp_path/"淘宝喜必顺-聚水潭成本.xlsx",[["说明"],COST_HEADER,
        ["I1","O1","淘宝喜必顺","2026-06-01","Sent","销售订单","S1","O1","SKU1","SKU1",quantity,unit,quantity*unit]])
    after=write_xlsx(tmp_path/"售后单_月份.xlsx",[AFTER_HEADER+["实退数量","进仓时间"],*[
        [event,"I1","淘宝喜必顺","O1","已确认","退款成功",goods,"SKU1","S1",requested,actual,when]
        for event,goods,requested,actual,when in returns]])
    return run(ingest([orders,costs,after],model,["淘宝喜必顺"]),"taobao")


def totals(result, metric):
    return {r["period"]:r["amount"] for r in result.spine_facts.filter(pl.col("metric_id")==metric).group_by("period").agg(pl.col("amount").sum()).to_dicts()}


def test_july_return_keeps_june_sale_and_reverses_original_cost_in_july(tmp_path):
    r=calculate(tmp_path,[("A1","卖家已收到退货",3,3,"2026/7/9 13:36:58")])
    assert totals(r,"goods_cost")=={"2026-06":-12}
    assert totals(r,"goods_return_cost")=={"2026-07":12}
    source=r.facts.filter(pl.col("metric_id")=="goods_return_cost")
    assert source["contribution"].sum()==12
    assert source["order_id"].to_list()==["O1"]
    assert source["file_name"].to_list()==["售后单_月份.xlsx"]
    assert source["row_no"].to_list()==[2]


def test_partial_and_repeated_returns_are_capped_at_original_quantity(tmp_path):
    r=calculate(tmp_path,[("A1","卖家已收到退货",3,1,"2026-07-09"),
                         ("A2","卖家已收到退货",3,3,"2026-08-02"),
                         ("A3","卖家已收到退货",3,3,"2026-08-03")])
    assert totals(r,"goods_return_cost")=={"2026-07":4,"2026-08":8}
    assert sum(totals(r,"goods_cost").values())+sum(totals(r,"goods_return_cost").values())==0


def test_requested_quantity_does_not_replace_actual_return_quantity(tmp_path):
    r=calculate(tmp_path,[("A1","卖家已收到退货",3,1,"2026-07-09")])
    assert totals(r,"goods_return_cost")=={"2026-07":4}


@pytest.mark.parametrize("when",[None,"", "not-a-date", "2026-05-01"])
def test_missing_invalid_or_pre_sale_receipt_date_never_uses_order_month(tmp_path,when):
    r=calculate(tmp_path,[("A1","卖家已收到退货",3,3,when)])
    assert totals(r,"goods_cost")=={"2026-06":-12}
    assert totals(r,"goods_return_cost")=={}
    assert r.eval_errors["cost_return"]


def test_refund_without_actual_return_does_not_reverse_cost(tmp_path):
    r=calculate(tmp_path,[("A1","买家已收到货",3,0,None)])
    assert totals(r,"goods_cost")=={"2026-06":-12}
    assert totals(r,"goods_return_cost")=={}


def test_other_store_keeps_existing_policy(tmp_path):
    r=calculate(tmp_path,[("A1","卖家已收到退货",3,3,"2026-07-09")],policy="order")
    assert totals(r,"goods_cost")=={}
    assert totals(r,"goods_return_cost")=={}


def test_conflicting_receipts_are_not_partially_posted(tmp_path):
    r=calculate(tmp_path,[("A1","卖家已收到退货",3,1,"2026-07-09"),
                         ("A1","卖家已收到退货",3,2,"2026-07-09")])
    assert totals(r,"goods_return_cost")=={}
    assert r.eval_errors["cost_return"]
    assert any(f.check_id=="cost_return_evidence" and f.blocking for sl in r.slices.values() for f in sl.audit.findings)


def test_export_has_actual_date_quantity_and_original_unit_cost(tmp_path):
    import csv,io
    from ledger.view import fees_csv
    r=calculate(tmp_path,[("A1","卖家已收到退货",3,1,"2026-07-09")])
    rows=list(csv.DictReader(io.StringIO(fees_csv(r.facts,r.model))))
    row=next(row for row in rows if row['科目']=='退货成本冲回')
    assert row['进账']=='4.0000'
    assert '2026-07-09' in row['计算说明']
    assert '本次冲回数量：1' in row['计算说明']
    assert '原成本单价：4' in row['计算说明']
