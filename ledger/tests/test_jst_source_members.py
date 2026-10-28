from copy import deepcopy
from decimal import Decimal
import hashlib
import io
import json
import re
import zipfile
from xml.sax.saxutils import escape
import pytest
from ledger.jst_source_members import Anchor, Capture, MemberPending, Node, Request, normalize_members, parse_order

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def xlsx(headers, values, formula=None):
    def cell(i, row, value):
        addr = chr(65 + i) + str(row)
        if isinstance(value, (int, Decimal)):
            return f'<c r="{addr}"><v>{value}</v></c>'
        return f'<c r="{addr}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
    hdr = ''.join(cell(i, 2, v) for i, v in enumerate(headers))
    data = ''.join(cell(i, 3, v) for i, v in enumerate(values))
    if formula:
        addr = chr(65 + formula[0]) + '3'
        data = data.replace(f'<c r="{addr}">', f'<c r="{addr}"><f>{formula[1]}</f>')
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="证据" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="2">{hdr}</row><row r="3">{data}</row></sheetData></worksheet>')
    return result.getvalue()

HEADERS = ['内部订单号','子订单编号','原始线上订单号','线上子订单编号','商品编码','数量','下单时间','店铺名称','订单类型']

def item(oid='100', iid='101', sku='A', qty='1.25', **changes):
    d = dict(o_id=oid,oi_id=iid,co_id='10',sku_id=sku,qty=qty,sku_type='normal',oi_so_id='202600001',
             outer_oi_id='C1',raw_so_id='202600001',outer_so_id='202600001',raw_sku_id=sku,
             src_combine_sku_id='',item_status='Sent',created='2026-05-15 12:00:00',is_gift=False)
    d.update(changes)
    return d

def order(oid, items, **changes):
    d = dict(o_id=oid,items=items,co_id='10',owner_co_id=0,wms_co_id='999',shop_id='77',so_id='202600001',
             order_date='2026-05-15 10:00:00',type='普通订单',status='Sent')
    d.update(changes)
    return d

def html(data):
    return ('<html><script>var _OrderData='+json.dumps(data,ensure_ascii=False)+';</script></html>').encode()

def build(cost, native, source, target):
    raw = [cost,native,html(source),html(target),b'<span onclick="ShowOrder(100)">x</span><span onclick="ShowOrder(200)">y</span>']
    blobs = {sha(v):v for v in raw}
    req = Request('100','202600001','alibaba1688',('测试店',),('77',),(Anchor(sha(cost),'证据',3),),(Anchor(sha(native),'证据',3),))
    cap = Capture((Node('100',sha(raw[2])),Node('200',sha(raw[3]))),(Node('100',sha(raw[4])),))
    return req,cap,blobs,source,target

def fixture(qty='1.25', bundle=False):
    sku = 'KIT' if bundle else 'A'
    cost = xlsx(HEADERS,['100','101','202600001','C1',sku,Decimal(qty),'2026-05-15','测试店','普通订单'])
    native = xlsx(['订单号','订单创建时间'],['202600001','2026-05-15'])
    if bundle:
        parents = [item(sku='A',qty='2',src_combine_sku_id='KIT',src_combine_sku_qty=qty,is_combine_item=True,created=None),
                   item(sku='B',qty='3',src_combine_sku_id='KIT',src_combine_sku_qty=qty,is_combine_item=True,created=None)]
        members = [item('200','201',sku='A',qty='2',src_combine_sku_id='KIT',src_combine_sku_qty=qty),
                   item('200','202',sku='B',qty='3',src_combine_sku_id='KIT',src_combine_sku_qty=qty)]
    else:
        parents,members = [item(qty=qty)],[item('200','201',qty=qty)]
    return build(cost,native,order('100',parents,status='Split'),order('200',members,link_o_id='100',is_split=True,is_merge=True))

def rebuild(f, source=None, target=None, cost=None, native=None):
    return build(cost or f[2][f[0].cost_anchors[0].sha256],native or f[2][f[0].native_anchors[0].sha256],source or f[3],target or f[4])

def prove(f):
    return normalize_members(*f[:3])

def rewrite(raw, name, fn):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as dst:
        for part in src.namelist():
            dst.writestr(part,fn(src.read(part)) if part == name else src.read(part))
        if name not in src.namelist():
            dst.writestr(name,fn(b''))
    return out.getvalue()

