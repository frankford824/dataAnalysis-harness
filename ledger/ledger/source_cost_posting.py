"""Render accepted V3 evidence as distinct inputs to the unchanged cost engine.

This module neither chooses a price nor writes a ledger run. The caller first
revalidates trusted raw evidence and authority. Baseline facts identify actual
financial ownership, not merely a header price or a container's zero cost.
"""
from __future__ import annotations
from copy import copy
from datetime import date,datetime
from decimal import Decimal,DecimalException
import math

import polars as pl
from .automation_cost_actions import Pending
from .order_lifecycle import SUPERSEDED_COST_NOTE
from .engine.runtime import Ingested
from .engine.recognize import Recognition
from .engine.types import FileRef,ANCHOR_FILE,ANCHOR_ROW,ANCHOR_SHA,ANCHOR_SHEET
from .model.schema import Template,ColumnBinding

PRICE_FIELDS={'unit_cost','total_cost','cost_amount','cost_source','cost_status','cost_as_of',
              'pricing_evidence','reference_unit_cost','failure_reason','pricing_missing',
              'pricing_suspect','pricing_source_suspect','legacy_quantity_guard_pending',
              'pricing_identity_changed','__identity_changed','__ledger_history_fact_period'}


def group_key(member):
    return str(member['source_order_id']),str(member['source_member_id']),str(member['source_sku'])


def physical_key(member):
    return str(member['internal_order_id']),str(member['internal_sub_order_id'])


def _anchor(sha,sheet,row):
    return str(sha),str(sheet),int(row)


def _frames(ingestion,source):
    return [i for i in ingestion.known if i.template and i.template.source==source and i.frame is not None]


def _source_rows(items,anchor):
    result=[]
    for item in items:
        frame=item.frame
        if not {ANCHOR_SHA,ANCHOR_SHEET,ANCHOR_ROW}<=set(frame.columns):continue
        selected=frame.filter((pl.col(ANCHOR_SHA).cast(pl.String)==anchor[0])
            &(pl.col(ANCHOR_SHEET).cast(pl.String)==anchor[1])&(pl.col(ANCHOR_ROW)==anchor[2]))
        result.extend((item,row) for row in selected.to_dicts())
    return result


def original_row(ingestion,member,source='order_cost'):
    originals=getattr(ingestion,'source_recovery_originals',None)
    items=list(originals) if originals is not None else ingestion.known
    items=[i for i in items if i.template and i.template.source==source and i.frame is not None]
    matches=[]
    for ref in member['source_anchors']:
        selected=_source_rows(items,_anchor(ref['sha256'],ref['sheet'],ref['row']))
        if len(selected)!=1:raise Pending('source_original_parsed_row_missing_or_ambiguous')
        item,row=selected[0]
        if getattr(item,'derivative',None) is not None:raise Pending('source_original_is_derivative')
        if (str(row.get('internal_order_id') or '')!=str(member['source_order_id'])
            or str(row.get('internal_sub_order_id') or '')!=str(member['source_member_id'])
            or str(row.get('sku') or '')!=str(member['source_sku'])):
            raise Pending('source_original_parsed_identity_conflict')
        try:
            if float(row['quantity'])!=float(Decimal(member['logical_quantity'])):
                raise Pending('source_original_unit_mapping_changed')
        except (TypeError,ValueError,OverflowError):raise Pending('source_original_unit_mapping_changed') from None
        matches.append((item,row))
    if not matches:raise Pending('source_original_parsed_row_missing_or_ambiguous')
    # Duplicate original exports must agree in every business-bearing field;
    # only their byte/row anchors and prices are separate evidence dimensions.
    drop=PRICE_FIELDS|{ANCHOR_FILE,ANCHOR_SHA,ANCHOR_SHEET,ANCHOR_ROW}
    from .automation_store import encoded
    variants={encoded({k:v for k,v in row.items() if k not in drop}) for _,row in matches}
    if len(variants)!=1:raise Pending('source_original_nonprice_fields_conflict')
    # All business-bearing variants were proved equal. Canonical ordering
    # chooses a display anchor, never a competing price/source authority.
    return min(matches,key=lambda pair:(pair[0].ref.sha256,str(pair[1][ANCHOR_SHEET]),int(pair[1][ANCHOR_ROW])))


