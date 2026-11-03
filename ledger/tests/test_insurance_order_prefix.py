from datetime import datetime
import csv
import pytest
import polars as pl
from conftest import MODELS
from ledger.model.loader import load_model
from ledger.engine.runtime import ingest,run,Ingestion
from ledger.engine.link import link,Spine,LINK_KEY,LINKED,SPINE_STORE,SPINE_PERIOD,SPINE_PRODUCT
from ledger.order_feed import OrderFeed
from ledger.model.schema import StatementNode

ORDER='6955289481845544427'

@pytest.mark.parametrize('template_id',['insurance_douyin_v1','insurance_douyin_v2'])
def test_prefix_is_narrow_and_preserves_raw_identifiers(template_id):
    model=load_model(MODELS/'cn-ecommerce');template=next(t for t in model.templates if t.id==template_id)
    metric=model.metric('insurance_cost_douyin').for_platform('douyin')
    values=[','+ORDER,ORDER,',,'+ORDER,'X,'+ORDER,ORDER+',','1,234',',123','6953149768301877000A00']
    frame=pl.DataFrame({'sub_order_id':values,'total_cost':[.32]*len(values)})
    spine=Spine(pl.DataFrame({'sub_order_id':[ORDER],SPINE_STORE:['shop'],SPINE_PERIOD:['2026-08'],SPINE_PRODUCT:['p']}))
    linked,_=link(frame,metric,spine,template)
    assert linked[LINK_KEY].to_list()==[ORDER,ORDER,*values[2:]]
    assert linked[LINKED].to_list()==[True,True,*([False]*6)]
    assert linked['sub_order_id'].to_list()==values

def test_real_csv_shape_books_each_policy_once_with_shared_payment(tmp_path):
    base=load_model(MODELS/'cn-ecommerce');store=next(s for s in base.stores if s.platform=='douyin')
    model=base.model_copy(update={'stores':(store,),'metrics':(base.metric('insurance_cost_douyin'),),
        'statement':(StatementNode(id='insurance',name='权益保险',formula={'op':'add','of':['insurance_cost_douyin']}),)})
    path=tmp_path/('权益保险-'+store.name+'-2608月.csv')
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['投保单号','订单编号','下单时间','动账时间','动账流水号','保险名称','支付保费','备注'])
        for n in range(2):writer.writerow([f',policy-{n}',','+str(int(ORDER)+n),'2026-08-19','2026-08-19',',shared-payment','退换货运费险','.32',''])
    original=path.read_bytes();received=ingest([path],model,[store.name])
    orders=pl.DataFrame({'order_id':[str(int(ORDER)+n) for n in range(2)],'sub_order_id':[str(int(ORDER)+n) for n in range(2)],
        'product_id':['p1','p2'],'store_name':[store.name]*2,'order_date':[datetime(2026,8,19)]*2,'order_time':[datetime(2026,8,19)]*2,'quantity':[1.,1.]})
    received.items.append(OrderFeed._item('order_detail','order_console_order_v1','orders',orders,OrderFeed._order_template(),'test'))
    result=run(received,'douyin');facts=result.facts.filter(pl.col('metric_id')=='insurance_cost_douyin')
    assert facts.height==2 and facts['counted'].all()
    assert facts['contribution'].sum()==pytest.approx(-.64)
    assert result.slices[(store.name,'2026-08')].nodes['insurance'].value==pytest.approx(-.64)
    assert path.read_bytes()==original
