"""Read stored commission amounts; never recalculate or change a closed period."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from datetime import date
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

import polars as pl

from . import commission_slice, overhead
from .commission_engine import allocated_outputs
from .commission_registry import RegistryError, json_text, member_active_at
from .labor_api import frozen_shares
from .money import money_float


def store_member_duties(registry, store_id, at=None):
    """Load confirmed store member duties at a point in time."""
    stamp = at or ''
    if stamp and len(stamp) == 7:
        stamp = f'{stamp}-01T00:00:00'
    return {r['person_id']: r for r in registry.store_members_at(store_id, stamp)}


def find_team_info(pid, roster):
    """Trace a person's hierarchy up to the top team leader."""
    cur = roster.get(pid)
    if not cur:
        return {'team_id': '', 'team_name': '未分配', 'leader_name': ''}
    visited = set()
    while cur and cur.get('parent_id') and cur['parent_id'] in roster and cur['parent_id'] not in visited:
        visited.add(cur['id'])
        cur = roster[cur['parent_id']]
    team_name = cur.get('alias') or cur.get('name') or '未分配'
    return {
        'team_id': cur.get('id', ''),
        'team_name': team_name,
        'leader_name': cur.get('name', ''),
    }


def months(start, end):
    if not re.fullmatch(r"\d{4}-\d{2}", start or '') or not re.fullmatch(r"\d{4}-\d{2}", end or ''):
        raise RegistryError('请选择起止账期')
    try:
        first=date.fromisoformat(start+'-01'); last=date.fromisoformat(end+'-01')
    except ValueError as exc:raise RegistryError('账期格式应为YYYY-MM') from exc
    count=(last.year-first.year)*12+last.month-first.month+1
    if count<1 or count>120:raise RegistryError('结束账期不能早于开始账期，单次最多查询10年')
    return [f'{(first.year*12+first.month-1+i)//12:04d}-{(first.year*12+first.month-1+i)%12+1:02d}' for i in range(count)]


def decimal(value):
    result=Decimal(str(value))
    if not result.is_finite():raise RegistryError('计算结果含无效金额，请核查对应账期')
    return result


def statement_amount(statement, node_id):
    """Read a displayed snapshot amount, preserving missing evidence as None."""
    row = next((item for item in statement if item.get('id') == node_id), None)
    return row.get('value') if row and row.get('available', True) else None


def profit_after_labor(profit, labor):
    """Display operating profit less the store labor cut."""
    if profit is None:
        return None
    return money_float(decimal(profit) - decimal(labor or 0))


def creator_rollup(rows):
    """Sum proven non-managed output without silently treating unknown as zero."""
    active = [row for row in rows if row.get('creator_active')]
    fields = ('creator_cost','creator_gross','creator_profit')
    pending = any(row.get('creator_pending') or any(row.get(name) is None for name in fields)
                  for row in active)
    return {'creator_active': bool(active), 'creator_pending': pending,
            **{name: (None if not active or pending else
                      money_float(sum((decimal(row[name]) for row in active),Decimal(0))))
               for name in fields}}


def metric_rollup(rows, fields=('managed_sales','labor_cost','profit_after_labor')):
    """Display totals keep unknown values distinct from an actual zero."""
    return {field: (money_float(sum((decimal(row[field]) for row in rows), Decimal(0)))
                    if rows and all(row.get(field) is not None for row in rows) else None)
            for field in fields}


def labor_keep(base_total, labor_cut):
    """Scale trial payouts so the store labor cut is taken out of the pool."""
    base = decimal(base_total or 0)
    if not base:
        return Decimal(1)
    return (base - decimal(labor_cut or 0)) / base


def suggested_payouts(commission, labor_cut, *, operating=None, registry=None, duties=None):
    """Human-facing trial payouts after the store labor cut.

    A unique link rate uses attributed profit after labor. Scaling the raw
    order trial by the labor keep would ignore cost supplements and unassigned
    losses that already reduced the store profit the page shows.

    When duties are provided, each person's payout = producer_profit_after_labor
    × their own share rate (not total_rate).
    """
    people = commission.get('people') or []
    profits = (attributed_profit(commission, operating, labor_cut, registry, duties=duties)
               if operating is not None else {})
    keep = (Decimal(1) if commission.get('manual_amounts_after_labor')
            else labor_keep(commission.get('base_total'), labor_cut))
    suggested = {}
    for person in people:
        pid = person.get('person_id')
        if not pid:
            continue
        rate = confirmed_profit_rate(commission, pid)
        if rate is not None and pid in profits:
            suggested[pid] = money_float(decimal(profits[pid]) * rate)
            continue
        if person.get('amount') is None:
            continue
        suggested[pid] = money_float(person['amount'] if commission.get('manual_amounts_after_labor')
                                     else decimal(person['amount']) * keep)
    return suggested


def _output_residual(total, values, field):
    """Return the visible store amount not represented by named people."""
    if total is None:
        return None
    represented = sum(
        (decimal(value[field]) for value in values.values()
         if value.get(field) is not None), Decimal(0)
    )
    return money_float(decimal(total) - represented)


_profit_lock = threading.RLock()
_profit_cache: OrderedDict[tuple, dict[str, dict[str, float]] | None] = OrderedDict()
_PROFIT_CACHE_LIMIT = 512  # Small person totals only, never full Parquet frames.
_OUTPUT_CODE = None


def _archived_allocated_outputs(registry, commission, *, production=False, sales=False):
    global _OUTPUT_CODE
    if sales:
        from .commission_sales import archived
        return archived(registry,commission,_archived_allocated_outputs(registry,commission,production=True))
    if production and commission.get('production_outputs') is not None:
        return commission['production_outputs']
    calculation = commission.get('calculation_id')
    if not calculation:
        return None
    with registry.connect() as conn:
        row = conn.execute('SELECT path,sha FROM calculation WHERE id=?',
                           (calculation,)).fetchone()
    if not row:
        return None
    key = (str(registry.root.resolve()), calculation, row['sha'], production)
    with _profit_lock:
        if key in _profit_cache:
            _profit_cache.move_to_end(key)
            return _profit_cache[key]
    path = registry.root / 'calculations' / Path(row['path']).name
    try:
        from . import commission_engine, money, derived_read_cache
        if _OUTPUT_CODE is None:
            _OUTPUT_CODE=hashlib.sha256(Path(commission_engine.__file__).read_bytes()+Path(money.__file__).read_bytes()+Path(__file__).read_bytes()).hexdigest()
        stat=path.stat()
        persistent_key='output:'+hashlib.sha256(json.dumps([calculation,row['sha'],stat.st_size,stat.st_mtime_ns,_OUTPUT_CODE,commission.get('base_node'),commission.get('on_loss')]).encode()).hexdigest()
        saved=derived_read_cache.get(registry,persistent_key)
        if saved is not None:
            results={False:saved['legacy'],True:saved['production']}
            with _profit_lock:
                for mode,value in results.items():_profit_cache[(*key[:3],mode)]=value
                while len(_profit_cache)>_PROFIT_CACHE_LIMIT:_profit_cache.popitem(last=False)
            return results[production]
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != row['sha']:
            return None
        from io import BytesIO
        schema = pl.read_parquet_schema(BytesIO(payload))
        cols = [name for name in ('status', 'person_id', 'share', 'total_rate', 'duty', 'spine_row', 'order_at', 'fallback_reason', 'product_id', 'managed', 'sales_unassigned',
                                  'participation_sales','participation_gross',
                                  'participation_profit', 'original_base')
                if name in schema]
        details = pl.read_parquet(BytesIO(payload), columns=cols)
        basis = commission.get('base_node') == 'net_profit' and commission.get('on_loss') == 'deduct'
        results = {mode: allocated_outputs(details, production=mode, net_profit_basis=basis) for mode in (False, True)}
        from .commission_sales import archived
        archived(registry,commission,results[True],frame=details)
        derived_read_cache.put(registry,persistent_key,{'legacy':results[False],'production':results[True]})
    except (OSError, ValueError, pl.exceptions.PolarsError):
        results = {False: None, True: None}
    with _profit_lock:
        for mode, result in results.items():
            _profit_cache[(*key[:3], mode)] = result
        while len(_profit_cache) > _PROFIT_CACHE_LIMIT:
            _profit_cache.popitem(last=False)
    return results[production]


def _split_cents(total, weights):
    """Allocate a nonnegative store amount by member sales, preserving cents."""
    total = decimal(total).quantize(Decimal('.01'))
    if total == 0:
        return {pid: Decimal(0) for pid in weights}
    basis = sum(weights.values(), Decimal(0))
    if basis <= 0:
        return None
    exact = {pid: total * 100 * weight / basis for pid, weight in weights.items()}
    whole = {pid: int(value.to_integral_value(rounding=ROUND_FLOOR))
             for pid, value in exact.items()}
    remainder = int(total * 100) - sum(whole.values())
    for pid in sorted(exact, key=lambda p: (-(exact[p] - whole[p]), p))[:remainder]:
        whole[pid] += 1
    return {pid: Decimal(cents) / 100 for pid, cents in whole.items()}


