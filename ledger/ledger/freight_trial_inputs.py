"""Capture and parse immutable inputs for a freight shadow proposal."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import sqlite3
from datetime import datetime,timezone,timedelta

import polars as pl

from .engine.rules import _norm, norm_expr
from .freight_allocation import number, text_amount


class InputError(RuntimeError):
    pass


def readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def scan_stamp(value,day):
    """Retain the original timestamp as identity; format Excel days for people."""
    raw=str(value or '')
    if re.fullmatch(r'\d{5}(\.\d+)?',raw):
        stamp=datetime(1899,12,30)+timedelta(days=float(raw))
        return stamp.isoformat(sep=' ',timespec='seconds'),raw
    return raw.replace('T',' ') or str(day or ''),raw or str(day or '')


def flag_charge_overlaps(charges):
    """Same-parcel same-time rows may be distinct outbound/return/service fees.

    Within one bill only identical signed amounts and settlement objects are
    indistinguishable candidates. Whole-file aliases are removed separately;
    overlapping nonidentical files still require source reconciliation.
    """
    overlaps={}
    for charge in charges:
        overlaps.setdefault((charge['tracking_no'],charge['scan_time_raw']),[]).append(charge)
    for group in overlaps.values():
        if len({r['file_sha'] for r in group})>1:
            for charge in group:charge['source_issue']='source_overlap'
            continue
        identical={}
        for charge in group:
            key=(number(charge['amount']),charge.get('settlement_object','').strip())
            identical.setdefault(key,[]).append(charge)
        for copies in identical.values():
            if len(copies)>1:
                for charge in copies:charge['source_issue']='duplicate_charge'


def capture(root, model, period):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period or ''):
        raise InputError('请选择有效月份')
    root = Path(root)
    with closing(readonly(root/'workspace.db')) as db:
        db.execute('BEGIN')
        active = [dict(r) for r in db.execute('SELECT store_id,name,sha FROM slot ORDER BY store_id,name')]
        runs = [dict(r) for r in db.execute('''SELECT p.store_id,p.period,p.state,p.run_id,
            r.shas FROM period p LEFT JOIN run r ON r.id=p.run_id
            WHERE p.period=? ORDER BY p.store_id''', (period,))]
        files = {r['sha']: dict(r) for r in db.execute('SELECT sha,name,size FROM file')}
    inventory = {}
    for record in active:
        name = record['name']
        if not any(word in name for word in ('订单', '宝贝', '运费')) or '成本' in name:
            continue
        value = inventory.setdefault(record['sha'], {'sha': record['sha'], 'name': name, 'owners': {}, 'active': True})
        value['owners'][record['store_id']] = 0
    for run in runs:
        for sha in json.loads(run['shas'] or '[]'):
            record = files.get(sha)
            if not record or not any(word in record['name'] for word in ('订单', '宝贝', '运费')) or '成本' in record['name']:
                continue
            value = inventory.setdefault(sha, {'sha': sha, 'name': record['name'], 'owners': {}, 'active': False})
            value['owners'].setdefault(run['store_id'], 1)
    with closing(readonly(root/'order-feed.db')) as db:
        db.execute('BEGIN')
        state = dict(db.execute('SELECT * FROM feed_state WHERE id=1').fetchone())
        mapping = [dict(r) for r in db.execute('SELECT order_store_id,ledger_store_id,mapping_status FROM feed_store ORDER BY order_store_id')]
        deltas = [dict(r) for r in db.execute("SELECT * FROM feed_entity WHERE entity_type IN ('order','order_item','order_relation') ORDER BY entity_type,entity_id")]
    manifest = json.loads(state.get('manifest_json') or '{}')
    if not manifest.get('objects'):
        raise InputError('订单台全量快照尚不可用')
    inputs = {'period': period, 'files': sorted(inventory.values(), key=lambda r: r['sha']),
              'captured_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'runs': [{k:v for k,v in r.items() if k!='shas'} for r in runs],
              'feed_manifest': manifest, 'snapshot_id': state['snapshot_id'],
              'consumed_seq': state['consumed_seq'], 'source_latest_seq': state.get('source_latest_seq'),
              'mapping': mapping, 'model_hash': digest(json.loads(model.model_dump_json())),
              'delta_hash': digest(deltas)}
    return inputs, deltas


def _materialize(root, shadow_root, record):
    source = root/'files'/record['sha'][:2]/record['sha']
    if not source.is_file():
        raise InputError('原始留档缺失：'+record['name'])
    # Hash before linking; malformed evidence never becomes a zero amount.
    h = hashlib.sha256()
    with source.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): h.update(block)
    if h.hexdigest() != record['sha']:
        raise InputError('原始留档校验未通过：'+record['name'])
    target = shadow_root/'input-copies-v1'/record['sha']/ntpath.basename(record['name'])
    # Archived originals may themselves be symlinks. Copy their verified bytes
    # into this isolated cache; never clone or overwrite the archival link.
    if not target.parent.resolve().is_relative_to(shadow_root.resolve()) or target.is_symlink():
        raise InputError('原始文件路径无效')
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        import uuid
        temporary=target.with_name('.input-'+uuid.uuid4().hex+'.tmp')
        shutil.copy2(source,temporary,follow_symlinks=True)
        check=hashlib.sha256()
        with temporary.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):check.update(block)
        if check.hexdigest()!=record['sha']:
            temporary.unlink();raise InputError('原始文件在读取时发生变化，请重新试算')
        os.replace(temporary,target)
    check=hashlib.sha256()
    with target.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):check.update(block)
    if check.hexdigest()!=record['sha']:raise InputError('试算输入副本校验未通过：'+record['name'])
    return target


def parse_sources(root, shadow_root, model, inputs, progress=lambda *_: None):
    """Use the accounting parser but never its posting/deduplication runtime."""
    from .engine.runtime import _ingest_file_cached, _header_row_candidates, engine_version
    from .model.schema import ColumnBinding
    # Read an explicitly named platform purchase quantity if an older template
    # does not bind it. This copy never changes the production model.
    templates = []
    for template in model.templates:
        if template.source=='order_detail' and not any(b.role=='quantity' for b in template.bindings):
            template = template.model_copy(update={'bindings': (*template.bindings,
                ColumnBinding(role='quantity', columns=('购买数量','商品数量','宝贝数量'), required=False, kind='number'))})
        templates.append(template)
    parsing_model = model.model_copy(update={'templates': tuple(templates)})
    model_key = digest([json.loads(parsing_model.model_dump_json()), engine_version(), 'shipment-source.v1'])
    known = [name for s in model.stores for name in (s.name,*s.aliases)]
    candidates = _header_row_candidates(parsing_model)
    file_groups, order_frames, warnings = {}, [], []
    aliases = {}
    for index, record in enumerate(inputs['files']):
        progress('读取原始账单与订单表', index, len(inputs['files']))
        path = _materialize(Path(root), Path(shadow_root), record)
        parsed = _ingest_file_cached(path, parsing_model, known, candidates,
                    Path(shadow_root)/'parse-cache', model_key, None)
        recognized = False;other_source=False
        for item in parsed:
            source = item.recognition.source_id
            if item.derivative is not None:other_source=True
            if source not in {'freight','order_detail'}:
                other_source |= bool(source and item.ok)
                continue
            if not item.ok:
                raise InputError(record['name']+'：'+(item.error or '来源未完整读取'))
            recognized = True
            frame = item.frame
            if frame is None or frame.is_empty(): continue
            if source == 'freight':
                if not {'tracking_no','total_cost','scan_time'} <= set(frame.columns):
                    raise InputError('运费账单缺少运单号、总金额或扫描时间：'+record['name'])
                fields = [c for c in ('tracking_no','scan_time','ship_date','total_cost','material_name','sub_order_id','sub_order_id_alt') if c in frame.columns]
                ordered=frame.with_columns(norm_expr(pl.col('tracking_no').cast(pl.String)).alias('__freight_tracking'))
                sort_fields=['__freight_tracking',*[c for c in fields if c!='tracking_no']]
                ordered=ordered.sort([*sort_fields,'__sheet__','__row__'])
                canonical=ordered.select(pl.col('__freight_tracking').alias('tracking_no'),*[c for c in fields if c!='tracking_no'])
                signature = hashlib.sha256(canonical.write_ndjson().encode()).hexdigest()
                if signature in file_groups:
                    aliases[record['sha']] = file_groups[signature]['sha']
                    continue
                file_groups[signature] = {'sha':record['sha'],'name':record['name'],'frame':ordered,'signature':signature}
                aliases[record['sha']] = record['sha']
            else:
                owners = {sid:p for sid,p in record['owners'].items() if sid in {s.id for s in model.stores}}
                if len(owners)!=1:
                    warnings.append({'file':record['name'],'reason':'订单原表的店铺归属不唯一，未用它确认商品金额'})
                    continue
                sid, priority = next(iter(owners.items()))
                order_frames.append((sid, priority, frame))
        if not recognized and not other_source and any(v in record['name'] for v in ('运费','订单明细','宝贝')):
            if '运费' in record['name']:
                raise InputError('该运费原表未识别，未生成不完整的金额总计：'+record['name'])
            warnings.append({'file':record['name'],'reason':'该原表未识别为可用运费或订单明细'})
    charges = []
    for value in file_groups.values():
        frame = value['frame']
        for ordinal, row in enumerate(frame.iter_rows(named=True)):
            raw_tracking = str(row.get('tracking_no') or '')
            tracking = _norm(raw_tracking)
            if not tracking or tracking in {'合计','总计','汇总'}: continue
            amount = number(row.get('total_cost'))
            if amount is None: raise InputError('运费金额缺失或无效：'+value['name'])
            day=str(row.get('ship_date') or '')
            if not re.match(r'^\d{4}-\d{2}-\d{2}',day):raise InputError('运费扫描日期缺失，无法确认账单月份：'+value['name'])
            if day[:7] != inputs['period']: continue
            when,raw_time=scan_stamp(row.get('scan_time'),row.get('ship_date'))
            sheet, row_no = str(row.get('__sheet__') or ''), int(row['__row__'])
            charge_id = digest([value['signature'],ordinal])[:32]
            charges.append({'charge_id':charge_id,'tracking_no':tracking,'raw_tracking_no':raw_tracking,
                'amount':text_amount(amount),'scan_time':when,'scan_time_raw':raw_time,'file_sha':value['sha'],'file':value['name'],
                'settlement_object':str(row.get('material_name') or '').strip(),
                'sheet':sheet,'row_no':row_no,'source_issue':'','carrier':''})
    # Cross-file overlap is not silently deduped or counted twice as confirmed.
    flag_charge_overlaps(charges)
    if not charges:
        raise InputError('该月份没有可读取的运费账单明细；未生成零元试算')
    return charges, order_frames, aliases, warnings


def feed_frames(feed_root, inputs, deltas, tracking, progress=lambda *_: None):
    from .order_feed import OrderFeed
    root = Path(feed_root).resolve()
    objects = inputs['feed_manifest']['objects']
    def source(obj):
        path = (root/objects[obj]['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file(): raise InputError('订单快照文件不可用')
        return pl.scan_parquet(path)
    def load(kind,obj,key,wanted,predicate):
        query=source(obj)
        columns=[c for c in wanted if c in query.collect_schema()]
        frame=query.select(columns).filter(predicate).collect()
        frame=OrderFeed._overlay(frame,deltas,kind,key,lambda p:[p.get(kind) or {}])
        return frame.select(columns).filter(predicate)
    hcols=('order_id','order_store_id','online_order_no','platform_order_ids','platform_order_identity_complete','tracking_no','ship_time','order_time',
        'paid_amount','refund_amount','freight_amount','order_type','order_status_raw','order_status_text','logistics_company','is_split','is_merge','split_id','merge_so_id')
    match=norm_expr(pl.col('tracking_no').cast(pl.String)).is_in(tracking)
    progress('读取实际发货范围',0,3)
    headers=load('order','orders.parquet','order_id',hcols,match)
    icols=('order_id','order_store_id','sub_order_id','outer_sku','online_order_no','quantity','line_amount','paid_amount','item_pay_amount',
        'refund_amount','tracking_no','shop_item_id','merchant_sku','sku_id','sku_type','product_name','is_gift','is_virtual','item_status_raw')
    items=load('order_item','order_items.parquet','sub_order_id',icols,
        match | pl.col('order_id').is_in(headers['order_id'].unique().to_list()))
    # Also load other shipments of the selected platform children. Otherwise a
    # partial shipment would be incorrectly treated as the entire child order.
    children=[v for v in items['outer_sku'].drop_nulls().unique().to_list() if _norm(v)]
    query=source('order_items.parquet')
    extra=query.select([c for c in icols if c in query.collect_schema()]).filter(pl.col('outer_sku').is_in(children)).collect()
    extra=OrderFeed._overlay(extra,deltas,'order_item','sub_order_id',lambda p:[p.get('order_item') or {}])
    extra=extra.select(items.columns).filter(pl.col('outer_sku').is_in(children) | match | pl.col('order_id').is_in(headers['order_id'].unique().to_list()))
    items=pl.concat([items,extra],how='vertical_relaxed').unique(maintain_order=True)
    progress('读取实际发货范围',1,3)
    headers=load('order','orders.parquet','order_id',hcols,
        match | pl.col('order_id').is_in(items['order_id'].unique().to_list()))
    if 'platform_order_ids' in headers.columns:
        mains=headers['platform_order_ids'].explode().drop_nulls().unique().to_list()
        if mains:
            headers=load('order','orders.parquet','order_id',hcols,
                pl.col('platform_order_ids').list.eval(pl.element().is_in(mains)).list.any().fill_null(False)
                | pl.col('order_id').is_in(headers['order_id'].unique().to_list()))
    rel=source('order_relations.parquet').collect()
    rel_records=[]
    for delta in deltas:
        if delta['entity_type']=='order_relation' and delta['operation']!='delete' and delta.get('payload_json'):
            value=json.loads(delta['payload_json']);rel_records.append(value.get('order_relation') or value)
    if rel_records: rel=OrderFeed._append_records(rel,rel_records).unique()
    if {'source_order_id','target_order_id'} <= set(rel.columns):
        origins=rel.filter(pl.col('target_order_id').is_in(headers['order_id'].unique().to_list()))['source_order_id'].drop_nulls().unique().to_list()
        if origins:
            headers=load('order','orders.parquet','order_id',hcols,
                match | pl.col('order_id').is_in([*headers['order_id'].unique().to_list(),*origins]))
    # Read all items of these headers for payment-control checks. A selected
    # child subset is never mistaken for the complete header total.
    complete=load('order_item','order_items.parquet','sub_order_id',icols,
        pl.col('order_id').is_in(headers['order_id'].unique().to_list()))
    items=pl.concat([items,complete],how='vertical_relaxed').unique(maintain_order=True)
    progress('读取实际发货范围',3,3)
    return headers,items,rel
