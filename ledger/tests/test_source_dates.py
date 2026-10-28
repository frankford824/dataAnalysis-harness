"""Date-only evidence is distinct from shipment, quantity and monetary proof."""
from copy import deepcopy
from datetime import datetime
import hashlib
from types import SimpleNamespace
import pytest
import polars as pl
from conftest import MODELS
from ledger.jst_source_members import Node,MemberPending
from ledger.source_dates import prove_original_day,input_csv,attach_input
from ledger.engine.runtime import Ingestion
from ledger.engine.refund_dates import lookup
from ledger.model.loader import load_model
from test_jst_source_members import order,item,html

def prove(*values):
    blobs={hashlib.sha256(html(v)).hexdigest():html(v) for v in values}
    nodes=[Node(str(v['o_id']),hashlib.sha256(html(v)).hexdigest()) for v in values]
    return prove_original_day('202600001',['77'],nodes,blobs)

def test_later_merged_original_still_supplies_its_exact_date():
    source=order('100',[item()],status='Merged',is_merge=True,merge_so_id='202600001')
    result=prove(source)
    assert result['status']=='proved' and result['original_day']=='2026-05-15'
    assert result['candidates'][0]['source_is_merge'] is True
    assert result['quantity_or_cost_admitted'] is False

def test_multi_parent_convergence_date_is_never_broadcast():
    merged=order('200',[item('200','201')],so_id='77:202600001,77:202600002',is_merge=True,
                 order_date='2026-06-01 00:00:00')
    original=order('100',[item()],status='Merged',is_merge=False)
    result=prove(merged,original)
    assert result['status']=='proved' and result['original_day']=='2026-05-15'
    assert result['source_order_ids']==['100']

@pytest.mark.parametrize('changes',[
    {'type':'补发订单'}, {'shop_id':'78'}, {'merge_so_id':'KV:m_so_id.123'},
    {'order_date':None},{'order_date':'unknown'},
])
def test_unknown_or_non_original_headers_never_invent_dates(changes):
    assert prove(order('100',[item()],**changes))['status']=='pending'

def test_conflicting_original_days_remain_pending_even_in_same_month():
    assert prove(order('100',[item()]),order('200',[item('200','201')],order_date='2026-05-16 10:00:00'))['status']=='pending'

def test_unknown_cost_status_does_not_discard_independent_original_date():
    raw=order('100',[item(item_status='SentCancelled')],status='Split',is_merge=False)
    result=prove(raw)
    assert result['status']=='proved' and result['quantity_or_cost_admitted'] is False

def test_date_input_uses_existing_importer_without_creating_orders_or_costs(tmp_path):
    model=load_model(MODELS/'cn-ecommerce');store=model.store('taobao_mt9s7n9x')
    proof=prove(order('100',[item()],status='Merged'))
    content=input_csv(store.name,[proof,proof]);path=tmp_path/'verified-date.csv';path.write_bytes(content)
    assert input_csv(store.name,[proof])==content
    ingestion=Ingestion(model=model,items=[]);ref=attach_input(ingestion,path,model,store)
    assert ref.template.source=='refund_order_dates' and ref.frame.height==1
    references=lookup(ingestion.items,{store.name:store.name})
    assert references[store.name,'202600001'][0]=='2026-05'
    assert not any(r.template.source in {'order_detail','order_cost'} for r in ingestion.known)
