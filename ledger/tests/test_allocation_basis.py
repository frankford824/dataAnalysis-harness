import polars as pl
import pytest

from ledger.engine.allocation import enrich
from ledger.engine.link import Spine
from ledger.engine.project import project,project_transactions
from ledger.model.schema import Allocation,LinkRule,Metric,ValueExpr


def metric():
    return Metric(id='trade_receipt',name='收入',source='settlement',
        value=ValueExpr(op='sum',of=['amount']),allocate=Allocation(mode='ratio',by='alloc_ratio'),
        link=LinkRule(key='order_id',to='order.order_id',grain='order',prefer_exported_orders=True))


def source(value=101.76):
    return pl.DataFrame({'metric_id':['trade_receipt'],'link_key':['master'],'amount':[value],
                         'store':['s'],'period':['2026-06']})


def spine(paid,ratios=None,refunds=None):
    return pl.DataFrame({'store':['s']*len(paid),'period':['2026-06']*len(paid),'order_id':['master']*len(paid),
        'sub_order_id':[str(i) for i in range(len(paid))],'product_id':[f'p{i}' for i in range(len(paid))],
        'buyer_paid':paid,'refund_amount':refunds if refunds is not None else [0.]*len(paid),
        'alloc_ratio':ratios if ratios is not None else [None]*len(paid),
        '__spine_origin__':['order_detail_file']*len(paid)})


def test_exact_feed_enrichment_preserves_platform_rows_and_zero_gifts():
    platform=spine([None]*7)
    feed=spine([17.15,14.73,14.74,55.14,0,0,0]).with_columns(pl.lit('order_console').alias('__spine_origin__'))
    combined=pl.concat([platform,feed],how='vertical_relaxed')
    enriched=enrich(combined)
    assert enriched.height==combined.height
    result=project(source(),metric(),Spine(enriched))
    assert not result.allocation_pending
    assert result.facts['amount'].to_list()==[17.15,14.73,14.74,55.14]
    assert round(result.facts['amount'].sum(),2)==101.76
    assert enriched.filter(pl.col('__spine_origin__')=='order_detail_file')['buyer_paid'].to_list()==[17.15,14.73,14.74,55.14,0,0,0]


@pytest.mark.parametrize('change',['store','product','ambiguous'])
def test_enrichment_rejects_mismatched_or_conflicting_source(change):
    platform=spine([None,None]);feed=spine([70.,30.]).with_columns(pl.lit('order_console').alias('__spine_origin__'))
    if change=='store':feed=feed.with_columns(pl.lit('other').alias('store'))
    if change=='product':feed=feed.with_columns(pl.lit('other').alias('product_id'))
    if change=='ambiguous':feed=pl.concat([feed,feed.with_columns((pl.col('buyer_paid')+1).alias('buyer_paid'))])
    combined=Spine(enrich(pl.concat([platform,feed],how='vertical_relaxed')))
    if change=='store':
        with pytest.raises(ValueError,match='跨店或跨期'):project(source(100),metric(),combined)
        return
    result=project(source(100),metric(),combined)
    assert result.allocation_pending
    assert result.facts['spine_row'].null_count()==result.facts.height
    assert result.facts['amount'].sum()==100


@pytest.mark.parametrize('paid,ratios,refunds',[
    ([None,None],None,None),([70.,None],None,None),([0.,0.],None,None),
    ([70.,30.],[1.,None],None),([70.,30.],[1.2,-.2],None),
    ([70.,30.],[.4,.4],None),([70.,30.],None,[70.,30.]),
    ([70.,30.],None,[None,0.]),
])
def test_missing_invalid_zero_basis_keeps_store_cash_without_person_allocation(paid,ratios,refunds):
    result=project(source(),metric(),Spine(spine(paid,ratios,refunds)))
    assert len(result.allocation_pending)==1
    assert result.facts['amount'].sum()==101.76
    assert result.facts['spine_row'].to_list()==[None]
    assert result.allocation_pending[0]['store']=='s'


def test_transaction_refund_does_not_reenable_even_fallback():
    m=metric().model_copy(update={'posting_basis':'transaction'})
    result=project_transactions(source(-100),m,Spine(spine([None,None])))
    assert result.facts['amount'].sum()==-100
    assert result.facts['spine_row'].to_list()==[None]
    assert len(result.allocation_pending)==1


def test_declared_zero_and_complete_ratios_do_not_need_payment_fallback():
    result=project(source(),metric(),Spine(spine([None,None],[1.,0.])))
    assert not result.allocation_pending
    assert result.facts['amount'].to_list()==[101.76]


def test_duplicate_children_are_pending_not_counted_twice():
    frame=spine([70.,30.]).with_columns(pl.lit('same').alias('sub_order_id'))
    result=project(source(),metric(),Spine(frame))
    assert result.allocation_pending[0]['reason']=='missing_or_duplicate_child'
    assert result.facts['amount'].sum()==101.76


def test_proven_single_platform_child_is_direct_not_even_fallback():
    frame=spine([None]).with_columns(pl.lit('master').alias('sub_order_id'))
    result=project(source(),metric(),Spine(frame))
    assert not result.allocation_pending
    assert result.facts['amount'].to_list()==[101.76]
    assert result.facts['spine_row'].to_list()==[0]


def test_offset_source_keeps_counted_zero_without_inventing_child_ownership():
    result=project(source(0.),metric(),Spine(spine([None,None])))
    assert not result.allocation_pending
    assert result.facts['amount'].to_list()==[0.]
    assert result.facts['spine_row'].to_list()==[None]
