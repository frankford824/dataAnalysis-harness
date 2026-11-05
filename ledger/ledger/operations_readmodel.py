"""Versioned management reporting over saved finance evidence.

No accounting result, commission amount, relationship or closed period is written.
Management definitions deliberately differ from the historical commission basis.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from collections import OrderedDict
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import polars as pl

VERSION = 'operations-20261027.v4'
_cache = OrderedDict()
_cache_lock = threading.Lock()
# 推广费用 = 平台推广 + 补单成本（本金佣金）；客服售后费用只含客服打款。
PROMOTION = ('ad_cost', 'brushing_cost')
AFTERSALES = ('small_payment_cost',)
PRIMITIVES = ('receipts', 'refunds', 'net_income', 'goods_cost', *PROMOTION, 'platform_cost', 'freight_cost', *AFTERSALES, 'labor_cost')
MONEY = (*PRIMITIVES, 'promotion_cost', 'aftersales_cost', 'gross_profit', 'profit', 'profit_after_labor', 'commission')
RATES = {'refund_rate': ('refunds', 'receipts'), 'goods_rate': ('goods_cost', 'net_income'),
         'gross_margin': ('gross_profit', 'net_income'), 'promotion_rate': ('promotion_cost', 'net_income'),
         'freight_rate': ('freight_cost', 'net_income'), 'profit_margin': ('profit', 'net_income'),
         'labor_rate': ('labor_cost', 'net_income'), 'after_labor_margin': ('profit_after_labor', 'net_income')}
LABELS = {'receipts':'对账销售收入（未扣退款）','refunds':'销售退款','net_income':'销售收入（到账）',
          'refund_rate':'退款率','goods_cost':'商品成本','goods_rate':'货本率','gross_profit':'商品毛利',
          'gross_margin':'毛利率','promotion_cost':'推广费用','ad_cost':'其中：平台推广','brushing_cost':'其中：补单成本',
          'promotion_rate':'推广费用占比',
          'platform_cost':'平台费用','freight_cost':'发货运费','freight_rate':'发货运费占比',
          'aftersales_cost':'客服售后费用','small_payment_cost':'客服打款',
          'profit':'利润（扣兼职前）','profit_margin':'利润率',
          'labor_cost':'兼职人工费用','labor_rate':'兼职人工费用占比','profit_after_labor':'利润额（扣兼职后）',
          'after_labor_margin':'利润额率','commission':'已保存提成','order_count':'订单数'}
DISPLAY = ('net_income','refund_rate','goods_cost','goods_rate','gross_profit','gross_margin',
           'promotion_cost',*PROMOTION,'promotion_rate','platform_cost','freight_cost','freight_rate',
           'aftersales_cost','profit','profit_margin','labor_cost','labor_rate','profit_after_labor','after_labor_margin')
COMPONENTS = {name:'promotion_cost' for name in PROMOTION}
NODES = {'receipts': ('n_receipt',), 'refunds': ('n_refund',),
         'goods_cost': ('n_goods','n_dropship','n_reshipment'), 'ad_cost': ('n_ad',),
         'platform_cost': ('g_platform',), 'freight_cost': ('n_freight',),
         'small_payment_cost': ('n_small_payment',), 'brushing_cost': ('n_brushing',)}

ROW_PRIMITIVES = (*NODES, 'net_income')
_UNSET = object()


class OperationsError(ValueError):
    pass


def decimal(value):
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def check_period(period):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period or ''):
        raise OperationsError('请选择有效月份（YYYY-MM）')
    return period


def shifted(period, months):
    year, month = map(int, check_period(period).split('-'))
    total = year * 12 + month - 1 + months
    return f'{total // 12:04d}-{total % 12 + 1:02d}'


def _sum(values):
    values = list(values)
    return sum(values, Decimal(0)) if values and all(v is not None for v in values) else None


def _difference(first, *rest):
    return first - sum(rest, Decimal(0)) if first is not None and all(v is not None for v in rest) else None


def derive(values, *, net_income=_UNSET):
    result = dict(values)
    result['net_income'] = (_difference(result.get('receipts'), result.get('refunds')) if net_income is _UNSET else net_income)
    result['gross_profit'] = _difference(result['net_income'], result.get('goods_cost'))
    result['promotion_cost'] = _sum(result.get(key) for key in PROMOTION)
    result['aftersales_cost'] = _sum(result.get(key) for key in AFTERSALES)
    result['profit'] = _difference(result['gross_profit'], *[result.get(k) for k in ('promotion_cost','platform_cost','freight_cost','aftersales_cost')])
    result['profit_after_labor'] = _difference(result['profit'], result.get('labor_cost'))
    for key, (numerator, denominator) in RATES.items():
        top, bottom = result.get(numerator), result.get(denominator)
        result[key] = top / bottom if top is not None and bottom is not None and bottom != 0 else None
    return result


def amount_text(value):
    # Serialisation is not a financial rounding boundary. Keep meaningful source
    # decimals, padding subcent values to at least six places for API/CSV users.
    text = format(value, 'f')
    compact = text.rstrip('0').rstrip('.') if '.' in text else text
    exact = Decimal(compact or '0')
    places = max(2, -exact.as_tuple().exponent)
    if places > 2:
        places = max(6, places)
    return format(exact, f'.{places}f')


def public_metrics(values):
    return {key: (None if value is None else format(value.quantize(Decimal('.00000001'), rounding=ROUND_HALF_UP), 'f') if key in RATES else amount_text(value))
            for key, value in values.items()}


def display_rounding(rows, summary):
    differences = {}
    for key in MONEY:
        values = [row.get(key) for row in rows]
        total = summary.get(key)
        if total is None or any(value is None for value in values):
            continue
        shown_rows = sum((value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP) for value in values), Decimal(0))
        shown_total = total.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        difference = shown_total - shown_rows
        if difference:
            differences[key] = {'label':LABELS[key], 'displayed_rows_total':format(shown_rows,'.2f'),
                'displayed_scope_total':format(shown_total,'.2f'), 'difference':format(difference,'.2f')}
    return differences


def comparison(now, before, *, rate_metric=False):
    if now is None or before is None:
        return {'state':'unavailable','delta':None,'rate':None,'direction':'unknown'}
    delta = now - before
    return {'state':'zero_base' if before == 0 else 'negative_base' if before < 0 else 'comparable',
            'delta':format(delta.quantize(Decimal('.00000001'), rounding=ROUND_HALF_UP),'f') if rate_metric else amount_text(delta),
            'rate':format(delta / before, 'f') if before > 0 else None,
            'direction':'up' if delta > 0 else 'down' if delta < 0 else 'flat'}


def spread(total, weights, quantum=Decimal('.01')):
    """Allocate one known amount; a deterministic remainder preserves its total."""
    if total is None or any(value is None or value < 0 for value in weights.values()):
        return None
    denominator = sum(weights.values(), Decimal(0))
    if denominator <= 0:
        return {key:Decimal(0) for key in weights} if total == 0 else None
    result = {key:(total * value / denominator).quantize(quantum, rounding=ROUND_HALF_UP) for key,value in weights.items()}
    recipient = min(weights, key=lambda key: (-weights[key], str(key)))
    result[recipient] += total - sum(result.values(), Decimal(0))
    return result


def capture_projection(projected, context):
    """Capture each projected metric contribution once, before recipient expansion."""
    if projected.is_empty():
        schema={'metric_id':pl.String,'source_id':pl.String,'store':pl.String,'period':pl.String,'link_key':pl.String,
                'spine_row':pl.UInt32,'amount':pl.Float64,'factor':pl.Float64,'product_id':pl.String,'order_id':pl.String,'sub_order_id':pl.String,'operations_contract':pl.String}
        return pl.DataFrame(schema=schema)
    names = ('spine_row','order_id','sub_order_id','product_id','product_name','order_type','order_time','internal_order_id','tracking_no')
    fields = [name for name in names if name in context.columns]
    frame = projected
    if 'spine_row' in frame.columns and 'spine_row' in fields:
        extra = [name for name in fields if name == 'spine_row' or name not in frame.columns]
        frame = frame.join(context.select(extra).unique('spine_row',keep='first'), on='spine_row', how='left', nulls_equal=False, maintain_order='left')
    return frame.with_columns(pl.lit(VERSION).alias('operations_contract'))


def keep_projection(ws, run_id, sl):
    """Persist the optional complete projection in the run's evidence directory."""
    frame = getattr(sl, 'operations_evidence', None)
    if frame is None:
        return None
    from .storage_integrity import seal
    path = ws.facts_path(run_id).with_suffix('.operations.parquet')
    frame.write_parquet(path, row_group_size=100_000)
    seal(path)
    return path


