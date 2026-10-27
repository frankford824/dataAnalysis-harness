"""Production member/day protocol with synthetic identifiers and full engine use."""
from datetime import date

import polars as pl
import pytest

from conftest import MODELS
from ledger.engine.runtime import Ingested,Ingestion,run
from ledger.engine.types import FileRef,Recognition
from ledger.model.loader import load_model
from ledger.model.schema import Metric,Model,StatementNode,Store
from ledger.order_feed import OrderFeed
from ledger.order_origin import original_parent
from test_order_feed import _fixture,_write

MASTER='1234567890123456789'
OTHER='2234567890123456789'


def parent(raw,header=MASTER,shop='10',exported=None,item_shop='10'):
    frame=pl.DataFrame({'raw':[raw],'header':[header],'shop':[shop],'child':['child'],'item_shop':[item_shop]})
    value,bound=original_parent(pl.col('raw'),pl.col('header'),pl.col('shop'),pl.col('child'),exported,item_shop=pl.col('item_shop'))
    return frame.select(value.alias('parent'),bound.alias('bound')).row(0)


def test_only_an_exact_same_store_member_can_supply_original_day_binding():
    assert parent('10:'+MASTER,MASTER+','+OTHER)==(MASTER,True)
    assert parent(MASTER,MASTER+','+OTHER)==(MASTER,True)
    assert parent('20:'+MASTER,MASTER+','+OTHER)==(MASTER+','+OTHER,False)
    assert parent('10:'+MASTER,MASTER+','+OTHER,item_shop='20')==(MASTER+','+OTHER,False)
    assert parent('10:'+MASTER,OTHER)==(OTHER,False)  # Existing header fallback is not a conflict certificate.


def test_certified_export_parent_does_not_authorize_a_conflicting_raw_member_day():
    assert parent('10:'+MASTER,MASTER+','+OTHER,exported={'child':OTHER})==(OTHER,False)


def frames(tmp_path,platform='taobao',day='2026-05-04',item_shop='10',raw=None):
    root=tmp_path/'feed';manifest=_fixture(root)
    def read(name):return pl.read_parquet(root/manifest['objects'][name]['path'])
    orders=read('orders.parquet').with_columns(pl.lit(MASTER+','+OTHER).alias('online_order_no'))
    items=read('order_items.parquet').with_columns(pl.lit(raw or '10:'+MASTER).alias('online_order_no'),
        pl.lit(item_shop).alias('order_store_id'),pl.lit(day).alias('original_order_date'))
    feed=OrderFeed(tmp_path/'ws');store=Store(id='taobao_test',name='synthetic',platform=platform)
    order=feed._order_frame(orders,items,read('after_sales.parquet').clear(),read('order_relations.parquet'),store,'synthetic')
    cost=feed._cost_frame(orders,items,read('order_costs.parquet'),read('order_relations.parquet'),store,'synthetic')
    return feed,store,order,cost


@pytest.mark.parametrize('platform',['taobao','douyin'])
def test_original_day_survives_template_and_engine_period_selection(tmp_path,platform):
    feed,store,orders,cost=frames(tmp_path,platform)
    assert orders['order_id'][0]==cost['order_id'][0]==MASTER
    assert orders['order_date'][0].date()==date(2026,5,4)
    assert orders['order_time'][0] is None
    template=feed._order_template()
    assert template.time_slots['order_date']=='order_date'
    builtin=load_model(MODELS/'cn-ecommerce')
    model=Model(id='origin-fixture',name='synthetic',stores=(store,),sources=(builtin.source('order_detail'),),
        metrics=(Metric(id='receipt',name='receipt',source='order_detail',value={'op':'sum','of':['buyer_paid']},
            link={'key':'sub_order_id','to':'order.sub_order_id','grain':'order'}),),
        statement=(StatementNode(id='sales',name='sales',formula={'op':'add','of':['receipt']}),))
    item=feed._item('order_detail','order_console_orders_v1','synthetic',orders,template,'synthetic')
    result=run(Ingestion(model=model,items=[item]),platform)
    assert result.slice(store.name,'2026-05').nodes['sales'].value==20
    assert (store.name,'2026-06') not in result.slices


