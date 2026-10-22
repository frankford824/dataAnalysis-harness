import hashlib
import json
from pathlib import Path

import polars as pl
import pytest

from ledger.freight_trial_inputs import _materialize,feed_frames,flag_charge_overlaps,InputError


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


def charge(amount,stamp='2026-06-22 11:23:24',**extra):
    return {'tracking_no':'YT7625530235832','scan_time_raw':stamp,'amount':amount,
            'settlement_object':'江苏省南京市江宁区江开','file_sha':'a','source_issue':'',**extra}


def test_outbound_return_and_interception_fees_are_distinct_bill_rows():
    rows=[charge('1.70','2026-06-05',settlement_object='皇莉诗旗舰店'),charge('1'),charge('.6')]
    flag_charge_overlaps(rows)
    assert all(not r['source_issue'] for r in rows)


def test_exact_candidates_do_not_block_other_distinct_fees_in_the_same_group():
    rows=[charge('1'),charge('1.00'),charge('.6'),charge('1',settlement_object='另一结算对象'),charge('-1')]
    flag_charge_overlaps(rows)
    assert [r['source_issue'] for r in rows]==['duplicate_charge','duplicate_charge','','','']


def test_overlapping_files_are_not_certified_as_additional_fees():
    rows=[charge('1'),charge('.6',file_sha='b')]
    flag_charge_overlaps(rows)
    assert all(r['source_issue']=='source_overlap' for r in rows)


def test_real_bill_reordered_copy_is_counted_once_and_keeps_distinct_fee_rows(tmp_path):
    from decimal import Decimal
    from conftest import MODELS,write_xlsx
    from ledger.model.loader import load_model
    from ledger.freight_trial_inputs import parse_sources
    header=['运单号码','账单结算对象名称','总金额','扫描时间']
    rows=[['YT7625530235832','皇莉诗旗舰店',1.7,'2026/06/05 12:00:00'],
          ['YT7625530235832','江苏省南京市江宁区江开',1,'2026/06/22 11:23:24'],
          ['YT7625530235832','江苏省南京市江宁区江开',.6,'2026/06/22 11:23:24'],
          ['YT7625391995793','南京永箔金属材料有限公司',2.4,'2026/06/04 12:00:00'],
          ['YT7625391995793','南京永箔金属材料有限公司',2.4,'2026/06/04 12:00:00']]
    root=tmp_path/'space';records=[]
    for index,data in enumerate((rows,list(reversed(rows)))):
        name=f'运费-6月-{index}.xlsx';file=write_xlsx(tmp_path/name,[header,*data])
        payload=file.read_bytes();sha=hashlib.sha256(payload).hexdigest()
        archive=root/'files'/sha[:2]/sha;archive.parent.mkdir(parents=True,exist_ok=True);archive.write_bytes(payload)
        records.append({'sha':sha,'name':name,'owners':{'__shared__':0},'active':True})
    assert records[0]['sha']!=records[1]['sha']
    charges,_orders,aliases,warnings=parse_sources(root,root/'freight-shadow',load_model(MODELS/'cn-ecommerce'),
                                                  {'period':'2026-06','files':records})
    assert not warnings and len(charges)==5 and len(set(aliases.values()))==1
    distinct=[r for r in charges if r['tracking_no']=='YT7625530235832']
    assert len(distinct)==3 and all(not r['source_issue'] for r in distinct)
    assert sum(Decimal(r['amount']) for r in distinct)==Decimal('3.30')
    assert all(r['settlement_object'] for r in distinct)
    assert sum(r['source_issue']=='duplicate_charge' for r in charges)==2
