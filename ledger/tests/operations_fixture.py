"""Small persisted business corpus shared by API and real-browser integration tests."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json

import polars as pl

from ledger.commission_registry import Registry
from ledger.model.loader import load_model
from ledger.model.schema import Overhead
from ledger.operations_readmodel import keep_projection
from ledger.storage_integrity import seal
from ledger.workspace import Workspace


def create_fixture(root, *, overhead=None, engine_can_close=False):
    base=load_model(Path(__file__).resolve().parents[2]/'models/cn-ecommerce')
    stores=tuple(base.stores[0].model_copy(update={'id':sid,'name':name,'platform':'taobao','archived':False,'aliases':()})
                 for sid,name in [('s1','经营测试店甲'),('s2','经营测试店乙')])
    model=base.model_copy(update={'stores':stores,'overheads':tuple(Overhead(period=p,amount=overhead) for p in ('2030-01','2029-12','2029-01')) if overhead is not None else ()})
    ws=Workspace(root)
    registry=Registry(root)
    with registry.transaction() as conn:
        for pid,name,parent in [('team','经营测试团队',''),('doer','做货甲','team'),('cut','抽点乙','team')]:
            conn.execute('INSERT INTO person(id,name,parent_id) VALUES(?,?,?)',(pid,name,parent))
    run_ids={}
    for period in ('2030-01','2029-12','2029-01'):
        for store in stores:
            receipts=100 if store.id=='s1' else 300
            if period!='2030-01':receipts//=2
            amounts={'trade_receipt':receipts,'trade_refund':-receipts/5,'goods_cost':-30,'dropship_cost':-5,'reshipment_cost':-5,
                     'ad_cost':-10,'software_fee':-5,'freight_cost':-3,'brushing_cost':-2,'small_payment_cost':-4}
            nodes={'n_receipt':receipts,'n_refund':-receipts/5,'n_goods':-30,'n_dropship':-5,'n_reshipment':-5,
                   'n_ad':-10,'g_platform':-5,'n_freight':-3,'n_brushing':-2,'n_small_payment':-4}
            calc_id=f'operations-fixture-{period}-{store.id}'
            payload={'store':store.name,'period':period,'can_close':engine_can_close,'findings':[],
                     'statement':[{'id':key,'value':value,'available':True} for key,value in nodes.items()],
                     'commission':{'calculation_id':calc_id,'total':3,'amount_complete':True},
                     'cost_coverage':{'expected':1,'covered':1,'uncovered':0,'passed':True},'cost_review':{'requires_human':False}}
            run=ws.record(store.id,period,payload,[],input_fingerprint=f'fixture-{period}-{store.id}')
            run_ids[period,store.id]=run
            projected=pl.DataFrame([{'metric_id':key,'source_id':'fixture','store':store.name,'period':period,'amount':float(amount),
                 'factor':1.,'spine_row':0,'product_id':'100000000001','product_name':'经营测试宝贝','order_id':f'{store.id}-order',
                 'sub_order_id':f'{store.id}-child','link_key':f'{store.id}-order'} for key,amount in amounts.items()],schema_overrides={'spine_row':pl.UInt32})
            keep_projection(ws,run,SimpleNamespace(operations_evidence=projected))
            raw=projected.with_row_index('row_no',offset=2).with_columns(pl.col('amount').alias('contribution'),pl.lit(True).alias('counted'),pl.lit(True).alias('linked'),
                pl.lit('经营测试原表.csv').alias('file_name'),pl.lit('原始流水').alias('sheet'),pl.lit('1'*64).alias('file_sha'),pl.lit('本地合成业务语料').alias('source_note'))
            raw.write_parquet(ws.facts_path(run));seal(ws.facts_path(run))
            details=pl.DataFrame([{'spine_row':0,'product_id':'100000000001','person_id':pid,'person':name,'share':share,'total_rate':'0.03',
                 'duty':duty,'managed':store.id=='s2','status':'distribute','amount':amount,'finance_run':run}
                 for pid,name,share,duty,amount in [('doer','做货甲','0.01','produce',1.),('cut','抽点乙','0.02','cut',2.)]],schema_overrides={'spine_row':pl.UInt32})
            target=registry.root/'calculations'/f'{calc_id}.parquet';target.parent.mkdir(exist_ok=True);details.write_parquet(target)
            sha=hashlib.sha256(target.read_bytes()).hexdigest()
            with registry.transaction() as conn:
                conn.execute('INSERT INTO calculation(id,finance_run,store_id,period,at,registry_revision,path,sha,model_json,rules_json,summary_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (calc_id,run,store.id,period,'2030-02-01T00:00:00',0,target.name,sha,model.model_dump_json(),'{}',json.dumps(payload['commission'])))
    return ws,model,registry,run_ids