def _profit_from_sales(people, operating, labor):
    """When allocated profit is missing, split store profit after labor by sales."""
    weights = {}
    for person in people:
        pid = person.get('person_id')
        sales = person.get('allocated_sales')
        if sales is None:
            sales = person.get('sales')
        if not pid or sales is None:
            return {}
        weights[pid] = max(decimal(sales), Decimal(0))
    after = profit_after_labor(operating, labor)
    if after is None:
        return {}
    amount = decimal(after)
    if amount == 0:
        return {pid: 0.0 for pid in weights}
    parts = _split_cents(abs(amount), weights)
    if parts is None:
        return {}
    sign = Decimal(1) if amount > 0 else Decimal(-1)
    return {pid: money_float(sign * parts[pid]) for pid in parts}


def attributed_labor(commission, labor, registry, *, duties=None, production=True):
    """Expose the exact cost split already used by attributed_profit, not a new charge."""
    if labor is None:
        return {}
    return attributed_profit(commission, None, labor, registry, duties=duties,
                             production=production, _labor_only=True)


def attributed_profit(commission, operating, labor, registry, *, store_id='', manual_cost=False, duties=None, production=False, _labor_only=False):
    """Additive person profit; keep full sales and gross output separate.

    Manual cost changes the store profit, not the ownership split. The
    allocated-to-operating residual already absorbs that gap.

    When duties are provided and there are producers among the people,
    profit goes 100% to producers; cut-only members get 0. A person who
    also has 做货 product IDs is a producer even if the store default is 抽点.
    """
    people = commission.get('people') or []
    if (operating is None and not _labor_only) or not people:
        return {}
    ids = [p.get('person_id') for p in people]
    if any(not pid for pid in ids) or len(ids) != len(set(ids)):
        return {}
    if _labor_only and not decimal(labor or 0):
        return {pid: 0.0 for pid in ids}
    production_split = _archived_allocated_outputs(registry, commission, production=True) if production else None
    if len(people) == 1 and production_split is None:
        return {ids[0]: money_float(decimal(labor or 0)) if _labor_only else profit_after_labor(operating, labor)}
    # duty-based: producers get all profit, cut members get 0
    if duties and len(people) > 1 and production_split is None:
        producers = [p for p in people if duties.get(p['person_id'], {}).get('duty', 'produce') == 'produce']
        if producers and len(producers) < len(people):
            producer_result = attributed_profit(
                {**commission, 'people': producers}, operating, labor, registry,
                store_id=store_id, manual_cost=manual_cost, _labor_only=_labor_only)
            result = {pid: 0.0 for pid in ids}
            result.update(producer_result)
            return result
    split = {p['person_id']: {'sales':p.get('allocated_sales'),
                              'gross':p.get('allocated_gross'),
                              'profit':p.get('allocated_profit')}
             for p in people if p.get('person_id') and
             all(p.get(key) is not None for key in
                 ('allocated_sales','allocated_gross','allocated_profit'))}
    if production_split is not None:
        split = production_split
    elif len(split) != len(people):
        split = _archived_allocated_outputs(registry, commission)
    if split is None or any(p.get('person_id') not in split for p in people):
        if _labor_only:
            return _profit_from_sales(people, labor or 0, 0)
        return _profit_from_sales(people, operating, labor)
    weights = {p['person_id']: max(decimal(split[p['person_id']].get('sales_basis', split[p['person_id']].get('sales')) or 0), Decimal(0))
               for p in people}
    # A negative store residual is shared as operating loss. Positive
    # unattributed profit remains at the store until its owner is known.
    cost = _split_cents(labor or 0, weights)
    if _labor_only:
        return {} if cost is None else {pid: money_float(value) for pid, value in cost.items()}
    values = {p['person_id']: decimal(split[p['person_id']]['profit']) for p in people}
    residual = max(sum(values.values(), Decimal(0)) - decimal(operating), Decimal(0))
    loss = _split_cents(residual, weights)
    if cost is None or loss is None:
        return {}
    return {pid: money_float(values[pid] - cost[pid] - loss[pid])
            for pid in weights}


def attributed_outputs(commission, store_sales, store_gross, registry, *, duties=None, production=False):
    if production and store_sales is not None:
        store_sales = money_float(decimal(store_sales) - sum((decimal(v) for v in (commission.get('managed_sales') or {}).values()), Decimal(0)))
    people=commission.get('people') or []
    ids=[p.get('person_id') for p in people]
    if not people or any(not pid for pid in ids) or len(ids)!=len(set(ids)):
        return {}
    production_split = _archived_allocated_outputs(registry, commission, production=True, sales=True) if production else None
    if len(people)==1 and production_split is None:
        return {ids[0]:{'sales':store_sales,'gross':store_gross}}
    # duty-based: producers get all sales/gross; cut-only members get 0
    if duties and len(people) > 1 and production_split is None:
        producers = [p for p in people if duties.get(p['person_id'], {}).get('duty', 'produce') == 'produce']
        if producers and len(producers) < len(people):
            producer_result = attributed_outputs(
                {**commission, 'people': producers}, store_sales, store_gross, registry)
            result = {pid: {'sales': 0.0, 'gross': 0.0} for pid in ids}
            result.update(producer_result)
            return result
    split={p['person_id']:{'sales':p.get('allocated_sales'),'gross':p.get('allocated_gross')}
           for p in people if p.get('person_id') and
           p.get('allocated_sales') is not None and p.get('allocated_gross') is not None}
    if production_split is not None:
        split={pid:{'sales':value.get('sales') if value.get('sales') is not None else value.get('reference_sales'),'gross':value.get('gross')} for pid,value in production_split.items()}
    elif len(split)!=len(people):
        archived=_archived_allocated_outputs(registry,commission)
        if archived is None:return {}
        split={pid:{'sales':value.get('sales'),'gross':value.get('gross')}
               for pid,value in archived.items()}
    if any(pid not in split or split[pid].get('sales') is None or
           split[pid].get('gross') is None for pid in ids):
        return {}
    values={pid:{'sales':decimal(split[pid]['sales']),
                 'gross':decimal(split[pid]['gross'])} for pid in ids}
    weights={pid:max(decimal((production_split or {}).get(pid,{}).get('sales_basis', values[pid]['sales'])),Decimal(0)) for pid in ids}
    reference_sales = {pid:decimal((production_split or {}).get(pid,{}).get('reference_sales',values[pid]['sales'])) for pid in ids}
    if store_sales is not None:
        reference_cuts = _split_cents(max(sum(reference_sales.values(),Decimal(0))-decimal(store_sales),Decimal(0)),weights)
        if reference_cuts is not None:
            reference_sales = {pid:value-reference_cuts[pid] for pid,value in reference_sales.items()}
    for name,store_value in (('sales',store_sales),('gross',store_gross)):
        if store_value is None:continue
        excess=max(sum((value[name] for value in values.values()),Decimal(0))
                   -decimal(store_value),Decimal(0))
        cuts=_split_cents(excess,weights)
        if cuts is None:return {}
        for pid in ids:values[pid][name]-=cuts[pid]
    result = {pid:{name:money_float(amount) for name,amount in value.items()} for pid,value in values.items()}
    for pid, value in (production_split or {}).items():
        if pid in result:
            for field in ('creator_cost','creator_gross','creator_profit','creator_pending','creator_active','creator_rule_revision'):
                result[pid][field] = value.get(field)
            result[pid]['sales_pending_products'] = value.get('sales_pending_products',[])
            result[pid]['sales_sources'] = value.get('sales_sources',[])
            result[pid]['reference_sales'] = money_float(reference_sales[pid])
            if value.get('sales_pending_products'):
                result[pid]['sales'] = None
    return result


def confirmed_profit_rate(commission, person_id, store_id=''):
    """Use the person's allocated profit only when its link total rate is unique."""
    if not person_id or commission.get('base_node') != 'net_profit':
        return None
    rates = {decimal(product.get('total_rate') or 0)
             for product in commission.get('products') or []
             if decimal(product.get('total_rate') or 0) > 0
             and any(crew.get('person_id') == person_id
                     for crew in product.get('people') or [])}
    rate = next(iter(rates)) if len(rates) == 1 else None
    return rate if rate is not None and rate > 0 else None


_configured_cache: OrderedDict[tuple, dict[str, frozenset[str]]] = OrderedDict()
_CONFIGURED_CACHE_LIMIT = 64
_configured_code = None


