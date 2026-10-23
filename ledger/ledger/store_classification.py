"""User-supplied store categories; never infer or mutate financial identities."""
import hashlib
import json
import csv
import io
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
    proof=value.get('erp_verification')
    if proof:
        evidence=(Path(__file__).parent/'data'/proof['file']).read_bytes()
        assert hashlib.sha256(evidence).hexdigest()==proof['sha256']
        verified=json.loads(evidence)['shops']
        erp_ids=[]
        for row in value['rows']:
            if not row.get('erp_verified'):continue
            hits=[s for s in verified if str(s['shop_id'])==row['erp_shop_id'] and s['shop_name']==row['name'] and s['shop_site']==row['platform']
                  and (not row['platform_name'] or s['platform_shop_name']==row['platform_name'])]
            assert len(hits)==1
            erp_ids.append(row['erp_shop_id'])
        assert len(erp_ids)==len(set(erp_ids))
    return {**value,'version':hashlib.sha256(raw).hexdigest()}


def resolve(stores):
    catalog=source();known={s['id']:s for s in stores};mapping={};erp_mapping={};unmatched=[];erp_only=[]
    counts={g['id']:{**g,'source_count':0,'matched_count':0,'erp_matched_count':0,'ledger_matched_count':0} for g in catalog['categories']}
    for row in catalog['rows']:
        counts[row['category_id']]['source_count']+=1
        sid=row['ledger_store_id'];store=known.get(sid)
        problem=''
        if row['match_status']=='platform_conflict':problem='平台登记不一致'
        elif row['match_status']!='confirmed':problem='尚未匹配台账店铺'
        elif not store:problem='本次试算未包含该登记店铺'
        elif store['platform']!=row['ledger_platform']:problem='登记平台已变化，需重新核对'
        if not problem:
            mapping[sid]=row['category_id'];counts[row['category_id']]['ledger_matched_count']+=1
        erp_verified=bool(row.get('erp_verified') and row.get('erp_shop_id'))
        if erp_verified:
            erp_mapping[row['erp_shop_id']+'|'+row['platform']]=row['category_id']
            counts[row['category_id']]['erp_matched_count']+=1
        if not problem or erp_verified:
            counts[row['category_id']]['matched_count']+=1
            if problem:erp_only.append({**row,'reason':problem,'category_name':counts[row['category_id']]['name']})
        else:
            missing='需要带聚水潭店铺ID、所属平台及店铺名称的档案导出记录。'
            if not row.get('platform_name'):missing+='原表平台店铺名称为空，应以店铺ID核实身份。'
            same=[r for r in catalog['rows'] if row.get('platform_name') and r.get('platform_name')==row['platform_name'] and r.get('platform')==row.get('platform')]
            if len(same)>1:missing+='同平台及平台店铺名称重复，另需各行独立ID或同店曾用名证明，暂不合并。'
            unmatched.append({**row,'reason':'当前聚水潭查询未返回可核对ID；'+problem,
                'missing_evidence':missing,'category_name':counts[row['category_id']]['name']})
    return {'source_file':catalog['source_file'],'source_sha256':catalog['source_sha256'],
            'source_date':catalog['source_date'],'version':catalog['version'],'source_count':len(catalog['rows']),
            'matched_count':sum(g['matched_count'] for g in counts.values()),'ledger_matched_count':len(mapping),
            'erp_matched_count':len(erp_mapping),'erp_verification':catalog.get('erp_verification'),
            'categories':list(counts.values()),'unmatched':unmatched,'erp_only':erp_only,'mapping':mapping,'erp_mapping':erp_mapping}


def unmatched_csv(catalog,version=''):
    if version and version!=catalog['version']:raise InputError('分类名单已更新，请重新读取后导出')
    output=io.StringIO();writer=csv.writer(output)
    writer.writerow(['原表行','店铺名称','所属平台','平台店铺名称','名单分类','当前核查结果','缺少的依据','原核查说明','名单来源','名单校验值'])
    def safe(value):
        value=str(value or '')
        return "'"+value if value.lstrip().startswith(('=','+','-','@')) or (value.isdigit() and len(value)>=15) else value
    for row in catalog['unmatched']:
        writer.writerow([safe(v) for v in [row['source_row'],row['name'],row['platform'],row['platform_name'],row['category_name'],
            row['reason'],row['missing_evidence'],row['note'],catalog['source_file'],catalog['version']]])
    return '\ufeff'+output.getvalue()


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
    def agreed(column,mapping):
        if not mapping:return pl.lit(UNMATCHED)
        categories=pl.col(column).list.eval(pl.element().replace_strict(mapping,default=''))
        confirmed=(pl.col(column).list.len()>0)&~categories.list.contains('')&(categories.list.n_unique()==1)
        return pl.when(confirmed).then(categories.list.first()).otherwise(pl.lit(UNMATCHED))
    ledger=agreed('store_ids',mapping)
    if 'erp_shop_keys' not in query.collect_schema():return query.with_columns(ledger.alias('store_category'))
    erp=agreed('erp_shop_keys',catalog.get('erp_mapping',{}))
    # ERP identities come from each actual package member/header, not bill names.
    # Incomplete or contradictory ERP evidence must not fall back to a partial
    # set of mapped ledger candidates. Old artifacts without IDs still work.
    conflict=(ledger!=UNMATCHED)&(erp!=UNMATCHED)&(ledger!=erp)
    category=pl.when(pl.col('erp_shop_keys').list.len()>0).then(pl.when(conflict).then(pl.lit(UNMATCHED)).otherwise(erp)).otherwise(ledger)
    return query.with_columns(category.alias('store_category'))


def filter_rows(query,catalog,only_category='',exclude_categories='',classification_view=''):
    excluded=selection(catalog,only_category,exclude_categories,classification_view)
    query=annotate(query,catalog)
    if classification_view=='unmatched':return query.filter(pl.col('store_category')==UNMATCHED)
    if only_category:query=query.filter(pl.col('store_category')==only_category)
    if excluded:query=query.filter(~pl.col('store_category').is_in(excluded))
    return query
