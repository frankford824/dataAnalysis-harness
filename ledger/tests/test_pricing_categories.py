import csv
import io
import json
import polars as pl
import pytest

from ledger import pricing_gaps, cost_lines
from ledger.storage_integrity import seal
from ledger.workspace import Workspace
from ledger.pricing_categories import categorized
from test_cost_lines import payload, archive
from test_pdd_historical_pricing import scenario


@pytest.mark.parametrize('source,basis,category,phrase',[
    ('register','change_log','register_evidence','原始调价凭据'),
    ('register','change_log_prior','register_evidence','不能证明'),
    ('register_first','first','register_evidence','晚于下单日'),
    ('history_carry',None,'history_carry','沿用较早日期'),
])
def test_reference_price_details_do_not_admit_unverified_cost(source,basis,category,phrase):
    _,r=scenario(cost_source=source,internal_sub_order_id='12345678901234567890',
        pricing_evidence=json.dumps({'order_date':'2026-06-05','register_basis':basis,'cost_company_id':'99','begin_date':'2026-06-01','end_date':'2026-06-02'}))
    gap=r.pricing_gaps.row(0,named=True)
    assert gap['category']==category and phrase in gap['reason']
    assert gap['internal_sub_order_id']=='12345678901234567890'
    assert gap['cost_company_id']=='99' and gap['price_end_date']=='2026-06-02'
    assert r.facts.filter(pl.col('counted')).is_empty()


def test_legacy_saved_gaps_remain_searchable_without_new_columns(tmp_path):
    p=tmp_path/'old.parquet'
    pl.DataFrame([dict(order_id='o',internal_order_id='1',sku='s',reason='登记价历史有效期未证实；保留参考，待核对',reference_unit_cost=2.)]).write_parquet(p)
    result=pricing_gaps.page(p,category='register_evidence')
    assert result['total']==1
    assert '历史有效期' in result['items'][0]['required_evidence']
    assert pricing_gaps.page(p,category='history_carry')['total']==0
    exported=list(csv.DictReader(io.StringIO(pricing_gaps.csv(p))))
    assert len(exported)==1 and exported[0]['还缺什么依据']==result['items'][0]['required_evidence']


def test_gap_categories_count_unique_keys_and_keep_mixed_and_unmatched(tmp_path):
    ws=Workspace(tmp_path);raw=payload();run=ws.record(raw['store_id'],raw['period'],raw,[])
    archive(ws,run,keys=('S1','S2','S3'))
    p=ws.pricing_gaps_path(run)
    pl.DataFrame([
        {'coverage_key':'S1','metric_id':'goods_cost','reason':'登记价历史有效期未证实','cost_source':'register'},
        {'coverage_key':'S1','metric_id':'goods_cost','reason':'登记价历史有效期未证实','cost_source':'register'},
        {'coverage_key':'S2','metric_id':'goods_cost','reason':'登记价历史有效期未证实','cost_source':'register'},
        {'coverage_key':'S2','metric_id':'goods_cost','reason':'缺少已核实的下单日历史成本','cost_source':None},
        {'coverage_key':'S3','metric_id':'reshipment_cost','reason':'登记价历史有效期未证实','cost_source':'register'},
    ]).write_parquet(p);seal(p)
    result=cost_lines.page(ws,run,raw['store_id'],raw['period'])
    assert {r['category']:r['count'] for r in result['category_counts']}=={'register_evidence':1,'mixed':1,'unmatched':1}
    filtered=cost_lines.page(ws,run,raw['store_id'],raw['period'],category='mixed')
    assert filtered['total']==3 and filtered['matching']==1 and filtered['items'][0]['coverage_key']=='S2'
    ws.close()


def test_source_label_does_not_hide_identity_risk_or_conflicting_prices():
    frame=pl.DataFrame([
        {'reason':'订单商品与成本明细不一致，等待更新','cost_source':'register'},
        {'reason':'成本与订单金额差异较大，需核对','cost_source':'history_carry'},
        {'reason':'同一日期存在多个历史成本价，请核对价格依据','cost_source':'history'},
    ])
    assert categorized(frame.lazy()).collect()['category'].to_list()==['identity','amount','price_conflict']


def test_evidence_export_preserves_numeric_sku_identifiers(tmp_path):
    path=tmp_path/'sku.parquet'
    pl.DataFrame([dict(order_id='o',internal_order_id='1',sku='12345678901234567890',
        reason='缺少已核实的下单日历史成本',reference_unit_cost=None)]).write_parquet(path)
    row=next(csv.DictReader(io.StringIO(pricing_gaps.csv(path))))
    assert row['商品编码']=='="12345678901234567890"'
    assert row['参考单价（未计入）']==''
