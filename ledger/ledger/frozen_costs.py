"""Read projection of frozen cost decisions; never modify source evidence.

The only authority for a historical adjustment is manual_finance for the exact
run. Live cost_line_log entries must not leak into a previous closing/export.
"""
import json
from decimal import Decimal
from pathlib import Path

import polars as pl

from .engine.project import claims
from .money import decimal_amount, money_float
from .workspace import WorkspaceError

COST_METRICS = {'goods_cost','goods_return_cost','dropship_cost','reshipment_cost'}


def for_run(ws, run_id, facts, model, *, metrics=None, required=False):
    row=ws.conn.execute('SELECT decision_json,result_json,at,by,store_id,period FROM manual_finance WHERE run_id=?',(run_id,)).fetchone()
    if row is None:
        if required:raise WorkspaceError('找不到对应结账的人工成本留痕，不能声称导出已包含补录')
        return facts
    decision=json.loads(row['decision_json'])
    result=json.loads(row['result_json'])
    if not result.get('manual_cost'):
        if required:raise WorkspaceError('对应结账记录没有人工成本确认依据')
        return facts  # Payout-only confirmations do not create cost adjustments.
    if decision.get('source_run_id')!=run_id or result['manual_cost'].get('source_run_id')!=run_id:
        raise WorkspaceError('人工成本确认记录与核算版本不一致，不能导出混合版本')
    if metrics is not None and not COST_METRICS.intersection(metrics):
        return facts
    if isinstance(facts,(str,Path)):
        lazy=pl.scan_parquet(facts)
        if metrics is not None:
            lazy=lazy.filter(pl.col('metric_id').is_in(metrics))
        facts=lazy.collect()
    return append(facts,model,decision,result['manual_cost'],run_id=run_id,
                  at=row['at'],by=row['by'],store=row['store_id'],period=row['period'],metrics=metrics)


def append(facts,model,decision,manual,*,run_id,at='',by='',store='',period='',metrics=None):
    try:
        keys=('goods','dropship','reshipment')
        confirmed={k:decimal_amount(decision['confirmed'][k]) for k in keys}
        if confirmed!={k:decimal_amount(manual['confirmed'][k]) for k in keys}:
            raise ValueError('确认金额不一致')
        if any(v!=v.quantize(Decimal('.01')) for v in confirmed.values()):
            raise ValueError('确认金额不是两位小数')
    except (KeyError,TypeError,ValueError) as exc:
        raise WorkspaceError('冻结人工成本快照不完整，不能生成最终成本明细') from exc
    target={'goods_cost':-confirmed['goods'],'goods_return_cost':Decimal(0),
            'dropship_cost':-confirmed['dropship'],'reshipment_cost':-confirmed['reshipment']}
    if metrics is not None:
        target={k:v for k,v in target.items() if k in metrics}
    if not facts.is_empty() and not {'counted','contribution','metric_id'}<=set(facts.columns):
        raise WorkspaceError('原始明细缺少进账依据，不能推算人工成本差额')
    definitions={m.id:m for m in model.metrics}
    current={}
    for mid in target:
        part=facts.filter(claims(definitions[mid]) if mid in definitions and 'major' in facts.columns else pl.col('metric_id')==mid) if 'metric_id' in facts.columns else facts
        current[mid]=sum((decimal_amount(v) for v in part.filter(pl.col('counted')).get_column('contribution').drop_nulls()),Decimal(0)) if not part.is_empty() else Decimal(0)
    observed=decision.get('observed') or manual.get('observed') or {}
    for name,parts in [('goods',{'goods_cost','goods_return_cost'}),('dropship',{'dropship_cost'}),('reshipment',{'reshipment_cost'})]:
        if name in observed and parts<=current.keys():
            if money_float(-sum((current[k] for k in parts),Decimal(0)))!=money_float(observed[name]):
                raise WorkspaceError('原始成本明细与结账时记录的原始成本不一致，不能用调整行掩盖差异')
    extra=[]
    def add(mid,amount,kind,*,key='',order='',revision=None,note=''):
        metric=definitions.get(mid)
        extra.append({'metric_id':mid,'source_id':'__frozen_manual_cost__','store':store,'period':period,
            'link_key':key or None,'order_id':order or None,'subject':kind,'minor':kind,
            'major':metric.major if metric else None,'linked':bool(key),'count_without_order':not bool(key),
            'counted':True,'amount':float(amount),'contribution':float(amount),
            'file_name':'结账人工成本（冻结快照）','sheet':'结账核算 '+str(run_id),
            'row_no':revision,'source_note':note,'record_type':kind,
            'closing_run_id':str(run_id),'closing_at':at,'closing_by':by})
        current[mid]+=amount
    # Each frozen line is a supplement, not a replacement of a raw order row.
    if 'goods_cost' in target:
        seen=set()
        for line in decision.get('line_reviews') or []:
            key=str(line.get('coverage_key') or '')
            if not key or key in seen:
                raise WorkspaceError('冻结补录明细缺少唯一订单键，不能重复计入')
            seen.add(key)
            try:
                amount=decimal_amount(line['amount'])
                if amount!=amount.quantize(Decimal('.01')):raise ValueError('补录金额精度无效')
            except (KeyError,TypeError,ValueError) as exc:
                raise WorkspaceError('冻结补录金额无效，不能导出') from exc
            add('goods_cost',-amount,'结账人工补录',key=key,order=str(line.get('order_id') or ''),
                revision=line.get('revision'),note=f"冻结补录版本 {line.get('revision','')}；依据：{line.get('reason','')}；不读取后续修改")
    for mid,value in target.items():
        delta=value-current[mid]
        if abs(delta)<Decimal('0.0000000001'):
            continue
        rounding=abs(delta)<Decimal('.005')
        add(mid,delta,'结账舍入调整' if rounding else '结账总额调整',
            note='原始分摊精度与结账两位小数的舍入差' if rounding else '冻结确认总额与原始进账及逐单补录的差额；确认依据：'+str(decision.get('reason','')))
    facts=facts.with_columns(pl.lit('原始流水').alias('record_type'),pl.lit(str(run_id)).alias('closing_run_id'),
                             pl.lit(at).alias('closing_at'),pl.lit(by).alias('closing_by'))
    if extra:
        facts=pl.concat([facts,pl.DataFrame(extra,infer_schema_length=None)],how='diagonal_relaxed')
    return facts
