from types import SimpleNamespace

import polars as pl
import pytest

from ledger.engine.runtime import Ingestion, run
from ledger.order_feed import OrderFeed
from ledger.order_flags import apply
from test_after_sale_identity import original_cost
from test_order_feed import _fixture, _model_and_store, _write, FakeClient


@pytest.mark.parametrize('platform,flag,expected',[('pdd','蓝色旗帜',0),('pdd','红色旗帜',-9),('pdd',None,-9),('taobao','蓝色旗帜',-9)])
def test_blue_cost_exclusion_survives_feed_without_changing_other_flags(tmp_path,platform,flag,expected):
    root=tmp_path/'feed';manifest=_fixture(root,after_sku=None,second_unnamed=True)
    if platform == 'pdd':
        for name in ('orders.parquet', 'order_items.parquet', 'order_costs.parquet'):
            if name not in manifest['objects']:
                continue
            frame = pl.read_parquet(root / manifest['objects'][name]['path'])
            if 'online_order_no' in frame.columns:
                frame = frame.with_columns(pl.lit('260601-163771850381198').alias('online_order_no'))
            if name == 'order_costs.parquet':
                frame = frame.with_columns(pl.lit('2026-06-01').alias('cost_as_of'), pl.lit('history').alias('cost_source'),
                                           pl.lit('{"order_date":"2026-06-01"}').alias('pricing_evidence'))
            manifest['objects'][name] = _write(root, name, frame)
    feed=OrderFeed(tmp_path/'ws',client=FakeClient(manifest),feed_root=root);feed.sync()
    model,store=_model_and_store(feed,pricing='required')
    store=store.model_copy(update={'platform':platform})
    model=model.model_copy(update={'stores':tuple(store if s.id==store.id else s for s in model.stores)})
    ing=Ingestion(model=model,items=[])
    old=original_cost(ing,[('1','11','S1','SKU1'),('1','12','S2','SKU2')])
    old.frame=old.frame.with_columns(pl.lit(flag,dtype=pl.Utf8).alias('order_flag'))
    feed.append_to(ing,store)
    if platform == 'pdd':
        orders = ing.frames_of('order_detail')[0].frame
        assert orders['order_date'].dt.strftime('%Y-%m-%d').unique().to_list() == ['2026-06-01']
        assert orders['order_time'].null_count() == orders.height
    costs=ing.frames_of('order_cost')[0].frame
    assert costs['quantity'].to_list()==[2,2]
    assert sorted(costs['unit_cost'].to_list())==[1,3.5]
    result=run(ing,platform)
    assert result.facts.filter(pl.col('metric_id')=='goods_cost')['contribution'].sum()==expected
    if platform=='pdd' and flag=='蓝色旗帜':
        assert result.spine['order_flag'].unique().to_list()==['蓝色旗帜']
        for sl in result.slices.values():
            report=sl.link_reports.get('goods_cost')
            if report:assert report.spine_keys==0


def test_mixed_flags_do_not_exempt_the_entire_merged_order():
    original=SimpleNamespace(frame=pl.DataFrame({'internal_order_id':['I1','I2'],'order_flag':['蓝色旗帜','红色旗帜']}))
    ing=SimpleNamespace(frames_of=lambda source:[original])
    cost=SimpleNamespace(frame=pl.DataFrame({'internal_order_id':['I1','I2'],'order_id':['MAIN','MAIN'],'unit_cost':[4,7]}),notes=[])
    orders=SimpleNamespace(frame=pl.DataFrame({'order_id':['MAIN']}))
    apply(ing,SimpleNamespace(platform='pdd',name='shop',aliases=[]),cost,orders)
    assert cost.frame['order_flag'].to_list()==['蓝色旗帜','红色旗帜']
    assert orders.frame['order_flag'].item() is None


