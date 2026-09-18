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

from . import overhead
from .commission_engine import allocated_outputs
from .commission_registry import RegistryError, json_text
from .labor_api import frozen_shares
from .money import money_float


def store_member_duties(registry, store_id, at=None):
    """Load confirmed store member duties at a point in time."""
    stamp = at or ''
    if stamp and len(stamp) == 7:
        stamp = f'{stamp}-01T00:00:00'
    return {r['person_id']: r for r in registry.store_members_at(store_id, stamp)}


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
_PROFIT_CACHE_LIMIT = 128


def _archived_allocated_outputs(registry, commission):
    calculation = commission.get('calculation_id')
    if not calculation:
        return None
    with registry.connect() as conn:
        row = conn.execute('SELECT path,sha FROM calculation WHERE id=?',
                           (calculation,)).fetchone()
    if not row:
        return None
    key = (str(registry.root.resolve()), calculation, row['sha'])
    with _profit_lock:
        if key in _profit_cache:
            _profit_cache.move_to_end(key)
            return _profit_cache[key]
    path = registry.root / 'calculations' / Path(row['path']).name
    try:
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != row['sha']:
            return None
        from io import BytesIO
        schema = pl.read_parquet_schema(BytesIO(payload))
        cols = [name for name in ('status', 'person_id', 'share', 'total_rate',
                                  'participation_sales','participation_gross',
                                  'participation_profit', 'original_base')
                if name in schema]
        details = pl.read_parquet(BytesIO(payload), columns=cols)
        result = allocated_outputs(details, net_profit_basis=(
            commission.get('base_node') == 'net_profit'
            and commission.get('on_loss') == 'deduct'))
    except (OSError, ValueError, pl.exceptions.PolarsError):
        result = None
    with _profit_lock:
        _profit_cache[key] = result
        if len(_profit_cache) > _PROFIT_CACHE_LIMIT:
            _profit_cache.popitem(last=False)
    return result


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


def attributed_profit(commission, operating, labor, registry, *, store_id='', manual_cost=False, duties=None):
    """Additive person profit; keep full sales and gross output separate.

    Manual cost changes the store profit, not the ownership split. The
    allocated-to-operating residual already absorbs that gap.

    When duties are provided and there are producers among the people,
    profit goes 100% to producers; cut members get 0.
    """
    people = commission.get('people') or []
    if operating is None or not people:
        return {}
    ids = [p.get('person_id') for p in people]
    if any(not pid for pid in ids) or len(ids) != len(set(ids)):
        return {}
    if len(people) == 1:
        return {ids[0]: profit_after_labor(operating, labor)}
    # duty-based: producers get all profit, cut members get 0
    if duties and len(people) > 1:
        producers = [p for p in people if duties.get(p['person_id'], {}).get('duty', 'produce') == 'produce']
        if producers and len(producers) < len(people):
            producer_result = attributed_profit(
                {**commission, 'people': producers}, operating, labor, registry,
                store_id=store_id, manual_cost=manual_cost)
            result = {pid: 0.0 for pid in ids}
            result.update(producer_result)
            return result
    split = {p['person_id']: {'sales':p.get('allocated_sales'),
                              'gross':p.get('allocated_gross'),
                              'profit':p.get('allocated_profit')}
             for p in people if p.get('person_id') and
             all(p.get(key) is not None for key in
                 ('allocated_sales','allocated_gross','allocated_profit'))}
    if len(split) != len(people):
        split = _archived_allocated_outputs(registry, commission)
    if split is None or any(p.get('person_id') not in split for p in people):
        return _profit_from_sales(people, operating, labor)
    values = {p['person_id']: decimal(split[p['person_id']]['profit']) for p in people}
    weights = {p['person_id']: max(decimal(split[p['person_id']].get('sales') or 0), Decimal(0))
               for p in people}
    # A negative store residual is shared as operating loss. Positive
    # unattributed profit remains at the store until its owner is known.
    residual = max(sum(values.values(), Decimal(0)) - decimal(operating), Decimal(0))
    cost = _split_cents(labor or 0, weights)
    loss = _split_cents(residual, weights)
    if cost is None or loss is None:
        return {}
    return {pid: money_float(values[pid] - cost[pid] - loss[pid])
            for pid in weights}