def _components(model):
    nodes = {node.id:node for node in model.statement}
    metric_ids = {metric.id for metric in model.metrics}
    def visit(name, seen=()):
        if name in seen:
            raise OperationsError('经营指标依赖存在循环')
        if name in metric_ids:
            return {name}
        node = nodes.get(name)
        if node is None:
            return set()
        refs = node.children or (node.formula.of if node.formula is not None else ())
        return set().union(*(visit(ref, (*seen,name)) for ref in refs)) if refs else set()
    return {key:set().union(*(visit(node) for node in refs)) for key,refs in NODES.items()}


def store_metrics(statement):
    nodes = {row.get('id'):row for row in statement or []}
    result = {}
    for key, refs in NODES.items():
        values = [decimal(nodes.get(ref,{}).get('value')) if nodes.get(ref,{}).get('available',True) else None for ref in refs]
        value = _sum(values)
        result[key] = -value if value is not None and key != 'receipts' else value
    result['labor_cost'] = None
    result['commission'] = None
    return result


@dataclass
class StoreInput:
    store_id: str
    store: str
    platform: str
    period: str
    run_id: int | None = None
    state: str = 'open'
    stale: bool = False
    input_sha: str = ''
    statement: list = field(default_factory=list)
    projections: list = field(default_factory=list)
    assignments: list = field(default_factory=list)
    evidence_complete: bool = False
    issues: list = field(default_factory=list)
    at: str = ''
    engine: str = ''
    commission_total: object = None
    frozen_labor: object = None
    detail_loaded: bool = True
    income: dict | None = None
    cost_input_scope: str = ''


