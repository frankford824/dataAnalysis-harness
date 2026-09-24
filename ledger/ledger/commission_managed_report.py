"""Read-only managed subsets of the existing person accounting amounts.

Managed ownership is frozen in calculation evidence. These are 'of which'
amounts, never extra earnings or an allocation of manually confirmed payouts.
"""
from collections import OrderedDict
from decimal import Decimal
import hashlib
from io import BytesIO
import json
from pathlib import Path

import polars as pl

from . import commission_engine, derived_read_cache
from .money import money_float
from .read_cache import cached

_cache = OrderedDict()
_code = None
FIELDS = ('sales','gross','profit_after_labor','trial_amount')


def evidence(registry, commission):
    global _code
    cid = commission.get('calculation_id')
    if not cid:
        return None
    with registry.connect() as conn:
        row = conn.execute('SELECT path,sha FROM calculation WHERE id=?',(cid,)).fetchone()
    if row is None:
        return None
    path = registry.root/'calculations'/Path(row['path']).name
    try:
        stat = path.stat()
    except OSError:
        return None
    if _code is None:
        _code = hashlib.sha256(Path(__file__).read_bytes()+Path(commission_engine.__file__).read_bytes()).hexdigest()
    key = (str(path),row['sha'],stat.st_size,stat.st_mtime_ns,_code)
    durable = 'managed-detail:'+hashlib.sha256(json.dumps(key).encode()).hexdigest()
    def build():
        hit = derived_read_cache.get(registry,durable)
        if hit is not None:
            return hit
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row['sha']:
            return None
        schema = pl.read_parquet_schema(BytesIO(raw))
        required = {'managed','managed_team_id','product_id','person_id','spine_row','status','share','total_rate','duty','participation_sales','participation_gross','participation_profit','amount'}
        if not required <= set(schema):
            return None
        frame = pl.read_parquet(BytesIO(raw),columns=sorted(required | ({'product_name'} & set(schema))))
        if 'product_name' not in frame.columns:
            frame=frame.with_columns(pl.lit('').alias('product_name'))
        frame=frame.with_columns(pl.col('managed_team_id').fill_null('').alias('managed_team_id'))
        pool_base=(frame.filter(pl.col('managed').fill_null(False) & (pl.col('managed_team_id')!='') & pl.col('status').is_in(['distribute','exclude','hold','wage_pending']))
              .unique(subset=['spine_row','product_id','managed_team_id']))
        paid=commission_engine.production_weights(frame.filter(pl.col('status')=='distribute'))
        if paid.filter(pl.any_horizontal(*[pl.col(k).is_null() for k in ('participation_sales','participation_gross','participation_profit','share','total_rate','amount')]) | (pl.col('total_rate').cast(pl.Decimal(16,8),strict=False).fill_null(0)<=0)).height:
            return None
        keys=['spine_row','product_id']
        paid=paid.with_columns(pl.col('duty').is_in(['produce','cut']).fill_null(False).all().over(keys).alias('_known'))
        ownership=paid.select(*keys,(pl.col('_known') & (pl.col('__output_share').sum().over(keys)>0)).alias('_covered')).unique(subset=keys)
        pool=(pool_base.join(ownership,on=keys,how='left').group_by('managed_team_id','product_id').agg(
            pl.col('participation_sales').cast(pl.Decimal(28,10)).sum().alias('sales'),pl.col('product_name').first(),
            pl.col('_covered').fill_null(False).all().alias('complete')))
        share=pl.col('__output_share').cast(pl.Decimal(28,10))/pl.col('__output_rate').cast(pl.Decimal(28,10))
        legacy=pl.col('share').cast(pl.Decimal(28,10))/pl.col('total_rate').cast(pl.Decimal(28,10))
        paid=paid.with_columns(
            pl.when(pl.col('managed').fill_null(False)).then(pl.col('managed_team_id')).otherwise(pl.lit('')).alias('_team'),
            (pl.col('participation_sales').cast(pl.Decimal(28,10))*share).alias('_sales'),
            (pl.col('participation_gross').cast(pl.Decimal(28,10))*share).alias('_gross'),
            (pl.col('participation_profit').cast(pl.Decimal(28,10))*share).alias('_profit'),
            (pl.col('participation_sales').cast(pl.Decimal(28,10))*legacy).alias('_legacy_sales'),
            (pl.col('participation_profit').cast(pl.Decimal(28,10))*legacy).alias('_legacy_profit'))
        groups=paid.group_by('_team','person_id','product_id').agg(
            *[pl.col(k).sum() for k in ('_sales','_gross','_profit','_legacy_sales','_legacy_profit','amount')],
            pl.col('_known').all(),pl.col('product_name').first())
        result={'pool':pool.sort('managed_team_id','product_id').to_dicts(),
                'groups':groups.sort('_team','person_id','product_id').to_dicts()}
        # Preserve Decimal evidence precision across the optional durable cache.
        result=json.loads(json.dumps(result,default=str))
        derived_read_cache.put(registry,durable,result)
        return result
    try:
        return cached(_cache,key,build,8)
    except (OSError,ValueError,pl.exceptions.PolarsError):
        return None


