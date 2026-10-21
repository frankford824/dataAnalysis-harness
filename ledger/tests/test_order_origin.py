from datetime import date
import json
import polars as pl
from ledger.order_origin import original_parent
from ledger.order_feed import OrderFeed
from ledger.model.schema import Store
from test_order_feed import _fixture

A='5127195351131187737'
B='5127693769183005335'


def test_member_requires_same_store_and_declared_header_membership():
    rows=pl.DataFrame({'raw':['10:'+A,'20:'+A,'10:'+B,A],
        'header':[B+',10:'+A]*4,'shop':['10']*4,'child':['C']*4})
    parent,bound=original_parent(pl.col('raw'),pl.col('header'),pl.col('shop'),pl.col('child'))
    out=rows.select(parent.alias('parent'),bound.alias('bound'))
    assert out['parent'].to_list()==[A,B+',10:'+A,B,A]
    assert out['bound'].to_list()==[True,False,True,True]
    parent,bound=original_parent(pl.col('raw'),pl.col('header'),pl.col('shop'),pl.col('child'),{'C':B})
    out=rows.select(parent.alias('parent'),bound.alias('bound'))
    assert out['parent'].to_list()==[B]*4
    assert out['bound'].to_list()==[False,False,True,False]


def frames(tmp_path):
    root=tmp_path/'input';manifest=_fixture(root)
    read=lambda n:pl.read_parquet(root/manifest['objects'][n]['path'])
    return read('orders.parquet'),read('order_items.parquet'),read('order_costs.parquet'),read('order_relations.parquet')


def test_merged_member_uses_its_own_parent_and_original_day_in_both_frames(tmp_path):
    orders,items,costs,relations=frames(tmp_path)
    orders=orders.with_columns(pl.lit(B+',10:'+A).alias('online_order_no'),pl.lit('2026-08-20 10:00:00').alias('order_time'))
    items=items.with_columns(pl.lit('10:'+A).alias('online_order_no'),pl.lit(None,dtype=pl.String).alias('outer_sku'),pl.lit('2026-08-19').alias('original_order_date'))
    costs=costs.with_columns(pl.lit('2026-08-19').alias('cost_as_of'),pl.lit(json.dumps({'order_date':'2026-08-19'})).alias('pricing_evidence'))
    feed=OrderFeed(tmp_path/'ws');store=Store(id='taobao_test',name='shop',platform='taobao')
    empty=pl.DataFrame(schema={'order_id':pl.String,'online_status_raw':pl.String})
    original=items.to_dicts()
    of=feed._order_frame(orders,items,empty,relations,store,'f')
    cf=feed._cost_frame(orders,items,costs,relations,store,'f',require_history=True)
    assert of['order_id'].item()==cf['original_order_id'].item()==A
    assert of['order_date'].item().date()==date(2026,8,19)
    assert of['order_time'].item() is None
    assert cf['cost_as_of'].item()=='2026-08-19'
    assert items.to_dicts()==original


def test_verified_original_day_crosses_month_without_changing_raw_header(tmp_path):
    orders,items,_,relations=frames(tmp_path)
    orders=orders.with_columns(pl.lit(A).alias('online_order_no'),pl.lit('2026-08-31 23:17:27').alias('order_time'))
    items=items.with_columns(pl.lit('10:'+A).alias('online_order_no'),pl.lit('2026-09-01').alias('original_order_date'))
    before=orders.to_dicts()
    result=OrderFeed(tmp_path/'ws')._order_frame(orders,items,pl.DataFrame(schema={'order_id':pl.String,'online_status_raw':pl.String}),relations,Store(id='s',name='shop',platform='taobao'),'f')
    assert result['order_date'].item().date()==date(2026,9,1)
    assert result['order_time'].item() is None
    assert orders.to_dicts()==before


def test_old_snapshot_keeps_original_date_from_a_live_delta(tmp_path):
    _,items,_,_=frames(tmp_path)
    delta={'entity_type':'order_item','entity_id':'11','operation':'upsert','order_id':'1','order_store_id':'10',
           'payload_json':json.dumps({'order_item':{'sub_order_id':'11','order_id':'1','original_order_date':'2026-08-19','cost_company_id':'99'}})}
    out=OrderFeed._overlay(items,[delta],'order_item','sub_order_id',lambda p:[p['order_item']])
    assert str(out['original_order_date'].item())=='2026-08-19'
    assert out['cost_company_id'].item()=='99'