def _scan_configured_rows(registry, start, end, store_ids=()):
    year, month = map(int, end.split('-'))
    upper = f"{year + (month == 12):04d}-{month % 12 + 1:02d}-01T00:00:00"
    lower = start + '-01T00:00:00'
    where = (' AND s.store_id IN (' + ','.join('?' for _ in store_ids) + ')') if store_ids else ''
    with registry.connect() as conn:
        rows = conn.execute("""SELECT DISTINCT s.store_id,a.person_id pid,a.duty duty
          FROM scheme_read_segment s JOIN scheme_read_person a
          ON a.scheme_id=s.scheme_id AND a.segment_no=s.segment_no
          WHERE s.mode='distribute' AND s.valid_from<?
          AND (s.valid_to='' OR s.valid_to>?)"""+where,
          (upper,lower,*store_ids)).fetchall()
    people, producers = {}, {}
    for row in rows:
        pid = row['pid']
        if not pid:
            continue
        people.setdefault(row['store_id'], set()).add(pid)
        if (row['duty'] or 'produce') == 'produce':
            producers.setdefault(row['store_id'], set()).add(pid)
    return people, producers


def _scan_configured_people(registry, start, end, store_ids=()):
    return _scan_configured_rows(registry, start, end, store_ids)[0]


def _configured_bundle(registry, start, end, store_ids=()):
    """Only product publications change this index, not unrelated audit events."""
    global _configured_code
    import hashlib
    from . import derived_read_cache
    if _configured_code is None:
        from . import commission_read_index
        _configured_code=hashlib.sha256(Path(__file__).read_bytes()+Path(commission_read_index.__file__).read_bytes()).hexdigest()
    scope=tuple(sorted(store_ids))
    with registry.connect() as conn:
        generation=conn.execute('SELECT generation FROM scheme_read_clock WHERE id=1').fetchone()[0]
    key=(str(registry.root.resolve()), generation, start, end, scope, _configured_code)
    persistent_key='configured:'+hashlib.sha256(json.dumps(key).encode()).hexdigest()
    def build():
        saved=derived_read_cache.get(registry,persistent_key)
        if saved is not None:
            return {kind:{sid:frozenset(pids) for sid,pids in stores.items()} for kind,stores in saved.items()}
        people, producers = _scan_configured_rows(registry, start, end, scope)
        with registry.connect() as conn:
            current=conn.execute('SELECT generation FROM scheme_read_clock WHERE id=1').fetchone()[0]
        if current!=generation:
            from .commission_registry import RevisionConflict
            raise RevisionConflict('商品规则正在更新，请稍后重新查询')
        saved={
            'people': {sid: frozenset(pids) for sid, pids in people.items()},
            'producers': {sid: frozenset(pids) for sid, pids in producers.items()},
        }
        derived_read_cache.put(registry,persistent_key,{kind:{sid:sorted(pids) for sid,pids in stores.items()} for kind,stores in saved.items()})
        return saved
    from .read_cache import cached
    return cached(_configured_cache,key,build,_CONFIGURED_CACHE_LIMIT)


def configured_people(registry, start, end, store_ids=()):
    return {sid: set(pids) for sid, pids in _configured_bundle(registry, start, end, store_ids)['people'].items()}


def configured_producers(registry, start, end, store_ids=()):
    """People who have at least one 做货 allocation in the store during the range."""
    return {sid: set(pids) for sid, pids in _configured_bundle(registry, start, end, store_ids)['producers'].items()}


def _slim_products_sql(payload):
    return commission_slice.slim_products_sql(payload)


def _visible_run_sql(run_ids):
    if run_ids is None:
        source = """FROM period p JOIN run r ON r.id=CASE WHEN p.state='closed' AND p.run_id IS NOT NULL THEN p.run_id
          ELSE (SELECT id FROM run latest WHERE latest.store_id=p.store_id AND latest.period=p.period ORDER BY id DESC LIMIT 1) END"""
        payload_kind = "CASE WHEN p.state='closed' AND mf.result_json IS NOT NULL THEN 'manual' ELSE 'run' END"
        payload = "coalesce(CASE WHEN p.state='closed' THEN mf.result_json END, r.result)"
        manual_join = " LEFT JOIN manual_finance mf ON mf.run_id=r.id AND p.state='closed'"
        return source, payload_kind, payload, manual_join
    if len(run_ids) != len(set(run_ids)):
        raise RegistryError('计算记录不能重复')
    source = 'FROM run r LEFT JOIN period p ON p.store_id=r.store_id AND p.period=r.period'
    payload_kind = "CASE WHEN mf.result_json IS NOT NULL THEN 'manual' ELSE 'run' END"
    payload = 'coalesce(mf.result_json,r.result)'
    manual_join = ' LEFT JOIN manual_finance mf ON mf.run_id=r.id'
    return source, payload_kind, payload, manual_join


def _fill_report_slices(workspace, start, end, run_ids=None, store_id=None, visible_only=False):
    source, payload_kind, payload, manual_join = _visible_run_sql(None if visible_only else run_ids)
    where = ['r.period>=?', 'r.period<=?']
    args = [start, end]
    if store_id:
        where.append('r.store_id=?')
        args.append(store_id)
    if run_ids is not None:
        where.append('r.id IN (' + ','.join('?' for _ in run_ids) + ')' if run_ids else '0')
        args.extend(run_ids)
    missing = [row['id'] for row in workspace.conn.execute(
        f"SELECT r.id {source}{manual_join} LEFT JOIN run_report_slice s "
        f"ON s.run_id=r.id AND s.payload_kind={payload_kind} "
        f"WHERE {' AND '.join(where)} AND (s.run_id IS NULL OR s.overview_json IS NULL)",
        args)]
    if not missing:
        return 0
    filled = 0
    # SQLite cannot upgrade a stale WAL read snapshot to a writer. Acquire the
    # writer before opening the JSON cursor; commit small batches so foreground
    # writes can interleave with historical warm-up.
    for offset in range(0, len(missing), 16):
      batch = missing[offset:offset + 16]
      marks = ','.join('?' for _ in batch)
      with workspace.conn as conn:
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        for row in conn.execute(
            f"SELECT r.id,{payload_kind} payload_kind,coalesce(existing.payload_bytes,length({payload})) payload_bytes,"
            f"CASE WHEN existing.run_id IS NOT NULL THEN existing.commission_json ELSE json_remove(json_extract({payload},'$.commission'),'$.products') END commission_json,"
            f"CASE WHEN existing.run_id IS NOT NULL THEN existing.products_slim_json ELSE {commission_slice.slim_products_sql(payload)} END products_slim_json,"
            f"CASE WHEN existing.run_id IS NOT NULL THEN existing.statement_json ELSE {commission_slice.compact_statement_sql(payload)} END statement_json,"
            f"CASE WHEN existing.run_id IS NOT NULL THEN existing.store_name ELSE json_extract({payload},'$.store') END store_name,"
            f"CASE WHEN existing.run_id IS NOT NULL THEN existing.manual_cost_json ELSE json_extract({payload},'$.manual_cost') END manual_cost_json "
            f",json_remove({payload},'$.commission.products') overview_json "
            f"{source}{manual_join} LEFT JOIN run_report_slice existing "
            f"ON existing.run_id=r.id AND existing.payload_kind={payload_kind} "
            f"WHERE r.id IN ({marks}) AND (existing.run_id IS NULL OR existing.overview_json IS NULL)", batch):
            conn.execute(
                "INSERT OR REPLACE INTO run_report_slice("
                "run_id,payload_kind,payload_bytes,commission_json,products_slim_json,"
                "statement_json,store_name,manual_cost_json,overview_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (row['id'], row['payload_kind'], row['payload_bytes'],
                 row['commission_json'] or '{}', row['products_slim_json'],
                 row['statement_json'] or '[]', row['store_name'] or '',
                 row['manual_cost_json'], row['overview_json']))
            filled += 1
    return filled


def ensure_report_slices(workspace, start, end, run_ids=None):
    """Fill slim rows for latest/closed runs in range, plus any pinned run ids."""
    months(start, end)
    filled = _fill_report_slices(workspace, start, end)
    if run_ids:
        filled += _fill_report_slices(workspace, start, end, run_ids=run_ids)
    return filled