def public_income(income):
    if income is None:
        return None
    def serialise(value):
        if isinstance(value,Decimal):return amount_text(value)
        if isinstance(value,dict):return {key:serialise(item) for key,item in value.items()}
        if isinstance(value,(tuple,list)):return [serialise(item) for item in value]
        return value
    return serialise({key:value for key,value in income.items() if key not in ('by_spine','detail_proof')})


def _team(person_id, roster):
    visited = set()
    person = roster.get(person_id, {})
    while person.get('parent_id') in roster and person.get('parent_id') not in visited:
        visited.add(person.get('id'))
        person = roster[person['parent_id']]
    return person.get('id',''), person.get('alias') or person.get('name') or '未分团队'


def _ownership(assignments, roster):
    producers = {}
    references = {}
    managed = {bool(row['managed']) for row in assignments if row.get('managed') is not None}
    classification = 'managed' if managed == {True} else 'standard' if managed == {False} else 'unknown'
    for row in assignments:
        pid = row.get('person_id')
        if not pid or row.get('status') != 'distribute':
            continue
        rate = decimal(row.get('share'))
        if rate is None or rate < 0:
            continue
        item = {'person_id':pid, 'person':row.get('person') or roster.get(pid,{}).get('name') or pid,
                'team_id':_team(pid,roster)[0], 'team':_team(pid,roster)[1], 'weight':rate,
                'commission':decimal(row.get('amount'))}
        duty = {'produce':producers, 'cut':references}.get(row.get('duty'))
        if duty is None:
            continue
        if pid in duty:
            duty[pid]['weight'] += rate
            duty[pid]['commission'] = _sum([duty[pid]['commission'], item['commission']])
        else:
            duty[pid] = item
    return producers, list(references.values()), classification


