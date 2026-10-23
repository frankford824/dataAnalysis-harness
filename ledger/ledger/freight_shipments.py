"""Resolve actual package members and their explicitly supported amount bases."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import json
import re

from .engine.allocation import refund_values
from .engine.rules import _norm
from .freight_allocation import number, text_amount
from .freight_trial_inputs import digest
from .order_lifecycle import SUPERSEDED_PARENT_STATES


_FEE_NAME = re.compile(r'补差价|差价|运费|邮费|运补|补邮')


def _non_shipping_item(row,header,tracking):
    kind=str(row.get('sku_type') or '').strip().lower()
    if kind in {'virtual','no_deliver','虚拟','虚拟商品'}:
        return True
    if row.get('is_virtual') is not True:
        return False
    name=str(row.get('product_name') or '')
    # Old feeds used a name heuristic that also matched physical 定制专拍.
    # Override only that legacy case, with a concrete normal SKU and dispatch;
    # unknown classifications and actual fee lines retain their exclusion.
    special_purchase=('专拍' in name and not _FEE_NAME.search(name)
        and _norm(row.get('sku_id'))
        and kind in {'normal','普通','普通商品'}
        and tracking and header.get('ship_time')
        and str(header.get('order_status_raw') or '') in {'Sent','Completed'})
    return not special_purchase


def _scalar(value):
    amount=number(value)
    return None if amount is None else format(amount.normalize(),'f')


def platform_bases(order_frames):
    candidates=defaultdict(list)
    native=defaultdict(list)
    for sid,priority,frame in order_frames:
        frame=refund_values(frame)
        for row in frame.iter_rows(named=True):
            main,child=_norm(row.get('order_id')),_norm(row.get('sub_order_id'))
            if not main:continue
            value={'order_id':main,'product_id':_norm(row.get('product_id')),
                'paid':_scalar(row.get('buyer_paid')),'refund':_scalar(row.get('refund_amount')),
                'ratio':_scalar(row.get('alloc_ratio')),'quantity':_scalar(row.get('quantity')),
                'priority':priority,'source':{'file_sha':row.get('__sha__'),'file':row.get('__file__'),
                    'sheet':row.get('__sheet__'),'row_no':row.get('__row__')}}
            if child:candidates[(sid,child)].append(value)
            else:native[(sid,main)].append(value)
    children={};groups=defaultdict(list)
    for key,records in candidates.items():
        priority=min(r['priority'] for r in records)
        records=[r for r in records if r['priority']==priority]
        variants={tuple(r[c] for c in ('order_id','product_id','paid','refund','ratio','quantity')) for r in records}
        chosen=min(records,key=lambda r:digest(r['source']))
        value={**chosen,'identity_conflict':len({(r['order_id'],r['product_id']) for r in records})!=1,
            'amount_conflict':len(variants)!=1,'amount_verified':False,'basis_amount':None,'basis_unit':'amount'}
        children[key]=value;groups[(key[0],chosen['order_id'])].append((key,value))
    for _group,rows in groups.items():
        values=[r for _,r in rows]
        declared=any(v['ratio'] is not None for v in values)
        ratios=[number(v['ratio']) for v in values]
        ratio_ok=(declared and all(r is not None and 0<=r<=1 for r in ratios)
                  and abs(sum(ratios)-1)<=Decimal('.000001'))
        nets=[]
        for value in values:
            paid,refund=number(value['paid']),number(value['refund'])
            nets.append(max(Decimal(0),paid-refund) if paid is not None and refund is not None and paid>=0 and refund>=0 else None)
        complete=all(v is not None for v in nets)
        total=sum(nets) if complete else None
        for index,value in enumerate(values):
            if value['amount_conflict'] or value['identity_conflict']:continue
            if declared:
                if not ratio_ok:continue
                value.update(amount_verified=True,basis_amount=text_amount(ratios[index]*total) if total and total>0 else text_amount(ratios[index]),
                    basis_unit='amount' if total and total>0 else 'ratio')
            elif nets[index] is not None:
                value.update(amount_verified=True,basis_amount=text_amount(nets[index]))
    controls={}
    native_rows={}
    for key,records in native.items():
        priority=min(r['priority'] for r in records);records=[r for r in records if r['priority']==priority]
        by_file=defaultdict(list)
        for r in records:by_file[r['source']['file_sha']].append(r)
        possible=None
        for rows in by_file.values():
            paid=[number(r['paid']) for r in rows]
            refunds=[number(r['refund']) for r in rows]
            if all(v is not None and v>=0 for v in [*paid,*refunds]):
                choices={(sum(paid),sum(refunds))}
                if len(set(paid))==len(set(refunds))==1:choices.add((paid[0],refunds[0]))
                possible=choices if possible is None else possible & choices
            else:possible=set()
        controls[key]=possible or set()
        native_rows[key]=records
    return children,controls,native_rows


def _main(header,row,store,children,platform):
    child=_norm(row.get('outer_sku'))
    basis=children.get((store,child))
    known={_norm(v) for v in (header.get('platform_order_ids') or []) if _norm(v)}
    if basis:
        if basis['identity_conflict'] or (known and header.get('platform_order_identity_complete') is True and basis['order_id'] not in known):return '',basis
        return basis['order_id'],basis
    if platform=='pdd':
        for raw in (child,row.get('online_order_no'),header.get('online_order_no')):
            found=re.fullmatch(r'(\d{6}-\d{15})(?:_\d+)?',re.sub(r'^\d+:','',_norm(raw)))
            if found:
                if known and header.get('platform_order_identity_complete') is True and found[1] not in known:return '',None
                return found[1],None
    # An item's online-order field may merely mirror the merged header's
    # primary member. It cannot select one parent from a multi-parent header.
    if len(known)==1 and header.get('platform_order_identity_complete') is True:return next(iter(known)),None
    return '',None


def build_packages(headers,items,relations,mapping,model,children,_controls,_native_rows,progress=lambda *_:None):
    stores={s.id:s for s in model.stores}
    mapped={r['order_store_id']:r['ledger_store_id'] for r in mapping if r['mapping_status']=='confirmed'}
    head={};head_conflicts=set()
    for h in headers.iter_rows(named=True):
        key=(_norm(h['order_store_id']),_norm(h['order_id']))
        if key in head and digest(head[key])!=digest(h):head_conflicts.add(key)
        else:head[key]=h
    item_by_id={};item_conflicts=set()
    fields=('order_id','outer_sku','shop_item_id','merchant_sku','tracking_no','quantity','line_amount','sku_id')
    for r in items.iter_rows(named=True):
        key=(_norm(r.get('order_store_id')),_norm(r['sub_order_id']))
        if key in item_by_id and any(item_by_id[key].get(c)!=r.get(c) for c in fields):item_conflicts.add(key)
        else:item_by_id[key]=r
    origins=defaultdict(list)
    for relation in relations.iter_rows(named=True):
        if relation.get('relation_type')=='reship':origins[_norm(relation.get('target_order_id'))].append(relation)
    packages=defaultdict(lambda:{'rows':[],'headers':{},'carriers':set(),'stores':set(),'unknown':False})
    seen_items={};excluded_packages={}
    for index,row in enumerate(items.iter_rows(named=True)):
        if index%100000==0:progress('确认包裹商品与金额依据',index,items.height)
        oid,osid=_norm(row['order_id']),_norm(row['order_store_id'])
        header=head.get((osid,oid),{})
        if str(header.get('order_status_raw') or '').strip() in SUPERSEDED_PARENT_STATES:
            continue
        sid=mapped.get(osid,'');store=stores.get(sid);platform=store.platform if store else ''
        tracking=_norm(row.get('tracking_no')) or _norm(header.get('tracking_no'))
        if _non_shipping_item(row,header,tracking):
            if tracking:
                excluded=excluded_packages.setdefault(tracking,{'rows':[],'headers':{},'carriers':set(),
                    'stores':set(),'unknown':False,'non_shipping':True})
                excluded['headers'][(osid,oid)]=header
                if sid:excluded['stores'].add(sid)
                else:excluded['unknown']=True
                if header.get('logistics_company'):excluded['carriers'].add(str(header['logistics_company']).strip())
            continue
        raw_child=_norm(row.get('outer_sku'));main,basis=_main(header,row,sid,children,platform)
        reship=bool(origins.get(oid)) or '补发' in str(header.get('order_type') or '')
        product=_norm(row.get('shop_item_id')) or _norm(row.get('merchant_sku'))
        role='platform_child' if platform in {'taobao','douyin'} else 'native_item'
        child=raw_child if role=='platform_child' else ''
        if reship:
            resolved=[]
            for relation in origins.get(oid,[]):
                if _norm(relation.get('target_sub_order_id')) not in ('',_norm(row['sub_order_id']),raw_child):continue
                source_key=(osid,_norm(relation.get('source_sub_order_id')))
                source=item_by_id.get(source_key) if source_key not in item_conflicts else None
                if source and _norm(source.get('order_id'))==_norm(relation.get('source_order_id')) and _norm(source.get('order_store_id'))==osid:
                    original_child=_norm(source.get('outer_sku'));original=children.get((sid,original_child))
                    if original and not original['identity_conflict']:resolved.append((original_child,original))
            if len({c for c,_b in resolved})==1:
                child,basis=resolved[0];main=basis['order_id']
            elif basis is None:
                # A uniquely identified ERP replacement item is a real target;
                # its internal item number is never mislabeled a platform child.
                role='erp_reship_item';child=''
                parent_mains=set()
                for relation in origins.get(oid,[]):
                    parent=head.get((osid,_norm(relation.get('source_order_id'))),{})
                    values={_norm(v) for v in parent.get('platform_order_ids') or [] if _norm(v)}
                    if len(values)==1 and parent.get('platform_order_identity_complete') is True:parent_mains.update(values)
                if len(parent_mains)==1:main=next(iter(parent_mains))
        identity=bool(store and main and product and (role!='platform_child' or (child and re.fullmatch(r'\d{6,24}',child))))
        if (osid,oid) in head_conflicts or (osid,_norm(row['sub_order_id'])) in item_conflicts:identity=False
        if basis and (basis['identity_conflict'] or (basis.get('product_id') and product!=basis['product_id'])):identity=False
        target=digest([sid,main,child or product,role,'reship' if reship else 'sale'])[:24]
        # line_amount is JST's 商品金额 (a line total), not payment or unit price.
        goods_amount=number(row.get('line_amount'))
        member={'target_key':target,'store_id':sid,'order_id':main,'child_id':child,'product_id':product,
            'product_name':str(row.get('product_name') or ''),'identity_kind':role,'identity_verified':identity,
            'internal_order_id':oid,'internal_item_id':_norm(row['sub_order_id']),'tracking_no':tracking,
            'is_reship':reship,'basis':basis,'raw_quantity':number(row.get('quantity')),
            'sku_id':_norm(row.get('sku_id')),'sku_type':str(row.get('sku_type') or ''),
            'erp_goods_amount':goods_amount,'header':header}
        item_key=(osid,member['internal_item_id'])
        signature=digest([oid,raw_child,product,tracking,str(goods_amount),member['sku_id'],str(member['raw_quantity'])])
        duplicate_conflict=item_key in seen_items and seen_items[item_key]!=signature
        if item_key in seen_items and not duplicate_conflict:continue
        seen_items[item_key]=signature
        if duplicate_conflict:member['identity_verified']=False
        if not tracking:continue
        package=packages[tracking];package['rows'].append(member);package['headers'][(osid,oid)]=header
        if sid:package['stores'].add(sid)
        else:package['unknown']=True
        carrier=str(header.get('logistics_company') or '').strip()
        if carrier:package['carriers'].add(carrier)
    for tracking,excluded in excluded_packages.items():
        if tracking not in packages:packages[tracking]=excluded
    output={}
    for tracking,package in packages.items():
        issue='non_shipping_items' if package.get('non_shipping') else ''
        if len(package['stores'])>1 or package['unknown']:issue='cross_store'
        elif len(package['carriers'])>1:issue='carrier_conflict'
        dispatches={str(h.get('ship_time') or '') for h in package['headers'].values()}
        if not issue and len(package['headers'])>1 and (len(dispatches)!=1 or not next(iter(dispatches))):issue='shipment_conflict'
        grouped=defaultdict(list)
        for row in package['rows']:grouped[row['target_key']].append(row)
        members=[]
        for target,rows in grouped.items():
            first=rows[0]
            m={k:first[k] for k in ('target_key','store_id','order_id','child_id','product_id','identity_kind','is_reship')}
            m['identity_verified']=all(r['identity_verified'] for r in rows)
            # A platform child may contain several ERP SKUs (e.g. glue + paper).
            # Sum this package's line totals without comparing unlike quantities,
            # subtracting refunds, or requiring agreement with platform payment.
            amounts=[r['erp_goods_amount'] for r in rows]
            verified=all(v is not None and v>=0 for v in amounts)
            total=sum(amounts,Decimal(0)) if verified else None
            erp_items=[{'internal_order_id':r['internal_order_id'],'internal_item_id':r['internal_item_id'],
                'sku_id':r['sku_id'],'product_name':r['product_name'],
                'quantity':text_amount(r['raw_quantity']),'goods_amount':text_amount(r['erp_goods_amount'])}
                for r in sorted(rows,key=lambda r:(r['internal_order_id'],r['internal_item_id']))]
            m.update(product_name=' / '.join(dict.fromkeys(r['product_name'] for r in erp_items if r['product_name'])),
                amount_verified=verified,basis_amount=text_amount(total),portion_amount=text_amount(total),
                whole_child=False,basis_unit='amount',basis_source='erp_goods_amount',
                ordered_quantity=None,shipped_quantity=None,quantity_verified=False,erp_items=erp_items,
                basis_reference={'internal_item_ids':[r['internal_item_id'] for r in erp_items],
                    'field':'line_amount','field_label':'聚水潭商品金额'})
            members.append(m)
        output[tracking]={'packet_id':digest([tracking,sorted(package['stores']),sorted(package['headers'])])[:24],
            'tracking_no':tracking,'store_ids':sorted(package['stores']),'carrier':';'.join(sorted(package['carriers'])),
            'erp_shop_keys':sorted({_norm(h.get('jst_shop_id'))+'|'+str(h.get('platform_code') or '').strip()
                if _norm(h.get('jst_shop_id')) and str(h.get('platform_code') or '').strip() else ''
                for h in package['headers'].values()}),
            'ship_time':';'.join(sorted(dispatches)),'internal_order_ids':sorted({key[1] for key in package['headers']}),
            'members':members,'issue':issue,'allow_zero_equal':False}
    return output