def build(workspace, registry, model, start, end, store_ids=None, person_ids=None, run_ids=None,
          model_root: Path | None = None, need_product_rates: bool = True, need_managed: bool = False):
    managed_rows=[]
    periods=months(start,end)
    selected_stores=set(store_ids or []); selected_people=set(person_ids or [])
    from .store_display import names as display_names
    names=display_names(workspace.root,model)
    roster={p['id']:p for p in registry.people()}
    configured=configured_people(registry,start,end,selected_stores)
    produce_by_store=configured_producers(registry,start,end,selected_stores)
    revenue_node=next((n.id for n in model.statement if n.headline=='revenue'),'')
    sales_node=next((n.id for n in model.statement if n.name=='销售收入' and n.level==2),'')
    gross_node=next((n.id for n in model.statement if n.name=='毛利' and n.is_total),'')
    profit_node=next((n.id for n in model.statement if n.headline=='profit' and n.is_total),'')
    ensure_report_slices(workspace, start, end, run_ids)
    source, payload_kind, _payload, manual_join = _visible_run_sql(None)
    basis_rows=[]
    for row in workspace.conn.execute(
        "SELECT r.store_id,r.period,s.statement_json "
        +source+manual_join+
        " JOIN run_report_slice s ON s.run_id=r.id AND s.payload_kind="+payload_kind+
        " WHERE r.period>=? AND r.period<=?", (start, end)):
        statement=json.loads(row['statement_json'] or '[]')
        basis_rows.append({'store_id':row['store_id'],'period':row['period'],
                           'revenue':statement_amount(statement, revenue_node)})
    labor_spreads={period:overhead.allocate(period,model.overhead(period),[
        (row['store_id'],float(row['revenue'] or 0)) for row in basis_rows if row['period']==period
    ]) for period in periods}
    frozen=frozen_shares(model_root) if model_root else {}
    where=['r.period>=?','r.period<=?']; args=[start,end]
    if selected_stores:
        where.append('r.store_id IN ('+','.join('?' for _ in selected_stores)+')');args.extend(sorted(selected_stores))
    source, payload_kind, _payload, manual_join = _visible_run_sql(run_ids)
    if run_ids is not None:
        where.append('r.id IN ('+','.join('?' for _ in run_ids)+')' if run_ids else '0');args.extend(run_ids)
    records=[dict(r) for r in workspace.conn.execute(
        "SELECT r.id,r.store_id,r.period,r.at,"
        "s.commission_json,s.products_slim_json products_json,"
        "s.statement_json,s.manual_cost_json,s.store_name,"
        "p.state,p.run_id frozen_id,rl.amount frozen_labor_cut "
        +source+manual_join+
        " JOIN run_report_slice s ON s.run_id=r.id AND s.payload_kind="+payload_kind+
        " LEFT JOIN run_labor rl ON rl.run_id=r.id"
        +' WHERE '+' AND '.join(where)+' ORDER BY r.period DESC,r.store_id',args)]
    if run_ids is not None and len(records)!=len(run_ids):raise RegistryError('部分计算记录已不存在或不在所选范围，请重新查询')
    keys=[(r['store_id'],r['period']) for r in records]
    if len(keys)!=len(set(keys)):raise RegistryError('同店同账期只能选择一份计算结果')
    from .commission_confirm import active_for_runs
    confirmed = active_for_runs(registry, records)
    full_commission = {}
    confirmed_ids = [record['id'] for record in records
                     if confirmed.get((record['store_id'], record['period'], record['id']))]
    if confirmed_ids:
        marks = ','.join('?' for _ in confirmed_ids)
        if run_ids is None:
            sha_sql = (
                "SELECT r.id,json_extract(coalesce("
                "CASE WHEN p.state='closed' THEN mf.result_json END,r.result),'$.commission') "
                "commission_json FROM run r "
                "LEFT JOIN period p ON p.store_id=r.store_id AND p.period=r.period "
                "LEFT JOIN manual_finance mf ON mf.run_id=r.id "
                f"WHERE r.id IN ({marks})"
            )
        else:
            sha_sql = (
                "SELECT r.id,json_extract(coalesce(mf.result_json,r.result),'$.commission') "
                "commission_json FROM run r LEFT JOIN manual_finance mf ON mf.run_id=r.id "
                f"WHERE r.id IN ({marks})"
            )
        for row in workspace.conn.execute(sha_sql, confirmed_ids):
            full_commission[row['id']] = json.loads(row['commission_json'] or '{}')
    _duties_cache = {}
    duties_by_store = {}
    for duty_row in registry.store_members_for_stores(sorted({r['store_id'] for r in records})):
        duties_by_store.setdefault(duty_row['store_id'], []).append(duty_row)
    def _get_duties(sid, period=''):
        key = (sid, period)
        if key not in _duties_cache:
            stamp = f'{period}-01T00:00:00' if period else ''
            found = {r['person_id']: r for r in duties_by_store.get(sid, []) if member_active_at(r, stamp)}
            _duties_cache[key] = found or None
        return _duties_cache[key]
    scopes={}; lines=[]; store_person_rows=[]; available={}; people_totals={}; store_totals={}; store_people={}
    managed_scopes = []
    for record in records:
        if record['id'] in full_commission:
            c=full_commission[record['id']]
        else:
            c=json.loads(record['commission_json'] or '{}')
            if record.get('products_json'):
                c['products']=json.loads(record['products_json'])
        sid=record['store_id'];period=record['period']
        source_c=c
        decision=confirmed.get((sid,period,record['id']))
        payout_risk = ((json.loads(decision['trial_json']).get('allocation_risk') or {})
                       if decision else {})
        if decision and hashlib.sha256(json_text(c).encode()).hexdigest() == decision['source_sha']:
            c={**c,'people':[dict(person) for person in c.get('people') or []]}
            by_id={str(person.get('person_id')):person for person in c['people']}
            for selected in json.loads(decision['payouts_json']):
                pid=selected['person_id']
                person=by_id.get(pid)
                if person is None:
                    person={'person_id':pid,'person':selected['person'],
                            'base':None,'sales':None,'gross':None}
                    c['people'].append(person)
                    by_id[pid]=person
                person['amount']=selected['amount']
            c.update(total=money_float(decimal(decision['confirmed_total'])),
                     manual_amounts_after_labor=True,manual_confirmed=True,
                     amount_complete=True)
            c.setdefault('notes',[]).append('本期提成已由人工逐人确认，原试算和未分配订单保留')
            if payout_risk.get('acknowledged'):
                c['notes'].append('人工核定实发时已接受分配依据待核对风险；系统应发仍为参考，原始分配缺口未消除')
        else:
            decision=None
        names.setdefault(sid,record['store_name'] or sid)
        closed=record['state']=='closed' and record['frozen_id']==record['id']
        legacy=c.get('engine')!='commission-v2'
        status=('已人工确认（分配待复核）' if decision and payout_risk.get('acknowledged') else
                '已人工确认' if decision else
                '试算（店铺已结账）' if closed and c.get('amount_complete') is False else
                '已结账' if closed else
                '历史口径' if legacy else
                '已计算' if c.get('amount_complete') else '试算')
        notes=list(c.get('notes') or [])
        correction=source_c.get('allocation_correction') or {}
        if correction.get('pending_orders'):
            status='试算（历史分配仍有待复核）'
            notes.append(f"已更正 {correction.get('verified_orders',0)} 个有据主单；另有 {len(correction['pending_orders'])} 个主单保留历史分配待复核，不能把参考金额当作重新确认的最终应发")
        if closed and c.get('amount_complete') is False:notes.append('原结账结果保留了试算标记')
        has_result=c.get('total') is not None or any(p.get('amount') is not None for p in c.get('people',[]))
        if not has_result:status='未计算提成'
        if not decision and (c.get('pricing_threshold_met') is False or
                             (c.get('pricing_pending_count') and not c.get('pricing_threshold_met'))):
            status='成本待人工确认'
        all_total=sum((decimal(p['amount']) for p in c.get('people',[]) if p.get('amount') is not None),Decimal(0))
        if c.get('total') is not None and abs(all_total-decimal(c['total']))>Decimal('.01'):
            notes.append('原记录的人员合计与店铺提成合计不一致');status+=' · 合计待核对'
        spread=labor_spreads[period]
        saved=frozen.get((period,sid,str(record['id'])))
        labor_cut=(decimal(record['frozen_labor_cut'])
                   if (closed or run_ids is not None) and record['frozen_labor_cut'] is not None
                   else decimal(saved['amount']) if (closed or run_ids is not None) and saved
                   else decimal(spread.of(sid)))
        base_total=decimal(c.get('base_total') or 0)
        keep=(Decimal(1) if c.get('manual_amounts_after_labor')
              else labor_keep(base_total, labor_cut))
        if spread.total is not None:
            notes.append(f"{next((x.name for x in model.overheads if x.period==period),'兼职人工费用')}已分摊 {money_float(labor_cut):,.2f} 元")
        scope={'store_id':sid,'store':names[sid],'period':period,'finance_run':record['id'],'calculated_at':record['at'],
               'status':status,'has_result':has_result,'notes':'；'.join(notes),'selected_amount':Decimal(0),
               'unassigned_orders':c.get('unassigned_orders'),
               'unassigned_base':c.get('unassigned_base'),
               'base_name':c.get('base_name') or c.get('base_node',''),
               'labor_cost':money_float(labor_cut)}
        statement=json.loads(record['statement_json'] or '[]')
        sales=statement_amount(statement,sales_node) if sales_node else None
        gross=statement_amount(statement,gross_node) if gross_node else None
        operating=statement_amount(statement,profit_node) if profit_node else None
        visible_labor=labor_cut if spread.total is not None else Decimal(0)
        store_duties=_get_duties(sid, period)
        effective_duties = dict(store_duties) if store_duties else {}
        run_producers = commission_slice.producer_ids(source_c)
        scheme_producers = produce_by_store.get(sid, set())
        for person in source_c.get('people', []):
            pid = person.get('person_id')
            if not pid:
                continue
            if pid in run_producers or pid in scheme_producers:
                effective_duties[pid] = {'duty': 'produce'}
            elif person.get('duty') in ('produce', 'cut'):
                effective_duties[pid] = {'duty': person['duty']}
        person_output=attributed_outputs(source_c,sales,gross,registry,duties=effective_duties,production=True)
        managed = source_c.get('managed_sales') or {}
        managed_total = money_float(sum((decimal(v) for v in managed.values()), Decimal(0)))
        # A team-owned pool is not a selected person's output.
        if not selected_people:
            for tid, value in managed.items():
                managed_scopes.append({'team_id':tid, 'sales':value, 'store_id':sid, 'period':period})
        payout_profit=attributed_profit(source_c,operating,visible_labor,registry,store_id=sid,duties=effective_duties)
        person_profit=attributed_profit(source_c,operating,visible_labor,registry,store_id=sid,duties=effective_duties,production=True)
        person_labor=attributed_labor(source_c, labor_cut if spread.total is not None else None,
                                     registry, duties=effective_duties)
        profit_rates={person.get('person_id'):confirmed_profit_rate(
            source_c,person.get('person_id'),sid) for person in source_c.get('people') or []}
        if not decision and any(
                rate is not None and person_profit.get(pid) is not None
                for pid,rate in profit_rates.items()):
            if len(source_c.get('people') or [])==1:
                notes.append('提成沿用原核算基数与点数；产出按商品做货身份另行归属')
            else:
                notes.append('产出按商品做货身份归属，抽点不参与产出分摊；提成金额沿用原核算基数与点数')
            scope['notes']='；'.join(notes)
        member_rows=[]
        member_sources={}
        original_people = {}
        for original_person in source_c.get('people', []):
            original_people.setdefault(original_person.get('person_id'), original_person)
        for person in c.get('people',[]):
            name=person.get('person') or '未命名人员'
            pid=person.get('person_id')
            # The v2 legacy bridge also emits name-derived IDs, shared across stores.
            # They do not establish that two same-name payees are the same person.
            if not pid or (pid.startswith('legacy:') and pid not in roster):
                pid='legacy:'+hashlib.sha256((sid+'\0'+name).encode()).hexdigest()
            label=roster.get(pid,{}).get('name') or name
            if pid not in roster:label+=f'（历史记录 · {names[sid]}）'
            available.setdefault(pid,{'id':pid,'name':label})
            if selected_people and pid not in selected_people:continue
            member_sources[pid]=person.get('person_id')
            duty=((effective_duties.get(person.get('person_id') or pid) or {}).get('duty')
                  or person.get('duty')
                  or (store_duties or {}).get(person.get('person_id') or pid, {}).get('duty'))
            if person.get('amount') is None:
                if person.get('sales') is not None or person.get('gross') is not None:
                    member_rows.append({'kind':'person','person_id':pid,'person':name,
                                        'employee_no':roster.get(pid,{}).get('employee_no',''),
                                        'duty':duty,
                                        'store_id':sid,'store':names[sid],'period':period,
                                        'sales':person_output.get(person.get('person_id'),{}).get('sales'),
                                        'sales_pending_products':person_output.get(person.get('person_id'),{}).get('sales_pending_products',[]),
                                        'sales_sources':person_output.get(person.get('person_id'),{}).get('sales_sources',[]),
                                        'gross':person_output.get(person.get('person_id'),{}).get('gross'),
                                        'creator_cost':person_output.get(person.get('person_id'),{}).get('creator_cost'),
                                        'creator_gross':person_output.get(person.get('person_id'),{}).get('creator_gross'),
                                        'creator_profit':person_output.get(person.get('person_id'),{}).get('creator_profit'),
                                        'creator_pending':person_output.get(person.get('person_id'),{}).get('creator_pending'),
                                        'creator_active':person_output.get(person.get('person_id'),{}).get('creator_active'),
                                        'creator_rule_revision':person_output.get(person.get('person_id'),{}).get('creator_rule_revision'),
                                        'profit_after_labor':person_profit.get(person.get('person_id')),
                                        'labor_cost':person_labor.get(person.get('person_id')),'base':None,'base_name':'',
                                        'amount':None,'store_amount':None,
                                        'status':status,'finance_run':record['id']})
                continue
            orig_person = original_people.get(pid)
            orig_profit_rate = profit_rates.get(pid)
            if (orig_profit_rate is not None and pid in payout_profit
                    and orig_person and orig_person.get('amount') is not None):
                system_trial = decimal(money_float(decimal(payout_profit[pid]) * orig_profit_rate))
            elif orig_person and orig_person.get('amount') is not None:
                system_trial = decimal(money_float(decimal(orig_person['amount']) * keep))
            else:
                system_trial = None

            profit_rate=(profit_rates.get(person.get('person_id'))
                         if not decision else None)
            if (profit_rate is not None and person.get('person_id') in payout_profit
                    and person.get('amount') is not None):
                amount=decimal(money_float(decimal(payout_profit[person['person_id']])*profit_rate))
            else:
                amount=decimal(money_float(decimal(person['amount'])*keep))

            if system_trial is None:
                system_trial = amount
            trial_val = system_trial if decision else amount
            diff_val = (amount - system_trial) if decision else Decimal(0)
            is_confirmed = bool(decision)

            scope['selected_amount']+=amount
            store_people.setdefault(sid,set()).add(pid)
            tinfo = find_team_info(pid, roster)
            team_name = tinfo['team_name']

            lines.append({'person_id':pid,'person':name,'team':team_name,'employee_no':roster.get(pid,{}).get('employee_no',''),
                          'store_id':sid,'store':names[sid],'period':period,'amount':money_float(amount),
                          'trial_amount':money_float(trial_val),'actual_amount':money_float(amount),
                          'diff_amount':money_float(diff_val),'is_confirmed':is_confirmed,
                          'base':person.get('base'),'base_name':scope['base_name'],
                          'sales':person.get('sales'),'gross':person.get('gross'),'status':status,
                          'calculated_at':record['at'],'finance_run':record['id'],'notes':scope['notes']})
            member_rows.append({'kind':'person','person_id':pid,'person':name,'team':team_name,
                                'employee_no':roster.get(pid,{}).get('employee_no',''),
                                'duty':duty,
                                'store_id':sid,'store':names[sid],'period':period,
                                'sales':person_output.get(person.get('person_id'),{}).get('sales'),
                                'sales_pending_products':person_output.get(person.get('person_id'),{}).get('sales_pending_products',[]),
                                'sales_sources':person_output.get(person.get('person_id'),{}).get('sales_sources',[]),
                                'gross':person_output.get(person.get('person_id'),{}).get('gross'),
                                'creator_cost':person_output.get(person.get('person_id'),{}).get('creator_cost'),
                                'creator_gross':person_output.get(person.get('person_id'),{}).get('creator_gross'),
                                'creator_profit':person_output.get(person.get('person_id'),{}).get('creator_profit'),
                                'creator_pending':person_output.get(person.get('person_id'),{}).get('creator_pending'),
                                'creator_active':person_output.get(person.get('person_id'),{}).get('creator_active'),
                                'creator_rule_revision':person_output.get(person.get('person_id'),{}).get('creator_rule_revision'),
                                'profit_after_labor':person_profit.get(person.get('person_id')),
                                'labor_cost':person_labor.get(person.get('person_id')),
                                'base':person.get('base'),'base_name':scope['base_name'],
                                'amount':money_float(amount),
                                'trial_amount':money_float(trial_val),'actual_amount':money_float(amount),
                                'diff_amount':money_float(diff_val),'is_confirmed':is_confirmed,
                                'store_amount':None,
                                'status':status,'finance_run':record['id']})
            total=people_totals.setdefault(pid,{'person_id':pid,'person':label,'team':team_name,'employee_no':roster.get(pid,{}).get('employee_no',''),
                                               'amount':Decimal(0),'trial_amount':Decimal(0),'actual_amount':Decimal(0),'diff_amount':Decimal(0),
                                               'stores':set(),'periods':set(),'statuses':set(),'confirmed_count':0})
            total['amount']+=amount;total['actual_amount']+=amount;total['trial_amount']+=trial_val;total['diff_amount']+=diff_val
            if is_confirmed:total['confirmed_count']+=1
            total['stores'].add(sid);total['periods'].add(period);total['statuses'].add(status)
        if not selected_people and source_c.get('people'):
            store_profit = profit_after_labor(operating, visible_labor)
            residual_sales = _output_residual(sales, person_output, 'sales')
            if any(value.get('sales_pending_products') for value in person_output.values()):
                residual_sales = None
            if residual_sales is not None:
                residual_sales = money_float(decimal(residual_sales) - decimal(managed_total))
            residual_gross = _output_residual(gross, person_output, 'gross')
            residual_profit = _output_residual(
                store_profit,
                {pid: {'profit': value} for pid, value in person_profit.items()},
                'profit',
            )
            # Preserve the financial residual row even when sales ownership is
            # pending; do not make a cost/profit row disappear due to this fix.
            reference_residual = _output_residual(sales, {pid:{'sales':value.get('reference_sales',value.get('sales'))} for pid,value in person_output.items()}, 'sales')
            if reference_residual is not None:
                reference_residual = money_float(decimal(reference_residual)-decimal(managed_total))
            residual_labor = (money_float(decimal(labor_cut) - sum((decimal(v) for v in person_labor.values()), Decimal(0)))
                              if spread.total is not None else None)
            residuals = (residual_sales, reference_residual, residual_gross, residual_profit, residual_labor)
            if any(value is not None and abs(value) > .01 for value in residuals):
                missing_count = int(source_c.get('unassigned_orders') or 0)
                label = (f'未分配（{missing_count}笔订单信息不完整）'
                         if missing_count else '未分配（缺少人员归属证据）')
                member_rows.append({
                    'kind':'unassigned','person_id':None,'person':label,'employee_no':'',
                    'store_id':sid,'store':names[sid],'period':period,
                    'sales':residual_sales,'gross':residual_gross,
                    'profit_after_labor':residual_profit,'labor_cost':residual_labor,
                    'base':source_c.get('unassigned_base'),'base_name':scope['base_name'],
                    'amount':None,'store_amount':None,'status':'待核对',
                    'finance_run':record['id'],
                })
                profit_label = ('待核对' if residual_profit is None
                                else f'{residual_profit:,.2f} 元')
                notes.append(f'{label}：利润额 {profit_label}；人员行与店铺合计的差额已显式列出')
                scope['notes']='；'.join(notes)
        scope_managed=[]
        if managed:
            from .commission_managed_report import scope as managed_scope
            managed_arguments=dict(managed=managed,outputs=person_output,
                profits=person_profit,payout_profits=payout_profit,rates=profit_rates,
                trials={r['person_id']:r.get('trial_amount') for r in member_rows if r.get('kind')=='person'},
                keep=(Decimal(1) if source_c.get('manual_amounts_after_labor') else labor_keep(base_total,labor_cut)),
                roster=roster,store_id=sid,store=names[sid],period=period,run_id=record['id'])
            scope_managed=managed_scope(registry,source_c,**managed_arguments)
            if need_managed:
                managed_rows.extend(managed_scope(registry,source_c,**managed_arguments,selected_people=selected_people)
                                    if selected_people else scope_managed)
        managed_people={}
        managed_known=not managed or bool(scope_managed) and all(t.get('evidence_available') for t in scope_managed)
        for team in scope_managed:
            for person in team.get('children',[]):
                if person.get('person_id'):
                    managed_people.setdefault(person['person_id'],[]).append(person)
        for row in member_rows:
            if row.get('kind')!='person':
                continue
            source_pid=member_sources.get(row['person_id'])
            parts=managed_people.get(source_pid,[])
            row['managed_sales']=(money_float(sum((decimal(p['sales']) for p in parts),Decimal(0)))
                                  if managed_known and all(p.get('sales') is not None for p in parts) else None)
            row['managed_sales_pending']=row['managed_sales'] is None
            row['store_labor_cost']=money_float(labor_cut) if spread.total is not None else None
            row['labor_pending']=spread.total is not None and row.get('labor_cost') is None
        scopes[(sid,period)]=scope
        if not selected_people:
            for tid, value in managed.items():
                member_rows.append({'kind':'managed','person_id':None,'person':'托管商品',
                    'team_id':tid,'team':roster.get(tid,{}).get('alias') or roster.get(tid,{}).get('name') or tid,
                    'employee_no':'','store_id':sid,'store':names[sid],'period':period,
                    'sales':value,'managed_sales':value,'gross':None,'profit_after_labor':None,
                    'labor_cost':None,'base':None,'base_name':'','amount':None,'store_amount':None,
                    'status':status,'finance_run':record['id']})
        if not selected_people or member_rows:
            store_person_rows.append({'kind':'store','person':'店铺合计','person_id':None,
                                      'employee_no':'','store_id':sid,'store':names[sid],
                                      'period':period,'sales':sales,'managed_sales':managed_total if not selected_people else None,'gross':gross,
                                      'profit_after_labor':profit_after_labor(operating, visible_labor),
                                      'labor_cost':money_float(labor_cut) if spread.total is not None else None,
                                      'base':None,'base_name':'','amount':None,
                                      'store_amount':money_float(scope['selected_amount']) if has_result else None,
                                      'status':status,'finance_run':record['id']})
            store_person_rows.extend(member_rows)
    invalid=selected_people-set(roster)-set(available)
    if invalid:raise RegistryError('所选人员不存在，请重新选择')
    if selected_stores-set(names):raise RegistryError('所选店铺不存在，请重新选择')
    covered_stores=selected_stores or set(names)
    for sid in sorted(covered_stores):
        total={'store_id':sid,'store':names[sid],'amount':Decimal(0),'labor_cost':Decimal(0),'people':set(),'periods':0,'missing':0,'statuses':set()}
        for period in periods:
            scope=scopes.get((sid,period))
            if not scope:
                scope={'store_id':sid,'store':names[sid],'period':period,'finance_run':None,'calculated_at':'',
                       'status':'未计算','has_result':False,'notes':'该店铺账期尚无计算结果','selected_amount':None,
                       'unassigned_orders':None,'base_name':'','labor_cost':0};scopes[(sid,period)]=scope
            if scope['has_result']:
                total['amount']+=scope['selected_amount'];total['labor_cost']+=decimal(scope['labor_cost']);total['periods']+=1
            else:total['missing']+=1
            total['statuses'].add(scope['status'])
        total['people']=store_people.get(sid,set())
        store_totals[sid]=total
    for pid in selected_people-set(people_totals):
        p=roster.get(pid,{})
        tinfo=find_team_info(pid,roster)
        people_totals[pid]={'person_id':pid,'person':p.get('name') or available[pid]['name'],'team':tinfo['team_name'],'employee_no':p.get('employee_no',''),
                            'amount':None,'trial_amount':None,'actual_amount':None,'diff_amount':None,'is_confirmed':False,
                            'stores':set(),'periods':set(),'statuses':{'无对应提成记录'},'confirmed_count':0}
    person_rows=[{**r,'amount':money_float(r['amount']) if r['amount'] is not None else None,
                  'trial_amount':money_float(r['trial_amount']) if r['amount'] is not None else None,
                  'actual_amount':money_float(r['actual_amount']) if r['amount'] is not None else None,
                  'diff_amount':money_float(r['diff_amount']) if r['amount'] is not None else None,
                  'is_confirmed':r.get('confirmed_count',0)>0,
                  'stores':len(r['stores']),'periods':len(r['periods']),'status':'、'.join(sorted(r['statuses']))} for r in people_totals.values()]
    creator_scopes = {}
    for row in store_person_rows:
        if row.get('kind') == 'person':
            creator_scopes.setdefault(row['person_id'], []).append(row)
    for r in person_rows:
        r.update(creator_rollup(creator_scopes.get(r['person_id'], [])))
        r.update(metric_rollup(creator_scopes.get(r['person_id'], [])))
        r['managed_sales_pending']=r['managed_sales'] is None
        r['labor_pending']=any(s.get('labor_pending') for s in creator_scopes.get(r['person_id'], []))
        r.pop('statuses',None)
        r.pop('confirmed_count',None)

    teams_totals = {}
    for pid, ptot in people_totals.items():
        if ptot['amount'] is None and not ptot['stores']:
            continue
        tinfo = find_team_info(pid, roster)
        tid = tinfo['team_id'] or pid
        tname = tinfo['team_name'] or ptot['person']
        leader = tinfo['leader_name'] or ptot['person']
        t = teams_totals.setdefault(tid, {
            'team_id': tid,
            'team': tname,
            'leader': leader,
            'amount': Decimal(0),
            'trial_amount': Decimal(0),
            'actual_amount': Decimal(0),
            'diff_amount': Decimal(0),
            'members': set(),
            'member_details': [],
            'stores': set(),
            'periods': set(),
            'statuses': set(),
        })
        if ptot['amount'] is not None:
            t['amount'] += ptot['amount']
            t['actual_amount'] += ptot['amount']
            t['trial_amount'] += ptot['trial_amount'] if ptot.get('trial_amount') is not None else ptot['amount']
            t['diff_amount'] += ptot.get('diff_amount') or Decimal(0)
        t['members'].add(pid)
        t['stores'].update(ptot['stores'])
        t['periods'].update(ptot['periods'])
        t['statuses'].update(ptot['statuses'])
        t['member_details'].append({
            'person_id': pid,
            'person': ptot['person'],
            'employee_no': ptot['employee_no'],
            'amount': money_float(ptot['amount']) if ptot['amount'] is not None else None,
            'trial_amount': money_float(ptot['trial_amount']) if ptot['amount'] is not None else None,
            'actual_amount': money_float(ptot['actual_amount']) if ptot['amount'] is not None else None,
            'diff_amount': money_float(ptot['diff_amount']) if ptot['amount'] is not None else None,
            'stores': len(ptot['stores']),
            'periods': len(ptot['periods']),
        })
    for owned in managed_scopes:
        tid = owned['team_id']
        leader = roster.get(tid, {})
        t = teams_totals.setdefault(tid, {
            'team_id':tid,'team':leader.get('alias') or leader.get('name') or tid,
            'leader':leader.get('name') or tid,'amount':Decimal(0),'trial_amount':Decimal(0),
            'actual_amount':Decimal(0),'diff_amount':Decimal(0),'members':set(),'member_details':[],
            'stores':set(),'periods':set(),'statuses':set()})
        t['managed_sales'] = t.get('managed_sales', Decimal(0)) + decimal(owned['sales'])
        t['stores'].add(owned['store_id']); t['periods'].add(owned['period'])
    person_sales = {}
    for row in store_person_rows:
        if row['kind'] == 'person' and row.get('sales') is not None:
            pid = row['person_id']
            person_sales[pid] = person_sales.get(pid, Decimal(0)) + decimal(row['sales'])
            info = find_team_info(pid, roster)
            tid = info['team_id'] or pid
            t = teams_totals.setdefault(tid, {
                'team_id':tid,'team':info['team_name'],'leader':info['leader_name'],
                'amount':Decimal(0),'trial_amount':Decimal(0),'actual_amount':Decimal(0),
                'diff_amount':Decimal(0),'members':set(),'member_details':[],
                'stores':set(),'periods':set(),'statuses':set()})
            t['members'].add(pid)
            t['stores'].add(row['store_id']); t['periods'].add(row['period']); t['statuses'].add(row['status'])
    for row in person_rows:
        row['sales'] = money_float(person_sales[row['person_id']]) if row['person_id'] in person_sales else None
    sales_pending = [r for r in store_person_rows if r.get('sales_pending_products')]
    creator_pending = [r for r in store_person_rows if r.get('kind') == 'person' and r.get('creator_active') and r.get('creator_pending')]
    pending_people = {r['person_id'] for r in sales_pending}
    for row in person_rows:
        if row['person_id'] in pending_people:
            row['sales'] = None
            row['sales_pending'] = True
    team_rows = []
    for t in teams_totals.values():
        team_rows.append({
            'team_id': t['team_id'],
            'managed_sales': money_float(t.get('managed_sales', 0)),
            'sales': money_float(sum((person_sales.get(pid, Decimal(0)) for pid in t['members']), Decimal(0)) + t.get('managed_sales', Decimal(0))) if 'managed_sales' in t or any(pid in person_sales for pid in t['members']) else None,
            'team': t['team'],
            'leader': t['leader'],
            'amount': money_float(t['amount']),
            'trial_amount': money_float(t['trial_amount']),
            'actual_amount': money_float(t['actual_amount']),
            'diff_amount': money_float(t['diff_amount']),
            'members_count': len(t['members']),
            'members': sorted(t['member_details'], key=lambda m: -(m['amount'] or 0)),
            'stores': len(t['stores']),
            'periods': len(t['periods']),
            'status': '、'.join(sorted(t['statuses'])) if t['statuses'] else '暂无数据',
        })
    team_rows.sort(key=lambda x: -(x['amount'] or 0))
    creator_people = {row['person_id']: row for row in person_rows}
    for row in team_rows:
        members=[creator_people[member['person_id']] for member in row['members']
                 if member['person_id'] in creator_people]
        row.update(creator_rollup(members))
        row.update(metric_rollup(members, ('labor_cost','profit_after_labor')))
        row['labor_pending']=any(member.get('labor_pending') for member in members)
    pending_teams = {find_team_info(pid,roster)['team_id'] or pid for pid in pending_people}
    for row in team_rows:
        if row['team_id'] in pending_teams:
            row['sales'] = None
            row['sales_pending'] = True

    store_rows=[{**r,'configured_people':len(configured.get(r['store_id'],set()) & selected_people if selected_people else configured.get(r['store_id'],set())),'amount':money_float(r['amount']) if r['periods'] else None,'labor_cost':money_float(r['labor_cost']) if r['periods'] else None,'people':len(r['people']),
                 'status':'、'.join(sorted(r['statuses']))} for r in store_totals.values()]
    for row in store_rows:
        row['managed_sales'] = money_float(sum((decimal(s['sales']) for s in managed_scopes if s['store_id']==row['store_id']), Decimal(0))) if not selected_people else None
        row.update(metric_rollup([r for r in store_person_rows if r.get('kind')=='store' and r['store_id']==row['store_id']], ('profit_after_labor',)))
    for r in store_rows:r.pop('statuses')
    coverage=[{**r,'selected_amount':money_float(r['selected_amount']) if r['has_result'] else None} for r in scopes.values()]
    configured_total=set().union(*(configured.get(sid,set()) for sid in covered_stores))
    if selected_people:configured_total &= selected_people
    # Keep the reference amount for historical calculations/exports; expose
    # confirmed money separately instead of presenting trial money as actual.
    by_person = {}
    by_scope = {}
    for line in lines:
        by_person.setdefault(line['person_id'], []).append(line)
        by_scope.setdefault((line['person_id'], line['store_id'], line['period']), []).append(line)
    def payout_summary(parts):
        confirmed_parts = [p for p in parts if p['is_confirmed']]
        count, done = len(parts), len(confirmed_parts)
        amount = money_float(sum((decimal(p['amount']) for p in confirmed_parts), Decimal(0))) if done else None
        return {'confirmation_count': count, 'confirmed_count': done,
                'confirmation_state': 'confirmed' if count and done == count else 'partial' if done else 'pending',
                'is_confirmed': bool(count and done == count),
                'confirmed_amount': amount, 'actual_amount': amount}
    for row in person_rows:
        row.update(payout_summary(by_person.get(row['person_id'], [])))
    for row in team_rows:
        row.update(payout_summary([p for member in row['members'] for p in by_person.get(member['person_id'], [])]))
        for member in row['members']:
            member.update(payout_summary(by_person.get(member['person_id'], [])))
    for row in store_person_rows:
        if row.get('kind') == 'person':
            row.update(payout_summary(by_scope.get((row['person_id'], row['store_id'], row['period']), [])))
    for line in lines:
        line.update(payout_summary([line]))
    row_metrics={(r['person_id'],r['store_id'],r['period']):r for r in store_person_rows if r.get('kind')=='person'}
    for line in lines:
        reference=row_metrics.get((line['person_id'],line['store_id'],line['period']),{})
        line.update({key:reference.get(key) for key in ('managed_sales','labor_cost','profit_after_labor','store_labor_cost','managed_sales_pending','labor_pending')})
    return {'managed':managed_rows,'teams':team_rows,'people':sorted(person_rows,key=lambda x:x['person']),'stores':store_rows,'rows':lines,
            'sales_pending_scopes':[{k:r.get(k) for k in ('store_id','store','period','person_id','person','finance_run','sales_pending_products','sales_sources')} for r in sales_pending],
            'creator_pending_scopes':[{k:r.get(k) for k in ('store_id','store','period','person_id','person','finance_run')} for r in creator_pending],
            'store_people':store_person_rows,'coverage':coverage,
            'configured_people_count':len(configured_total),'available_people':list(available.values()),'run_ids':[r['id'] for r in records],
            'total':money_float(sum((decimal(x['amount']) for x in lines),Decimal(0))) if any(r['has_result'] for r in coverage) else None,
            'missing_periods':sum(not r['has_result'] for r in coverage),
            'trial_periods':sum('试算' in r['status'] for r in coverage),
            'selection':{'start':start,'end':end,'store_ids':sorted(selected_stores),'person_ids':sorted(selected_people)}}


