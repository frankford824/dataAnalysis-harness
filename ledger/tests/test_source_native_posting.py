"""Native source and actual full histories feed the original engine separately."""
from copy import deepcopy
from types import SimpleNamespace
import json
import polars as pl
import pytest
from ledger.source_native_posting import render_native_history_items
from ledger.source_cost_posting import replace_inputs
from ledger.automation_cost_actions import Pending
from ledger.engine.runtime import run
from ledger.upstream_history import HistoryUnavailable
from test_source_native_members import sample
from test_source_cost_posting import sample as cost_case,cost_sum
from test_full_history_intervals import source,period as history_period


def fixture():
    ing,_,store,target,_,_=cost_case()
    platforms=tuple(p.model_copy(update={"id":"taobao","cost_pricing":"historical"}) for p in ing.model.platforms)
    stores=tuple(s.model_copy(update={"platform":"taobao"}) for s in ing.model.stores)
    metrics=tuple(m.model_copy(update={"link":m.link.model_copy(update={"key":"sub_order_id","to":"order.sub_order_id"})})
                  if m.id=="goods_cost" else m for m in ing.model.metrics)
    ing.model=ing.model.model_copy(update={"platforms":platforms,"stores":stores,"metrics":metrics})
    ing.items=ing.items[:1];ing.source_recovery_originals=tuple(ing.known)
    store=ing.model.store("s");target={"order_id":"202600001","coverage_key":"C1","sub_order_id":"C1"}
    f=sample();history={("10","A","2026-05-15"):source([history_period("3.335")])}
    return ing,run(ing,store.platform),store,target,f,history


def render(c,**changes):
    ing,baseline,store,target,f,history=c
    return render_native_history_items(ing,baseline,store,"2026-05",target,*f[:3],history,input_version=changes.get("input_version","native-isolated-v1"))


def test_native_proof_and_actual_full_history_feed_original_engine():
    c=fixture();ing,baseline,store,target,f,history=c
    made,anchors,physical=render(c)
    assert anchors==set() and physical=={("200","201")}
    assert made[0].template.id=="native_source_history_preview_v1"
    assert made[0].ref.filename.endswith(".html") and made[0].ref.sheet=="/items"
    assert made[0].ref.sha256==f[1].nodes[1].sha256
    evidence=json.loads(made[0].frame["pricing_evidence"].item())
    assert evidence["quote"]["schema"]=="ledger-openweb-full-history-quote.v1"
    assert "as_of" not in evidence["quote"]["source_request"]
    assert "artifact_refs" not in evidence["quote"]
    assert evidence["quote"]["financial_authority"] is None
    replace_inputs(ing,made,anchors,physical)
    actual=run(ing,store.platform)
    assert cost_sum(actual)==-4.16875
    assert cost_sum(baseline)==0


def test_missing_full_history_is_pending_without_current_price_fallback():
    c=list(fixture());c[-1]={}
    with pytest.raises(Pending,match="native_full_history_source_missing"):render(c)


def test_full_history_must_cover_the_native_original_day():
    c=list(fixture());c[-1]={("10","A","2026-05-15"):source([history_period("3.335",begin="2026-06-01")])}
    with pytest.raises(HistoryUnavailable,match="not_covering_original_day"):render(c)


def test_existing_nonzero_cost_authority_cannot_be_overwritten():
    c=list(fixture());ing,baseline,store,_,_,_=c
    financial=baseline.slices[(store.name,"2026-05")]
    financial.facts=pl.DataFrame({"metric_id":["goods_cost"],"counted":[True],"link_key":["C1"],"contribution":[-1.]})
    with pytest.raises(Pending,match="native_existing_cost_authority_preserved"):render(c)


def test_other_target_or_unversioned_preview_is_refused():
    c=list(fixture());c[3]={**c[3],"coverage_key":"OTHER"}
    with pytest.raises(Pending,match="native_preview_target_identity_conflict"):render(c)
    with pytest.raises(Pending,match="native_preview_input_version_required"):render(fixture(),input_version="")