def owned_members(ingestion,baseline,store,period,target,members,metric):
    """Prove existing contribution ownership before selecting a missing part."""
    sl=baseline.slices.get((store.name,period))
    if sl is None:raise Pending('current_financial_slice_missing')
    by_physical={physical_key(m):m for m in members}
    if len(by_physical)!=len(members):raise Pending('physical_source_member_duplicated')
    groups={};anchors={}
    for member in members:
        groups.setdefault(group_key(member),[]).append(member)
        for ref in member['source_anchors']:
            anchors.setdefault(_anchor(ref['sha256'],ref['sheet'],ref['row']),set()).add(group_key(member))
    required={'metric_id','counted','link_key','source_id','source_note','file_sha','sheet','row_no'}
    if not required<=set(sl.facts.columns):raise Pending('current_cost_fact_provenance_missing')
    facts=sl.facts.filter((pl.col('metric_id')=='goods_cost')&pl.col('counted'))
    owned=set();current=_frames(ingestion,metric.source)
    for fact in facts.to_dicts():
        if fact.get('count_without_order') and not fact.get('linked'):
            raise Pending('existing_store_cost_ownership_unattributed')
        if str(fact.get('link_key') or '')!=str(target['coverage_key']):continue
        if SUPERSEDED_COST_NOTE in str(fact.get('source_note') or ''):
            continue  # Container zero never proves its shipped members free.
        if fact.get('source_id')!=metric.source:
            raise Pending('existing_other_cost_authority_preserved')
        anchor=_anchor(fact['file_sha'],fact['sheet'],fact['row_no'])
        logical=anchors.get(anchor,set())
        if len(logical)==1:
            group=groups[next(iter(logical))]
            original_row(ingestion,group[0],metric.source)
            owned.update(physical_key(m) for m in group)
            continue
        if len(logical)>1:raise Pending('existing_cost_component_ownership_ambiguous')
        rows=_source_rows(current,anchor)
        if len(rows)!=1:raise Pending('existing_cost_component_ownership_unverified')
        row=rows[0][1];key=(str(row.get('internal_order_id') or ''),str(row.get('internal_sub_order_id') or ''))
        member=by_physical.get(key)
        if not member or str(row.get('sku') or '')!=str(member['sku']):
            raise Pending('existing_cost_component_ownership_unverified')
        try:
            if float(row['quantity'])!=float(Decimal(member['quantity'])):
                raise Pending('existing_cost_quantity_conflict')
        except (TypeError,ValueError,OverflowError):raise Pending('existing_cost_quantity_conflict') from None
        owned.add(key)
    return owned


def supported_metric(model,store):
    metric=model.metric('goods_cost').for_platform(store.platform)
    if (metric is None or metric.source!='order_cost' or metric.value.op!='sum_product'
        or set(metric.value.of)!={'quantity','unit_cost'} or not metric.link):
        raise Pending('source_cost_original_metric_contract_unsupported')
    return metric


def guard_return_identity(ingestion,store,members):
    # Return receipts identifying only an old container cannot silently vanish
    # when that container is replaced by its physical stock members. The
    # unchanged return adapter needs exact member identities; do not invent a
    # quantity/SKU allocation for a container-level receipt.
    if getattr(store,'cost_return_posting',None)=='transaction':
        physical_ids={str(m['internal_order_id']) for m in members}
        logical_only={str(m['source_order_id']) for m in members}-physical_ids
        for item in _frames(ingestion,'after_sales'):
            if 'internal_order_id' not in item.frame.columns:continue
            for receipt in item.frame.filter(pl.col('internal_order_id').cast(pl.String).is_in(logical_only)).to_dicts():
                if receipt.get('refund_status')!='退款成功':continue
                if receipt.get('goods_status') not in ('买家未收到货','买家已退货','卖家已收到退货'):continue
                quantity=receipt.get('returned_quantity')
                try:no_return=quantity is not None and Decimal(str(quantity))==0
                except (DecimalException,TypeError,ValueError):no_return=False
                if not no_return:raise Pending('physical_source_after_sale_identity_unproved')


