"""Read-only creator attribution over immutable financial evidence.

Confirmed, effective product duties resolve independent sales, cost, gross and
product-profit output. They never replace archived income, financial splits,
share, payout or confirmation data.
Organization roles and suggestions are deliberately not authority.
"""
from collections import OrderedDict, defaultdict
from decimal import Decimal
from datetime import datetime
import json
import hashlib
import math
import threading

import polars as pl

_cache = OrderedDict()
_lock = threading.Lock()


def _revision(conn, store_id):
    row = conn.execute('SELECT generation FROM scheme_store_clock WHERE store_id=?', (store_id,)).fetchone()
    members = [dict(row) for row in conn.execute('SELECT * FROM store_member WHERE store_id=? ORDER BY person_id,valid_from,id', (store_id,))]
    revision = str(row[0] if row else 0) + ':' + hashlib.sha256(json.dumps(members,sort_keys=True).encode()).hexdigest()
    return revision, members


def rule_revision(registry, store_id):
    """Small transactional fingerprint; no product JSON decoding on cache hits."""
    with registry.connect() as conn:
        conn.execute('BEGIN')
        return _revision(conn, store_id)[0]


class _RulesChanged(Exception):
    pass


def rules(registry, store_id, *, expected=None):
    with registry.connect() as conn:
        conn.execute('BEGIN')
        revision, members = _revision(conn, store_id)
        if expected is not None and expected != revision:
            raise _RulesChanged()
        key = (str(registry.root), store_id, revision)
        with _lock:
            if key in _cache:
                _cache.move_to_end(key)
                return revision, _cache[key]
        grouped = defaultdict(list)
        for row in conn.execute('SELECT rs.product_id,rs.value,s.active_version FROM scheme_read_segment rs JOIN scheme s ON s.id=rs.scheme_id WHERE rs.store_id=? ORDER BY rs.valid_from', (store_id,)):
            grouped[row[0]].append({**json.loads(row[1]),'_version_id':row[2]})
    with _lock:
        _cache[key] = (grouped, members)
        while len(_cache) > 128:
            _cache.popitem(last=False)
    return revision, (grouped, members)


def period_consensus(configured, members, product, period, pids):
    """Prove the existing uniform-fallback case, including duties and coverage."""
    from .commission_registry import member_active_at
    try:
        year, month = map(int, period.split('-'))
        start = datetime(year,month,1).isoformat()
        end = datetime(year+(month==12),month%12+1,1).isoformat()
    except (ValueError, TypeError):
        return None
    groups = [configured[product]] if product and product in configured else [configured['*']] if product and '*' in configured else list(configured.values())
    signature = None; chosen = None; versions = set()
    for segments in groups:
        active = [s for s in segments if s['valid_from'] < end and (not s.get('valid_to') or s['valid_to'] > start)]
        if not active:
            continue
        cursor = start
        for segment in active:
            if segment['valid_from'] > cursor or segment.get('mode','distribute')!='distribute':
                return None
            defaults = {m['person_id']:m['duty'] for m in members if member_active_at(m,segment['valid_from'])}
            allocations = [{**a,'duty':a.get('duty') if a.get('duty') in ('produce','cut') else defaults.get(a['person_id'])} for a in segment.get('allocations',[])]
            if {a['person_id'] for a in allocations}!=pids or any(a.get('duty') not in ('produce','cut') for a in allocations):
                return None
            current = (tuple(sorted((a['person_id'],a['duty'],Decimal(str(a['rate']))) for a in allocations)),bool(segment.get('managed')),segment.get('managed_team_id',''))
            if signature is not None and signature!=current:
                return None
            signature=current;chosen={**segment,'allocations':allocations}
            versions.add(segment.get('_version_id',''))
            cursor=max(cursor,min(segment.get('valid_to') or end,end))
        if cursor < end:
            return None
    if chosen is not None:
        chosen['_version_id'] = next(iter(versions)) if len(versions)==1 else 'period-consensus:'+hashlib.sha256(json.dumps(sorted(versions)).encode()).hexdigest()
    return chosen


