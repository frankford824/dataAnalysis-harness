import polars as pl
from ledger.engine.runtime import Ingestion,run
from ledger.order_feed import OrderFeed
from test_order_feed import _fixture,_write,FakeClient,_model_and_store


class SnapshotClient(FakeClient):
    def revision(self,etag=""):
        data,tag=super().revision(etag)
        if data is not None:data["latest_seq"]=self.manifest["through_seq"]
        return data,tag
    def get(self,path,params=None):
        if path=='changes':return {'to_seq':int((params or {}).get('after_seq',10)),'has_more':False,'changes':[]}
        return super().get(path,params)


def test_marker_restores_original_month_without_reclassifying_normal_merged_items(tmp_path):
    root=tmp_path/'feed';manifest=_fixture(root,after_sku=None,second_unnamed=True)
    orders=pl.read_parquet(root/manifest['objects']['orders.parquet']['path']).to_dicts()
    orders[0].update(online_order_no='MERGED',order_time='2026-07-02 10:00:00',pay_time='2026-07-02 10:01:00')
    orders.append({**orders[0],'order_id':'2','online_order_no':'ROOT','order_time':'2026-06-01 10:00:00','pay_time':'2026-06-01 10:01:00'})
    manifest['objects']['orders.parquet']=_write(root,'orders.parquet',pl.DataFrame(orders))
    items=pl.read_parquet(root/manifest['objects']['order_items.parquet']['path']).to_dicts()
    items[1]['outer_sku']='$Asr-A1'
    items.append({**items[0],'order_id':'2','sub_order_id':'13','outer_sku':'ORIG-SUB','online_order_no':'ROOT'})
    manifest['objects']['order_items.parquet']=_write(root,'items.parquet',pl.DataFrame(items))
    after=pl.read_parquet(root/manifest['objects']['after_sales.parquet']['path']).head(1).with_columns(pl.lit('2').alias('order_id'),pl.lit('ROOT').alias('online_order_no'),pl.lit('补发').alias('after_sale_type_raw'))
    manifest['objects']['after_sales.parquet']=_write(root,'after.parquet',after)
    feed=OrderFeed(tmp_path/'ws',client=SnapshotClient(manifest),feed_root=root);feed.sync()
    model,store=_model_and_store(feed);ing=Ingestion(model=model,items=[]);feed.append_to(ing,store)
    orders=ing.frames_of('order_detail')[0].frame
    assert orders.filter(pl.col('sub_order_id')=='S1')['order_type'].item()=='销售订单'
    assert orders.filter(pl.col('sub_order_id')=='$Asr-A1')['order_type'].item()=='补发订单'
    costs=ing.frames_of('order_cost')[0].frame
    row=costs.filter(pl.col('sub_order_id')=='$Asr-A1').row(0,named=True)
    assert row['original_order_id']=='ROOT' and row['order_id']=='MERGED'
    result=run(ing,store.platform)
    returns=result.spine_facts.filter(pl.col('metric_id')=='reshipment_cost')
    assert returns['period'].unique().to_list()==['2026-06']
    assert returns['amount'].sum()==-2
    normal=result.spine_facts.filter(pl.col('metric_id')=='goods_cost')
    assert normal['amount'].sum()==-7


def test_non_reship_and_ambiguous_originals_are_not_inferred_from_marker():
    data=pl.DataFrame({'after_sale_id':['A1','A2','A2','A3'],'online_order_no':['O1','O2','O3','O4,O5'],'after_sale_type_raw':['普通退货','补发','补发','补发']})
    assert OrderFeed._reshipment_origins(data)=={}


def test_original_item_retains_sale_identity_after_parent_is_merged():
    after=pl.DataFrame({'after_sale_id':['A1'],'order_id':['I1'],'sub_order_id':['L1'],
        'order_store_id':['10'],'online_order_no':['HEADER'],'after_sale_type_raw':['补发']})
    items=pl.DataFrame({'order_id':['I1','I1'],'sub_order_id':['L1','L2'],'online_order_no':['10:ACTUAL','10:OTHER']})
    assert OrderFeed._reshipment_origins(after,items)=={'$asr-a1':'ACTUAL'}
    assert OrderFeed._reshipment_origins(after,items.with_columns(pl.lit('99:OTHER').alias('online_order_no')))=={}
    conflict=pl.concat([items,items.head(1).with_columns(pl.lit('10:CONFLICT').alias('online_order_no'))])
    assert OrderFeed._reshipment_origins(after,conflict)=={}
    merged=items.with_columns(pl.lit('HEADER,10:ACTUAL').alias('online_order_no'),pl.Series('outer_sku',['CHILD','OTHER-CHILD']))
    assert OrderFeed._reshipment_origins(after,merged,{'CHILD':'ACTUAL'})=={'$asr-a1':'ACTUAL'}
    assert OrderFeed._reshipment_origins(after,merged,{'CHILD':None})=={}


def test_child_equal_to_main_order_keeps_cost_when_there_is_no_return(tmp_path):
    root=tmp_path/'feed';manifest=_fixture(root,after_sku=None,second_unnamed=True)
    items=pl.read_parquet(root/manifest['objects']['order_items.parquet']['path']).with_columns(
        pl.when(pl.col('sub_order_id')=='11').then(pl.lit('ON1')).otherwise(pl.lit('OTHER')).alias('outer_sku'))
    manifest['objects']['order_items.parquet']=_write(root,'items.parquet',items)
    feed=OrderFeed(tmp_path/'ws',client=FakeClient(manifest),feed_root=root);feed.sync()
    model,store=_model_and_store(feed);ing=Ingestion(model=model,items=[]);feed.append_to(ing,store)
    facts=run(ing,store.platform).spine_facts.filter(pl.col('metric_id')=='goods_cost')
    assert dict(facts.select('link_key','amount').iter_rows())=={'ON1':-7.0,'OTHER':-2.0}
