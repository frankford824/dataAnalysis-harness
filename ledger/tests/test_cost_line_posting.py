from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import polars as pl
import pytest

from ledger import cost_lines, cost_line_posting, frozen_costs, manual_cost, service
from ledger.engine import calculate
from ledger.engine.runtime import _empty_spine_facts
from ledger.workspace import Workspace
from test_cost_lines import model, payload, archive


def setup(tmp_path, amount='12.50'):
    ws = Workspace(tmp_path)
    m = model(); raw = payload(); store = m.store(raw['store_id'])
    rid = ws.record(store.id, raw['period'], raw, [])
    gaps = archive(ws, rid, ('S2',))
    saved = cost_lines.save(ws, store.id, raw['period'], rid, coverage_key='S2',
        context_sha=gaps['context_sha'][0], amount=amount, reason='原单核对')
    inputs = deepcopy(raw['calculation_inputs']); inputs['unavailable_metrics'] = []
    sl = SimpleNamespace(coverage_gap_rows=gaps, cost_line_posting={},
        calculation_inputs=inputs, nodes=calculate.evaluate_statement(m,inputs['metric_totals'],set(),set()),
        facts=pl.DataFrame({'metric_id':['goods_cost'],'source_id':['order_cost'],
                           'amount':[-600.0],'contribution':[-600.0],'counted':[True]}))
    # Another month's row must not shift this month's global spine identity.
    spine = pl.DataFrame({'store':[store.name]*2, 'period':['2026-05','2026-06'],
        'order_id':['O1','O2'],'sub_order_id':['S1','S2'],'product_id':['P1','P2'],
        '__spine_origin__':['order_detail_file']*2})
    result = SimpleNamespace(model=m, slices={(store.name,'2026-06'):sl},
                             spine=spine, spine_facts=_empty_spine_facts(), facts=sl.facts.clone())
    return ws, m, store, result, saved, rid


@pytest.mark.parametrize('amount',['12.50','0.00','-2.00'])
def test_booked_costs_reconcile_profit_facts_preview_and_frozen_export(tmp_path,amount):
    ws,m,store,result,saved,rid=setup(tmp_path,amount)
    cost_line_posting.apply(ws,result,store)
    sl=result.slices[(store.name,'2026-06')]
    expected=600+Decimal(amount)
    assert sl.calculation_inputs['metric_totals']['goods_cost']==-float(expected)
    assert sl.nodes['net_profit'].value==float(1000-expected)
    assert sl.cost_line_posting['observed']['goods']==600
    assert sl.facts['contribution'].sum()==pytest.approx(-float(expected))
    assert result.spine_facts['spine_row'].to_list()==[1]
    preview=manual_cost.preview(m,{'calculation_inputs':sl.calculation_inputs},
        {'goods':expected,'dropship':0,'reshipment':0})
    assert preview['profit']==float(1000-expected)
    decision={'confirmed':preview['confirmed'],'observed':preview['observed'],
        'line_reviews':[{'coverage_key':'S2','amount':amount,'revision':saved['line_revision']}]}
    frozen=frozen_costs.append(sl.facts,m,decision,{'confirmed':preview['confirmed']},run_id=rid)
    assert frozen['contribution'].sum()==pytest.approx(-float(expected))
    assert frozen.filter(pl.col('record_type')=='结账人工补录').is_empty()
    # Saving/recomputing does not mutate the source run.
    assert manual_cost.observed(__import__('json').loads(ws.latest_run(store.id,'2026-06')['result']))['goods']==600
    with pytest.raises(ValueError,match='重复计入'):
        cost_line_posting.apply(ws,result,store)


@pytest.mark.parametrize('case',['removed','context_changed','source_arrived','closed','other_store'])
def test_only_current_uncovered_context_in_open_store_can_be_posted(tmp_path,case):
    ws,m,store,result,saved,rid=setup(tmp_path)
    sl=result.slices[(store.name,'2026-06')]
    if case=='removed':
        cost_lines.save(ws,store.id,'2026-06',rid,coverage_key='S2',context_sha=sl.coverage_gap_rows['context_sha'][0],
            amount=None,reason='撤销',action='remove',expected_line_revision=saved['line_revision'])
    elif case=='context_changed':sl.coverage_gap_rows=sl.coverage_gap_rows.with_columns(pl.lit('different').alias('context_sha'))
    elif case=='source_arrived':sl.coverage_gap_rows=sl.coverage_gap_rows.head(0)
    elif case=='closed':ws.conn.execute("UPDATE period SET state='closed' WHERE store_id=?",(store.id,));ws.conn.commit()
    else:store=m.store('taobao_mt9scbh6')
    cost_line_posting.apply(ws,result,store)
    assert not sl.cost_line_posting.get('count') and sl.facts['contribution'].sum()==-600


