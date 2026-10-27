"""Original projection lineage and nonoverlapping A/B comparison components."""
from collections import defaultdict
from decimal import Decimal

from .freight_trial_inputs import digest

Q=Decimal('.000001')


def amount(value):
    return Decimal(str(value or 0))


def anchor(row):
    return (row.get('file_sha') or '',str(row.get('sheet') or ''),int(row.get('row_no') or 0))


def key(row):
    """Only the shared store/platform-parent/product grain is comparable."""
    return (row.get('store_id') or '',row.get('order_id') or '',row.get('product_id') or '')


def scope_groups(source,projection,spine,store):
    groups={};labels={store.name,*store.aliases};spine_rows=spine.to_dicts()
    def group(label,period,link):
        gid=digest([store.id,label,period,link])[:32]
        if gid not in groups:groups[gid]={'group_id':gid,'store_id':store.id,'store':label,'period':period,
            'link_key':link,'source_contribution':'0','projection_cost':'0','source_anchors':[],'targets':[]}
        return gid,groups[gid]
    for row in source.iter_rows(named=True):
        if row.get('store') not in labels or not row.get('counted'):continue
        gid,value=group(row.get('store'),row.get('period'),row.get('link_key'))
        value['source_contribution']=str(amount(value['source_contribution'])+amount(row.get('contribution')))
        value['source_anchors'].append({'file_sha':row.get('file_sha'),'sheet':row.get('sheet') or '',
            'row_no':row.get('row_no'),'contribution':str(row.get('contribution') or 0)})
    for row in projection.iter_rows(named=True):
        if row.get('store') not in labels:continue
        gid,value=group(row.get('store'),row.get('period'),row.get('link_key'))
        ordinal=row.get('spine_row')
        original=spine_rows[ordinal] if isinstance(ordinal,int) and 0<=ordinal<len(spine_rows) else {}
        cost=-amount(row.get('amount'))
        value['projection_cost']=str(amount(value['projection_cost'])+cost)
        value['targets'].append({'projection_record_id':digest([gid,len(value['targets'])])[:32],
            'store_id':store.id,'order_id':str(original.get('order_id') or ''),
            'child_id':str(original.get('sub_order_id') or ''),'product_id':str(original.get('product_id') or ''),
            'period':str(row.get('period') or ''),'spine_row':ordinal,
            'original_order_date':str(original.get('order_date') or ''),
            'identity_verified':bool(original.get('order_id') and original.get('product_id')),
            'identity_kind':'platform_child' if store.platform in {'taobao','douyin'} else 'native_item',
            'factor':str(row.get('factor') or 0),'amount':str(cost)})
    for value in groups.values():
        original=-amount(value['source_contribution']);projected=amount(value['projection_cost'])
        # The original ratio/even projector rounds each target to six decimals.
        # Preserve its actual residue; this bound never changes target amounts.
        bound=max(Q,Q*Decimal('0.5')*len(value['targets']))+Decimal('0.00000001')
        value.update(source_cost=str(original),original_rounding_residue=str(original-projected),
            rounding_bound=str(bound),within_original_rounding_bound=abs(original-projected)<=bound,
            identity_verified=bool(value['targets']) and all(t['identity_verified'] for t in value['targets']))
    return groups


def finish(charge,record,groups):
    related=[groups[gid] for gid in sorted(set(record.get('group_ids') or []))]
    observed=sum((-amount(r.get('contribution')) for r in record['source_facts'] if r.get('counted')),Decimal(0))
    original=amount(charge['amount'])
    matched=(bool(related) and abs(observed-original)<=Q
        and all(g['identity_verified'] and g['within_original_rounding_bound'] for g in related))
    single=matched and all({anchor(r) for r in g['source_anchors']}=={anchor(charge)} for g in related)
    record.update(group_ids=[g['group_id'] for g in related],source_booked_amount=str(observed),
        source_matched=matched,attribution_status='single_source_actual_projection' if single else
        'charge_attribution_unavailable' if matched else 'unmatched_or_unverified',
        allocations=[dict(t) for g in related for t in g['targets']] if single else [],
        status='allocated' if single else 'group_matched' if matched else 'pending',
        reason='' if single else 'charge_attribution_unavailable' if matched else 'formal_unbooked_or_unresolved',
        method='original_actual_projection',original_rounding_residue=str(
            sum((amount(g['original_rounding_residue']) for g in related),Decimal(0))) if single else None)
    return record


