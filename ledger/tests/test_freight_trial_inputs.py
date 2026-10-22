import hashlib
import json
from pathlib import Path

import polars as pl
import pytest

from ledger.freight_trial_inputs import _materialize,feed_frames,InputError


def source(tmp_path,data=b'original-data'):
    root=tmp_path/'space';sha=hashlib.sha256(data).hexdigest();path=root/'files'/sha[:2]/sha
    path.parent.mkdir(parents=True);path.write_bytes(data)
    return root,path,{'sha':sha,'name':'订单明细.xlsx'}


def test_archived_symlink_is_copied_without_cloning_or_changing_the_link(tmp_path):
    root,path,record=source(tmp_path)
    archive=tmp_path/'archive.data';archive.write_bytes(path.read_bytes());path.unlink();path.symlink_to(archive)
    target=_materialize(root,root/'freight-shadow',record)
    assert path.is_symlink() and not target.is_symlink() and target.read_bytes()==archive.read_bytes()
    assert target!=path and target.stat().st_ino!=archive.stat().st_ino
    assert _materialize(root,root/'freight-shadow',record)==target


def test_existing_bad_input_copy_is_not_silently_used_or_overwritten(tmp_path):
    root,path,record=source(tmp_path)
    target=_materialize(root,root/'freight-shadow',record);target.write_bytes(b'wrong')
    with pytest.raises(InputError,match='校验'):_materialize(root,root/'freight-shadow',record)
    assert path.read_bytes()==b'original-data' and target.read_bytes()==b'wrong'


def test_original_hash_mismatch_blocks_proposal_input(tmp_path):
    root,path,record=source(tmp_path);path.write_bytes(b'changed')
    with pytest.raises(InputError,match='校验'):_materialize(root,root/'freight-shadow',record)


def test_untrusted_copy_symlink_cannot_redirect_a_write(tmp_path):
    root,path,record=source(tmp_path)
    cache=root/'freight-shadow';target=cache/'input-copies-v1'/record['sha']/record['name']
    target.parent.mkdir(parents=True);outside=tmp_path/'outside';outside.write_bytes(b'keep');target.symlink_to(outside)
    with pytest.raises(InputError,match='路径'):_materialize(root,cache,record)
    assert outside.read_bytes()==b'keep'


def test_shipment_feed_preserves_goods_amount_independently_of_payment(tmp_path):
    pl.DataFrame({'order_id':['e1'],'tracking_no':['W1'],
        'platform_order_ids':[['100000001']]}).write_parquet(tmp_path/'orders.parquet')
    pl.DataFrame({'order_id':['e1'],'sub_order_id':['i1'],'outer_sku':['100000001'],
        'tracking_no':['W1'],'line_amount':['7.00'],'item_pay_amount':['99.00']}).write_parquet(tmp_path/'order_items.parquet')
    pl.DataFrame(schema={'source_order_id':pl.String,'target_order_id':pl.String}).write_parquet(tmp_path/'order_relations.parquet')
    inputs={'feed_manifest':{'objects':{name:{'path':name} for name in
        ('orders.parquet','order_items.parquet','order_relations.parquet')}}}
    _headers,items,_relations=feed_frames(tmp_path,inputs,[],['W1'])
    assert items['line_amount'].to_list()==['7.00']
    delta={'entity_type':'order_item','entity_id':'i1','operation':'upsert',
        'payload_json':json.dumps({'order_item':{'order_id':'e1','sub_order_id':'i1','outer_sku':'100000001',
            'tracking_no':'W1','line_amount':'0.00','item_pay_amount':'99.00'}})}
    _headers,items,_relations=feed_frames(tmp_path,inputs,[delta],['W1'])
    assert items['line_amount'].to_list()==['0.00']
    assert items['item_pay_amount'].to_list()==['99.00']
