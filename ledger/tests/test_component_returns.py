from types import SimpleNamespace
import polars as pl
from ledger.engine.component_returns import normalize


def data(rows=None,policy='transaction'):
    costs=pl.DataFrame({'internal_order_id':['I'],'internal_sub_order_id':['L'],'sub_order_id':['S'],'sku':['KIT'],'quantity':[2.0],'store_name':['shop']})
    original=SimpleNamespace(frame=pl.DataFrame({'internal_order_id':['I','I'],'internal_sub_order_id':['L','L'],'sub_order_id':['S','S'],'sku':['A','B'],'quantity':[4.0,6.0]}))
    records=rows or [('A',4.0),('B',6.0)]
    after=SimpleNamespace(frame=pl.DataFrame([{'after_sale_id':'AF','internal_order_id':'I','internal_sub_order_id':'L','sku':'KIT','original_sku':sku,'quantity':q,'returned_quantity':q,'refund_status':'退款成功','goods_status':'卖家已收到退货','__row__':i+2} for i,(sku,q) in enumerate(records)]))
    ing=SimpleNamespace(model=SimpleNamespace(stores=[SimpleNamespace(name='shop',aliases=[],cost_return_posting=policy)]),frames_of=lambda source:[original] if source=='order_cost' else [after])
    return ing,costs,after


def test_complete_components_convert_to_bundle_quantity_without_double_credit():
    ing,cost,after=data();normalize(ing,cost)
    assert after.frame['returned_quantity'].to_list()==[2,2]
    assert after.frame['component_original_returned_quantity'].to_list()==[4,6]
    assert not after.frame['__component_return_pending'].any()


def test_partial_bundle_uses_consistent_component_ratios():
    ing,cost,after=data([('A',2),('B',3)]);normalize(ing,cost)
    assert after.frame['returned_quantity'].to_list()==[1,1]


def test_missing_or_conflicting_component_does_not_reverse_whole_bundle():
    for rows in [[('A',4)],[('A',4),('B',3)]]:
        ing,cost,after=data(rows);normalize(ing,cost)
        assert after.frame['returned_quantity'].null_count()==len(rows)
        assert after.frame['__component_return_pending'].all()


def test_one_closed_refund_component_does_not_reverse_entire_bundle():
    ing,cost,after=data()
    after.frame=after.frame.with_columns(pl.when(pl.col('original_sku')=='B').then(pl.lit('退款关闭')).otherwise(pl.col('refund_status')).alias('refund_status'))
    normalize(ing,cost)
    assert after.frame['__component_return_pending'].all()


def test_other_store_policy_does_not_change_component_quantities():
    ing,cost,after=data(policy='order');before=after.frame.clone();normalize(ing,cost)
    assert after.frame.equals(before)


def test_uploaded_components_are_credited_once_after_feed_replaces_costs(tmp_path):
    from datetime import date
    from ledger.order_feed import OrderFeed
    from ledger.engine.runtime import run
    from test_order_feed import _fixture, _model_and_store, _uploaded_after_sales
    from test_merged_reship_markers import SnapshotClient
    from test_after_sale_identity import original_cost
    root=tmp_path/'feed';manifest=_fixture(root,after_sku=None)
    feed=OrderFeed(tmp_path/'ws',client=SnapshotClient(manifest),feed_root=root);feed.sync()
    model,store=_model_and_store(feed)
    store=store.model_copy(update={'cost_return_posting':'transaction'})
    model=model.model_copy(update={'stores':tuple(store if s.id==store.id else s for s in model.stores)})
    ing=_uploaded_after_sales(tmp_path,model,store,('S1','PART-A'),('S1','PART-B'))
    raw=original_cost(ing,[('1','11','S1','PART-A'),('1','11','S1','PART-B')])
    raw.frame=raw.frame.with_columns(pl.Series('quantity',[4.0,6.0]))
    after=ing.frames_of('after_sales')[0]
    after.frame=after.frame.with_columns(pl.Series('quantity',[4.0,6.0]),pl.Series('returned_quantity',[4.0,6.0]),
        pl.lit('AS1').alias('after_sale_id'),pl.lit('卖家已收到退货').alias('goods_status'),pl.lit(date(2026,7,4)).alias('return_time'))
    feed.append_to(ing,store)
    result=run(ing,store.platform)
    costs=result.facts.filter(pl.col('metric_id')=='goods_cost')
    credit=result.facts.filter(pl.col('metric_id')=='goods_return_cost')
    assert costs['contribution'].sum()==-7
    assert credit.height==1 and credit['contribution'].sum()==7
    assert credit['period'].to_list()==['2026-07']
    assert not result.eval_errors.get('cost_return')