def render_items(ingestion,baseline,store,period,target,members,quotes,*,fact_id):
    """Select only missing stock members and preserve original nonprice policy.

    Quotes are backend-verified, keyed by (company, SKU, original day). Their
    numerical values are never taken from a model/request. The original engine
    will still execute cancellation, exemptions, dropship and after-sales.
    """
    from . import automation_source_cost_v3 as v3,automation_store as journal
    metric=supported_metric(ingestion.model,store)
    guard_return_identity(ingestion,store,members)
    owned=owned_members(ingestion,baseline,store,period,target,members,metric)
    missing=[m for m in members if physical_key(m) not in owned]
    if not missing:return [],set(),set()
    rows_by_source={};replace_anchors=set();replace_physical=set()
    for member in sorted(missing,key=physical_key):
        quote=quotes.get((member['company'],member['sku'],member['pricing_day']))
        if (not quote or quote.get('sku')!=member['sku'] or quote.get('company')!=member['company']
            or quote.get('pricing_day')!=member['pricing_day']):
            raise Pending('source_history_quote_unbound')
        amount=v3.exact_product(member['quantity'],quote['unit_cost'])
        item,original=original_row(ingestion,member,metric.source)
        row={k:value for k,value in original.items() if k not in PRICE_FIELDS}
        # Parsed order_date is the normalized semantic slot. order_time may
        # remain the original ISO string and must not mask that verified date.
        stamp=row.get('order_date') or row.get('order_time')
        day=stamp.date() if isinstance(stamp,datetime) else stamp if isinstance(stamp,date) else None
        if day is None or day.isoformat()!=member['pricing_day']:
            raise Pending('source_original_book_date_unverified')
        observation=member.get('observed_flags',{}).get('physical_member',{}).get('item_status',{})
        if observation.get('present') is not True or observation.get('value')!='Sent':
            raise Pending('physical_source_lifecycle_unproved')
        row.update(order_id=member['platform_order_id'],original_order_id=member['platform_order_id'],
            # The raw source/native proof requires child identity for TB/DY.
            # Ali is parent grain: an internal-member fallback is explicitly
            # not a claimed native child and follows the existing cost frame.
            sub_order_id=member['platform_sub_order_id'] or member['internal_sub_order_id'],
            internal_order_id=member['internal_order_id'],internal_sub_order_id=member['internal_sub_order_id'],
            parent_internal_order_id=member['source_order_id'],sku=member['sku'],
            quantity=float(v3.positive(member['quantity'])),unit_cost=float(v3.positive(quote['unit_cost'])),
            total_cost=float(amount),cost_company_id=member['company'],
            order_state=observation['value'],cost_source='history',cost_status='priced',cost_as_of=member['pricing_day'],
            source_pricing_day=member['pricing_day'],
            pricing_evidence=journal.encoded({'order_date':member['pricing_day'],
                'cost_company_id':member['company'],'source_fact_id':fact_id,
                'source_rule':v3.RULE,'source_rule_sha':v3.RULE_SHA,
                'raw_quantity':member['quantity'],'raw_unit_cost':quote['unit_cost'],
                'history_quote_sha':quote['quote_sha'],'source_scope':'physical_stock_member'}),
            source_note='独立核验物理成员及原日历史价；来源事实 '+fact_id+'；原容器身份、原件及报价见来源凭据',
            __source_v3_native_child_present=bool(member['platform_sub_order_id']))
        # These are derived admission results, not invented raw flags. Raw
        # is_suspect/is_virtual and their absence remain in the source proof.
        row.update(pricing_missing=False,pricing_suspect=False,pricing_identity_changed=False,
                   pricing_source_suspect=False)
        key=(item.ref.sha256,str(row[ANCHOR_SHEET]))
        rows_by_source.setdefault(key,{'ref':item.ref,'rows':[]})['rows'].append(row)
        replace_anchors.update(_anchor(ref['sha256'],ref['sheet'],ref['row']) for ref in member['source_anchors'])
        replace_physical.add(physical_key(member))
    items=[]
    for key,value in sorted(rows_by_source.items()):
        frame=pl.from_dicts(value['rows'],infer_schema_length=None,strict=False)
        template=Template(id=v3.TEMPLATE,name='已独立核验的聚水潭物理成员成本',source=metric.source,
            match_columns=('__internal_v3_source__',),time_slots={'order_date':'order_time'},
            bindings=tuple(ColumnBinding(role=role,columns=(role,)) for role in
                ('order_id','sub_order_id','original_order_id','internal_order_id','internal_sub_order_id',
                 'sku','quantity','unit_cost','order_time','order_state','order_type','store_name')))
        ref=value['ref']
        items.append(Ingested(ref=ref,template=template,frame=frame,rows=frame.height,
            recognition=Recognition(ref=ref,signature=template.signature,header_count=len(frame.columns),
                source_id=metric.source,template_id=template.id),
            notes=['独立来源事实；不改上传原件、订单台快照、原容器状态或既有价格']))
    return items,replace_anchors,replace_physical

