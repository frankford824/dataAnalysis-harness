"""Subcent conservation uses real saved runs and sealed evidence, never rounded-row oracles."""
import csv
import io
from decimal import Decimal
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.operations_api import install
from ledger.operations_readmodel import read_month, keep_projection, comparison
from ledger.storage_integrity import seal, verified
from operations_fixture import create_fixture


def subcent_fixture(root, *, store_count=1, orders=100, freight=Decimal('.004')):
    ws, model, registry, _ = create_fixture(root, overhead=0)
    model = model.model_copy(update={'stores':model.stores[:store_count]})
    for store in model.stores:
        payload = ws.state(store.id, '2030-01').result
        for row in payload['statement']:
            row['value'] = orders if row['id']=='n_receipt' else -float(freight*orders) if row['id']=='n_freight' else 0
        run = ws.record(store.id, '2030-01', payload, [], input_fingerprint='sealed-subcent-regression')
        rows = [{'metric_id':metric, 'amount':amount, 'source_id':'fixture', 'store':store.name,
            'period':'2030-01', 'spine_row':index, 'product_id':f'p{index}', 'order_id':f'{store.id}-{index}',
            'sub_order_id':f'{store.id}-c{index}', 'link_key':f'{store.id}-{index}'}
            for index in range(orders) for metric,amount in [('trade_receipt',1.),('freight_cost',-float(freight))]]
        frame = pl.from_dicts(rows)
        keep_projection(ws, run, SimpleNamespace(operations_evidence=frame))
        raw = frame.with_columns(pl.lit(True).alias('counted'), pl.col('amount').alias('contribution'))
        raw.write_parquet(ws.facts_path(run)); seal(ws.facts_path(run))
        assert verified(ws.facts_path(run).with_suffix('.operations.parquet'))
    app = FastAPI(); install(app, lambda:ws, lambda:model)
    return ws, model, TestClient(app)


def test_hundred_subcent_orders_survive_every_grain_export_and_display_note(tmp_path):
    ws, model, client = subcent_fixture(tmp_path)
    before = ws.state('s1','2030-01').result
    for level in ('store','product','order','person','team'):
        report = read_month(ws, model, '2030-01', level=level, store_id='s1')
        assert report['summary']['freight_cost']=='0.40'
        assert report['summary']['freight_rate']=='0.00400000'
        assert sum(Decimal(row['metrics']['freight_cost']) for row in report['rows'])==Decimal('.400')
        if level in ('product','order'):
            assert len(report['rows'])==100
            assert {row['metrics']['freight_cost'] for row in report['rows']}=={'0.004000'}
            assert report['display_rounding']['freight_cost']=={
                'label':'发货运费','displayed_rows_total':'0.00','displayed_scope_total':'0.40','difference':'0.40'}
    response = client.get('/api/operations/month',params={'period':'2030-01','level':'order','store_id':'s1','limit':200})
    assert response.status_code==200
    assert response.json()['summary']['freight_cost']=='0.40'
    export = client.get('/api/operations/export.csv',params={'period':'2030-01','level':'order','store_id':'s1'})
    assert export.status_code==200
    rows = list(csv.DictReader(io.StringIO(export.text.lstrip('\ufeff'))))
    assert len(rows)==100
    assert sum(Decimal(row['发货运费']) for row in rows)==Decimal('.400')
    assert {row['发货运费'] for row in rows}=={'0.004000'}
    assert '显示尾差0.40' in rows[0]['全范围两位显示尾差（不计金额）']
    assert ws.state('s1','2030-01').result==before
    ws.close()


def test_two_half_cent_stores_aggregate_before_display_rounding(tmp_path):
    ws, model, client = subcent_fixture(tmp_path,store_count=2,orders=1,freight=Decimal('.005'))
    for level in ('store','product','person','team'):
        report = read_month(ws,model,'2030-01',level=level)
        assert report['summary']['freight_cost']=='0.01'
        assert sum(Decimal(row['metrics']['freight_cost']) for row in report['rows'])==Decimal('.010')
    report = client.get('/api/operations/month',params={'period':'2030-01'}).json()
    assert {row['metrics']['freight_cost'] for row in report['rows']}=={'0.005000'}
    assert report['display_rounding']['freight_cost']['displayed_rows_total']=='0.02'
    assert report['display_rounding']['freight_cost']['difference']=='-0.01'
    rows = list(csv.DictReader(io.StringIO(client.get('/api/operations/export.csv?period=2030-01').text.lstrip('\ufeff'))))
    assert sum(Decimal(row['发货运费']) for row in rows)==Decimal('.010')
    assert comparison(Decimal('.009'),Decimal('.005'))['delta']=='0.004000'
    ws.close()