def build_month(inputs, model, period, overhead_total, roster=(), labor_shares=None):
    """Build additive ownership rows and separate non-additive cut references."""
    roster = {row['id']:row for row in roster}
    components = _components(model)
    category = {metric:key for key,metrics in components.items() for metric in metrics}
    if sum(map(len,components.values())) != len(category):
        raise OperationsError('同一来源指标被经营分类重复认领')
    stores = []
    for item in inputs:
        base = store_metrics(item.statement)
        if item.income is not None:
            for name in ('receipts','refunds','net_income'):
                base[name] = item.income.get(name)
        else:
            base['net_income'] = _difference(base.get('receipts'),base.get('refunds'))
        base['commission'] = decimal(item.commission_total)
        stores.append({'input':item,'base':base,'rows':[],'references':[]})
    cuts = ({key:decimal(value) for key,value in labor_shares.items()} if labor_shares is not None
            else spread(decimal(overhead_total), {item['input'].store_id:store_metrics(item['input'].statement)['receipts'] for item in stores}))
    atoms, reference_atoms = [], []
    for store in stores:
        item, base = store['input'], store['base']
        base['labor_cost'] = cuts.get(item.store_id) if cuts is not None else None
        if item.state=='closed' and decimal(item.frozen_labor) is not None:
            base['labor_cost']=decimal(item.frozen_labor)
        aggregate_cost_only=item.cost_input_scope=='approved_store_period_total'
        by_order = {}
        for projection in item.projections:
            key = projection.get('spine_row')
            if key is None:
                key = 'unallocated'
            row = by_order.setdefault(key, {'spine_row':None if key == 'unallocated' else key,
                'product_id':str(projection.get('product_id') or ''),'product_name':projection.get('product_name') or '',
                'order_id':str(projection.get('order_id') or ''),'sub_order_id':str(projection.get('sub_order_id') or ''),
                'link_keys':set(), 'values':{name:(Decimal(0) if item.evidence_complete and base[name] is not None and not (aggregate_cost_only and name=='goods_cost') else None) for name in ROW_PRIMITIVES}})
            row['link_keys'].update(projection.get('link_keys') or [str(projection.get('link_key') or '')])
            if 'values' in projection:
                for name,value in projection['values'].items():
                    amount=decimal(value)
                    if name in NODES and amount is not None and base[name] is not None and not (aggregate_cost_only and name=='goods_cost'):
                        row['values'][name]=(row['values'][name] or Decimal(0))+amount
                continue
            name = category.get(projection.get('metric_id'))
            value = decimal(projection.get('amount'))
            if aggregate_cost_only and name=='goods_cost':continue
            if name and value is not None:
                if row['values'][name] is None and base[name] is not None:
                    row['values'][name] = Decimal(0)
                if row['values'][name] is not None:
                    row['values'][name] += value if name == 'receipts' else -value
        if not by_order:
            by_order['unallocated'] = {'spine_row':None,'product_id':'','product_name':'','order_id':'','sub_order_id':'','link_keys':set(),
                'values':{name:Decimal(0) if item.evidence_complete and base[name] is not None and not (aggregate_cost_only and name=='goods_cost') else None for name in ROW_PRIMITIVES}}
        for row in by_order.values():
            if item.income is not None:
                basis = item.income.get('by_spine',{}).get(row['spine_row'],{})
                for name in ('receipts','refunds','net_income'):
                    row['values'][name] = basis.get(name)
            else:
                row['values']['net_income'] = _difference(row['values'].get('receipts'),row['values'].get('refunds'))
        # Store-only amounts remain a visible residual. They are never spread to products.
        residual = {name:_difference(base[name], _sum(row['values'][name] for row in by_order.values())) for name in ROW_PRIMITIVES}
        if item.evidence_complete and any(value not in (None,Decimal(0)) for value in residual.values()):
            row = by_order.setdefault('unallocated', {'spine_row':None,'product_id':'','product_name':'','order_id':'','sub_order_id':'','link_keys':set(),'values':{name:Decimal(0) if base[name] is not None else None for name in ROW_PRIMITIVES}})
            for name,value in residual.items():
                if value is not None and row['values'][name] is not None:
                    row['values'][name] += value
        assignments = defaultdict(list)
        for row in item.assignments:
            assignments[row.get('spine_row')].append(row)
        for row in by_order.values():
            owners, refs, classification = _ownership(assignments[row['spine_row']], roster) if row['spine_row'] is not None else ({},[],'unknown')
            common = {'store_id':item.store_id,'store':item.store,'platform':item.platform,'period':period,'run_id':item.run_id,
                'product_id':row['product_id'],'product_key':f'{item.store_id}:{row["product_id"]}' if row['product_id'] else '',
                'product_name':row['product_name'],'order_id':row['order_id'],'sub_order_id':row['sub_order_id'],
                'spine_row':row['spine_row'],'link_keys':sorted(row['link_keys']),'managed':classification,'cost_input_scope':item.cost_input_scope}
            weights = {pid:owner['weight'] for pid,owner in owners.items()}
            splits = {name:spread(value, weights, Decimal('.000001')) for name,value in row['values'].items()}
            if not weights or sum(weights.values(), Decimal(0)) <= 0:
                atoms.append({**common,'person_id':'','person':'未认领业绩','team_id':'','team':'未认领',
                              'values':{**row['values'],'labor_cost':None,'commission':None},'unclaimed':True})
            else:
                for pid,owner in owners.items():
                    values = {name:split.get(pid) if split is not None else None for name,split in splits.items()}
                    atoms.append({**common,**{k:v for k,v in owner.items() if k != 'weight'},
                                  'values':{**values,'labor_cost':None,'commission':None if aggregate_cost_only else owner['commission']},'unclaimed':False})
            # A cut sees the order's complete (produce + cut) basis it was paid on; never additive.
            for ref in refs:
                reference_atoms.append({**common,**{k:v for k,v in ref.items() if k != 'weight'},
                                        'values':{**row['values'],'labor_cost':None,'commission':None if aggregate_cost_only else ref['commission']},'reference_only':True})
        owned = [row for row in atoms if row['store_id'] == item.store_id and not row['unclaimed']]
        labor_weights = {index:row['values']['receipts'] for index,row in enumerate(owned)}
        labor = spread(base['labor_cost'],labor_weights)
        for index,row in enumerate(owned):
            row['values']['labor_cost'] = labor.get(index) if labor is not None else None
        # Unclaimed business is visible but does not take a share from the claimed pool.
        for row in atoms:
            if row['store_id'] == item.store_id and row['unclaimed']:
                row['values']['labor_cost'] = Decimal(0) if labor is not None else None
        order_labor=defaultdict(list)
        for row in atoms:
            if row['store_id']==item.store_id:
                order_labor[row['spine_row']].append(row['values']['labor_cost'])
        for row in reference_atoms:
            if row['store_id']==item.store_id:
                row['values']['labor_cost']=_sum(order_labor[row['spine_row']])
        store['metrics'] = derive(base,net_income=base['net_income'])
        store['labor_unallocated'] = base['labor_cost'] if labor is None else Decimal(0)
    return {'period':period,'stores':stores,'atoms':atoms,'references':reference_atoms,
            'organization_mode':'current_revision','version':VERSION}


