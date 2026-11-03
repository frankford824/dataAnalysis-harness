import csv
from datetime import datetime
import pytest
import polars as pl
from conftest import MODELS
from ledger.model.loader import load_model
from ledger.engine.runtime import ingest,run
from ledger.engine.identifier_extraction import apply
from ledger.order_feed import OrderFeed
from ledger.model.schema import StatementNode

ORDER='6954817248638342942'
OTHER='6954785668155315283'
SCENE='上门取件-支付快递费'

@pytest.mark.parametrize('template_id',['douyin_settlement_v1','douyin_settlement_v2'])
def test_only_declared_scene_uses_unique_remark_order(template_id):
    model=load_model(MODELS/'cn-ecommerce');template=next(t for t in model.templates if t.id==template_id)
    remarks=[f'因商品存在问题-订单{ORDER}-售后单147597547561566208-运单73719849772840',
             f'订单{ORDER}-订单{OTHER}',f'售后单{ORDER}-运单{OTHER}',f'订单{ORDER}0',None,
             f'订单{ORDER}-订单{ORDER}',f'订单{ORDER}',f'订单： {ORDER}',f'售后订单{ORDER}']
    frame=pl.DataFrame({'subject':[SCENE]*6+['其他费用',SCENE,SCENE],
        'base_order_id':['tracking']*len(remarks),'sub_order_id':['child']*len(remarks),'remark':remarks,'income':[-5.1]*len(remarks)})
    result=apply(frame,template,[])
    assert result['base_order_id'].to_list()==[ORDER,None,None,None,None,ORDER,'tracking',ORDER,None]
    assert result['__raw_base_order_id'].to_list()==['tracking']*len(remarks)
    assert result.select('sub_order_id','remark','income').equals(frame.select('sub_order_id','remark','income'))

def test_pickup_csv_books_only_the_remark_order_and_keeps_settlement_date(tmp_path):
    model=load_model(MODELS/'cn-ecommerce');store=model.store('douyin_qianhuajian')
    model=model.model_copy(update={'stores':(store,),'statement':(StatementNode(id='all',name='all',formula={'op':'add','of':['logistics_fee_douyin','cross_border_fee_douyin','trade_compensation_douyin','software_fee_douyin']}),)})
    path=tmp_path/('对账-'+store.name+'-2608月.csv')
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['动账流水号','关联订单号','关联子订单号','动账时间','账户方向','动账金额(元)','动账场景','账户余额(元)','备注'])
        w.writerow(['txn','73719849772840','wrong-child','2026-08-01 06:31:40','支出','-5.10',SCENE,'0',f'赔付-订单{ORDER}-售后单147597547561566208-运单73719849772840'])
    original=path.read_bytes();received=ingest([path],model,[store.name])
    orders=pl.DataFrame({'order_id':[ORDER],'sub_order_id':[ORDER],'product_id':['p'],'store_name':[store.name],
        'order_type':['销售订单'],'order_date':[datetime(2026,8,1)],'order_time':[datetime(2026,8,1)],'quantity':[1.],'alloc_ratio':[1.]})
    received.items.append(OrderFeed._item('order_detail','order_console_order_v1','orders',orders,OrderFeed._order_template(),'test'))
    result=run(received,'douyin');counted=result.facts.filter(pl.col('counted'))
    assert counted.height==1 and counted['link_key'].item()==ORDER
    assert counted['contribution'].sum()==pytest.approx(-5.1)
    assert counted['period'].item()=='2026-08'
    assert path.read_bytes()==original
