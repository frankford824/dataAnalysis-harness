"""A present-but-null live column must not erase certified uploaded flags."""
from types import SimpleNamespace
import polars as pl
import pytest
from ledger.order_flags import collect,fill_missing,apply
from ledger.engine.runtime import Ingestion,run
from ledger.order_feed import OrderFeed
from test_order_feed import _fixture,_model_and_store,_write,FakeClient
from test_after_sale_identity import original_cost


@pytest.mark.parametrize('missing',[None,'','  ','absent'])
def test_absent_null_and_blank_flags_share_the_same_fill_policy(missing):
    frame=pl.DataFrame({'internal':['I']})
    if missing!='absent':frame=frame.with_columns(pl.lit(missing,dtype=pl.Utf8).alias('order_flag'))
    fixed=fill_missing(frame,'internal',{'I':'蓝色旗帜'})
    assert fixed['order_flag'].item()=='蓝色旗帜'
    assert fixed['__flag_from_upload'].item()
    assert fill_missing(fixed,'internal',{'I':'蓝色旗帜'}).equals(fixed)


def test_live_non_blue_and_mixed_order_are_never_overwritten():
    frame=pl.DataFrame({'internal':[' I ','I','J'],'order_flag':['红色旗帜',None,'蓝色旗帜']})
    fixed=fill_missing(frame,'internal',{'I':'蓝色旗帜','J':'红色旗帜'})
    assert fixed['order_flag'].to_list()==['红色旗帜',None,'蓝色旗帜']
    assert not fixed['__flag_from_upload'].any()


@pytest.mark.parametrize('owner,hint,expected',[
    ('shop',None,True),('alias',None,True),('other','shop',False),
    (None,'shop',True),(None,None,False),('shop','other',True)])
def test_upload_owner_is_required_and_explicit_other_store_wins(owner,hint,expected):
    frame=pl.DataFrame({'internal_order_id':['I'],'order_flag':['蓝色旗帜'],
        'store_name':[owner],'__hint_store__':[hint]},schema_overrides={'store_name':pl.String,'__hint_store__':pl.String})
    flags,_=collect(SimpleNamespace(frames_of=lambda s:[SimpleNamespace(frame=frame)]),SimpleNamespace(name='shop',aliases=['alias']))
    assert ('I' in flags) is expected


def test_unknown_owner_and_conflicting_upload_cannot_supply_flags():
    frames=[pl.DataFrame({'internal_order_id':['I'],'order_flag':['蓝色旗帜']}),
            pl.DataFrame({'internal_order_id':['J','J'],'order_flag':['蓝色旗帜','红色旗帜'],'store_name':['shop','shop']})]
    flags,conflicts=collect(SimpleNamespace(frames_of=lambda s:[SimpleNamespace(frame=f) for f in frames]),SimpleNamespace(name='shop',aliases=[]))
    assert flags=={} and conflicts==1


@pytest.mark.parametrize('missing',[None,'','absent'])
@pytest.mark.parametrize('platform',['douyin','taobao','pdd'])
def test_missing_live_flags_preserve_brushing_exemption_end_to_end(tmp_path,missing,platform):
    root=tmp_path/'feed';manifest=_fixture(root,after_sku=None,second_unnamed=True)
    if platform=='pdd':
        for name in ('orders.parquet','order_items.parquet','order_costs.parquet'):
            frame=pl.read_parquet(root/manifest['objects'][name]['path'])
            if 'online_order_no' in frame.columns:
                frame=frame.with_columns(pl.lit('260601-163771850381198').alias('online_order_no'))
                manifest['objects'][name]=_write(root,name,frame)
    items=pl.read_parquet(root/manifest['objects']['order_items.parquet']['path'])
    items=items.drop('order_flag',strict=False)
    if missing!='absent':items=items.with_columns(pl.lit(missing,dtype=pl.Utf8).alias('order_flag'))
    manifest['objects']['order_items.parquet']=_write(root,'order_items.parquet',items)
    client=FakeClient(manifest);original_get=client.get
    def get(path,params=None):
        response=original_get(path,params)
        if path in {'entities/order/1','orders/1'}:response['order']['order_remark']='by6王娜'
        return response
    client.get=get
    feed=OrderFeed(tmp_path/'ws',client=client,feed_root=root);feed.sync()
    model,store=_model_and_store(feed,pricing='required')
    store=store.model_copy(update={'platform':platform})
    model=model.model_copy(update={'stores':tuple(store if s.id==store.id else s for s in model.stores)})
    ing=Ingestion(model=model,items=[])
    old=original_cost(ing,[('1','11','S1','SKU1'),('1','12','S2','SKU2')])
    old.frame=old.frame.with_columns(pl.lit(store.name).alias('store_name'),pl.lit('蓝色旗帜').alias('order_flag'))
    feed.append_to(ing,store)
    cost=ing.frames_of('order_cost')[0].frame
    assert cost['order_flag'].to_list()==['蓝色旗帜','蓝色旗帜']
    assert cost['source_note'].str.contains('同店原始聚水潭表').all()
    result=run(ing,platform)
    facts=result.facts.filter(pl.col('metric_id')=='goods_cost')
    assert facts.height==2 and facts['contribution'].sum()==0
    assert facts['counted'].all()
    assert facts['source_note'].str.contains('按刷单规则不计商品成本').all()