def verified_run_path(ws, run_id, suffix='.parquet'):
    """Read existing paths only; an M3 artifact keeps its first recorded SHA."""
    from .run_artifacts import path as artifact_path, verified_path
    source=artifact_path(ws.root,run_id).with_suffix(suffix)
    row=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(run_id,)).fetchone()
    expected=None
    if row:
        try:
            manifest=json.loads(row['manifest_json']);artifacts=manifest.get('artifacts')
            if not isinstance(artifacts,list) or any(not isinstance(item,dict) for item in artifacts):return None
            matches=[item.get('sha') for item in artifacts if item.get('path')==source.relative_to(ws.root).as_posix()]
            if len(matches)!=1 or not isinstance(matches[0],str) or not re.fullmatch('[0-9a-f]{64}',matches[0]):return None
            expected=matches[0]
        except (ValueError,TypeError,AttributeError):return None
    return verified_path(ws.root,run_id,suffix,expected_sha=expected)


def _read_parquet(path, *, require_seal=True):
    from .storage_integrity import verified
    if not path.is_file() or (require_seal and not verified(path)):
        return None
    return pl.read_parquet(path).to_dicts()


def _read_projection(path, model):
    """Reduce metric×order evidence in Polars before entering Python objects."""
    from .storage_integrity import verified
    if not path.is_file() or not verified(path):
        return None
    source=pl.scan_parquet(path)
    schema=source.collect_schema()
    if not {'spine_row','metric_id','amount'}<=set(schema.names()):
        return None
    components=_components(model)
    expressions=[]
    for key,metrics in components.items():
        condition=pl.col('metric_id').is_in(sorted(metrics))
        expressions.append(pl.when(condition.any()).then(pl.col('amount').cast(pl.String).cast(pl.Decimal(38,18)).filter(condition).sum()*(1 if key=='receipts' else -1)).otherwise(None).alias(key))
    context=[name for name in ('product_id','product_name','order_id','sub_order_id') if name in schema]
    grouped=source.group_by('spine_row').agg(*[pl.col(name).first() for name in context],
        *([pl.col('link_key').drop_nulls().unique().alias('link_keys')] if 'link_key' in schema else []),*expressions).collect()
    return [{**{key:value for key,value in row.items() if key not in NODES},'values':{key:row[key] for key in NODES}} for row in grouped.to_dicts()]