def test_conflicting_flag_evidence_is_not_used_to_zero_cost():
    original=SimpleNamespace(frame=pl.DataFrame({'internal_order_id':['I','I'],'order_flag':['蓝色旗帜','红色旗帜']}))
    cost=SimpleNamespace(frame=pl.DataFrame({'internal_order_id':['I'],'order_id':['MAIN']}),notes=[])
    orders=SimpleNamespace(frame=pl.DataFrame({'order_id':['MAIN']}))
    apply(SimpleNamespace(frames_of=lambda source:[original]),SimpleNamespace(platform='pdd',name='shop',aliases=[]),cost,orders)
    assert cost.frame['order_flag'].item() is None
    assert '不一致' in cost.notes[0]


@pytest.mark.parametrize('virtual', [False, True])
@pytest.mark.parametrize('platform', ['pdd', 'taobao', 'douyin'])
def test_absent_cost_row_is_pending_instead_of_an_implicit_zero(tmp_path, virtual, platform):
    root=tmp_path/'feed';manifest=_fixture(root,after_sku=None,second_unnamed=True)
    for name in ('orders.parquet','order_items.parquet','order_costs.parquet'):
        frame=pl.read_parquet(root/manifest['objects'][name]['path'])
        if 'online_order_no' in frame.columns:
            frame=frame.with_columns(pl.lit('260601-163771850381198').alias('online_order_no'))
        if name=='order_items.parquet':
            frame=frame.with_columns(pl.Series('is_virtual',[False,virtual]))
        if name=='order_costs.parquet':
            frame=frame.head(1).with_columns(pl.lit('2026-06-01').alias('cost_as_of'),
                                           pl.lit('{"order_date":"2026-06-01"}').alias('pricing_evidence'))
        manifest['objects'][name]=_write(root,name,frame)
    feed=OrderFeed(tmp_path/'ws',client=FakeClient(manifest),feed_root=root);feed.sync()
    model,store=_model_and_store(feed,pricing='required')
    store=store.model_copy(update={'platform':platform})
    model=model.model_copy(update={'stores':tuple(store if s.id==store.id else s for s in model.stores)})
    ing=Ingestion(model=model,items=[]);feed.append_to(ing,store)
    result=run(ing,platform)
    if virtual:
        assert result.pricing_gaps.is_empty()
    else:
        assert result.pricing_gaps.height==1
        assert result.pricing_gaps['reason'].item()=='成本明细尚未同步'
        assert result.pricing_gaps['sku'].item()=='SKU2'
        assert result.slices[(store.name,'2026-06')].nodes['net_profit'].value is None


@pytest.mark.parametrize('platform', ['pdd', 'taobao', 'douyin'])
def test_replaced_bundle_cost_is_not_added_to_its_current_components(tmp_path, platform):
    root=tmp_path/'feed';manifest=_fixture(root,after_sku=None,second_unnamed=True)
    for name in ('orders.parquet','order_items.parquet','order_costs.parquet'):
        frame=pl.read_parquet(root/manifest['objects'][name]['path'])
        if 'online_order_no' in frame.columns:
            frame=frame.with_columns(pl.lit('260601-163771850381198').alias('online_order_no'))
        if name=='order_costs.parquet':
            frame=frame.with_columns(pl.lit('2026-06-01').alias('cost_as_of'),
                                    pl.lit('{"order_date":"2026-06-01"}').alias('pricing_evidence'))
            old=frame.head(1).with_columns(pl.lit('999').alias('sub_order_id'),pl.lit('OLD-KIT').alias('sku_id'),
                                         pl.lit('99').alias('unit_cost'),pl.lit('198').alias('cost_amount'))
            frame=pl.concat([frame,old])
        manifest['objects'][name]=_write(root,name,frame)
    feed=OrderFeed(tmp_path/'ws',client=FakeClient(manifest),feed_root=root);feed.sync()
    model,store=_model_and_store(feed,pricing='required');store=store.model_copy(update={'platform':platform})
    model=model.model_copy(update={'stores':tuple(store if s.id==store.id else s for s in model.stores)})
    ing=Ingestion(model=model,items=[]);feed.append_to(ing,store)
    result=run(ing,platform)
    assert result.pricing_gaps.is_empty()
    assert result.facts.filter(pl.col('metric_id')=='goods_cost')['contribution'].sum()==-9
    assert feed._retired_cost_rows==1
    assert '旧商品行' in ' '.join(ing.frames_of('order_cost')[0].notes)


