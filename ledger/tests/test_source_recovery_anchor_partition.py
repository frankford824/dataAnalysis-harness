"""Ordinary goods recovery must not mix a replacement shipment's source root."""
from types import SimpleNamespace
import polars as pl
import pytest
from ledger.source_recovery import anchor_index


def indexed(types):
    store=SimpleNamespace(id="s",name="测试店",aliases=())
    model=SimpleNamespace(sources=[SimpleNamespace(id="order_cost",is_spine=False,platform="*")],
                          metrics=[SimpleNamespace(id="goods_cost",source="order_cost")])
    rows=[{"original_order_id":"202600001","internal_order_id":str(100+n),
           "store_name":"测试店","order_type":kind,"__sheet__":"原件","__row__":3+n}
          for n,kind in enumerate(types)]
    item=SimpleNamespace(ref=SimpleNamespace(sha256="a"*64),
        template=SimpleNamespace(source="order_cost"),frame=pl.from_dicts(rows))
    ingestion=SimpleNamespace(model=model,known=[item])
    coverage=pl.DataFrame({"order_id":["202600001"],"period":["2026-05"]})
    return anchor_index(ingestion,store,"2026-05",None,coverage)["orders"]["202600001"]


def test_replacement_root_is_separate_evidence_and_does_not_ambiguate_ordinary_root():
    result=indexed(["普通订单","补发订单"])
    assert result["internal_order_ids"]==["100"]
    assert result["cost"]==[{"sha256":"a"*64,"sheet":"原件","row":3}]
    assert result["separate_cost_anchors"]==[{"sha256":"a"*64,"sheet":"原件","row":4,
                                              "reason":"reshipment_cost_separate"}]


@pytest.mark.parametrize("kind",[None,"","未知类型","补发"])
def test_unknown_type_cannot_be_removed_as_if_proved_replacement(kind):
    result=indexed(["普通订单",kind])
    assert result["internal_order_ids"]==["100","101"]
    assert len(result["cost"])==2 and result["separate_cost_anchors"]==[]


def test_replacement_only_source_never_certifies_ordinary_goods_cost():
    result=indexed(["补发订单"])
    assert result["cost"]==[] and result["internal_order_ids"]==[]
    assert len(result["separate_cost_anchors"])==1
