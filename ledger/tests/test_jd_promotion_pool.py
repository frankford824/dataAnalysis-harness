import polars as pl
import pytest
from conftest import MODELS
from ledger.model.loader import load_model
from ledger.model.schema import Model,Metric,Store,SourceContract,StatementNode,Template,ColumnBinding
from ledger.engine.runtime import Ingestion,run
from ledger import view
from test_promotion_dates import ingested


@pytest.mark.parametrize('day',['20260719','2026-07-19','20260701~20260731','20260701至20260731'])
def test_jd_unmatched_daily_or_monthly_spend_joins_only_current_month_pool(day,tmp_path):
    builtin=load_model(MODELS/'cn-ecommerce')
    model=Model(id='jd-test',name='test',stores=(Store(id='s',name='shop',platform='jd'),),
        sources=(SourceContract(id='order_detail',name='订单',is_spine=True,owner_role='operations',cadence='monthly'),
                 SourceContract(id='promotion',name='推广',owner_role='operations',cadence='monthly',required_for_close=False)),
        metrics=(builtin.metric('ad_cost'),Metric(id='sales',name='订单金额',source='order_detail',value={'op':'sum','of':['buyer_paid']})),
        statement=(StatementNode(id='ad',name='推广费用',formula={'op':'add','of':['ad_cost']}),))
    fields=['order_id','product_id','store_name','order_time','buyer_paid']
    template=Template(id='jd_original',source='order_detail',match_columns=('order_id',),
        time_slots={'order_date':'order_time'},bindings=tuple(ColumnBinding(role=f,columns=(f,)) for f in fields))
    orders=ingested(template,fields,[['A','MATCH','shop','2026-06-01',10],
        ['B','MATCH','shop','2026-07-01',10],['C','MATCH','shop','2026-07-02',100],
        ['D','OTHER','shop','2026-07-03',10],['E','LATER','shop','2026-08-01',10]])
    promo=ingested(builtin.template('promotion_jd_v1'),['跟单SKU ID','跟单SKU名称','花费','日期','展现数','点击数'],
        [['MATCH','已匹配',30,day,100,10],['LATER','仅次月有订单',6,day,10,1],['UNKNOWN','未匹配',.02,day,1,1]])
    promo.frame=promo.frame.with_columns(pl.lit('shop').alias('__hint_store__'))
    result=run(Ingestion(model=model,items=[orders,promo]),'jd')
    sl=result.slice('shop','2026-07')
    assert sl.nodes['ad'].available and sl.nodes['ad'].value==-36.02
    facts=sl.facts.filter(pl.col('metric_id')=='ad_cost')
    assert facts['counted'].all() and facts.filter(pl.col('link_key')=='MATCH')['contribution'].item()==-30
    pool=facts.filter(pl.col('booking_status')=='store_wide')
    assert pool.height==2 and pool['contribution'].sum()==pytest.approx(-6.02)
    projected=result.spine_facts.filter((pl.col('metric_id')=='ad_cost') & (pl.col('link_key')=='__store_wide__'))
    assert projected.height==3 and projected['period'].unique().to_list()==['2026-07']
    assert projected['amount'].sum()==pytest.approx(-6.02)
    path=tmp_path/'facts.parquet';sl.facts.write_parquet(path)
    d=view.drill(path,model,'ad',only='allocated')
    assert d['rows']==2 and d['uncounted']['rows']==0 and d['source_total']==-6.02
    assert '全店分摊已计入' in view.fees_csv(path,model)
