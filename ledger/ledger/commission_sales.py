"""Read-only sales attribution over immutable financial evidence.

Confirmed, effective product duties may correct sales ownership. They never
replace archived income, cost, gross/profit, share, payout or confirmation data.
Organization roles and suggestions are deliberately not authority.
"""
from collections import OrderedDict, defaultdict
from decimal import Decimal
from datetime import datetime
import json
import hashlib
import threading

import polars as pl

_cache = OrderedDict()
_lock = threading.Lock()


def rules(registry, store_id):
    with registry.connect() as conn:
        conn.execute('BEGIN')
        generation = conn.execute('SELECT generation FROM scheme_read_clock WHERE id=1').fetchone()[0]
        members = [dict(row) for row in conn.execute('SELECT * FROM store_member WHERE store_id=? ORDER BY person_id,valid_from,id', (store_id,))]
        revision = str(generation) + ':' + hashlib.sha256(json.dumps(members,sort_keys=True).encode()).hexdigest()
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


def attach(registry, store_id, period, details, snapshot=None):
    """Resolve complete participant groups before filtering to any one person."""
    required = {'product_id','person_id','share','total_rate','spine_row'}
    if not required <= set(details.columns) or details.is_empty():
        return details
    revision, (configured, members) = snapshot if snapshot is not None else rules(registry, store_id)
    columns = [x for x in (*required, 'order_at','duty','managed') if x in details.columns]
    rows = details.select(columns).to_dicts()
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[(row['product_id'], row['spine_row'])].append(index)
    shares = ['0'] * len(rows); rates = ['1'] * len(rows)
    pending = [False] * len(rows); sources = [''] * len(rows)
    versions = [''] * len(rows)
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
            duties, source = explicit, 'effective_product_rule'
        elif all(d in ('produce','cut') for d in archive.values()):
            duties, source = archive, 'archived_duty'
        else:
            duties, source = {}, 'pending_identity'
        missing_input = (not product or ('order_at' in columns and not stamp)) and source!='archived_managed'
        known = bool(duties) and not managed_pending and not roster_pending and not conflicted and not missing_input
        denominator = sum((Decimal(str(rows[i]['share'])) for i in indexes if duties.get(rows[i]['person_id'])=='produce'), Decimal(0))
        for i in indexes:
            row = rows[i]
            pending[i] = not known
            sources[i] = 'pending_input' if missing_input else 'pending_identity' if conflicted else 'pending_roster_refresh' if roster_pending else source if not managed_pending else 'pending_managed_refresh'
            versions[i] = (current or {}).get('_version_id','') if source=='effective_product_rule' else ''
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


def totals(frame, outputs):
    if outputs is None or '__sales_share' not in frame.columns or 'participation_sales' not in frame.columns:
        return outputs
    grouped = frame.with_columns(expression(frame).alias('__corrected_sales')).group_by('person_id').agg(
        pl.col('__corrected_sales').filter(~pl.col('__sales_pending')).sum().alias('known_sales'),
        pl.col('__sales_pending').any().alias('pending'),
        pl.col('product_id').filter(pl.col('__sales_pending')).unique().sort().alias('pending_products'),
        pl.col('__sales_source').unique().sort().alias('sources'))
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
        _code_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with registry.connect() as conn:
        meta = conn.execute('SELECT path,sha,store_id,period FROM calculation WHERE id=?', (commission['calculation_id'],)).fetchone()
    if not meta:
        return outputs
    snapshot = rules(registry,meta['store_id'])
    revision = snapshot[0]
    path = registry.root / 'calculations' / Path(meta['path']).name
    stat = path.stat()
    base_sha = hashlib.sha256(json.dumps(outputs,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    key = (str(registry.root), commission['calculation_id'], meta['sha'], revision, stat.st_mtime_ns, stat.st_size, base_sha, _code_sha)
    persistent_key = 'sales:' + hashlib.sha256(json.dumps(key).encode()).hexdigest()
    def build():
        saved = derived_read_cache.get(registry,persistent_key)
        if saved is not None:
            return saved
        if frame is None:
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest()!=meta['sha']:
                from .commission_registry import RegistryError
                raise RegistryError('销售归属证据校验失败，请核对原始提成档案')
            schema = pl.read_parquet_schema(BytesIO(data))
            cols = [c for c in ('product_id','person_id','status','spine_row','order_at','share','total_rate','duty','managed','participation_sales') if c in schema]
            loaded = pl.read_parquet(BytesIO(data),columns=cols)
        else:
            loaded = frame
        loaded = loaded.filter(pl.col('status')=='distribute')
        value = totals(attach(registry,meta['store_id'],meta['period'],loaded,snapshot=snapshot),outputs)
        derived_read_cache.put(registry,persistent_key,value)
        return value
    return cached(_archive_cache,key,build,256)