def test_exact_original_quantity_and_real_physical_members():
    r = prove(fixture(qty='0.123456789012345678901234567890123456789'))
    assert r['status']=='proved',r
    m, = r['members']
    assert m['quantity']=='0.123456789012345678901234567890123456789'
    assert (m['internal_order_id'],m['internal_sub_order_id'],m['company'],m['pricing_day'])==('200','201','10','2026-05-15')
    assert len(m['graph_refs'])==3 and 'amount' not in m and 'unit_cost' not in m and 'financial_ready' not in r

def test_bundle_units_are_not_piece_units():
    r = prove(fixture(qty='1',bundle=True))
    assert r['status']=='proved',r
    assert {m['logical_quantity'] for m in r['members']}=={'1'}
    assert {m['quantity'] for m in r['members']}=={'2','3'}

def test_other_merged_members_are_not_broadcast():
    f=fixture();t=deepcopy(f[4]);t['items'].append(item('200','299',qty='25',raw_so_id='OTHER',oi_so_id='OTHER',outer_oi_id='OTHER'))
    r=prove(rebuild(f,target=t));assert r['status']=='proved' and len(r['members'])==1

@pytest.mark.parametrize('changes,reason',[
    ({'qty':'2'},'physical_quantity_not_conserved'),({'created':None},'physical_member_lifecycle_unproved'),
    ({'created':'2020-01-01 00:00:00'},'physical_member_creation_precedes_original'),
    ({'item_status':'Cancelled'},'physical_member_lifecycle_unproved'),({'is_gift':True},'physical_member_lifecycle_unproved'),
    ({'is_combine':True},'physical_member_lifecycle_unproved'),({'co_id':'22'},'member_company_namespace_conflict'),
    ({'raw_so_id':'OTHER'},'physical_member_missing_or_ambiguous')])
def test_physical_conflicts_are_pending(changes,reason):
    f=fixture();t=deepcopy(f[4]);t['items'][0].update(changes);r=prove(rebuild(f,target=t))
    assert reason in r['reasons'] and not r['members'],r

@pytest.mark.parametrize('changes,reason',[
    ({'shop_id':'88'},'source_cross_shop'),({'type':'补发订单'},'non_normal_order_source_separate'),
    ({'link_o_id':'999'},'typed_relation_target_missing'),({'owner_co_id':'88'},'price_company_changed'),
    ({'status':'Cancelled'},'source_lifecycle_pending')])
def test_header_conflicts_are_pending(changes,reason):
    f=fixture();t=deepcopy(f[4]);t.update(changes);r=prove(rebuild(f,target=t))
    assert reason in r['reasons'] and not r['members'],r

def test_owner_co_is_cost_namespace_and_not_warehouse():
    f=fixture();s,t=deepcopy(f[3]),deepcopy(f[4]);s['owner_co_id']=t['owner_co_id']='88'
    r=prove(rebuild(f,source=s,target=t));assert r['status']=='proved' and r['members'][0]['company']=='88'

def test_duplicate_physical_bundle_members_are_rejected():
    f=fixture(qty='1',bundle=True);t=deepcopy(f[4]);t['items'][1]['oi_id']=t['items'][0]['oi_id']
    assert 'physical_member_reused' in prove(rebuild(f,target=t))['reasons']

@pytest.mark.parametrize('index,value,reason',[(0,'999','original_cost_identity_conflict'),(4,'OTHER','bundle_original_inventory_unproved'),
    (5,Decimal('2'),'original_member_quantity_changed'),(6,'2026-05-16','original_cost_native_day_conflict'),
    (7,'其他店','original_cost_identity_conflict'),(8,'补发订单','reship_original_source_separate')])
def test_original_cell_values_are_rechecked(index,value,reason):
    f=fixture();row=['100','101','202600001','C1','A',Decimal('1.25'),'2026-05-15','测试店','普通订单'];row[index]=value
    assert reason in prove(rebuild(f,cost=xlsx(HEADERS,row)))['reasons']

def test_quantity_formula_is_not_executed():
    f=fixture();row=['100','101','202600001','C1','A',Decimal('1.25'),'2026-05-15','测试店','普通订单']
    assert 'source_cell_missing_or_formula' in prove(rebuild(f,cost=xlsx(HEADERS,row,formula=(5,'1.25'))))['reasons']