def attach(registry, store_id, period, details, snapshot=None):
    """Resolve complete participant groups before filtering to any one person."""
    required = {'product_id','person_id','share','total_rate','spine_row'}
    if not required <= set(details.columns) or details.is_empty():
        return details
    revision, (configured, members) = snapshot if snapshot is not None else rules(registry, store_id)
    columns = [x for x in (*required, 'order_at','duty','managed','fallback_reason') if x in details.columns]
    rows = details.select(columns).to_dicts()
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[(row['product_id'], row['spine_row'])].append(index)
    shares = ['0'] * len(rows); rates = ['1'] * len(rows)
    pending = [False] * len(rows); sources = [''] * len(rows)
    versions = [''] * len(rows)
    consensus_cache = {}
    for (product, _), indexes in groups.items():
        first = rows[indexes[0]]
        stamp = first.get('order_at')
        if isinstance(stamp, datetime):
            stamp = stamp.replace(tzinfo=None).isoformat(timespec='seconds')
        else:
            stamp = str(stamp or '').replace(' ', 'T')[:19]
        candidates = configured.get(product, [])
        prior = [s for s in candidates if stamp and s['valid_from'] <= stamp]
        if not prior:
            prior = [s for s in configured.get('*',[]) if stamp and s['valid_from'] <= stamp]
        latest = prior[-1] if prior else None
        current = latest if latest and (not latest.get('valid_to') or stamp < latest['valid_to']) else None
        pids = {rows[i]['person_id'] for i in indexes}
        from_consensus = False
        if (not product or not stamp) and first.get('fallback_reason')=='store_uniform_distribution':
            key = (product,tuple(sorted(pids)))
            if key not in consensus_cache:
                consensus_cache[key] = period_consensus(configured,members,product,period,pids)
            if consensus_cache[key] is not None:
                current = consensus_cache[key]
                from_consensus = True
        archive = {rows[i]['person_id']:rows[i].get('duty') for i in indexes}
        explicit = {a['person_id']:a.get('duty') for a in (current or {}).get('allocations', [])}
        declared = defaultdict(set)
        for allocation in (current or {}).get('allocations', []):
            if allocation.get('duty') in ('produce','cut'):
                declared[allocation['person_id']].add(allocation['duty'])
        conflicted = any(len(values)>1 for values in declared.values())
        if current:
            from .commission_registry import member_active_at
            confirmed = {m['person_id']:m['duty'] for m in members if member_active_at(m,current['valid_from'])}
            explicit = {pid:duty if duty in ('produce','cut') else confirmed.get(pid) for pid,duty in explicit.items()}
        # Preserve managed-sales evidence; a new managed destination requires
        # its team projection to refresh rather than silently crediting a person.
        managed_pending = bool(current and current.get('managed') and not first.get('managed'))
        roster_pending = bool(current and set(explicit)!=pids)
        if all(rows[i].get('managed') for i in indexes):
            duties, source = {pid:'cut' for pid in pids}, 'archived_managed'
            roster_pending = False
        elif current and set(explicit)==pids and all(d in ('produce','cut') for d in explicit.values()):
            duties, source = explicit, 'effective_period_rule' if from_consensus else 'effective_product_rule'
        elif all(d in ('produce','cut') for d in archive.values()):
            duties, source = archive, 'archived_duty'
        else:
            duties, source = {}, 'pending_identity'
        missing_input = (not product or ('order_at' in columns and not stamp)) and source!='archived_managed' and not from_consensus
        known = bool(duties) and not managed_pending and not roster_pending and not conflicted and not missing_input
        denominator = sum((Decimal(str(rows[i]['share'])) for i in indexes if duties.get(rows[i]['person_id'])=='produce'), Decimal(0))
        for i in indexes:
            row = rows[i]
            pending[i] = not known
            sources[i] = 'pending_input' if missing_input else 'pending_identity' if conflicted else 'pending_roster_refresh' if roster_pending else source if not managed_pending else 'pending_managed_refresh'
            versions[i] = (current or {}).get('_version_id','') if source.startswith('effective_') else ''
            shares[i] = str(row['share']) if duties.get(row['person_id'])=='produce' else '0'
            rates[i] = str(denominator or 1)
    return details.with_columns(
        pl.Series('__sales_share', shares).cast(pl.Decimal(16,8)),
        pl.Series('__sales_rate', rates).cast(pl.Decimal(16,8)),
        pl.Series('__sales_pending', pending), pl.Series('__sales_source', sources),
        pl.Series('__sales_versions', versions),
        pl.lit(revision).alias('__sales_rule_revision'))


def expression(frame):
    value = pl.col('participation_sales').cast(pl.Decimal(28,10)) * pl.col('__sales_share') / pl.col('__sales_rate')
    if 'managed' in frame.columns:
        value = pl.when(pl.col('managed').fill_null(False)).then(0).otherwise(value)
    return value