def _split(total, weights):
    from .commission_reports import _split_cents
    value=Decimal(str(total))
    parts=_split_cents(abs(value),weights)
    return None if parts is None else {k:v*(1 if value>=0 else -1) for k,v in parts.items()}


def _reconcile(values, target, weights):
    if target is None or any(v is None for v in values.values()):
        return {k:None for k in values}
    rounded={k:Decimal(str(money_float(v))) for k,v in values.items()}
    difference=Decimal(str(target))-sum(rounded.values(),Decimal(0))
    changes=_split(difference,weights)
    return {k:money_float(v+changes[k]) if changes is not None else None for k,v in rounded.items()}


def scope(registry, commission, *, managed, outputs, profits, payout_profits, rates,
          trials, keep, roster, store_id, store, period, run_id, selected_people=()):
    """Split each person's existing costs using their original output-sales basis.

    First split across ALL products, then select managed subsets. Selecting a
    person or team must never change denominators or consume other people's costs.
    """
    raw=evidence(registry,commission)
    if raw is not None:
        archived_totals={}
        for p in raw['pool']:
            archived_totals[p['managed_team_id']]=archived_totals.get(p['managed_team_id'],Decimal(0))+Decimal(str(p['sales']))
        if {k:money_float(v) for k,v in archived_totals.items()}!={k:money_float(Decimal(str(v))) for k,v in managed.items()}:
            raw=None  # Never mix a new classification with an older sales pool.
    common={'store_id':store_id,'store':store,'period':period,'finance_run':run_id,
            'evidence_available':raw is not None,
            'amount':None,'confirmed_amount':None,'kind':'managed_detail',
            'notes':'其中托管部分，不与人员/店铺合计重复相加；不拆分实发'}
    leaves=[]
    if raw is not None:
        people={}
        for i,g in enumerate(raw['groups']):
            people.setdefault(g['person_id'],{})[str(i)]=g
        for pid,groups in people.items():
            weights={k:max(Decimal(str(g['_sales'])),Decimal(0)) for k,g in groups.items()}
            gross=_reconcile({k:Decimal(str(g['_gross'])) for k,g in groups.items()},outputs.get(pid,{}).get('gross'),weights)
            profit=_reconcile({k:Decimal(str(g['_profit'])) for k,g in groups.items()},profits.get(pid),weights)
            if rates.get(pid) is not None and pid in payout_profits:
                legacy_weights={k:max(Decimal(str(g['_legacy_sales'])),Decimal(0)) for k,g in groups.items()}
                legacy_profit=_reconcile({k:Decimal(str(g['_legacy_profit'])) for k,g in groups.items()},payout_profits[pid],legacy_weights)
                amounts={k:None if v is None else Decimal(str(v))*rates[pid] for k,v in legacy_profit.items()}
            else:
                amounts={k:Decimal(str(g['amount']))*keep for k,g in groups.items()}
            trial=_reconcile(amounts,trials.get(pid),{k:abs(v) if v is not None else Decimal(0) for k,v in amounts.items()})
            for k,g in groups.items():
                if not g['_team']:
                    continue
                leaves.append({**common,'team_id':g['_team'],'person_id':pid,
                    'person':roster.get(pid,{}).get('name') or '待确认人员',
                    'product_id':g['product_id'],'subject':g['product_name'] or g['product_id'] or '缺少商品ID',
                    'sales':money_float(Decimal(str(g['_sales']))) if g['_known'] else None,
                    'gross':gross[k],'profit_after_labor':profit[k],'trial_amount':trial[k]})
        # Unassigned/excluded/unknown-duty sales remain visible, never guessed.
        by_product={}
        for r in leaves:by_product.setdefault((r['team_id'],r['product_id']),[]).append(r)
        for p in raw['pool']:
            key=(p['managed_team_id'],p['product_id'])
            matching=by_product.get(key,[])
            if matching and all(r['sales'] is not None for r in matching) and p['complete']:
                amounts=_reconcile({str(i):Decimal(str(r['sales'])) for i,r in enumerate(matching)},money_float(Decimal(str(p['sales']))),{str(i):abs(Decimal(str(r['sales']))) for i,r in enumerate(matching)})
                for i,r in enumerate(matching):r['sales']=amounts[str(i)]
            represented=sum((Decimal(str(r['sales'])) for r in matching if r['sales'] is not None),Decimal(0))
            residual=Decimal(str(money_float(Decimal(str(p['sales'])))))-represented
            if residual or not matching:
                leaves.append({**common,'team_id':p['managed_team_id'],'person_id':'','person':'待分配',
                    'product_id':p['product_id'],'subject':p['product_name'] or p['product_id'] or '缺少商品ID',
                    'sales':money_float(residual),'gross':None,'profit_after_labor':None,'trial_amount':None})
    teams={}
    for tid,total in managed.items():
        team=roster.get(tid,{}).get('alias') or roster.get(tid,{}).get('name') or tid
        known=[r for r in leaves if r['team_id']==tid and r.get('sales') is not None]
        if known:
            corrected=_reconcile({str(i):Decimal(str(r['sales'])) for i,r in enumerate(known)},total,
                {str(i):abs(Decimal(str(r['sales']))) for i,r in enumerate(known)})
            for i,r in enumerate(known):r['sales']=corrected[str(i)]
        members={}
        for leaf in leaves:
            if leaf['team_id']!=tid or selected_people and leaf['person_id'] not in selected_people:
                continue
            members.setdefault(leaf['person_id'],[]).append(leaf)
        if not members and not selected_people:
            members['']=[{**common,'team_id':tid,'person_id':'','person':'待分配','subject':'缺少可核对的托管明细',
                          'product_id':'','sales':total,'gross':None,'profit_after_labor':None,'trial_amount':None}]
        children=[]
        def summed(rows):
            return {field:money_float(sum((Decimal(str(r[field])) for r in rows),Decimal(0))) if all(r.get(field) is not None for r in rows) else None for field in FIELDS}
        for pid,products in sorted(members.items()):
            for i,p in enumerate(products):
                p.update(team=team,key=f'{run_id}:{tid}:{pid}:{i}:product',status='明细待核对' if any(p[f] is None for f in FIELDS) else '其中托管部分')
            children.append({**common,**summed(products),'key':f'{run_id}:{tid}:{pid}:person','subject':products[0]['person'],
                'person':products[0]['person'],'person_id':pid,'team_id':tid,'team':team,'children':products,
                'status':'待分配' if not pid else '部分待核对' if any(p[f] is None for p in products for f in FIELDS) else '其中托管部分'})
        if children:
            teams[tid]={**common,**summed(children),'key':f'{run_id}:{tid}:team','subject':team+' · 托管合计',
                        'team_id':tid,'team':team,'person':'托管合计','children':children,'status':'筛选人员托管小计' if selected_people else '其中托管部分'}
            if not selected_people:teams[tid]['sales']=total
            if any(teams[tid][f] is None for f in FIELDS):teams[tid]['status']='部分待核对'
    return list(teams.values())