def load_inputs(ws, model, period, *, details=True):
    states = (ws.overview_summaries(period=period) if hasattr(ws,'overview_summaries') else [st for st in ws.overview() if st.period == period])
    by_store = {st.store_id:st for st in states}
    registry_path = ws.root / 'commission' / 'registry.db'
    roster, revision, calculations = [], None, {}
    if registry_path.is_file():
        with closing(sqlite3.connect(registry_path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN')
            roster = [dict(row) for row in conn.execute('SELECT * FROM person')]
            revision = conn.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
            ids = sorted({(st.result or {}).get('commission',{}).get('calculation_id') for st in states} - {None,''})
            if ids:
                marks = ','.join('?' for _ in ids)
                calculations = {row['id']:dict(row) for row in conn.execute(f'SELECT id,finance_run,path,sha FROM calculation WHERE id IN ({marks})',ids)}
    inputs = []
    from .automation_checker import operating_stores
    for store in operating_stores(ws,model,period,include_excluded=True):
        st = by_store.get(store.id)
        payload = (st.result or {}) if st else {}
        run_id = getattr(st,'run_id',None)
        issues = []
        projections, assignments, complete = [], [], False
        proof_sha = ''
        assignments_verified=False
        if run_id:
            path = verified_run_path(ws,run_id,'.operations.parquet')
            from .storage_integrity import verified
            saved = (_read_projection(path,model) if details else []) if path is not None else None
            complete = saved is not None
            if complete:
                proof_sha = path.with_name(path.name+'.sha256').read_text(encoding='ascii').strip()
            projections = saved or []
            if saved is None:
                fallback=verified_run_path(ws,run_id,'.allocation.parquet') if details else None
                projections = (_read_projection(fallback,model) or []) if fallback is not None else []
                issues.append('旧核算未留存完整经营分项投影；缺少的商品与个人分项不作均摊')
            calc = calculations.get((payload.get('commission') or {}).get('calculation_id'))
            if calc and calc['finance_run'] == run_id:
                target = ws.root / 'commission' / 'calculations' / Path(calc['path']).name
                if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == calc['sha']:
                    assignments_verified=True
                    if details:
                        columns=[name for name in ('spine_row','product_id','person_id','person','share','total_rate','duty','managed','status','amount') if name in pl.read_parquet_schema(target)]
                        assignments = pl.read_parquet(target,columns=columns).to_dicts()
                else:
                    issues.append('人员归属证据不完整，保留为未认领业绩')
            if details and projections and not assignments:
                issues.append('没有可验证的本次做货/抽点归属证据')
        else:
            issues.append('本月尚无保存的核算结果')
        from .operations_income import load as income_load
        income = income_load(ws,run_id,payload,store.platform,details=details) if run_id else None
        if income is not None and income['status']!='split_verified':
            issues.append('1688未扣退款收入与销售退款尚无可闭合原件依据，退款率与个人兼职权重保持未知')
        elif income is not None and details and income.get('detail_proof',{}).get('reason_codes'):
            issues.append('1688订单分项与原收退款依据未能闭合，未证明的分项及个人兼职权重保持未知')
        inputs.append(StoreInput(store.id,store.name,store.platform,period,run_id,
            getattr(st,'state','open'),getattr(st,'stale',False),digest([run_id,payload,proof_sha,(calc or {}).get('sha') if run_id else '',assignments_verified,complete,public_income(income)]),payload.get('statement') or [],
            projections,assignments,complete,issues,getattr(st,'at',''),getattr(st,'engine',''),
            (payload.get('commission') or {}).get('total') if (payload.get('commission') or {}).get('amount_complete',False) else None,
            getattr(st,'labor_cut',None) if st and st.state=='closed' else None,details,income,
            'approved_store_period_total' if (payload.get('period_cost_input') or {}).get('status')=='applied' else ''))
    return inputs, roster, revision


def select_report(month, *, level='store', platform='', store_id='', team_id='', person_id='', product_id='', managed='all'):
    if level not in {'store','team','person','product','order'} or managed not in {'all','managed','standard'}:
        raise OperationsError('不支持的经营分析维度')
    scope = dict(period=month['period'],level=level,platform=platform,store_id=store_id,team_id=team_id,person_id=person_id,product_id=product_id,managed=managed)
    def match(row):
        return all(not value or row.get(key) == value for key,value in (('platform',platform),('store_id',store_id),('team_id',team_id),('person_id',person_id),('product_id',product_id))) and (managed == 'all' or row['managed'] == managed)
    atoms = [row for row in month['atoms'] if match(row)]
    refs = [row for row in month['references'] if match(row)]
    detail_loaded=all(row['input'].detail_loaded for row in month['stores'])
    def grouped(items, per_person=False):
        groups = defaultdict(list)
        for atom in items:
            key = {'store':atom['store_id'],'team':atom['team_id'] or '__unclaimed__','person':atom['person_id'] or '__unclaimed__',
                   'product':atom['product_key'] or f'{atom["store_id"]}:__unallocated__',
                   'order':f'{atom["store_id"]}:{atom["order_id"]}:{atom["sub_order_id"]}:{atom["spine_row"]}'}[level]
            groups[f'{key}|{atom["person_id"]}' if per_person and level != 'person' else key].append(atom)
        return groups
    def total(group):
        values = {name:_sum(row['values'].get(name) for row in group) for name in (*PRIMITIVES,'commission')}
        return derive(values,net_income=values['net_income'])
    def managed_part(group):
        # Unknown classification keeps the managed share unknown; it is never read as non-managed.
        if not detail_loaded or any(row['managed']=='unknown' and any(row['values'].get(key)!=0 for key in ROW_PRIMITIVES) for row in group):
            return None
        selected=[row for row in group if row['managed']=='managed']
        if not selected:
            return derive({name:Decimal(0) for name in (*PRIMITIVES,'commission')},net_income=Decimal(0))
        return total(selected)
    def describe(key, group, metrics):
        head = group[0]
        name = {'store':head['store'],'team':head['team'],'person':head['person'],
                'product':head['product_name'] or head['product_id'] or '待归属商品',
                'order':head['sub_order_id'] or head['order_id'] or '待归属金额'}[level]
        part = managed_part(group)
        return {'id':key,'name':name,'store_id':head['store_id'] if len({row['store_id'] for row in group}) == 1 else '',
            'store':head['store'] if len({row['store_id'] for row in group}) == 1 else '',
            'platform':head['platform'] if len({row['platform'] for row in group}) == 1 else '',
            'person_id':head['person_id'] if level == 'person' else '', 'team_id':head['team_id'] if level in {'person','team'} else '',
            'team':head['team'] if level in {'person','team'} else '',
            'product_id':head['product_id'] if level in {'product','order'} else '',
            'order_id':head['order_id'] if level=='order' else '', 'sub_order_id':head['sub_order_id'] if level=='order' else '',
            'order_count':len({(row['store_id'],row['order_id'],row['sub_order_id']) for row in group if row['order_id'] or row['sub_order_id']}),
            'metrics':public_metrics(metrics),'managed_metrics':None if part is None else public_metrics(part),
            'run_ids':sorted({row['run_id'] for row in group if row['run_id']}),
            'managed':sorted({row['managed'] for row in group}),'cost_input_scope':head.get('cost_input_scope','')}
    # Store summary remains authoritative even if historical order detail is absent.
    store_summary = level == 'store' and not any((team_id,person_id,product_id)) and managed == 'all'
    rows, raw_metrics = [], []
    for key,group in grouped(atoms).items():
        metrics = total(group)
        if store_summary:
            source = next(row for row in month['stores'] if row['input'].store_id == key)
            metrics = source['metrics']
        raw_metrics.append(metrics)
        row = describe(key,group,metrics)
        if store_summary and not source['input'].detail_loaded:
            row['order_count'] = None
        row['unclaimed'] = False if store_summary else all(item['unclaimed'] for item in group)
        rows.append(row)
    summary_values = {name:_sum(metrics.get(name) for metrics in raw_metrics) for name in (*PRIMITIVES,'commission')}
    summary = derive(summary_values,net_income=summary_values['net_income'])
    rows.sort(key=lambda row:(row['metrics']['profit_after_labor'] is None,-(decimal(row['metrics']['profit_after_labor']) or Decimal(0)),row['id']))
    for index,row in enumerate(rows):
        row['rank'] = index + 1 if not row['unclaimed'] and row['metrics']['profit_after_labor'] is not None else None
    # Each cut sees a complete order basis, so cut groups are never merged across people.
    reference_groups = [{**describe(key,group,total(group)),'person_id':group[0]['person_id'],'person':group[0]['person'],
                         'team_id':group[0]['team_id'],'team':group[0]['team'],'duty':'cut','reference_only':True}
                        for key,group in grouped(refs,per_person=True).items()]
    reference_groups.sort(key=lambda row:(row['metrics']['net_income'] is None,-(decimal(row['metrics']['net_income']) or Decimal(0)),row['id']))
    managed_summary = managed_part(atoms)
    all_runs = [{'store_id':row['input'].store_id,'run_id':row['input'].run_id,'input_sha':row['input'].input_sha,'state':row['input'].state,
             'stale':row['input'].stale,'at':row['input'].at,'engine':row['input'].engine,'evidence_complete':row['input'].evidence_complete,'cost_input_scope':row['input'].cost_input_scope}
            for row in month['stores']]
    runs = [row for row in all_runs if not store_id or row['store_id'] == store_id]
    input_sha = digest([VERSION,month['period'],all_runs,month.get('organization_revision'),month.get('model_sha'),month.get('overhead_total')])
    attribution_pending=any(row['unclaimed'] and any(row['values'].get(key) not in (None,Decimal(0)) for key in ROW_PRIMITIVES) for row in atoms) if detail_loaded else None
    return {'version':VERSION,'period':month['period'],'scope':scope,'input_sha':input_sha,'scope_sha':digest([input_sha,scope]),
        'run_refs':runs,'is_final':bool(runs) and all(row['state']=='closed' and not row['stale'] and row['evidence_complete'] for row in runs) and all(summary.get(key) is not None for key in PRIMITIVES) and not any(row['input'].issues for row in month['stores']) and (store_summary or not attribution_pending),
        'details_loaded':detail_loaded,'attribution_pending':attribution_pending,
        'organization_mode':month['organization_mode'],'organization_revision':month.get('organization_revision'),
        'metrics':[{'key':key,'label':LABELS[key],'kind':'rate' if key in RATES else 'money',**({'component_of':COMPONENTS[key]} if key in COMPONENTS else {})} for key in DISPLAY],
        'income_evidence':{row['input'].store_id:public_income(row['input'].income) for row in month['stores'] if row['input'].income is not None and (not store_id or row['input'].store_id==store_id)},
        'amount_precision':{'money':'source_decimal','subcent_minimum_fraction_digits':6,'display_fraction_digits':2,'aggregation':'before_display_rounding'},
        'display_rounding':display_rounding(raw_metrics,summary),
        'summary':public_metrics(summary),'managed_summary':None if managed_summary is None else public_metrics(managed_summary),
        'rows':rows,'reference_groups':reference_groups,'reference_rows':[{**{k:v for k,v in row.items() if k not in ('values','link_keys') and not k.startswith('_')},'metrics':public_metrics(derive(row['values'],net_income=row['values'].get('net_income')))} for row in refs],
        'issues':sorted({issue for row in month['stores'] for issue in row['input'].issues} | ({'店月成本已批准，商品与人员尚无完整成本分配依据，明细利润保持待补齐'} if not store_summary and any(row['input'].cost_input_scope=='approved_store_period_total' for row in month['stores']) else set())),
        'unclaimed_count':sum(row['unclaimed'] for row in atoms) if detail_loaded else None,
        'classification_pending_count':sum(row['managed']=='unknown' for row in month['atoms']) if detail_loaded else None,
        'labor_unallocated':public_metrics({'labor_cost':_sum(row['labor_unallocated'] for row in month['stores'])})['labor_cost']}


def read_month(ws, model, period, *, require_detail=False, **scope):
    check_period(period)
    policy_total, excluded, shares, policy_sha, policy_issues = model.overhead(period), set(), None, '', []
    tables = {row[0] for row in ws.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'automation_decision' in tables:
        from .automation_store import active_decisions
        from .automation_checker import company_basis
        decisions = active_decisions(ws.conn,period)
        excluded = {store for (store,kind),row in decisions.items() if kind=='store_scope' and row['values'].get('included') is False}
        company = company_basis(ws,model,period)
        policy_total, shares, policy_sha = company['total'], company['shares'], company['input_sha']
        policy_issues = [issue['message'] for issue in company['issues']]
        frozen = ws.conn.execute('SELECT shares_json,total,input_sha FROM automation_overhead_close WHERE period=? ORDER BY rowid DESC LIMIT 1',(period,)).fetchone()
        if frozen:
            shares, policy_total = json.loads(frozen['shares_json']), frozen['total']
            policy_sha = digest([policy_sha,dict(frozen)])
    states = (ws.overview_summaries(period=period) if hasattr(ws,'overview_summaries') else [st for st in ws.overview() if st.period==period])
    descriptor=[]
    for state in states:
        artifacts=[]
        if state.run_id:
            for suffix in ('.parquet','.parquet.sha256','.operations.parquet','.allocation.parquet','.operations.parquet.sha256','.allocation.parquet.sha256'):
                from .run_artifacts import path as artifact_path
                path=artifact_path(ws.root,state.run_id).with_suffix(suffix)
                if path.is_file():
                    stat=path.stat()
                    artifacts.append([suffix,stat.st_size,stat.st_mtime_ns])
        descriptor.append([state.store_id,state.run_id,state.state,state.stale,digest(state.result),artifacts])
    registry_path=ws.root/'commission'/'registry.db'
    organization_revision=None
    if registry_path.is_file():
        with closing(sqlite3.connect(registry_path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            organization_revision=conn.execute('SELECT revision FROM meta WHERE id=1').fetchone()[0]
            calc_ids=sorted({(state.result or {}).get('commission',{}).get('calculation_id') for state in states}-{None,''})
            if calc_ids:
                marks=','.join('?' for _ in calc_ids)
                for cid,relative,sha in conn.execute(f'SELECT id,path,sha FROM calculation WHERE id IN ({marks})',calc_ids):
                    target=registry_path.parent/'calculations'/Path(relative).name
                    stat=target.stat() if target.is_file() else None
                    descriptor.append(['calculation',cid,sha,None if stat is None else [stat.st_size,stat.st_mtime_ns]])
    signature=digest([VERSION,model.model_dump_json(),descriptor,organization_revision,policy_sha])
    details=require_detail or not (scope.get('level','store')=='store' and not any(scope.get(name) for name in ('team_id','person_id','product_id')) and scope.get('managed','all')=='all')
    key=(str(ws.root.resolve()),period,signature,details)
    with _cache_lock:
        cached=_cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
    if cached is not None:
        return select_report(cached,**scope)
    inputs, roster, revision = load_inputs(ws,model,period,details=details)
    inputs=[item for item in inputs if item.store_id not in excluded]
    month = build_month(inputs,model,period,policy_total,roster,shares)
    for item in inputs:
        item.issues.extend(policy_issues)
    month['organization_revision'] = revision
    month['overhead_total'] = policy_total
    month['model_sha'] = digest([model.model_dump_json(),policy_sha])
    for item in inputs:
        item.projections=[]
        item.assignments=[]
        if item.income is not None:
            item.income={name:value for name,value in item.income.items() if name not in ('by_spine','detail_proof')}
    with _cache_lock:
        _cache[key]=month
        while len(_cache)>6:
            _cache.popitem(last=False)
    return select_report(month,**scope)