@pytest.mark.parametrize('platform', ['taobao', 'douyin'])
def test_present_order_with_missing_unit_price_is_reviewable_and_blocks_profit(tmp_path, platform):
    root=tmp_path/'feed';manifest=_fixture(root,after_sku=None,second_unnamed=True)
    name='order_costs.parquet'
    frame=pl.read_parquet(root/manifest['objects'][name]['path'])
    frame=frame.with_columns(
        pl.when(pl.col('sub_order_id')=='11').then(None).otherwise(pl.col('unit_cost')).alias('unit_cost'),
        pl.when(pl.col('sub_order_id')=='11').then(pl.lit('missing_price')).otherwise(pl.col('cost_status')).alias('cost_status'))
    manifest['objects'][name]=_write(root,name,frame)
    feed=OrderFeed(tmp_path/'ws',client=FakeClient(manifest),feed_root=root);feed.sync()
    model,store=_model_and_store(feed,pricing='required');store=store.model_copy(update={'platform':platform})
    model=model.model_copy(update={'stores':tuple(store if s.id==store.id else s for s in model.stores)})
    ing=Ingestion(model=model,items=[]);feed.append_to(ing,store)
    result=run(ing,platform)
    assert result.pricing_gaps.height==1
    gap=result.pricing_gaps.row(0,named=True)
    assert gap['sku']=='SKU1'
    assert gap['reason']=='商品成本单价待核对'
    assert gap['order_date']=='2026-06-02'
    assert result.slices[(store.name,'2026-06')].nodes['net_profit'].value is None
    assert not result.slices[(store.name,'2026-06')].can_close


def test_source_flagged_item_is_pending_instead_of_disappearing_from_cost(tmp_path):
    root=tmp_path/'feed';manifest=_fixture(root,after_sku=None,second_unnamed=True)
    name='order_items.parquet'
    frame=pl.read_parquet(root/manifest['objects'][name]['path']).with_columns(pl.Series('is_suspect',[True,False]))
    manifest['objects'][name]=_write(root,name,frame)
    feed=OrderFeed(tmp_path/'ws',client=FakeClient(manifest),feed_root=root);feed.sync()
    model,store=_model_and_store(feed,pricing='required')
    ing=Ingestion(model=model,items=[]);feed.append_to(ing,store)
    result=run(ing,store.platform)
    assert result.pricing_gaps['sku'].to_list()==['SKU1']
    assert result.slices[(store.name,'2026-06')].nodes['net_profit'].value is None


def test_changed_item_quantity_cannot_reuse_a_stale_cost_assertion(tmp_path):
    root=tmp_path/'feed';manifest=_fixture(root,after_sku=None,second_unnamed=True)
    name='order_items.parquet'
    frame=pl.read_parquet(root/manifest['objects'][name]['path']).with_columns(
        pl.when(pl.col('sub_order_id')=='11').then(pl.lit('3')).otherwise(pl.col('quantity')).alias('quantity'))
    manifest['objects'][name]=_write(root,name,frame)
    feed=OrderFeed(tmp_path/'ws',client=FakeClient(manifest),feed_root=root);feed.sync()
    model,store=_model_and_store(feed,pricing='required')
    ing=Ingestion(model=model,items=[]);feed.append_to(ing,store)
    result=run(ing,store.platform)
    assert result.pricing_gaps['sku'].to_list()==['SKU1']
    assert result.pricing_gaps['reason'].item()=='订单商品与成本明细不一致，等待更新'