def test_invalid_explicit_bound_original_day_is_unknown_instead_of_header_fallback(tmp_path):
    _,_,orders,_=frames(tmp_path,day='not-a-date')
    assert orders['order_date'][0] is None and orders['order_time'][0] is None


def test_wrong_store_member_cannot_promote_a_day_from_another_store(tmp_path):
    _,_,orders,cost=frames(tmp_path,item_shop='20')
    assert orders['order_id'][0]==cost['order_id'][0]==MASTER+','+OTHER
    assert orders['order_date'][0].date()==date(2026,6,2)


def test_same_day_header_clock_is_retained_but_does_not_prove_original_intraday_time(tmp_path):
    _,_,orders,_=frames(tmp_path,day='2026-06-02')
    assert orders['order_time'][0].hour==10
    # This test documents compatibility, not an oracle for an intraday payroll
    # change: the member supplied a day, never an independently proved clock.
    assert orders['order_date'][0].hour==0


def test_snapshot_append_forwards_certified_export_binding_to_both_order_and_cost(tmp_path):
    from datetime import datetime
    from ledger.model.schema import Template,ColumnBinding
    root=tmp_path/'feed';manifest=_fixture(root)
    orders=pl.read_parquet(root/manifest['objects']['orders.parquet']['path']).with_columns(
        pl.lit(MASTER+','+OTHER).alias('online_order_no'))
    items=pl.read_parquet(root/manifest['objects']['order_items.parquet']['path']).with_columns(
        pl.lit('10:'+MASTER).alias('online_order_no'),pl.lit('10').alias('order_store_id'),
        pl.lit('2026-05-04').alias('original_order_date'))
    for name,frame in [('orders.parquet',orders),('order_items.parquet',items)]:
        manifest['objects'][name]=_write(root,name,frame)
    store=Store(id='taobao_test',name='synthetic',platform='taobao')
    builtin=load_model(MODELS/'cn-ecommerce')
    model=Model(id='append-fixture',name='synthetic',stores=(store,),
        sources=tuple(builtin.source(name).model_copy(update={'provides':()}) for name in ('order_detail','order_cost','after_sales')))
    export=pl.DataFrame({'order_id':[OTHER],'sub_order_id':['S1'],'product_id':['P1'],'store_name':[store.name],
        'order_date':[datetime(2026,6,2)],'order_time':[datetime(2026,6,2,9)],'buyer_paid':[20.]})
    template=Template(id='synthetic_export',source='order_detail',match_columns=('order_id',),
        bindings=tuple(ColumnBinding(role=c,columns=(c,)) for c in export.columns),time_slots={'order_date':'order_time'})
    ref=FileRef('synthetic-export','orders.csv','sheet')
    item=Ingested(ref=ref,template=template,frame=export,rows=1,
        recognition=Recognition(ref=ref,signature=template.signature,header_count=len(export.columns),source_id='order_detail',template_id=template.id))
    ingestion=Ingestion(model=model,items=[item])
    feed=OrderFeed(tmp_path/'ws',feed_root=root)
    feed._install_snapshot(manifest,{'stores':[{'order_store_id':'10','ledger_store_id':store.id,'mapping_status':'confirmed'}]},
        {'healthy':True},source_revision=10,source_latest_seq=10,revision_etag='synthetic')
    feed.append_to(ingestion,store)
    cost=ingestion.frames_of('order_cost')[0].frame
    assert cost['order_id'][0]==cost['original_order_id'][0]==OTHER
    # Raw member MASTER disagrees with this original export. Its May day must
    # not be promoted to the export's certified OTHER member.
    assert ingestion.frames_of('order_detail')[0].frame['order_date'][0].date()==date(2026,6,2)