def attributed_outputs(commission, store_sales, store_gross, registry, *, duties=None):
    people=commission.get('people') or []
    ids=[p.get('person_id') for p in people]
    if not people or any(not pid for pid in ids) or len(ids)!=len(set(ids)):
        return {}
    if len(people)==1:
        return {ids[0]:{'sales':store_sales,'gross':store_gross}}
    # duty-based: producers get all sales/gross, cut members get 0
    if duties and len(people) > 1:
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
    if len(split)!=len(people):
        archived=_archived_allocated_outputs(registry,commission)
        if archived is None:return {}
        split={pid:{'sales':value.get('sales'),'gross':value.get('gross')}
               for pid,value in archived.items()}
    if any(pid not in split or split[pid].get('sales') is None or
           split[pid].get('gross') is None for pid in ids):
        return {}
    values={pid:{'sales':decimal(split[pid]['sales']),
                 'gross':decimal(split[pid]['gross'])} for pid in ids}
    weights={pid:max(values[pid]['sales'],Decimal(0)) for pid in ids}
    for name,store_value in (('sales',store_sales),('gross',store_gross)):
        if store_value is None:continue
        excess=max(sum((value[name] for value in values.values()),Decimal(0))
                   -decimal(store_value),Decimal(0))
        cuts=_split_cents(excess,weights)
        if cuts is None:return {}
        for pid in ids:values[pid][name]-=cuts[pid]
    return {pid:{name:money_float(amount) for name,amount in value.items()}
            for pid,value in values.items()}


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


_configured_lock = threading.RLock()
_configured_cache: OrderedDict[tuple, dict[str, frozenset[str]]] = OrderedDict()
_CONFIGURED_CACHE_LIMIT = 64


def _scan_configured_people(registry, start, end, store_ids=()):
    year, month = map(int, end.split('-'))
    upper = f"{year + (month == 12):04d}-{month % 12 + 1:02d}-01T00:00:00"
    lower = start + '-01T00:00:00'
    where = (' AND s.store_id IN (' + ','.join('?' for _ in store_ids) + ')') if store_ids else ''
    with registry.connect() as conn:
        rows = conn.execute("""SELECT DISTINCT s.store_id,json_extract(a.value,'$.person_id') pid
          FROM scheme s JOIN scheme_version v ON v.id=s.active_version
          JOIN json_each(v.body,'$.segments') seg JOIN json_each(seg.value,'$.allocations') a
          WHERE json_extract(seg.value,'$.mode')='distribute'
          AND json_extract(seg.value,'$.valid_from')<?
          AND (coalesce(json_extract(seg.value,'$.valid_to'),'')='' OR json_extract(seg.value,'$.valid_to')>?)"""+where,
          (upper,lower,*store_ids)).fetchall()
    out = {}
    for row in rows:
        if row['pid']:out.setdefault(row['store_id'],set()).add(row['pid'])
    return out


def configured_people(registry, start, end, store_ids=()):
    """Amortize the 100k-scheme scan across reads; business audit revision invalidates it."""
    scope=tuple(sorted(store_ids))
    key=(str(registry.root.resolve()), registry.revision(), start, end, scope)
    with _configured_lock:
        saved=_configured_cache.get(key)
        if saved is None:
            fresh=_scan_configured_people(registry,start,end,scope)
            saved={sid:frozenset(people) for sid,people in fresh.items()}
            _configured_cache[key]=saved
            if len(_configured_cache)>_CONFIGURED_CACHE_LIMIT:
                _configured_cache.popitem(last=False)
        else:
            _configured_cache.move_to_end(key)
    return {sid:set(people) for sid,people in saved.items()}


