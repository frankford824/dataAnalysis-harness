import hashlib
import json

import polars as pl
import pytest

from ledger.order_components import fingerprint, load
from test_component_returns import data
from ledger.engine.component_returns import normalize


def evidence(tmp_path):
    payload={'source':'jst_order_expanded_v1','order_id':'I','order_store_id':'10','captured_at':'2026-09-09T00:00:00Z',
        'bundles':[{'oi_id':'L','sku_id':'KIT','qty':2,'outer_oi_id':'S'}],
        'components':[{'o_id':'I','oi_id':'L','sku_id':sku,'qty':q,'src_combine_sku_id':'KIT','src_combine_sku_qty':2}
                      for sku,q in [('A',4),('B',6)]]}
    raw=json.dumps(payload);doc={'payload_json':raw,'sha256':hashlib.sha256(raw.encode()).hexdigest()}
    root=tmp_path/'order-components/shop';root.mkdir(parents=True)
    p=root/'I.json';p.write_text(json.dumps(doc));return p,doc


def test_historical_order_expansion_replaces_later_catalog_components(tmp_path):
    ing,cost,after=data();evidence(tmp_path)
    original=ing.frames_of('order_cost')[0]
    original.frame=original.frame.with_columns(pl.lit('NEW-PACK').alias('sku'))
    components=load(tmp_path,'shop',['10'],cost)
    from ledger.order_feed import OrderFeed
    after.notes=[]
    after.frame=after.frame.with_columns(pl.col('original_sku').alias('sku'),pl.lit('S').alias('sub_order_id')).drop('internal_sub_order_id')
    OrderFeed._align_after_sale_skus(ing,cost,components)
    assert after.frame['sku'].to_list()==['KIT','KIT']
    normalize(ing,cost,components)
    assert after.frame['returned_quantity'].to_list()==[2,2]
    assert '原订单展开明细' in after.frame['component_note'][0]
    assert fingerprint(tmp_path).startswith(':components:')


def test_component_evidence_hash_store_and_live_quantity_are_checked(tmp_path):
    _,cost,_=data();p,doc=evidence(tmp_path)
    with pytest.raises(ValueError,match='店铺不匹配'):load(tmp_path,'shop',['OTHER'],cost)
    assert load(tmp_path,'shop',['10'],cost.with_columns(pl.lit(1.0).alias('quantity'))) is None
    before=fingerprint(tmp_path)
    doc['payload_json']+=' ';p.write_text(json.dumps(doc))
    assert fingerprint(tmp_path)!=before
    with pytest.raises(ValueError,match='校验失败'):load(tmp_path,'shop',['10'],cost)