def test_new_run_uses_latest_amount_once_and_keeps_other_missing_evidence(tmp_path):
    ws,m,store,result,saved,rid=setup(tmp_path)
    sl=result.slices[(store.name,'2026-06')]
    cost_lines.save(ws,store.id,'2026-06',rid,coverage_key='S2',context_sha=sl.coverage_gap_rows['context_sha'][0],
        amount='20.00',reason='更正',expected_line_revision=saved['line_revision'])
    sl.calculation_inputs['unavailable_metrics']=['trade_receipt']
    sl.nodes=calculate.evaluate_statement(m,sl.calculation_inputs['metric_totals'],{'trade_receipt'},set())
    cost_line_posting.apply(ws,result,store)
    assert sl.cost_line_posting['count']==1 and sl.cost_line_posting['total']==20
    assert sl.facts['contribution'].sum()==-620
    assert not sl.nodes['net_profit'].available


def test_source_projection_mismatch_blocks_publication(tmp_path):
    ws,m,store,result,_,_=setup(tmp_path)
    result.spine=result.spine.filter(pl.col('period')!='2026-06')
    with pytest.raises(ValueError,match='分摊金额不一致'):
        cost_line_posting.apply(ws,result,store)


def test_concurrent_edit_is_not_reported_as_already_booked(tmp_path,monkeypatch):
    ws,m,store,result,saved,rid=setup(tmp_path)
    original=cost_lines._latest
    def racing(*args,**kwargs):
        rows=original(*args,**kwargs)
        gap=result.slices[(store.name,'2026-06')].coverage_gap_rows.row(0,named=True)
        cost_lines.save(ws,store.id,'2026-06',rid,coverage_key='S2',context_sha=gap['context_sha'],
            amount='25.00',reason='核算中修改',expected_line_revision=saved['line_revision'])
        return rows
    monkeypatch.setattr(cost_lines,'_latest',racing)
    # save() returns a page which also calls _latest, so prevent the simulated
    # edit from recursively re-entering this test hook.
    def once(*args,**kwargs):
        monkeypatch.setattr(cost_lines,'_latest',original)
        return racing(*args,**kwargs)
    monkeypatch.setattr(cost_lines,'_latest',once)
    cost_line_posting.apply(ws,result,store)
    posting=result.slices[(store.name,'2026-06')].cost_line_posting
    assert posting['total']==12.5 and posting['line_revision']==saved['line_revision']
    assert cost_lines.revision(ws,store.id,'2026-06')>posting['line_revision']


def test_service_persists_supplement_in_same_statement_and_drill(tmp_path,monkeypatch):
    from datetime import datetime
    from io import BytesIO
    from ledger import view
    from test_brushing_coverage import calculate as engine_fixture, missing_cost, STORE
    orders=[dict(order_id=f'O{i}',sub_order_id=f'S{i}',product_id=f'P{i}',store_name=STORE.name,
        order_type='销售订单',tracking_no=f'T{i}',order_time=datetime(2026,7,2)) for i in range(20)]
    costs=[{**missing_cost(), 'order_id':f'O{i}','original_order_id':f'O{i}','sub_order_id':f'S{i}',
        'unit_cost':5., 'cost_source':'history','cost_status':'priced','cost_as_of':'2026-07-02'} for i in range(19)]
    original=engine_fixture(pl.DataFrame(),orders=orders,costs=costs)
    m=original.model;store=m.store('s');ws=Workspace(tmp_path)
    ws.keep('orders.csv',BytesIO(b'original input'),'s')
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    monkeypatch.setattr(service,'ingest',lambda *a,**kw:original.ingestion)
    monkeypatch.setattr(service,'run',lambda *a,**kw:deepcopy(original))
    first=service.recompute(ws,m,store)
    assert not first.failure
    rid=ws.state('s','2026-07').run_id
    gap=cost_lines.current(ws,rid,'s','2026-07')['items'][0]
    cost_lines.save(ws,'s','2026-07',rid,coverage_key=gap['coverage_key'],context_sha=gap['context_sha'],amount='8.00',reason='核对')
    second=service.recompute(ws,m,store)
    assert not second.failure
    state=ws.state('s','2026-07')
    assert state.run_id!=rid
    assert state.result['cost_review']['observed']['goods']==95
    assert state.result['cost_line_posting']['total']==8
    assert state.result['statement'][0]['value']==-103
    assert view.drill(service.facts_of(ws,state.run_id),m,'cost')['source_total']==-103
    supplement=view.drill(service.facts_of(ws,state.run_id),m,'cost',file='人工补录订单成本')['sample']
    assert len(supplement)==1 and supplement[0]['file_sha'] is None
    third=service.recompute(ws,m,store)
    assert not third.failure and ws.state('s','2026-07').run_id==state.run_id