def test_raw_byte_mutation_and_missing_native_anchor_are_rejected():
    f=fixture();blobs=dict(f[2]);blobs[f[1].nodes[0].sha256]+=b' '
    assert 'source_blob_sha_changed' in normalize_members(f[0],f[1],blobs)['reasons']
    blobs=dict(f[2]);del blobs[f[0].native_anchors[0].sha256]
    assert 'source_anchor_invalid' in normalize_members(f[0],f[1],blobs)['reasons']

def test_relation_listing_alone_is_not_directed_membership():
    f=fixture();t=deepcopy(f[4]);del t['link_o_id']
    assert 'no_proved_terminal_shipments' in prove(rebuild(f,target=t))['reasons']

@pytest.mark.parametrize('body',['var note="_OrderData = {fake}";','function f(){_OrderData=DATA;}',
    'if(false) _OrderData=DATA;','var rx=/_OrderData=DATA/;','_OrderData=DATA;_OrderData=DATA;',
    '_OrderData={"o_id":"100","o_id":"999","items":[]};'])
def test_fake_assignments_and_duplicate_keys(body):
    raw=('<script>'+body.replace('DATA',json.dumps(order('100',[item()])))+'</script>').encode()
    with pytest.raises(MemberPending):parse_order(raw,'100')

def test_comments_and_multibyte_text_preserve_original_byte_span():
    raw=('中文<script>/* _OrderData = {} */ const _OrderData='+json.dumps(order('100',[item()]))+';</script>').encode()
    data,ref=parse_order(raw,'100');assert data['o_id']=='100'
    assert sha(raw[ref['start_byte']:ref['end_byte']])==ref['json_sha256']

@pytest.mark.parametrize('qty',['1E999999999','1E-999999999','1E65','1E-65'])
def test_extreme_exponents_do_not_expand_or_overflow(qty):
    assert 'quantity_invalid' in prove(fixture(qty=qty))['reasons']

def test_cell_coordinate_must_match_container_row():
    f=fixture();raw=f[2][f[0].cost_anchors[0].sha256]
    raw=rewrite(raw,'xl/worksheets/sheet1.xml',lambda b:b.replace(b'r="A3"',b'r="A999"'))
    assert 'source_cell_row_conflict' in prove(rebuild(f,cost=raw))['reasons']