def creator_fields(frame):
    """Independent product output; commission shares remain in archived money.

    The effective, proven producer share already resolved by ``attach`` owns
    every source fact. Managed merchandise keeps its established team sales
    treatment and is shown through the managed report instead.
    """
    required = {'__sales_share','__sales_rate','__sales_pending','person_id','spine_row',
                'product_id','participation_sales'}
    if not required <= set(frame.columns) or frame.is_empty():
        return frame
    managed = (pl.col('managed').fill_null(False) if 'managed' in frame.columns
               else pl.lit(False))
    share = pl.col('__sales_share').cast(pl.Decimal(16,8))
    rate = pl.col('__sales_rate').cast(pl.Decimal(16,8))
    no_producer = (share.sum().over('spine_row','product_id') <= 0)
    missing = (pl.col('participation_sales').is_null() |
               pl.col('spine_row').is_null() |
               pl.col('person_id').is_null() |
               pl.col('__sales_rate').is_null() | (rate <= 0) | (share < 0))
    for field in ('participation_gross','participation_profit'):
        if field in frame.columns:
            missing |= pl.col(field).is_null()
        else:
            missing |= pl.lit(True)
    for field in ('participation_sales','participation_gross','participation_profit'):
        if field in frame.columns:
            missing |= pl.col(field).n_unique().over('spine_row','product_id') != 1
    pending = (~managed) & (pl.col('__sales_pending') | no_producer |
                            missing.any().over('spine_row','product_id'))
    values = {}
    for name, source in (('creator_sales','participation_sales'),
                         ('creator_gross','participation_gross'),
                         ('creator_profit','participation_profit')):
        amount = (pl.col(source).cast(pl.Decimal(28,10), strict=False)
                  if source in frame.columns else pl.lit(None, dtype=pl.Decimal(28,10)))
        values[name] = amount * share / rate
    creator_values = [(key,value) for key,value in values.items() if key!='creator_sales']
    calculated = frame.with_columns(
        pending.alias('creator_pending'),
        pl.when(managed).then(0).when(pending).then(None)
          .otherwise(values['creator_sales']).alias('creator_sales'),
        *[pl.when(managed | pending).then(None).otherwise(value).alias(name)
          for name,value in creator_values],
    )
    calculated = reconcile_creator_cents(calculated)
    return calculated.with_columns(
        (pl.col('creator_sales') - pl.col('creator_gross')).alias('creator_cost'))


def reconcile_creator_cents(frame):
    """Assign each product's display-cent residue to one proven producer.

    Keep sub-cent source precision until the product/person aggregation, then
    add the small rounding residue to the largest producer. This makes the
    person rows add to the once-rounded product source without touching money.
    """
    metric_sources = {'creator_sales':'participation_sales',
                      'creator_gross':'participation_gross',
                      'creator_profit':'participation_profit'}
    if not set(metric_sources.values()) <= set(frame.columns):
        return frame
    complete = frame.group_by('product_id').agg(
        pl.col('creator_pending').any().alias('__pending'))
    valid = complete.filter(~pl.col('__pending'))['product_id'].to_list()
    eligible = frame.filter(pl.col('product_id').is_in(valid) &
                            pl.col('creator_gross').is_not_null())
    if eligible.is_empty():
        return frame
    source = eligible.unique(subset=['product_id','spine_row']).group_by('product_id').agg(*[
        pl.col(column).alias('__source_'+name)
        for name,column in metric_sources.items()])
    # Sum each source product deterministically. Parallel float group sums can
    # vary at a half-cent when row order changes between archival reads.
    from .money import money_float
    source = source.with_columns(*[
        pl.Series('__target_'+name,[money_float(math.fsum(values))
                                    for values in source['__source_'+name]])
          .cast(pl.Decimal(28,10)) for name in metric_sources])
    people = eligible.group_by('product_id','person_id').agg(
        *[pl.col(name).sum().alias('__raw_'+name) for name in metric_sources],
        pl.col('__sales_share').sum().alias('__weight'),
        pl.col('spine_row').min().alias('__first_row')).join(source,on='product_id')
    people = people.with_columns(*[
        pl.col('__raw_'+name).round(2,mode='half_away_from_zero').alias('__rounded_'+name)
        for name in metric_sources])
    winner = pl.when(pl.col('__weight')==pl.col('__weight').max().over('product_id'))
    winner = winner.then(pl.col('person_id')).otherwise(None).min().over('product_id')
    people = people.with_columns(winner.alias('__winner'), *[
        (pl.col('__target_'+name) - pl.col('__rounded_'+name).sum().over('product_id')).alias('__residual_'+name)
        for name in metric_sources])
    adjustments = people.select('product_id','person_id','__first_row',*[
        pl.when(pl.col('person_id')==pl.col('__winner'))
          .then(pl.col('__residual_'+name)).otherwise(0).alias('__adjust_'+name)
        for name in metric_sources])
    adjusted = frame.join(adjustments,on=['product_id','person_id'],how='left',maintain_order='left')
    return adjusted.with_columns(*[
        (pl.col(name) + pl.when(pl.col('spine_row')==pl.col('__first_row'))
         .then(pl.col('__adjust_'+name).fill_null(0)).otherwise(0)).alias(name)
        for name in metric_sources]).drop('__first_row',*[f'__adjust_{name}' for name in metric_sources])