def replace_inputs(ingestion,items,anchors,physical):
    """Replace only missing source rows proved by render_items, in memory."""
    from dataclasses import replace
    output=[]
    for item in ingestion.items:
        if not item.template or item.template.source!='order_cost' or item.frame is None:
            output.append(item);continue
        frame=item.frame;remove=pl.lit(False)
        if {ANCHOR_SHA,ANCHOR_SHEET,ANCHOR_ROW}<=set(frame.columns):
            for sha,sheet,row in sorted(anchors):
                remove=remove|((pl.col(ANCHOR_SHA).cast(pl.String)==sha)
                    &(pl.col(ANCHOR_SHEET).cast(pl.String)==sheet)&(pl.col(ANCHOR_ROW)==row))
        if {'internal_order_id','internal_sub_order_id'}<=set(frame.columns):
            for oid,iid in sorted(physical):
                remove=remove|((pl.col('internal_order_id').cast(pl.String)==oid)
                    &(pl.col('internal_sub_order_id').cast(pl.String)==iid))
        kept=frame.filter(~remove.fill_null(False))
        output.append(replace(item,frame=kept,rows=kept.height))
    ingestion.items=[*output,*items]


def _archive_frame(ws,item,frame,role):
    from hashlib import sha256
    from pathlib import Path
    import io,os
    buffer=io.BytesIO();frame.write_parquet(buffer,compression='zstd')
    raw=buffer.getvalue();digest=sha256(raw).hexdigest()
    folder=ws.root/'automation-evidence'/'physical-source-inputs'
    if folder.is_symlink() or folder.parent.is_symlink() or not folder.resolve().is_relative_to(ws.root.resolve()):raise Pending('frozen_source_input_path_invalid')
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=folder/(digest+'.parquet')
    if path.is_symlink():raise Pending('frozen_source_input_path_invalid')
    if path.exists():
        if sha256(path.read_bytes()).hexdigest()!=digest:raise Pending('frozen_source_input_integrity_failed')
    else:
        with path.open('xb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        path.chmod(0o600)
        if os.name!='nt':
            fd=os.open(folder,os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
    return {'role':role,'path':str(path),'sha256':digest,
        'template':item.template.model_dump(mode='json'),
        'ref':{'sha256':item.ref.sha256,'filename':item.ref.filename,'sheet':item.ref.sheet}}


def _group(row):
    return (str(row.get('internal_order_id') or ''),str(row.get('sku') or ''),
        str(row.get(ANCHOR_SHA) or row.get('file_sha') or ''),str(row.get(ANCHOR_SHEET) or row.get('sheet') or ''),
        int(row.get(ANCHOR_ROW) or row.get('row_no') or 0))


def archive_consumed(ws,ingestion,result,store,fact,items):
    """Freeze only source rows actually counted, never a whole manual total."""
    from collections import defaultdict
    own=result.facts.filter((pl.col('metric_id')=='goods_cost')&(pl.col('template_id')=='jst_source_cost_v3')
        &pl.col('counted')&(pl.col('period')==fact['period'])&(pl.col('contribution')!=0)
        &pl.col('source_note').cast(pl.String).str.contains(fact['decision_id'],literal=True).fill_null(False))
    if own.is_empty():return None
    actual=result.facts.filter((pl.col('metric_id')=='goods_cost')&(pl.col('source_id')=='order_cost')
        &pl.col('counted')&(pl.col('period')==fact['period'])&(pl.col('link_key')==str(fact['target']['coverage_key'])))
    items=ingestion.frames_of('order_cost')
    actual_groups=defaultdict(list)
    for row in actual.to_dicts():actual_groups[_group(row)].append(row)
    selected=[];keys=set()
    for item in items:
        groups=defaultdict(list)
        for index,row in enumerate(item.frame.to_dicts()):groups[_group(row)].append((index,row))
        indices=[]
        for key,rows in groups.items():
            facts=actual_groups.get(key,[])
            total=sum((Decimal(str(r['contribution'])) for r in facts),Decimal(0))
            if not facts or total==0:continue
            wanted=sum((Decimal(str(row['quantity']))*Decimal(str(row['unit_cost'])) for _,row in rows),Decimal(0))
            if len(facts)!=len(rows) or abs(abs(total)-wanted)>Decimal('.0000005'):
                raise Pending('frozen_cost_member_ownership_unproved')
            indices.extend(index for index,_ in rows);keys.add(key)
        if indices:selected.append((item,item.frame[indices]))
    if not selected:return None
    refs=[]
    for item,frame in selected:
        saved=_archive_frame(ws,item,frame,'counted_cost')
        saved['origin']='v3' if item.template.id=='jst_source_cost_v3' else 'existing_counted_source'
        saved['quote_keys']=(sorted({(str(row['cost_company_id']),str(row['sku']),str(row['cost_as_of'])) for row in frame.to_dicts()})
            if saved['origin']=='v3' else [])
        refs.append(saved)
    native=[]
    for item in ingestion.known:
        if not item.template or not ingestion.model.source(item.template.source).is_spine:continue
        frame=item.frame
        if 'order_id' not in frame.columns:continue
        rows=frame.filter(pl.col('order_id').cast(pl.String)==str(fact['target']['order_id']))
        if rows.height:
            native.append(_archive_frame(ws,item,rows,'native_order'))
    if not native:raise Pending('frozen_native_order_basis_unavailable')
    return [*refs,*native]


def frozen_inputs(ws,store,fact,frozen_run,source):
    """Read captured source inputs after their frozen accounting use is proved."""
    from .automation_period_proof import valid_artifact
    from .engine.cost_returns import number
    from .storage_integrity import digest,verified
    from .engine.types import FileRef
    from pathlib import Path
    records=source.get('source_inputs')
    if not records or not any(r.get('role')=='native_order' for r in records):
        raise Pending('frozen_source_input_archive_missing')
    manual=ws.conn.execute('SELECT result_json FROM manual_finance WHERE run_id=?',(frozen_run['id'],)).fetchone()
    if manual:
        import json
        if json.loads(manual[0]).get('manual_cost'):
            raise Pending('whole_period_manual_total_has_no_member_price_basis')
    path=ws.facts_path(frozen_run['id'])
    if not verified(path):raise Pending('frozen_counted_cost_archive_unverified')
    facts=pl.read_parquet(path)
    needed={'metric_id','template_id','counted','contribution','internal_order_id','sku','file_sha','sheet','row_no','source_note'}
    if not needed<=set(facts.columns):raise Pending('frozen_counted_cost_inventory_missing')
    counted=facts.filter((pl.col('metric_id')=='goods_cost')&(pl.col('source_id')=='order_cost')
        &pl.col('counted')&(pl.col('link_key')==str(fact['target']['coverage_key'])))
    from collections import defaultdict
    groups=defaultdict(list)
    for row in counted.to_dicts():groups[_group(row)].append(row)
    output=[]
    for record in records:
        p=Path(record['path'])
        try:relative=p.resolve().relative_to(ws.root.resolve()).as_posix()
        except ValueError:raise Pending('frozen_source_input_path_invalid') from None
        if not valid_artifact(ws,{'path':relative,'sha':record['sha256'],'sealed':False}):
            raise Pending('frozen_source_input_integrity_failed')
        frame=pl.read_parquet(p)
        template=Template.model_validate(record['template'])
        if record['role']=='counted_cost':
            if template.source!='order_cost' or record.get('origin') not in {'v3','existing_counted_source'}:
                raise Pending('frozen_cost_template_unverified')
            captured=defaultdict(list);frozen_days=[]
            from .automation_source_cost_v3 import decimal_semantic
            import json
            members={(str(m['internal_order_id']),str(m['internal_sub_order_id'])):m for m in fact['members']}
            quotes={(q['company'],q['sku'],q['pricing_day']):q for q in fact['quotes']}
            for row in frame.to_dicts():
                member=members.get((str(row.get('internal_order_id')),str(row.get('internal_sub_order_id'))))
                if not member or row.get('sku')!=member['sku']:raise Pending('frozen_source_member_identity_conflict')
                quote=quotes.get((member['company'],member['sku'],member['pricing_day']))
                evidence=json.loads(row.get('pricing_evidence') or '{}')
                common=(row.get('quantity')==float(Decimal(member['quantity']))
                    and row.get('order_id')==member['platform_order_id'])
                if not common:raise Pending('frozen_source_quantity_or_price_conflict')
                if record['origin']=='v3':
                    if (not quote or decimal_semantic(evidence.get('raw_quantity'))!=decimal_semantic(member['quantity'])
                        or decimal_semantic(evidence.get('raw_unit_cost'))!=decimal_semantic(quote['unit_cost'])
                        or row.get('unit_cost')!=float(Decimal(quote['unit_cost']))
                        or row.get('cost_as_of')!=member['pricing_day']
                        or _anchor(row[ANCHOR_SHA],row[ANCHOR_SHEET],row[ANCHOR_ROW]) not in
                            {_anchor(r['sha256'],r['sheet'],r['row']) for r in member['source_anchors']}):
                        raise Pending('frozen_source_quantity_or_price_conflict')
                elif number(row.get('unit_cost')) is None or number(row.get('unit_cost'))<=0:
                    raise Pending('frozen_existing_price_unproved')
                frozen_days.append(member['pricing_day'])
                captured[_group(row)].append(row)
            frame=frame.with_columns(pl.Series('__frozen_original_day__',frozen_days,dtype=pl.String))
            for key,rows in captured.items():
                actual=groups.get(key,[])
                amount=sum((Decimal(str(r['contribution'])) for r in actual),Decimal(0))
                expected=sum((Decimal(str(r['quantity']))*Decimal(str(r['unit_cost'])) for r in rows),Decimal(0))
                if len(actual)!=len(rows) or expected<=0 or abs(abs(amount)-expected)>Decimal('.0000005'):
                    raise Pending('frozen_cost_member_ownership_unproved')
            template=template.model_copy(update={'id':'jst_source_cost_frozen_v3','name':'冻结原销售逐成员成本依据（只读）'})
        elif record['role']!='native_order':raise Pending('frozen_source_input_role_unverified')
        # Frozen run store_id is the ownership authority. A later display
        # name/alias edit must not make the old basis vanish from policy_mask.
        # Normalize only this in-memory copy, keeping archived bytes intact.
        if frozen_run['store_id']!=store.id:raise Pending('frozen_source_store_identity_conflict')
        renames=[pl.lit(store.name).alias(k) for k in ('store_name','__hint_store__') if k in frame.columns]
        if renames:frame=frame.with_columns(renames)
        ref=FileRef(**record['ref'])
        output.append(Ingested(ref=ref,template=template,frame=frame,rows=frame.height,
            recognition=Recognition(ref=ref,signature=template.signature,header_count=len(frame.columns),
                source_id=template.source,template_id=template.id),notes=['只读复用冻结原销售实际计入依据，原月不发布']))
    return output


def merge_frozen_inputs(ingestion,items):
    """Restore exact frozen cost members; add native anchors only if absent."""
    from collections import Counter
    from dataclasses import replace
    from .automation_store import encoded
    cost=[i for i in items if i.template.source=='order_cost']
    keys=set()
    for item in cost:
        keys.update((str(r['internal_order_id']),str(r['internal_sub_order_id'])) for r in item.frame.to_dicts())
    replace_inputs(ingestion,cost,set(),keys)
    for item in items:
        if item.template.source=='order_cost':continue
        frame=item.frame
        if 'order_id' not in frame.columns:raise Pending('frozen_native_identity_missing')
        existing=[i.frame for i in ingestion.known if i.template and i.template.id==item.template.id and 'order_id' in i.frame.columns]
        missing=[]
        for parent in frame['order_id'].cast(pl.String).unique().to_list():
            saved=frame.filter(pl.col('order_id').cast(pl.String)==parent)
            rows=[r for current in existing for r in current.filter(pl.col('order_id').cast(pl.String)==parent).to_dicts()]
            if rows:
                business=set(saved.columns)-{ANCHOR_FILE,ANCHOR_SHA,ANCHOR_ROW,ANCHOR_SHEET}
                expected=Counter(encoded({k:r.get(k) for k in business}) for r in saved.to_dicts())
                current=Counter(encoded({k:r.get(k) for k in business}) for r in rows)
                if expected!=current:raise Pending('frozen_native_context_conflict')
            else:missing.append(saved)
        if missing:
            saved=pl.concat(missing,how='vertical_relaxed')
            ingestion.items.append(replace(item,frame=saved,rows=saved.height))