def test_negative_shared_string_index_does_not_select_last_value():
    f=fixture();raw=f[2][f[0].cost_anchors[0].sha256]
    raw=rewrite(raw,'xl/worksheets/sheet1.xml',lambda b:re.sub(br'<c r="A3"[^>]*>.*?</c>',b'<c r="A3" t="s"><v>-1</v></c>',b))
    raw=rewrite(raw,'xl/sharedStrings.xml',lambda _:b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>100</t></si></sst>')
    assert 'source_shared_string_index' in prove(rebuild(f,cost=raw))['reasons']

def test_duplicate_relationship_ids_are_not_overwritten():
    f=fixture();raw=f[2][f[0].cost_anchors[0].sha256]
    raw=rewrite(raw,'xl/_rels/workbook.xml.rels',lambda b:b.replace(b'</Relationships>',b'<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'))
    assert 'workbook_relation_ambiguous' in prove(rebuild(f,cost=raw))['reasons']

@pytest.mark.parametrize('opening',['<script type="text/plain">','<script src="ignored.js">','<template><script>','<noscript><script>'])
def test_inert_script_bodies_are_not_source(opening):
    raw=(opening+'var _OrderData='+json.dumps(order('100',[item()]))+';</script></template></noscript>').encode()
    with pytest.raises(MemberPending):parse_order(raw,'100')

def test_html_comment_fake_script_is_ignored():
    fake=('<!--<script>var _OrderData='+json.dumps(order('999',[item('999')]))+';</script>-->').encode()
    assert parse_order(fake+html(order('100',[item()])),'100')[0]['o_id']=='100'

def test_formula_cell_cannot_fall_back_to_merge_root():
    f=fixture();raw=f[2][f[0].cost_anchors[0].sha256]
    def change(b):
        b=re.sub(br'<c r="F3"[^>]*>.*?</c>',b'<c r="F3"><f>1.25</f></c>',b)
        return b.replace(b'</worksheet>',b'<mergeCells><mergeCell ref="E3:F3"/></mergeCells></worksheet>')
    raw=rewrite(raw,'xl/worksheets/sheet1.xml',change)
    assert 'source_cell_missing_or_formula' in prove(rebuild(f,cost=raw))['reasons']

def test_overlapping_merge_regions_are_ambiguous():
    f=fixture();raw=f[2][f[0].cost_anchors[0].sha256]
    raw=rewrite(raw,'xl/worksheets/sheet1.xml',lambda b:b.replace(b'</worksheet>',b'<mergeCells><mergeCell ref="E3:F3"/><mergeCell ref="F3:G3"/></mergeCells></worksheet>'))
    assert 'source_merge_ambiguous' in prove(rebuild(f,cost=raw))['reasons']

def test_utf16_xml_cannot_bypass_entity_guard():
    f=fixture();raw=f[2][f[0].cost_anchors[0].sha256]
    raw=rewrite(raw,'xl/worksheets/sheet1.xml',lambda b:b.decode().encode('utf-16'))
    assert 'workbook_xml_encoding' in prove(rebuild(f,cost=raw))['reasons']

@pytest.mark.parametrize('qty',[Decimal('1.25'),Decimal('99')])
def test_two_quantity_headers_never_guess_even_if_values_equal(qty):
    f=fixture();row=['100','101','202600001','C1','A',Decimal('1.25'),'2026-05-15','测试店','普通订单',qty]
    assert 'source_header_role_ambiguous' in prove(rebuild(f,cost=xlsx([*HEADERS,'数量'],row)))['reasons']

def test_duplicate_native_date_header_is_pending():
    f=fixture();raw=xlsx(['订单号','订单创建时间','订单创建时间'],['202600001','2026-05-15','2026-05-15'])
    assert 'source_header_role_ambiguous' in prove(rebuild(f,native=raw))['reasons']

def test_approved_ali_first_order_column_contract_is_explicit():
    f=fixture();raw=xlsx(['订单号','订单创建时间','订单号'],['202600001','2026-05-15','filled copy'])
    assert prove(rebuild(f,native=raw))['status']=='proved'

@pytest.mark.parametrize('created',['2026-05-15','2026-05-15 BAD','2026-05-15 99:99:99'])
def test_created_is_an_actual_timestamp_not_just_a_date_prefix(created):
    f=fixture();t=deepcopy(f[4]);t['items'][0]['created']=created
    assert 'physical_creation_timestamp_invalid' in prove(rebuild(f,target=t))['reasons']

def test_unknown_flags_remain_explicitly_unknown_for_later_financial_admission():
    r=prove(fixture());flags=r['members'][0]['observed_flags']['physical_member']
    assert flags['is_virtual']=={'present':False,'value':None}
    assert flags['is_gift']=={'present':True,'value':False}

def test_unrelated_merged_input_does_not_require_its_untraversed_relation_page():
    f=fixture();req,cap,blobs=f[:3];blobs=dict(blobs)
    other=order('300',[item('300','301',sku='OTHER',qty='7',oi_so_id='OTHER')],status='Merged',link_o_id='100')
    raw=html(other);blobs[sha(raw)]=raw
    relation=blobs[cap.relations[0].sha256]+b'<span onclick="ShowOrder(300)">z</span>'
    blobs[sha(relation)]=relation
    cap=Capture((*cap.nodes,Node('300',sha(raw))),(Node('100',sha(relation)),))
    r=normalize_members(req,cap,blobs)
    assert r['status']=='proved' and len(r['members'])==1,r

def test_reachable_split_container_still_requires_its_relation_page():
    f=fixture();req,cap,blobs=f[:3]
    r=normalize_members(req,Capture(cap.nodes,()),blobs)
    assert r['status']=='pending' and not r['members']

def test_real_expanded_item_flag_cannot_impersonate_a_physical_member():
    f=fixture();t=deepcopy(f[4]);t['items'][0]['is_combine_item']=True
    assert 'physical_member_lifecycle_unproved' in prove(rebuild(f,target=t))['reasons']

def test_generic_combine_flag_is_not_substituted_for_actual_source_contract():
    f=fixture(qty='1',bundle=True);s=deepcopy(f[3])
    for i in s['items']:
        del i['is_combine_item'];i['is_combine']=True
    assert 'bundle_original_inventory_unproved' in prove(rebuild(f,source=s))['reasons']
