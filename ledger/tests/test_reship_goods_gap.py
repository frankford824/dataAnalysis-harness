"""A replacement shipment cannot require a second ordinary goods cost."""
from copy import deepcopy
from datetime import datetime

import polars as pl
import pytest

from ledger import cost_lines, cost_line_posting, service, view
from ledger.engine.runtime import Ingestion, run
from ledger.model.schema import StatementNode
from ledger.storage_integrity import seal
from ledger.workspace import Workspace, WorkspaceError
from test_brushing_coverage import calculate, missing_cost, STORE
from test_reship_period_and_cost_drill import item
from test_cost_lines import archive, payload, edited_cost_export


def scenario(*, reship_type='补发订单', priced=True, missing_sale=False, live=True):
    orders=[dict(order_id='A',sub_order_id='sale',product_id='P',store_name=STORE.name,
        order_type='销售订单',tracking_no='T1',order_time=datetime(2026,7,2)),
        dict(order_id='A',sub_order_id='$Asr-1022170307',product_id='P',store_name=STORE.name,
        order_type=reship_type,tracking_no='T2',order_time=datetime(2026,7,30))]
    costs=[{**missing_cost(), 'sub_order_id':'sale','internal_order_id':'normal',
        'unit_cost':2.75, 'cost_source':'history','cost_status':'priced','cost_as_of':'2026-07-02'}]
    if missing_sale:costs=[]
    if priced:
        costs.append({**missing_cost(), 'sub_order_id':'$Asr-1022170307','internal_order_id':'reship',
            'order_type':'补发订单','unit_cost':2.75,'cost_source':'history','cost_status':'priced','cost_as_of':'2026-07-02'})
    result=calculate(pl.DataFrame(),orders=orders,costs=costs)
    from conftest import MODELS
    from ledger.model.loader import load_model
    metric=load_model(MODELS/'cn-ecommerce').metric('reshipment_cost').for_platform('taobao')
    m=result.model.model_copy(update={'metrics':(*result.model.metrics,metric),
        'statement':(*result.model.statement,StatementNode(id='reship',name='补发成本',formula={'op':'add','of':['reshipment_cost']}))})
    frames=[item('order_detail',orders,orders[0])]
    if costs:frames.append(item('order_cost',costs,costs[0]))
    if not live:
        for i in frames:i.template=i.template.model_copy(update={'id':'export_'+i.template.source})
    return run(Ingestion(model=m,items=frames),'taobao')


@pytest.mark.parametrize('live',[True,False])
def test_paid_reship_not_a_goods_gap_but_original_cost_remains(live):
    result=scenario(live=live);sl=result.slices[(STORE.name,'2026-07')]
    assert sl.coverage_gap_rows.is_empty()
    assert sl.cost_coverage['expected']==1 and sl.cost_coverage['covered']==1
    assert sl.nodes['cost'].value==-2.75
    assert sl.nodes['reship'].value==-2.75


@pytest.mark.parametrize('kind',['销售订单',None])
def test_marker_or_same_master_does_not_exempt_unproven_or_normal_goods(kind):
    result=scenario(reship_type=kind,priced=False)
    sl=result.slices[(STORE.name,'2026-07')]
    assert sl.coverage_gap_rows['coverage_key'].to_list()==['$Asr-1022170307']
    assert sl.cost_coverage['expected']==2


def test_genuine_sale_gap_on_same_parent_is_still_required():
    sl=scenario(missing_sale=True).slices[(STORE.name,'2026-07')]
    assert sl.coverage_gap_rows['coverage_key'].to_list()==['sale']
    assert sl.cost_coverage['expected']==1


def test_historical_reship_supplement_retires_without_deleting_log(tmp_path):
    result=scenario();store=result.model.store('s');ws=Workspace(tmp_path)
    # Reproduce a pre-fix gap and its approved human record.
    previous=scenario(reship_type='销售订单',priced=False)
    gap=previous.slices[(STORE.name,'2026-07')].coverage_gap_rows
    rid=ws.record('s','2026-07',view.slice_dict(previous.slices[(STORE.name,'2026-07')],store,result.model),[])
    gap.write_parquet(ws.coverage_gaps_path(rid));seal(ws.coverage_gaps_path(rid))
    cost_lines.save(ws,'s','2026-07',rid,coverage_key=gap['coverage_key'][0],context_sha=gap['context_sha'][0],amount='2.75',reason='旧版缺口补录')
    cost_line_posting.apply(ws,result,store)
    sl=result.slices[(STORE.name,'2026-07')]
    assert sl.cost_line_posting['total']==0 and sl.cost_line_posting['count']==0
    assert sl.facts.filter(pl.col('source_id')=='__manual_order_cost__').is_empty()
    assert ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0]==1
    new=ws.record('s','2026-07',view.slice_dict(sl,store,result.model),[])
    service._keep_facts(ws,new,sl)
    assert cost_lines.page(ws,new,'s','2026-07')['total']==0
    with pytest.raises(WorkspaceError,match='对应关系'):
        cost_lines.save(ws,'s','2026-07',new,coverage_key=gap['coverage_key'][0],context_sha=gap['context_sha'][0],amount='2.75',reason='再补')


def test_legacy_gap_export_cannot_bypass_new_scope_check(tmp_path):
    ws=Workspace(tmp_path);raw=payload();rid=ws.record(raw['store_id'],raw['period'],raw,[])
    frame=archive(ws,rid)
    content=edited_cost_export(ws,rid,{'S2':'2.75'})
    frame.drop('goods_scope_version').write_parquet(ws.coverage_gaps_path(rid));seal(ws.coverage_gaps_path(rid))
    assert not any(r['editable'] for r in cost_lines.current(ws,rid,raw['store_id'],raw['period'])['items'])
    with pytest.raises(WorkspaceError,match='排除补发'):
        cost_lines.batch_preview(ws,raw['store_id'],raw['period'],rid,content,'旧模板')
    with pytest.raises(WorkspaceError,match='排除补发'):
        cost_lines.save(ws,raw['store_id'],raw['period'],rid,coverage_key='S2',context_sha=frame['context_sha'][0],amount='2.75',reason='旧页面')
    assert ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0]==0
