"""Publish original platform order dates, with source-row evidence, for cost pricing."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid

from .engine.link import normalize_key
from .engine.types import ANCHOR_SHA, ANCHOR_ROW


def collect(ingestion, store):
    dates = defaultdict(dict)
    for source in ingestion.frames_of('order_detail'):
        frame = source.frame
        if frame is None or source.template.id.startswith('order_console_'):
            continue
        time_fields = [name for name in ('order_time', 'order_date') if name in frame.columns]
        if not time_fields or not {ANCHOR_SHA, ANCHOR_ROW} <= set(frame.columns):
            continue
        columns = [name for name in ('order_id','sub_order_id','store_name',ANCHOR_SHA,ANCHOR_ROW,*time_fields) if name in frame.columns]
        for row in frame.select(columns).iter_rows(named=True):
            if row.get('store_name') and row['store_name'] not in [store.name,*store.aliases]:
                continue
            sha = str(row[ANCHOR_SHA] or '')
            if not re.fullmatch(r'[0-9a-f]{64}', sha):
                continue
            when = next((row.get(name) for name in time_fields if row.get(name)), None)
            try:
                if isinstance(when,datetime) and when.tzinfo is not None:
                    when=when.astimezone(timezone(timedelta(hours=8)))
                day = when.date().isoformat() if isinstance(when,datetime) else when.isoformat() if isinstance(when,date) else date.fromisoformat(str(when)[:10]).isoformat()
            except (ValueError, TypeError):
                continue
            for name in ('order_id','sub_order_id'):
                key = normalize_key(row.get(name))
                if re.fullmatch(r'\d{19}',key):
                    dates[key].setdefault(day,{'document_sha':sha,'document_row':int(row[ANCHOR_ROW])})
    return [{'order_id':key,'order_date':next(iter(found)) if len(found)==1 else None,
             'status':'confirmed' if len(found)==1 else 'conflict',
             'evidence':[{'order_date':day,**proof} for day,proof in sorted(found.items())]}
            for key,found in sorted(dates.items())]


def publish(ingestion, store, workspace_root):
    if store.platform not in {'taobao','douyin'}:
        return
    if not re.fullmatch(r'[A-Za-z0-9_-]+',store.id):
        raise ValueError('Invalid store identity for original-date context')
    rows = collect(ingestion,store)
    body = {'version':1,'store_id':store.id,'platform':store.platform,'orders':rows}
    encoded=json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    digest=hashlib.sha256(encoded).hexdigest()
    root=Path(workspace_root)/'order-date-context';target=root/(store.id+'.json')
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==digest:
        return
    root.mkdir(parents=True,exist_ok=True)
    temp=root/(store.id+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_bytes(encoded);temp.replace(target)