COLUMNS={
 'managed':['层级','行类型','托管团队','店铺','月份','人员','宝贝ID','商品','托管销售额','托管毛利额','托管利润额（分摊后）','托管系统应发','说明'],
 'teams':['团队/团队长','系统应发','实发提成','调整差额','团队人数','店铺数','账期数','计算状态','做货创造毛利','做货创造利润','托管类销售额','兼职额','利润额（扣兼职）'],
 'people':['人员','工号','提成金额','店铺数','账期数','计算状态','人员ID','做货创造毛利','做货创造利润','托管类销售额','兼职额','利润额（扣兼职）'],
 'stores':['店铺','提成金额','兼职分摊','提成设置人数','已出金额人数','已计算账期数','未计算账期数','计算状态','托管类销售额','利润额（扣兼职）'],
 'store_people':['店铺','分配人','月份','销售额/参与销售额','毛利额/参与毛利额','利润额','兼职额','本人参与基数','基数名称','提成额','店铺提成合计','状态','做货成本','做货创造毛利','做货创造利润','托管类销售额'],
 'breakdown':['人员','工号','店铺','账期','提成金额','本人参与基数','基数名称','计算状态','计算时间','说明','计算记录','托管类销售额','兼职额','利润额（扣兼职）'],
 'coverage':['店铺','账期','筛选范围提成金额','计算状态','未分配订单数','计算时间','说明','计算记录']}


