import polars as pl
import pytest
from conftest import MODELS
from ledger.model.loader import load_model
from ledger.engine.link import Spine, SPINE_STORE, SPINE_PERIOD, SPINE_PRODUCT, SPINE_ORIGIN
from ledger.engine.normalize import STORE_WIDE_PRODUCT
from ledger.engine.runtime import _project_scoped_live, _mark_counted


def sample(multi=False):
    metric=load_model(MODELS/'cn-ecommerce').metric('ad_cost').for_platform('pdd')
    spine=Spine(pl.DataFrame({SPINE_STORE:['s']*2,SPINE_PERIOD:['2026-06','2026-07'],
        SPINE_PRODUCT:['p']*2,SPINE_ORIGIN:['order_detail_file']*2,'order_id':['a','b'],'sub_order_id':['a','b']}))
    names=['推广_20260601至20260630.xlsx','推广_20260701至20260731.xlsx']
    rows=[{'metric_id':'ad_cost','source_id':'promotion','store':'s','period':period,'link_key':key,
           'amount':amount,'file_name':name,'major':None} for period,name in zip(['2026-06','2026-07'],names)
          for key,amount in [('p',-40. if period=='2026-06' else -10.),(STORE_WIDE_PRODUCT,-100. if period=='2026-06' else -50.)]]
    if multi:
        rows=[r for r in rows if r['link_key']!=STORE_WIDE_PRODUCT]
        rows.append({**rows[0],'link_key':STORE_WIDE_PRODUCT,'amount':-150.,'file_name':'推广_20260601至20260731.xlsx'})
    return metric,spine,pl.from_dicts(rows)


@pytest.mark.parametrize('reverse',[False,True])
def test_separate_monthly_exports_keep_their_own_control_totals(reverse):
    metric,spine,source=sample()
    if reverse:source=source.reverse()
    projected=_project_scoped_live(source,metric,spine).facts
    totals=projected.group_by('period').agg(pl.col('amount').sum()).sort('period')
    assert totals['amount'].to_list()==pytest.approx([-100.,-50.])
    marked=_mark_counted(source,projected,[metric])
    for month in ['06','07']:
        rows=marked.filter(pl.col('file_name').str.contains('2026'+month+'01'))
        assert rows['period'].unique().to_list()==['2026-'+month]
        assert rows['contribution'].sum()==pytest.approx(-100 if month=='06' else -50)


def test_one_export_covering_two_months_retains_its_full_window():
    metric,spine,source=sample(True)
    projected=_project_scoped_live(source,metric,spine).facts
    assert projected['amount'].sum()==pytest.approx(-150)
    marked=_mark_counted(source,projected,[metric])
    assert set(marked.filter(pl.col('link_key')==STORE_WIDE_PRODUCT)['period'])=={'2026-06','2026-07'}
    assert marked['contribution'].sum()==pytest.approx(-150)