def totals(frame, outputs):
    if outputs is None or '__sales_share' not in frame.columns or 'participation_sales' not in frame.columns:
        return outputs
    frame = creator_fields(frame)
    creator = 'creator_pending' in frame.columns
    grouped = frame.with_columns(expression(frame).alias('__corrected_sales')).group_by('person_id').agg(
        pl.col('__corrected_sales').filter(~pl.col('__sales_pending')).sum().alias('known_sales'),
        pl.col('__sales_pending').any().alias('pending'),
        pl.col('product_id').filter(pl.col('__sales_pending')).unique().sort().alias('pending_products'),
        pl.col('__sales_source').unique().sort().alias('sources'))
    if creator:
        fields=('creator_cost','creator_gross','creator_profit')
        per_product=frame.group_by('person_id','product_id').agg(
            pl.col('creator_pending').any(),
            (pl.col('managed').fill_null(False).not_().sum() if 'managed' in frame.columns
             else pl.len()).alias('__creator_rows'),
            *[pl.col(name).sum().round(2,mode='half_away_from_zero').alias(name)
              for name in fields])
        by_person=per_product.group_by('person_id').agg(
            pl.col('creator_pending').any(),pl.col('__creator_rows').sum(),
            *[pl.col(name).sum() for name in fields])
        grouped=grouped.join(by_person,on='person_id',how='left',maintain_order='left')
    from .money import money_float
    result = {pid:dict(value) for pid,value in outputs.items()}
    for row in grouped.iter_rows(named=True):
        if row['person_id'] not in result:
            continue
        out = result[row['person_id']]
        out['sales_basis'] = out.get('sales_basis', out.get('sales'))
        out['reference_sales'] = out.get('sales')
        out['sales'] = None if row['pending'] else money_float(row['known_sales'])
        out['known_sales'] = money_float(row['known_sales'])
        out['sales_pending_products'] = row['pending_products']
        out['sales_sources'] = row['sources']
        if '__sales_rule_revision' in frame.columns:
            out['creator_rule_revision'] = frame['__sales_rule_revision'][0]
        if creator:
            out['creator_pending'] = bool(row['creator_pending'])
            out['creator_active'] = bool(row['__creator_rows'])
            for name in ('creator_cost','creator_gross','creator_profit'):
                out[name] = None if row['creator_pending'] or not row['__creator_rows'] else money_float(row[name])
    return result


_archive_cache = OrderedDict()
_code_sha = None


def archived(registry, commission, outputs, frame=None):
    """Versioned, bounded read cache; never updates archive or financial rows."""
    global _code_sha
    if outputs is None or not commission.get('calculation_id'):
        return outputs
    from pathlib import Path
    from io import BytesIO
    from .read_cache import cached
    from . import derived_read_cache
    if _code_sha is None:
        from . import commission_registry, commission_read_index
        _code_sha = hashlib.sha256(b''.join(Path(module.__file__).read_bytes()
            for module in (commission_registry,commission_read_index)) + Path(__file__).read_bytes()).hexdigest()
    with registry.connect() as conn:
        meta = conn.execute('SELECT path,sha,store_id,period FROM calculation WHERE id=?', (commission['calculation_id'],)).fetchone()
    if not meta:
        return outputs
    revision = rule_revision(registry,meta['store_id'])
    path = registry.root / 'calculations' / Path(meta['path']).name
    stat = path.stat()
    base_sha = hashlib.sha256(json.dumps(outputs,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    key = (str(registry.root), commission['calculation_id'], meta['sha'], revision, stat.st_mtime_ns, stat.st_size, base_sha, _code_sha)
    persistent_key = 'sales:' + hashlib.sha256(json.dumps(key).encode()).hexdigest()
    def build():
        saved = derived_read_cache.get(registry,persistent_key)
        if saved is not None:
            return saved
        snapshot = rules(registry,meta['store_id'],expected=revision)
        if frame is None:
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest()!=meta['sha']:
                from .commission_registry import RegistryError
                raise RegistryError('销售归属证据校验失败，请核对原始提成档案')
            schema = pl.read_parquet_schema(BytesIO(data))
            cols = [c for c in ('product_id','person_id','status','spine_row','order_at','fallback_reason','share','total_rate','duty','managed','participation_sales','participation_gross','participation_profit') if c in schema]
            loaded = pl.read_parquet(BytesIO(data),columns=cols)
        else:
            loaded = frame
        loaded = loaded.filter(pl.col('status')=='distribute')
        value = totals(attach(registry,meta['store_id'],meta['period'],loaded,snapshot=snapshot),outputs)
        derived_read_cache.put(registry,persistent_key,value)
        return value
    try:
        return cached(_archive_cache,key,build,256)
    except _RulesChanged:
        # Do not persist a newer rule snapshot under an older cache key.
        # The caller may retry after a concurrent batch publish completes.
        from .commission_registry import RevisionConflict
        raise RevisionConflict('商品规则正在更新，请稍后重新查询')