def export_rows(report, kind):
    if kind=='managed':
        yield from business_export(report,kind)[1]
    elif kind=='teams':
        for r in report.get('teams',[]):yield dict(zip(COLUMNS[kind],[r['team'],r.get('trial_amount'),r['amount'],r.get('diff_amount'),r['members_count'],r['stores'],r['periods'],r['status'],r.get('creator_gross'),r.get('creator_profit'),r.get('managed_sales'),r.get('labor_cost'),r.get('profit_after_labor')]))
    elif kind=='people':
        for r in report['people']:yield dict(zip(COLUMNS[kind],[r['person'],r['employee_no'],r['amount'],r['stores'],r['periods'],r['status'],r['person_id'],r.get('creator_gross'),r.get('creator_profit'),r.get('managed_sales'),r.get('labor_cost'),r.get('profit_after_labor')]))
    elif kind=='stores':
        for r in report['stores']:yield dict(zip(COLUMNS[kind],[r['store'],r['amount'],r['labor_cost'],r['configured_people'],r['people'] if r['periods'] else None,r['periods'],r['missing'],r['status'],r.get('managed_sales'),r.get('profit_after_labor')]))
    elif kind=='store_people':
        for r in report['store_people']:
            yield dict(zip(COLUMNS[kind],[r['store'],r['person'],r['period'],r['sales'],r['gross'],r['profit_after_labor'],r['labor_cost'],
                                          r['base'],r['base_name'],r['amount'],r['store_amount'],r['status'],
                                          r.get('creator_cost'),r.get('creator_gross'),r.get('creator_profit'),r.get('managed_sales')] ))
    elif kind=='breakdown':
        for r in report['rows']:yield dict(zip(COLUMNS[kind],[r['person'],r['employee_no'],r['store'],r['period'],r['amount'],r['base'],r['base_name'],r['status'],r['calculated_at'],r['notes'],r['finance_run'],r.get('managed_sales'),r.get('labor_cost'),r.get('profit_after_labor')]))
    elif kind=='coverage':
        for r in report['coverage']:yield dict(zip(COLUMNS[kind],[r['store'],r['period'],r['selected_amount'],r['status'],r['unassigned_orders'],r['calculated_at'],r['notes'],r['finance_run']]))
    else:raise RegistryError('请选择导出类型')


