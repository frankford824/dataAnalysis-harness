"""Fail closed on overlapping dated promotion exports; do not invent dedupe."""
from collections import defaultdict
import hashlib
import json
import polars as pl

PREFIX='推广证据待核对：'


def business_fingerprint(table):
    from .normalize import _as_text
    # Campaign deletion is a current UI status, not another historical charge.
    # All other original columns, row multiplicity and ordering must match.
    columns=[i for i,h in enumerate(table.headers) if str(h).strip()!='是否已删除']
    h=hashlib.sha256(json.dumps([table.headers[i] for i in columns],ensure_ascii=False).encode())
    for row in table.rows:
        h.update(json.dumps([_as_text(row.cells[i]) if i<len(row.cells) else None for i in columns],ensure_ascii=False,default=str).encode())
        h.update(b'\n')
    return h.hexdigest()


def overlaps(ingestion):
    parts=[]
    seen={}
    # A clearly labelled promotion upload that is actually an order table is
    # not evidence that ad spend was zero. Mixed workbooks containing a real
    # promotion sheet remain supported.
    by_file=defaultdict(list)
    for item in ingestion.items:by_file[item.ref.sha256].append(item)
    if any(s.id=='promotion' for s in ingestion.model.sources):
        for items in by_file.values():
            for item in items:
                if item.recognition.source_id=='promotion' and not item.ok and not item.derivative:
                    ingestion.validation_errors.setdefault('promotion',[]).append(
                        PREFIX+f'{item.ref.label()} 解析未通过，不能按零费用核定：{item.error}')
            labelled=any(i.ref.filename.startswith(('推广-','推广_')) for i in items)
            if labelled and any(not i.derivative for i in items) and not any(i.ok and i.recognition.source_id=='promotion' for i in items):
                message=PREFIX+f'{items[0].ref.filename} 实际识别为其他业务表，未找到推广费用数据；请核对是否上传错表，不能视为推广费为零'
                ingestion.validation_errors.setdefault('promotion',[]).append(message)
    for index,item in enumerate(ingestion.items):
        if not item.ok or item.recognition.source_id!='promotion':continue
        f=item.frame
        owners=f['__hint_store__'].drop_nulls().unique().to_list() if '__hint_store__' in f.columns else []
        scope=f['source_period'].drop_nulls().unique().sort().to_list() if 'source_period' in f.columns else []
        if not scope and '__hint_period__' in f.columns:scope=f['__hint_period__'].drop_nulls().unique().sort().to_list()
        if len(owners)==1 and item.business_fingerprint:
            key=(owners[0],tuple(scope),item.template.id,item.business_fingerprint)
            prior=seen.get(key)
            if prior:
                count=f.height;item.frame=f.clear()
                message=f'推广表完整业务内容重复：{item.ref.label()} 与 {prior.ref.label()} 的日期、商品、计划及全部费用数据一致；只计一次，计划删除状态不改变历史费用。原文件均保留。'
                ingestion.deduplication.append(dict(source='promotion',namespace='promotion_business_table',status='deduplicated',removed_rows=count,
                    file=item.ref.filename,sheet=item.ref.sheet,sha256=item.ref.sha256,kept_sha=prior.ref.sha256,message=message))
                item.notes.append(message)
                continue
            seen[key]=item
        if not {'spend_date','product_id','spend'}<=set(f.columns):continue
        owner=pl.col('__hint_store__') if '__hint_store__' in f.columns else pl.lit(None,dtype=pl.String)
        p=f.filter((pl.col('spend').fill_null(0)!=0) & pl.col('spend_date').is_not_null()
                   & pl.col('product_id').is_not_null() & (pl.col('product_id')!='__store_wide__')).select(
            owner.alias('owner'),'product_id','spend_date',pl.lit(item.ref.sha256).alias('sha'),pl.lit(index).alias('item')).unique()
        parts.append(p)
    if not parts:return
    keys=pl.concat(parts,how='vertical_relaxed').filter(pl.col('owner').is_not_null())
    dup=keys.group_by('owner','product_id','spend_date').agg(pl.col('sha').n_unique().alias('files')).filter(pl.col('files')>1)
    if dup.is_empty():return
    indexes=keys.join(dup,on=['owner','product_id','spend_date'],how='inner')['item'].unique().to_list()
    names=[ingestion.items[i].ref.label() for i in indexes]
    message=PREFIX+'多份推广表覆盖相同商品和日期，无法证明可以叠加；请在数据与店铺核实替换关系或独立范围：'+'；'.join(names)
    ingestion.validation_errors.setdefault('promotion',[]).append(message)
    for index in indexes:
        item=ingestion.items[index]
        item.frame=item.frame.with_columns(pl.lit(message).alias('source_note'))