def build(workspace, registry, model, start, end, store_ids=None, person_ids=None, run_ids=None,
          model_root: Path | None = None):
    periods=months(start,end)
    selected_stores=set(store_ids or []); selected_people=set(person_ids or [])
    names={s.id:s.name for s in model.stores}
    roster={p['id']:p for p in registry.people()}
    configured=configured_people(registry,start,end,selected_stores)
    revenue_node=next((n.id for n in model.statement if n.headline=='revenue'),'')
    sales_node=next((n.id for n in model.statement if n.name=='销售收入' and n.level==2),'')
    gross_node=next((n.id for n in model.statement if n.name=='毛利' and n.is_total),'')
    profit_node=next((n.id for n in model.statement if n.headline=='profit' and n.is_total),'')
    basis_rows=workspace.conn.execute("""SELECT r.store_id,r.period,
      CASE WHEN coalesce(json_extract(node.value,'$.available'),1)=1
           THEN json_extract(node.value,'$.value') ELSE NULL END revenue
      FROM period p JOIN run r ON r.id=CASE WHEN p.state='closed' AND p.run_id IS NOT NULL THEN p.run_id
        ELSE (SELECT id FROM run latest WHERE latest.store_id=p.store_id AND latest.period=p.period ORDER BY id DESC LIMIT 1) END
      LEFT JOIN json_each(r.result,'$.statement') node ON json_extract(node.value,'$.id')=?
      WHERE r.period>=? AND r.period<=?""",(revenue_node,start,end)).fetchall()
    labor_spreads={period:overhead.allocate(period,model.overhead(period),[
        (row['store_id'],float(row['revenue'] or 0)) for row in basis_rows if row['period']==period
    ]) for period in periods}
    frozen=frozen_shares(model_root) if model_root else {}
    where=['r.period>=?','r.period<=?']; args=[start,end]
    if selected_stores:
        where.append('r.store_id IN ('+','.join('?' for _ in selected_stores)+')');args.extend(sorted(selected_stores))
    if run_ids is None:
        source="""FROM period p JOIN run r ON r.id=CASE WHEN p.state='closed' AND p.run_id IS NOT NULL THEN p.run_id
          ELSE (SELECT id FROM run latest WHERE latest.store_id=p.store_id AND latest.period=p.period ORDER BY id DESC LIMIT 1) END"""
    else:
        if len(run_ids)!=len(set(run_ids)):raise RegistryError('计算记录不能重复')
        source='FROM run r LEFT JOIN period p ON p.store_id=r.store_id AND p.period=r.period'
        where.append('r.id IN ('+','.join('?' for _ in run_ids)+')' if run_ids else '0');args.extend(run_ids)
    manual_join=' LEFT JOIN manual_finance mf ON mf.run_id=r.id' if run_ids is not None else " LEFT JOIN manual_finance mf ON mf.run_id=r.id AND p.state='closed'"
    records=[dict(r) for r in workspace.conn.execute("SELECT r.id,r.store_id,r.period,r.at,json_extract(coalesce(mf.result_json,r.result),'$.commission') commission_json,json_extract(coalesce(mf.result_json,r.result),'$.statement') statement_json,json_extract(coalesce(mf.result_json,r.result),'$.manual_cost') manual_cost_json,json_extract(coalesce(mf.result_json,r.result),'$.store') store_name,p.state,p.run_id frozen_id,rl.amount frozen_labor_cut "+source+' LEFT JOIN run_labor rl ON rl.run_id=r.id'+manual_join+' WHERE '+' AND '.join(where)+' ORDER BY r.period DESC,r.store_id',args)]
    if run_ids is not None and len(records)!=len(run_ids):raise RegistryError('部分计算记录已不存在或不在所选范围，请重新查询')
    keys=[(r['store_id'],r['period']) for r in records]
    if len(keys)!=len(set(keys)):raise RegistryError('同店同账期只能选择一份计算结果')
    from .commission_confirm import active_for_runs
    confirmed = active_for_runs(registry, records)
    _duties_cache = {}
    def _get_duties(sid, period=''):
        key = (sid, period)
        if key not in _duties_cache:
            found = store_member_duties(registry, sid, period)
            _duties_cache[key] = found or None
        return _duties_cache[key]
    scopes={}; lines=[]; store_person_rows=[]; available={}; people_totals={}; store_totals={}; store_people={}
    for record in records:
        c=json.loads(record['commission_json'] or '{}');sid=record['store_id'];period=record['period']
        source_c=c
        decision=confirmed.get((sid,period,record['id']))
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
        else:
            decision=None
        names.setdefault(sid,record['store_name'] or sid)
        closed=record['state']=='closed' and record['frozen_id']==record['id']
        legacy=c.get('engine')!='commission-v2'
        status=('已人工确认' if decision else
                '试算（店铺已结账）' if closed and c.get('amount_complete') is False else
                '已结账' if closed else
                '历史口径' if legacy else
                '已计算' if c.get('amount_complete') else '试算')
        notes=list(c.get('notes') or [])
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
        person_output=attributed_outputs(source_c,sales,gross,registry,duties=store_duties)
        person_profit=attributed_profit(source_c,operating,visible_labor,registry,store_id=sid,duties=store_duties)
        profit_rates={person.get('person_id'):confirmed_profit_rate(
            source_c,person.get('person_id'),sid) for person in source_c.get('people') or []}
        if not decision and any(
                rate is not None and person_profit.get(pid) is not None
                for pid,rate in profit_rates.items()):
            if len(source_c.get('people') or [])==1:
                notes.append(f'本店仅一位分配人：店铺利润全部归本人，提成按利润额乘唯一有效点数{money_float(next(iter(profit_rates.values()))*100):g}%计算')
            else:
                notes.append('人员销售、毛利、利润按提成点数拆分；总点数唯一时，提成按人员利润额乘该点数计算')
            scope['notes']='；'.join(notes)
        member_rows=[]
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
            duty=(store_duties or {}).get(person.get('person_id') or pid, {}).get('duty')
            if person.get('amount') is None:
                if person.get('sales') is not None or person.get('gross') is not None:
                    member_rows.append({'kind':'person','person_id':pid,'person':name,
                                        'employee_no':roster.get(pid,{}).get('employee_no',''),
                                        'duty':duty,
                                        'store_id':sid,'store':names[sid],'period':period,
                                        'sales':person_output.get(person.get('person_id'),{}).get('sales'),
                                        'gross':person_output.get(person.get('person_id'),{}).get('gross'),
                                        'profit_after_labor':person_profit.get(person.get('person_id')),
                                        'labor_cost':None,'base':None,'base_name':'',
                                        'amount':None,'store_amount':None,
                                        'status':status,'finance_run':record['id']})
                continue
            profit_rate=(profit_rates.get(person.get('person_id'))
                         if not decision else None)
            if (profit_rate is not None and person.get('person_id') in person_profit
                    and person.get('amount') is not None):
                amount=decimal(money_float(decimal(person_profit[person['person_id']])*profit_rate))
            else:
                amount=decimal(money_float(decimal(person['amount'])*keep))
            scope['selected_amount']+=amount
            store_people.setdefault(sid,set()).add(pid)
            lines.append({'person_id':pid,'person':name,'employee_no':roster.get(pid,{}).get('employee_no',''),
                          'store_id':sid,'store':names[sid],'period':period,'amount':money_float(amount),
                          'base':person.get('base'),'base_name':scope['base_name'],
                          'sales':person.get('sales'),'gross':person.get('gross'),'status':status,
                          'calculated_at':record['at'],'finance_run':record['id'],'notes':scope['notes']})
            member_rows.append({'kind':'person','person_id':pid,'person':name,
                                'employee_no':roster.get(pid,{}).get('employee_no',''),
                                'duty':duty,
                                'store_id':sid,'store':names[sid],'period':period,
                                'sales':person_output.get(person.get('person_id'),{}).get('sales'),
                                'gross':person_output.get(person.get('person_id'),{}).get('gross'),
                                'profit_after_labor':person_profit.get(person.get('person_id')),
                                'labor_cost':None,
                                'base':person.get('base'),'base_name':scope['base_name'],
                                'amount':money_float(amount),'store_amount':None,
                                'status':status,'finance_run':record['id']})
            total=people_totals.setdefault(pid,{'person_id':pid,'person':label,'employee_no':roster.get(pid,{}).get('employee_no',''),
                                               'amount':Decimal(0),'stores':set(),'periods':set(),'statuses':set()})
            total['amount']+=amount;total['stores'].add(sid);total['periods'].add(period);total['statuses'].add(status)
        if not selected_people and source_c.get('people'):
            store_profit = profit_after_labor(operating, visible_labor)
            residual_sales = _output_residual(sales, person_output, 'sales')
            residual_gross = _output_residual(gross, person_output, 'gross')
            residual_profit = _output_residual(
                store_profit,
                {pid: {'profit': value} for pid, value in person_profit.items()},
                'profit',
            )
            residuals = (residual_sales, residual_gross, residual_profit)
            if any(value is not None and abs(value) > .01 for value in residuals):
                missing_count = int(source_c.get('unassigned_orders') or 0)
                label = (f'未分配（{missing_count}笔订单信息不完整）'
                         if missing_count else '未分配（缺少人员归属证据）')
                member_rows.append({
                    'kind':'unassigned','person_id':None,'person':label,'employee_no':'',
                    'store_id':sid,'store':names[sid],'period':period,
                    'sales':residual_sales,'gross':residual_gross,
                    'profit_after_labor':residual_profit,'labor_cost':None,
                    'base':source_c.get('unassigned_base'),'base_name':scope['base_name'],
                    'amount':None,'store_amount':None,'status':'待核对',
                    'finance_run':record['id'],
                })
                profit_label = ('待核对' if residual_profit is None
                                else f'{residual_profit:,.2f} 元')
                notes.append(f'{label}：利润额 {profit_label}；人员行与店铺合计的差额已显式列出')
                scope['notes']='；'.join(notes)
        scopes[(sid,period)]=scope
        if not selected_people or member_rows:
            store_person_rows.append({'kind':'store','person':'店铺合计','person_id':None,
                                      'employee_no':'','store_id':sid,'store':names[sid],
                                      'period':period,'sales':sales,'gross':gross,
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
        people_totals[pid]={'person_id':pid,'person':p.get('name') or available[pid]['name'],'employee_no':p.get('employee_no',''),
                            'amount':None,'stores':set(),'periods':set(),'statuses':{'无对应提成记录'}}
    person_rows=[{**r,'amount':money_float(r['amount']) if r['amount'] is not None else None,'stores':len(r['stores']),
                  'periods':len(r['periods']),'status':'、'.join(sorted(r['statuses']))} for r in people_totals.values()]
    for r in person_rows:r.pop('statuses')
    store_rows=[{**r,'configured_people':len(configured.get(r['store_id'],set()) & selected_people if selected_people else configured.get(r['store_id'],set())),'amount':money_float(r['amount']) if r['periods'] else None,'labor_cost':money_float(r['labor_cost']) if r['periods'] else None,'people':len(r['people']),
                 'status':'、'.join(sorted(r['statuses']))} for r in store_totals.values()]
    for r in store_rows:r.pop('statuses')
    coverage=[{**r,'selected_amount':money_float(r['selected_amount']) if r['has_result'] else None} for r in scopes.values()]
    configured_total=set().union(*(configured.get(sid,set()) for sid in covered_stores))
    if selected_people:configured_total &= selected_people
    return {'people':sorted(person_rows,key=lambda x:x['person']),'stores':store_rows,'rows':lines,
            'store_people':store_person_rows,'coverage':coverage,
            'configured_people_count':len(configured_total),'available_people':list(available.values()),'run_ids':[r['id'] for r in records],
            'total':money_float(sum((decimal(x['amount']) for x in lines),Decimal(0))) if any(r['has_result'] for r in coverage) else None,
            'missing_periods':sum(not r['has_result'] for r in coverage),
            'trial_periods':sum('试算' in r['status'] for r in coverage),
            'selection':{'start':start,'end':end,'store_ids':sorted(selected_stores),'person_ids':sorted(selected_people)}}


COLUMNS={
 'people':['人员','工号','提成金额','店铺数','账期数','计算状态','人员ID'],
 'stores':['店铺','提成金额','兼职分摊','提成设置人数','已出金额人数','已计算账期数','未计算账期数','计算状态'],
 'store_people':['店铺','分配人','月份','销售额/参与销售额','毛利额/参与毛利额','利润额','兼职额','本人参与基数','基数名称','提成额','店铺提成合计','状态'],
 'breakdown':['人员','工号','店铺','账期','提成金额','本人参与基数','基数名称','计算状态','计算时间','说明','计算记录'],
 'coverage':['店铺','账期','筛选范围提成金额','计算状态','未分配订单数','计算时间','说明','计算记录']}


def export_rows(report, kind):
    if kind=='people':
        for r in report['people']:yield dict(zip(COLUMNS[kind],[r['person'],r['employee_no'],r['amount'],r['stores'],r['periods'],r['status'],r['person_id']]))
    elif kind=='stores':
        for r in report['stores']:yield dict(zip(COLUMNS[kind],[r['store'],r['amount'],r['labor_cost'],r['configured_people'],r['people'] if r['periods'] else None,r['periods'],r['missing'],r['status']]))
    elif kind=='store_people':
        for r in report['store_people']:
            yield dict(zip(COLUMNS[kind],[r['store'],r['person'],r['period'],r['sales'],r['gross'],r['profit_after_labor'],r['labor_cost'],
                                          r['base'],r['base_name'],r['amount'],r['store_amount'],r['status']]))
    elif kind=='breakdown':
        for r in report['rows']:yield dict(zip(COLUMNS[kind],[r['person'],r['employee_no'],r['store'],r['period'],r['amount'],r['base'],r['base_name'],r['status'],r['calculated_at'],r['notes'],r['finance_run']]))
    elif kind=='coverage':
        for r in report['coverage']:yield dict(zip(COLUMNS[kind],[r['store'],r['period'],r['selected_amount'],r['status'],r['unassigned_orders'],r['calculated_at'],r['notes'],r['finance_run']]))
    else:raise RegistryError('请选择导出类型')


def business_export(report, kind):
    columns = {
        'people': [('人员','person'),('工号','employee_no'),('提成金额','amount'),('店铺数','stores'),('月份数','periods'),('状态','status')],
        'stores': [('店铺','store'),('提成金额','amount'),('兼职分摊','labor_cost'),('提成设置人数','configured_people'),('已出金额人数','people'),('已有金额月份','periods'),('未出金额月份','missing'),('状态','status')],
        'store_people': [('店铺','store'),('分配人','person'),('月份','period'),('销售额/参与销售额','sales'),('毛利额/参与毛利额','gross'),
                         ('利润额','profit_after_labor'),('兼职额','labor_cost'),('本人参与提成基数','base'),('提成额','amount'),
                         ('店铺提成合计','store_amount'),('状态','status')],
        'breakdown': [('人员','person'),('工号','employee_no'),('店铺','store'),('月份','period'),('提成金额','amount'),('状态','status')],
        'coverage': [('店铺','store'),('月份','period'),('提成金额','selected_amount'),('状态','status'),('未分配人员订单数','unassigned_orders')],
    }[kind]
    def rows():
        for row in report['rows' if kind == 'breakdown' else kind]:
            item = {label:row.get(key) for label,key in columns}
            if kind=='stores' and not row.get('periods'):item['已出金额人数']=None
            for original, replacement in [('未计算提成','未出金额'),('未计算','未出金额'),('试算','待核对'),('历史口径','历史提成'),('已计算','待结账'),('无对应提成记录','暂无提成'),('合计待核对','金额待核对')]:
                item['状态'] = item['状态'].replace(original, replacement)
            yield item
    return [label for label,_ in columns], rows()
