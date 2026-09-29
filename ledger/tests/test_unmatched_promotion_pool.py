import csv,io
import polars as pl
import pytest
from ledger import view
from ledger.engine.runtime import run
from ledger.engine.link import Spine
from ledger.engine.project import Projection
from ledger.engine.promotion_allocation import allocate
from ledger.model.loader import load_model
from ledger.model.schema import Allocation,Metric,LinkRule
from conftest import MODELS
from test_promotion_dates import calculate


def test_only_authorized_platforms_enable_store_pool():
    m=load_model(MODELS/'cn-ecommerce').metric('ad_cost')
    assert m.for_platform('taobao').allocate.unmatched=='store_wide'
    assert m.for_platform('douyin').allocate.unmatched=='store_wide'
    assert m.for_platform('alibaba1688').allocate.unmatched=='store_wide'
    assert m.for_platform('pdd').allocate.unmatched=='pending'
    assert m.for_platform('jd').allocate.unmatched=='store_wide'
    with pytest.raises(ValueError):Allocation(mode='ratio',by='alloc_ratio',unmatched='store_wide')
    with pytest.raises(ValueError):Metric(id='revenue',name='收入',source='settlement',value={'op':'sum','of':['amount']},
        link=LinkRule(key='product_id',to='order.product_id',grain='product'),allocate=Allocation(mode='even',unmatched='store_wide'))


@pytest.mark.parametrize('live',[False,True])
def test_unmatched_product_stays_in_its_spend_month_and_pool(live,tmp_path):
    result=calculate([['P1',10,'2026-06-01'],['P2',20,'2026-06-02'],
                      ['UNKNOWN',.02,'2026-07-19'],['P1',30,'2026-08-01']],live=live,unmatched='store_wide')
    june=result.slice('shop','2026-06');july=result.slice('shop','2026-07')
    assert june.nodes['ad'].value==-30 and july.nodes['ad'].value==-.02
    extra=july.facts.filter(pl.col('booking_status')=='store_wide')
    assert extra.height==1 and extra['link_key'].item()=='UNKNOWN'
    assert extra['counted'].item() and extra['contribution'].item()==-.02
    assert extra['file_sha'].item() and extra['row_no'].item()==4
    assert july.nodes['ad'].available
    projected=result.spine_facts.filter((pl.col('metric_id')=='ad_cost') & (pl.col('period')=='2026-07'))
    assert projected['amount'].sum()==pytest.approx(-.02)
    target_ids=set(result.spine.with_row_index('spine_row').filter(pl.col('period')=='2026-07')['spine_row'].to_list())
    assert set(projected['spine_row'].to_list())<=target_ids
    path=tmp_path/'facts.parquet';july.facts.write_parquet(path)
    for mode in ['counted','uncounted','allocated','all']:
        assert view.drill(path,result.model,'ad',only=mode)==view.drill(july.facts,result.model,'ad',only=mode)
    d=view.drill(path,result.model,'ad',only='allocated')
    assert d['rows']==1 and d['uncounted']['rows']==0 and d['source_total']==-.02
    rows=list(csv.DictReader(io.StringIO(view.fees_csv(path,result.model))))
    row=next(r for r in rows if r.get('入账路径')=='全店分摊入账')
    assert row['入账路径']=='全店分摊入账' and float(row['进账'])==-.02


def test_independent_monthly_control_already_includes_orphans():
    result=calculate([['P1',10,'2026-07-01'],['P2',20,'2026-07-01'],['-',40,'总计']],platform='pdd',unmatched='store_wide')
    sl=result.slice('shop','2026-07')
    assert sl.nodes['ad'].value==-40
    assert sl.facts.filter(pl.col('booking_status')=='store_wide').is_empty()
    assert sl.facts.filter(pl.col('booking_status')=='allocated').height==1


