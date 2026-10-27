"""Synthetic boundaries for audited production compatibility hunks.

No private production model, business workbook or live endpoint is used here.
"""
import json

import polars as pl
import pytest

from ledger.cost_input_evidence import verified_placeholder,verified_quantity_input,placeholder_admission
from test_cost_input_evidence import proof,row


def normal_quantity_row():
    declaration=proof();declaration['sale_price_status']='normal'
    value=row(declaration);value['__current_sale_price']='20.00'
    return value


def test_normal_quantity_proof_does_not_waive_the_placeholder_guard():
    value=normal_quantity_row()
    assert verified_quantity_input(value)
    assert not verified_placeholder(value)
    frame=pl.from_dicts([{**value,'is_suspect':True}])
    assert frame.select(placeholder_admission(frame)).item() is False
    assert verified_quantity_input(row()) and verified_placeholder(row())


@pytest.mark.parametrize('change',[
    {'__current_order_hash':'b'*64},{'__current_item_quantity':'3'},
    {'__current_item_sku':'other'},{'__identity_changed':True},
    {'__current_sale_price':'99999'},{'__current_sale_price':'-1'},
])
def test_normal_quantity_proof_keeps_exact_identity_and_price_classification(change):
    assert not verified_quantity_input({**normal_quantity_row(),**change})


@pytest.mark.parametrize('sku',['HQT00365','HQT01773'])
def test_confirmed_zero_cost_sku_cannot_generate_a_return_credit(tmp_path,sku):
    from test_cost_return_month import calculate,totals
    result=calculate(tmp_path,[('A1','卖家已收到退货',3,3,'2026-07-09')],sku=sku,unit=.0001)
    assert totals(result,'goods_cost')=={'2026-06':0.0}
    assert totals(result,'goods_return_cost')=={}
    assert result.pricing_gaps.is_empty()


def test_an_ordinary_tiny_cost_is_not_covered_by_exact_sku_zero_policies(tmp_path):
    from test_cost_return_month import calculate,totals
    result=calculate(tmp_path,[('A1','卖家已收到退货',3,3,'2026-07-09')],sku='ORDINARY-SKU',unit=.0001)
    assert totals(result,'goods_cost')['2026-06']==pytest.approx(-.0003)
    assert totals(result,'goods_return_cost')['2026-07']==pytest.approx(.0003)


def test_trace_only_cost_does_not_block_independently_proven_brushing_zero():
    from test_brushing_coverage import calculate,evidence,missing_cost,STORE
    from ledger.order_lifecycle import SUPERSEDED_COST_NOTE
    cost={**missing_cost(),'unit_cost':10.,'cost_status':'priced','cost_source':'provided',
        'order_state':'Merged'}
    result=calculate(evidence(),costs=[cost],scope='internal_order_header')
    zeros=result.facts.filter(pl.col('template_id')=='order_console_brushing_zero_v1')
    assert zeros.height==3 and 'A1' in zeros['link_key'].to_list()
    trace=result.facts.filter(pl.col('source_note').str.contains(SUPERSEDED_COST_NOTE,literal=True).fill_null(False))
    assert trace.height==1
    assert result.slices[(STORE.name,'2026-07')].cost_coverage['uncovered']==0


def test_an_existing_current_cost_fact_still_suppresses_a_duplicate_zero():
    from test_brushing_coverage import calculate,evidence
    from ledger.brushing_zero import supplement
    result=calculate(evidence(),scope='internal_order_header')
    # This is the supplement consumer's existing-fact contract, not a claim
    # that arbitrary uploaded prices passed historical-price admission.
    existing=result.facts.filter(pl.col('link_key')=='A1').with_columns(
        pl.lit('current_cost_fact').alias('template_id'),pl.lit('已验证现存成本').alias('source_note'),
        pl.lit(10.).alias('amount'),pl.lit(-10.).alias('contribution'))
    metric=result.model.metric('goods_cost').for_platform('taobao')
    added=supplement(evidence(),result.spine,metric,existing)
    assert set(added['link_key'])=={'A2','B1'}


@pytest.mark.parametrize('captured_error,after_error',[(False,True),(True,False)])
def test_source_health_is_bound_to_frame_capture_and_current_close_still_checks_live_state(tmp_path,monkeypatch,captured_error,after_error):
    from ledger.order_feed import OrderFeed
    from ledger.engine.runtime import Ingestion
    from ledger.model.schema import Store,Overhead
    from ledger.workspace import Workspace
    from ledger import automation_checker as checker
    from test_order_feed import _fixture,FakeClient
    from test_commission import _model
    root=tmp_path/'feed';feed=OrderFeed(tmp_path/'ws',client=FakeClient(_fixture(root)),feed_root=root);feed.sync()
    with feed._connect() as conn:conn.execute('UPDATE feed_state SET last_error=? WHERE id=1',('captured failure' if captured_error else '',))
    build=feed._frames
    def frames(*args,**kwargs):
        result=build(*args,**kwargs)
        with feed._connect() as conn:conn.execute('UPDATE feed_state SET last_error=? WHERE id=1',('later failure' if after_error else '',))
        return result
    monkeypatch.setattr(feed,'_frames',frames)
    store=Store(id='taobao_test',name='淘宝测试店',platform='taobao')
    ingestion=Ingestion(model=None);feed.append_to(ingestion,store)
    assert ingestion.source_sync_pending is captured_error
    assert bool(feed.state()['last_error']) is after_error
    ws=Workspace(tmp_path/'ws');model=_model(stores=(store,),overheads=(Overhead(period='2026-06',amount=0),))
    model=model.model_copy(update={'statement':tuple(n.model_copy(update={'headline':'revenue'}) if n.id=='n_receipt' else n for n in model.statement)})
    checker.bind_model(ws,model)
    ws.record(store.id,'2026-06',{'can_close':True,'source_sync_pending':ingestion.source_sync_pending,
        'findings':[],'missing_sources':[],'statement':[{'id':'n_receipt','value':100,'available':True}]},[])
    checked=checker.check(ws,store.id,'2026-06',model=model)
    # Neither a later outage nor a captured failure can obtain close approval.
    assert not checked['allowed'] and 'source_sync_pending' in {i['code'] for i in checked['issues']}
