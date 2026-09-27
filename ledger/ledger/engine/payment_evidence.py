"""Resolve reviewed supplemental-payment references against platform orders.

No filenames or named order lists confer authority. Only the explicit payment
kind, SS case ID, original order export and an unambiguous same-money event do.
"""
from collections import defaultdict
from datetime import datetime
import re
import math
import polars as pl
from .types import ANCHOR_ROW


def identifier(value):
    text=str(value or '').strip()
    if text.startswith('="') and text.endswith('"'): text=text[2:-1]
    return text.strip("'`\" ")


def resolve_supplemental_payments(ingestion):
    from .statement_dedupe import _compatible_precision, _time
    orders=defaultdict(set)
    aliases={a:s.name for s in ingestion.model.stores for a in (s.name,*s.aliases)}
    def owner(row):
        hint=row.get('__hint_store__') or '';written=row.get('store_name') or ''
        hint,written=aliases.get(hint,hint),aliases.get(written,written)
        if hint and written and hint!=written:return ''
        return hint or written
    for item in ingestion.known:
        # Only the reviewed platform order export establishes the original
        # Douyin order. ERP shadows and other platforms cannot confer authority.
        if item.recognition.source_id!='order_detail' or not item.template or item.template.id!='douyin_order_detail_v1': continue
        for row in item.frame.select([c for c in ['order_id','store_name','__hint_store__'] if c in item.frame.columns]).iter_rows(named=True):
            if owner(row) and identifier(row.get('order_id')):
                orders[owner(row)].add(identifier(row.get('order_id')))
    candidates=defaultdict(list)
    all_events=defaultdict(set)
    legacy=[]
    for index,item in enumerate(ingestion.items):
        if not item.ok or not item.template or item.template.id not in {'douyin_settlement_v1','douyin_settlement_v2'}:continue
        for pos,row in enumerate(item.frame.iter_rows(named=True)):
            try:
                timestamp=_time(str(row.get('settle_time') or ''))
                datetime.fromisoformat(timestamp)
                money=float(row.get('income'))
            except (ValueError,TypeError):continue
            if not (math.isfinite(money) and money>0 and row.get('subject')=='货款结算入账'):continue
            key=(owner(row),timestamp,money,row['subject'])
            event=identifier(row.get('txn_id'));parent=identifier(row.get('base_order_id'))
            if event.isdigit():all_events[key].add(event)
            record=(index,pos,row,event,parent)
            if (item.template.id=='douyin_settlement_v1' and row.get('payment_title')=='用户向商家打款'
                and re.fullmatch(r'SS\d+',identifier(row.get('payment_case_id')))
                and event.isdigit() and parent in orders[key[0]]):
                candidates[key].append(record)
            elif item.template.id=='douyin_settlement_v2' and parent and parent not in orders[key[0]]:
                legacy.append((key,record))
    proposals=defaultdict(list)
    for key,old in legacy:
        matches=[new for new in candidates.get(key,[]) if old[3]==new[3] or _compatible_precision(old[3],new[3])]
        possible={event for event in all_events.get(key,set()) if old[3]==event or _compatible_precision(old[3],event)}
        events={new[3] for new in matches}
        if len(possible)==1 and len(events)==1 and len({(new[4],identifier(new[2].get('payment_case_id'))) for new in matches})==1:
            new=matches[0]
            proposals[(key[0],new[3])].append((old,new))
    removed=defaultdict(set);notes=defaultdict(dict)
    for (_owner,event),pairs in proposals.items():
        # A second legacy reference might be a different event whose rounded
        # ID collided. Do not choose between those records.
        if len({(old[4],identifier(old[2].get('sub_order_id'))) for old,_ in pairs}) != 1:continue
        for old,new in pairs:
            oi,op,orow,_,old_parent=old;ni,np,nrow,_,new_parent=new
            old_item,new_item=ingestion.items[oi],ingestion.items[ni]
            removed[oi].add(op)
            note=(f'补款关联互证：平台月表明确为用户向商家打款，原订单 {new_parent} 已在订单明细中核实；'
                  f'另一版关联号 {old_parent} 为不同引用，未作为原订单。'
                  f'互证来源：{old_item.ref.label()} 第{orow[ANCHOR_ROW]}行；同店、时间、科目、金额与唯一流水一致。')
            notes[ni][np]=note
            ingestion.deduplication.append(dict(source='settlement',status='reference_resolved',removed_rows=1,
                message=note,event_id=event,file_sha=new_item.ref.sha256,row_no=int(nrow[ANCHOR_ROW]),
                peer_sha=old_item.ref.sha256,peer_row_no=int(orow[ANCHOR_ROW]),original_order=new_parent))
    for index,item in enumerate(ingestion.items):
        if index in notes:
            existing=item.frame['source_note'].to_list() if 'source_note' in item.frame.columns else [None]*item.frame.height
            for pos,note in notes[index].items(): existing[pos]='；'.join(x for x in [existing[pos],note] if x)
            item.frame=item.frame.with_columns(pl.Series('source_note',existing,dtype=pl.String))
        if index in removed:
            item.frame=item.frame.filter(~pl.int_range(0,pl.len()).is_in(list(removed[index])))