def test_no_current_month_order_targets_stays_pending():
    result=calculate([['UNKNOWN',20,'2026-09-01']],unmatched='store_wide')
    assert result.facts.filter(pl.col('metric_id')=='ad_cost')['counted'].sum()==0
    assert result.projections['ad_cost'].store_wide_evidence.is_empty()


@pytest.mark.parametrize('amount',[.0001,-.02])
def test_small_cost_and_refund_keep_exact_contribution(amount):
    result=calculate([['UNKNOWN',amount,'2026-07-01']],unmatched='store_wide')
    fact=result.facts.filter(pl.col('metric_id')=='ad_cost')
    assert fact['contribution'].sum()==pytest.approx(-amount)
    assert fact['counted'].all()


def test_absent_source_period_cannot_create_store_allocation():
    result=calculate([['UNKNOWN',20]],dated=False,unmatched='pending')
    # Even when another layer assigns a viewing period, no certified source
    # period means this new policy cannot silently allocate the amount.
    source=result.facts.with_columns(pl.lit(None,dtype=pl.String).alias('source_period'))
    m=result.model.metric('ad_cost');m=m.model_copy(update={'allocate':Allocation(mode='even',unmatched='store_wide')})
    p=allocate(source,m,Spine(result.spine),Projection(facts=result.spine_facts))
    assert p.store_wide_evidence.is_empty()


def test_original_order_rows_are_preferred_over_live_additions():
    result=calculate([['UNKNOWN',10,'2026-07-01']],unmatched='pending')
    m=result.model.metric('ad_cost');m=m.model_copy(update={'allocate':Allocation(mode='even',unmatched='store_wide')})
    targets=result.spine.with_row_index('spine_row').filter(pl.col('period')=='2026-07')
    targets=targets.with_columns(pl.lit('order_detail_file').alias('__spine_origin__'))
    extra=targets.with_columns(pl.lit('order_console').alias('__spine_origin__'),(pl.col('spine_row')+100).alias('spine_row'))
    source=result.facts.filter(pl.col('metric_id')=='ad_cost')
    p=allocate(source,m,Spine(pl.concat([targets,extra])),Projection(facts=result.spine_facts.clear()))
    assert p.facts['amount'].sum()==-10
    assert p.facts['spine_row'].to_list()==targets['spine_row'].to_list()


def test_monthly_pool_does_not_expand_to_other_store():
    result=calculate([['UNKNOWN',10,'2026-07-01']],unmatched='pending')
    m=result.model.metric('ad_cost');m=m.model_copy(update={'allocate':Allocation(mode='even',unmatched='store_wide')})
    source=result.facts.filter(pl.col('metric_id')=='ad_cost')
    # The allocator itself must reject mixed targets even if a caller fails to scope them.
    targets=result.spine.with_row_index('spine_row').filter(pl.col('period')=='2026-07')
    other=targets.with_columns(pl.lit('other').alias('store'),(pl.col('spine_row')+100).alias('spine_row'))
    p=allocate(source,m,Spine(pl.concat([targets,other])),Projection(facts=result.spine_facts.clear()))
    assert set(p.facts['store'].to_list())<={'shop'}


def test_multiple_orphan_products_share_one_even_pool_not_cartesian_rows():
    result=calculate([['P1',30,'2026-08-01'],['UNKNOWN',20,'2026-08-01'],['UNKNOWN2',10,'2026-08-02']],unmatched='store_wide')
    posted=result.spine_facts.filter((pl.col('metric_id')=='ad_cost') & (pl.col('period')=='2026-08'))
    pool=posted.filter(pl.col('link_key')=='__store_wide__')
    assert pool.height==2 and pool['amount'].to_list()==[15*-1,15*-1]
    assert sorted(posted.group_by('spine_row').agg(pl.col('amount').sum())['amount'].to_list())==[-45.,-15.]
    original=result.facts.filter((pl.col('metric_id')=='ad_cost') & (pl.col('booking_status')=='store_wide'))
    assert original.height==2 and original['contribution'].sum()==-30
