"""User-supplied store categories; never infer or mutate financial identities."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path

import polars as pl

from .freight_trial_inputs import InputError

UNMATCHED='__unmatched__'


@lru_cache(maxsize=1)
def source():
    raw=(Path(__file__).parent/'data/store_classification_20261023.json').read_bytes()
    value=json.loads(raw)
    assert len(value['rows'])==115 and len({r['source_row'] for r in value['rows']})==115
    ids=[r['ledger_store_id'] for r in value['rows'] if r['match_status']=='confirmed']
    assert len(ids)==len(set(ids)) and all(ids)
    return {**value,'version':hashlib.sha256(raw).hexdigest()}


def resolve(stores):
    catalog=source();known={s['id']:s for s in stores};mapping={};unmatched=[]
    counts={g['id']:{**g,'source_count':0,'matched_count':0} for g in catalog['categories']}
    for row in catalog['rows']:
        counts[row['category_id']]['source_count']+=1
        sid=row['ledger_store_id'];store=known.get(sid)
        problem=''
        if row['match_status']=='platform_conflict':problem='平台登记不一致'
        elif row['match_status']!='confirmed':problem='尚未匹配台账店铺'
        elif not store:problem='本次试算未包含该登记店铺'
        elif store['platform']!=row['ledger_platform']:problem='登记平台已变化，需重新核对'
        if problem:unmatched.append({**row,'reason':problem,'category_name':counts[row['category_id']]['name']})
        else:
            mapping[sid]=row['category_id'];counts[row['category_id']]['matched_count']+=1
    return {'source_file':catalog['source_file'],'source_sha256':catalog['source_sha256'],
            'source_date':catalog['source_date'],'version':catalog['version'],'source_count':len(catalog['rows']),
            'matched_count':len(mapping),'categories':list(counts.values()),'unmatched':unmatched,'mapping':mapping}


def selection(catalog,only_category='',exclude_categories='',classification_view=''):
    known={g['id'] for g in catalog['categories']}
    excluded=sorted(set(v.strip() for v in exclude_categories.split(',') if v.strip()))
    if only_category and only_category not in known:raise InputError('请选择有效店铺分类')
    if any(v not in known for v in excluded):raise InputError('排除列表包含未知店铺分类')
    if classification_view not in {'','unmatched'}:raise InputError('请选择有效分类核对范围')
    if only_category in excluded:raise InputError('同一分类不能同时选择只看和排除')
    return excluded


def annotate(query,catalog):
    mapping=catalog['mapping']
    if not mapping:return query.with_columns(pl.lit(UNMATCHED).alias('store_category'))
    categories=pl.col('store_ids').list.eval(pl.element().replace_strict(mapping,default=''))
    confirmed=(pl.col('store_ids').list.len()>0)&~categories.list.contains('')&(categories.list.n_unique()==1)
    return query.with_columns(pl.when(confirmed).then(categories.list.first()).otherwise(pl.lit(UNMATCHED)).alias('store_category'))


def filter_rows(query,catalog,only_category='',exclude_categories='',classification_view=''):
    excluded=selection(catalog,only_category,exclude_categories,classification_view)
    query=annotate(query,catalog)
    if classification_view=='unmatched':return query.filter(pl.col('store_category')==UNMATCHED)
    if only_category:query=query.filter(pl.col('store_category')==only_category)
    if excluded:query=query.filter(~pl.col('store_category').is_in(excluded))
    return query
