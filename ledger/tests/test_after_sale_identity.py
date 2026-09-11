"""ERP item IDs bridge SKU renames without merging bundled products."""
import polars as pl
import pytest
from ledger.engine.runtime import Ingested, run
from ledger.engine.types import FileRef, Recognition
from ledger.order_feed import OrderFeed
from test_order_feed import _fixture, FakeClient, _model_and_store, _uploaded_after_sales


def original_cost(ingestion, rows):
    ref = FileRef("old-cost", "原始成本.xlsx", "Sheet1")
    frame = pl.DataFrame(rows, schema=["internal_order_id", "internal_sub_order_id", "sub_order_id", "sku"], orient="row")
    item = Ingested(ref=ref, recognition=Recognition(ref=ref, signature="old-cost", header_count=4, source_id="order_cost", template_id="old-cost"),
                    frame=frame, template=OrderFeed._cost_template(), rows=frame.height)
    ingestion.items.append(item)
    return item


def test_sku_rename_matches_exact_internal_item_and_keeps_other_product_cost(tmp_path):
    root = tmp_path / "feed"
    manifest = _fixture(root, after_sku=None, second_unnamed=True)
    feed = OrderFeed(tmp_path / "ws", client=FakeClient(manifest), feed_root=root)
    feed.sync()
    model, store = _model_and_store(feed)
    ingestion = _uploaded_after_sales(tmp_path, model, store, ("S1", "OLD-CODE"))
    original = original_cost(ingestion, [("1", "11", "S1", "OLD-CODE")])
    feed.append_to(ingestion, store)
    after = ingestion.frames_of("after_sales")[0].frame
    assert after["sku"].to_list() == ["SKU1"]
    assert after["original_sku"].to_list() == ["OLD-CODE"]
    assert original.frame["sku"].to_list() == ["OLD-CODE"]
    result = run(ingestion, store.platform)
    goods = result.facts.filter(pl.col("metric_id") == "goods_cost")
    assert goods.filter(pl.col("amount") != 0)["sku"].to_list() == ["SKU2"]
    assert goods.filter(pl.col("sku") == "SKU1")["amount"].item() == 0
    assert goods["amount"].sum() == -2


@pytest.mark.parametrize("wrong", [("2","11","S1","OLD"), ("1","12","S1","OLD"), ("1","11","OTHER","OLD")])
def test_order_item_or_platform_child_mismatch_cannot_rename_sku(tmp_path, wrong):
    root=tmp_path/"feed";manifest=_fixture(root,after_sku=None)
    feed=OrderFeed(tmp_path/"ws",client=FakeClient(manifest),feed_root=root);feed.sync()
    model,store=_model_and_store(feed)
    ing=_uploaded_after_sales(tmp_path,model,store,("S1","OLD"))
    original_cost(ing,[wrong]);feed.append_to(ing,store)
    assert ing.frames_of("after_sales")[0].frame["sku"].to_list()==["OLD"]
    assert run(ing,store.platform).facts.filter(pl.col("metric_id")=="goods_cost")["amount"].sum()==-7


def test_ambiguous_alias_is_not_applied(tmp_path):
    root=tmp_path/"feed";manifest=_fixture(root,after_sku=None)
    feed=OrderFeed(tmp_path/"ws",client=FakeClient(manifest),feed_root=root);feed.sync()
    model,store=_model_and_store(feed)
    ing=_uploaded_after_sales(tmp_path,model,store,("S1","OLD"))
    original_cost(ing,[("1","11","S1","OLD"),("1","12","S1","OLD")])
    costs=pl.DataFrame({"internal_order_id":["1","1"],"internal_sub_order_id":["11","12"],"sub_order_id":["S1","S1"],"sku":["SKU1","SKU2"]})
    OrderFeed._align_after_sale_skus(ing,costs)
    assert ing.frames_of("after_sales")[0].frame["sku"].to_list()==["OLD"]