def business_export(report, kind):
    if kind=='managed':
        selected_people=bool(report.get('selection',{}).get('person_ids'))
        def export_item(item,level,kind):
            note='仅对同一层级求和；团队合计、个人小计与商品明细不可重复相加'
            if selected_people:
                note+='；当前仅包含筛选人员，不代表完整团队'
            note+='；'+item.get('notes','')+'；'+item.get('status','')
            return dict(zip(COLUMNS['managed'],[level,kind,item['team'],item['store'],item['period'],
                item.get('person','') if level>1 else '',item.get('product_id','') if level==3 else '',
                item.get('subject','') if level==3 else '',item.get('sales'),item.get('gross'),
                item.get('profit_after_labor'),item.get('trial_amount'),note]))
        def managed_lines():
            for team in report.get('managed',[]):
                yield export_item(team,1,'团队小计（筛选人员）' if selected_people else '团队合计')
                for person in team.get('children',[]):
                    yield export_item(person,2,'个人小计' if person.get('person_id') else '待分配小计')
                    for item in person.get('children',[]):
                        yield export_item(item,3,'商品明细')
        return COLUMNS[kind],managed_lines()
    columns = {
        'teams': [('团队/团队长','team'),('做货创造毛利','creator_gross'),('做货创造利润','creator_profit'),('系统应发','trial_amount'),('实发提成','amount'),('调整差额','diff_amount'),('团队人数','members_count'),('负责店铺数','stores'),('月份数','periods'),('状态','status')],
        'people': [('人员','person'),('所属团队','team'),('工号','employee_no'),('做货创造毛利','creator_gross'),('做货创造利润','creator_profit'),('系统应发','trial_amount'),('提成金额','amount'),('调整差额','diff_amount'),('店铺数','stores'),('月份数','periods'),('状态','status')],
        'stores': [('店铺','store'),('提成金额','amount'),('兼职分摊','labor_cost'),('提成设置人数','configured_people'),('已出金额人数','people'),('已有金额月份','periods'),('未出金额月份','missing'),('状态','status')],
        'store_people': [('店铺','store'),('分配人','person'),('所属团队','team'),('月份','period'),('核算记录','finance_run'),('做货规则指纹','creator_rule_revision'),('销售额','sales'),('做货成本','creator_cost'),('做货创造毛利','creator_gross'),('做货创造利润','creator_profit'),('原核算毛利','gross'),
                         ('利润额（扣兼职）','profit_after_labor'),('兼职额','labor_cost'),('本人参与提成基数','base'),('系统应发','trial_amount'),('提成额','amount'),('调整差额','diff_amount'),
                         ('店铺提成合计','store_amount'),('状态','status')],
        'breakdown': [('人员','person'),('所属团队','team'),('工号','employee_no'),('店铺','store'),('月份','period'),('系统应发','trial_amount'),('提成金额','amount'),('调整差额','diff_amount'),('状态','status')],
        'coverage': [('店铺','store'),('月份','period'),('提成金额','selected_amount'),('状态','status'),('未分配人员订单数','unassigned_orders')],
    }[kind]
    if kind in {'teams', 'people', 'store_people', 'breakdown'}:
        columns = [('参考提成金额' if key == 'amount' else label, key) for label, key in columns]
        columns += [('已核定实发','confirmed_amount'), ('核定状态','confirmation_state'),
                    ('已核定项数','confirmed_count'), ('待核定及已核定项数','confirmation_count')]
    if kind in {'teams', 'people'}:
        columns += [('销售额','sales'),('兼职额','labor_cost'),('利润额（扣兼职）','profit_after_labor')]
    if kind in {'stores','breakdown'}:
        columns += [('利润额（扣兼职）','profit_after_labor')]
    if kind=='breakdown':
        columns += [('兼职额','labor_cost'),('托管类销售额','managed_sales')]
    if kind in {'teams', 'people', 'store_people', 'stores'}:
        columns += [('托管类销售额','managed_sales')]
    if kind in {'teams','people','store_people'}:
        columns += [('销售归属状态','sales_attribution_state')]
    if kind=='store_people':columns += [('行类型','kind'),('销售口径','sales_scope')]
    if kind in {'teams','people','stores','store_people','breakdown'}:
        columns += [('利润口径','profit_scope'),('兼职分摊说明','labor_scope')]
    def rows():
        for row in report['rows' if kind == 'breakdown' else kind]:
            item = {label:row.get(key) for label,key in columns}
            if '利润口径' in item:
                item['利润口径']=('全店经营利润（含托管）减本店兼职' if kind=='stores' or row.get('kind')=='store' else
                               '原财务分摊利润（含托管及调整），已经扣兼职；不等同非托管做货创造利润')
                item['兼职分摊说明']='沿用原利润扣减的销售基数（含托管）；仅展示已扣金额，不再次扣减；筛选不重分摊'
            if '销售口径' in item:
                item['销售口径']={'person':'非托管商品创造业绩按做货身份归属；提成与原核算另列','managed':'托管销售额；不计入个人非托管销售','store':'全店财务销售额（含托管）；个人创造列不作店铺合计','unassigned':'尚未分配到人员'}.get(row.get('kind'),'')
                item['销售口径']+='；店铺合计、个人和托管团队小计为不同层级，托管额不可重复相加'
                item['行类型']={'person':'个人','managed':'托管团队小计','store':'店铺合计','unassigned':'待分配'}.get(row.get('kind'),'')
            if '销售归属状态' in item:
                item['销售归属状态'] = ('店铺财务口径' if row.get('kind')=='store' else
                    '托管团队归属' if row.get('kind')=='managed' else
                    '待确认' if row.get('sales_pending') or row.get('sales_pending_products') else
                    '未取得销售额' if row.get('sales') is None else '已解析')
            if '核定状态' in item:
                item['核定状态'] = {'pending':'待核定','partial':'部分核定','confirmed':'已核定实发'}.get(item['核定状态'], '')
            if kind=='stores' and not row.get('periods'):item['已出金额人数']=None
            for original, replacement in [('未计算提成','未出金额'),('未计算','未出金额'),('试算','待核对'),('历史口径','历史提成'),('已计算','待结账'),('无对应提成记录','暂无提成'),('合计待核对','金额待核对')]:
                item['状态'] = item['状态'].replace(original, replacement)
            yield item
    return [label for label,_ in columns], rows()