def components(charges,baseline,candidate,groups):
    by_id={c['charge_id']:c for c in charges};by_anchor={anchor(c):c['charge_id'] for c in charges}
    parents={cid:cid for cid in by_id}
    def root(cid):
        while parents[cid]!=cid:
            parents[cid]=parents[parents[cid]];cid=parents[cid]
        return cid
    def union(ids):
        ids=list(ids)
        if not ids:return
        first=root(ids[0])
        for cid in ids[1:]:parents[root(cid)]=first
    packets=defaultdict(list);gids=defaultdict(set);external=set()
    for cid,c in by_id.items():
        packet=candidate[cid].get('packet_id') or ('tracking:'+c['tracking_no'] if c['tracking_no'] else 'charge:'+cid)
        packets[packet].append(cid)
    for ids in packets.values():union(ids)
    for gid,g in groups.items():
        ids={by_anchor[anchor(a)] for a in g['source_anchors'] if anchor(a) in by_anchor}
        if not ids:continue
        union(ids)
        for cid in ids:gids[cid].add(gid)
        if any(anchor(a) not in by_anchor for a in g['source_anchors']):external.update(ids)
    buckets=defaultdict(list)
    for cid in by_id:buckets[root(cid)].append(cid)
    rows=[];seen_projection=set()
    for ids in sorted(buckets.values(),key=lambda ids:min(ids)):
        ids=sorted(ids);component=digest(ids)[:32];source=sum((amount(by_id[c]['amount']) for c in ids),Decimal(0))
        source_abs=sum((abs(amount(by_id[c]['amount'])) for c in ids),Decimal(0))
        related=sorted({g for cid in ids for g in gids[cid]})
        reason=('original_group_spans_other_source_charges' if any(cid in external for cid in ids)
            else 'original_source_or_target_unverified' if not all(baseline[c].get('source_matched') for c in ids)
            else 'candidate_unresolved' if not all(candidate[c]['status']=='allocated' for c in ids) else '')
        maps=[];children=[];periods=[];kinds=[]
        left=[];projection_ids=[]
        for gid in related:
            for item in groups[gid]['targets']:
                pid=item['projection_record_id']
                if pid in seen_projection:raise ValueError('Original projection record overlaps comparison components')
                seen_projection.add(pid);projection_ids.append(pid);left.append(item)
        right=[allocation for cid in ids for allocation in candidate[cid]['allocations']]
        for values in (left,right):
            values_by_key=defaultdict(Decimal);child=defaultdict(set);period=defaultdict(set);kind=defaultdict(set)
            for item in values:
                target=key(item);values_by_key[target]+=amount(item['amount'])
                if item.get('child_id'):child[target].add(item['child_id'])
                if item.get('period'):period[target].add(item['period'])
                if item.get('identity_kind'):kind[target].add(item['identity_kind'])
            maps.append(values_by_key);children.append(child);periods.append(period);kinds.append(kind)
        exemplar=by_id[ids[0]]
        for index,target in enumerate(sorted(set(maps[0])|set(maps[1])) or [('', '', '')]):
            a=maps[0][target] if not reason else None;b=maps[1][target] if all(candidate[c]['status']=='allocated' for c in ids) else None
            rows.append({'component_id':component,'charge_ids':ids,'source_charge_count':len(ids),'source_first':index==0,
                'source_amount':str(source) if index==0 else None,'source_abs':str(source_abs) if index==0 else None,
                'store_id':target[0],'order_id':target[1],'product_id':target[2],
                'a_amount':str(a) if a is not None else None,'b_amount':str(b) if b is not None else None,
                'delta':str(b-a) if a is not None and b is not None else None,'comparable':not bool(reason),'reason':reason,
                'a_child_ids':sorted(children[0][target]),'b_child_ids':sorted(children[1][target]),
                'a_original_periods':sorted(periods[0][target]),'b_declared_periods':sorted(periods[1][target]),
                'a_identity_kinds':sorted(kinds[0][target]),'b_identity_kinds':sorted(kinds[1][target]),
                'original_projection_record_ids':projection_ids if index==0 else [],
                'original_group_ids':related if index==0 else [],
                'component_source_amount':str(source),'component_source_abs':str(source_abs),
                'billing_month':exemplar.get('period') or ''})
    return rows
