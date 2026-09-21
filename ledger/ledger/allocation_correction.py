"""Auditable reallocation of verified historical master-order groups.

Only frozen income/platform-fee rows with provable old equal allocation are
redistributed. Source cash, original cost rows, and manual payouts are not
recomputed from the live cost feed. Unverifiable groups remain explicitly listed.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import shutil
import uuid
from pathlib import Path

import polars as pl

from .engine.allocation import prepare, ORIGIN
from .engine.project import claims
from .commission_engine import allocated_outputs,managed_sales
from .money import money_float
from .workspace import WorkspaceError


def preview(model, source, details, original_spine, current_spine, summary):
    metric=model.metric('trade_receipt').for_platform('taobao')
    required={'order_id','sub_order_id','product_id'}
    if not required<=set(original_spine.columns) or not required<=set(details.columns):
        raise WorkspaceError('历史分配更正缺少主单、子单或商品身份')
    if any(c in original_spine.columns and original_spine[c].drop_nulls().len() for c in ('buyer_paid','alloc_ratio')):
        raise WorkspaceError('原档案已有金额或比例，不能套用缺失依据均摊的更正程序')
    old_rows=details.filter(pl.col('spine_row').is_not_null()).unique(subset=['spine_row'])
    original={}
    for row in original_spine.iter_rows(named=True):
        original.setdefault(row['order_id'],[]).append(row)
    current=current_spine
    if ORIGIN in current.columns:current=current.filter(pl.col(ORIGIN)=='order_detail_file')
    current=prepare(current.with_columns(pl.col('order_id').alias('link_key')),metric)
    new_groups={}
    for row in current.iter_rows(named=True):new_groups.setdefault(row['order_id'],[]).append(row)
    archive={}
    for row in old_rows.iter_rows(named=True):archive.setdefault(row['order_id'],[]).append(row)
    metrics=[m.for_platform('taobao') for m in model.metrics]
    metrics=[m for m in metrics if m is not None and m.allocate and m.allocate.mode=='ratio'
             and m.link and m.link.prefer_exported_orders and m.link.grain=='order']
    controls={}
    for m in metrics:
        rows=source.filter(claims(m) & pl.col('counted'))
        controls[m.id]={r['link_key']:Decimal(str(r['amount'])) for r in rows.group_by('link_key').agg(pl.col('amount').sum()).to_dicts()}
    deltas={};proof=[];pending=[]
    for master,old in original.items():
        control=controls.get('trade_receipt',{}).get(master,Decimal(0))
        fresh=new_groups.get(master,[]);saved=archive.get(master,[])
        old_keys=[(r['sub_order_id'],r['product_id']) for r in old]
        fresh_map={(r['sub_order_id'],r['product_id']):r for r in fresh}
        saved_map={(r['sub_order_id'],r['product_id']):r for r in saved}
        why=''
        if len(set(old_keys))!=len(old_keys) or len(fresh_map)!=len(fresh) or len(saved_map)!=len(saved):why='duplicate_identity'
        elif set(old_keys)!=set(fresh_map) or not set(old_keys)<=set(saved_map):why='changed_membership'
        elif any(r['__allocation_reason'] for r in fresh):why=next(r['__allocation_reason'] for r in fresh if r['__allocation_reason'])
        elif any(abs(Decimal(str(saved_map[k].get('participation_sales') or 0))-(control/len(old)).quantize(Decimal('.000001'),rounding=ROUND_HALF_UP))>Decimal('.000002') for k in old_keys):why='old_allocation_not_reproduced'
        if why:
            if any(values.get(master,Decimal(0)) for values in controls.values()):
                pending.append({'order_id':master,'sales':money_float(control),'reason':why})
            continue
        for mid,totals in controls.items():
            amount=totals.get(master,Decimal(0))
            if not amount:continue
            old_amount=(amount/len(old)).quantize(Decimal('.000001'),rounding=ROUND_HALF_UP)
            for key in old_keys:
                row=saved_map[key];factor=Decimal(str(fresh_map[key]['__allocation_factor']))
                new_amount=(amount*factor).quantize(Decimal('.000001'),rounding=ROUND_HALF_UP)
                deltas.setdefault(row['spine_row'],{})[mid]=new_amount-old_amount
        if control:
            proof.extend({'order_id':master,'sub_order_id':key[0],'product_id':key[1],
                'spine_row':saved_map[key]['spine_row'],
                'source_amount':str(control),'old_factor':str(Decimal(1)/len(old)),
                'new_factor':str(fresh_map[key]['__allocation_factor']),
                'buyer_paid':fresh_map[key].get('buyer_paid'),'refund_amount':fresh_map[key].get('refund_amount'),
                'basis':fresh_map[key].get('allocation_basis_source','original_source')} for key in old_keys)
    sales_metrics={'trade_receipt'}
    gross_node=next(n.id for n in model.statement if n.name=='毛利' and n.is_total)
    profit_node=next(n.id for n in model.statement if n.headline=='profit' and n.is_total)
    gross_metrics=set(model.commission_base_metrics(gross_node))
    profit_metrics=set(model.commission_base_metrics(profit_node))
    base_metrics=set(model.commission_base_metrics(summary['base_node']))
    columns={k:[] for k in ('participation_sales','participation_gross','participation_profit','original_base','base','amount')}
    for row in details.iter_rows(named=True):
        changes=deltas.get(row['spine_row'],{})
        def changed(field,ids):
            return None if row.get(field) is None else Decimal(str(row[field]))+sum((v for mid,v in changes.items() if mid in ids),Decimal(0))
        columns['participation_sales'].append(changed('participation_sales',sales_metrics))
        columns['participation_gross'].append(changed('participation_gross',gross_metrics))
        columns['participation_profit'].append(changed('participation_profit',profit_metrics))
        base=changed('original_base',base_metrics)
        columns['original_base'].append(base)
        base=max(base,Decimal(0)) if base is not None and summary.get('on_loss')=='skip' else base
        columns['base'].append(base)
        columns['amount'].append((base*Decimal(str(row['share']))).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
                                 if row.get('status')=='distribute' and base is not None else row.get('amount'))
    updated=details.with_columns(*[pl.Series(k,v).cast(details.schema[k]) for k,v in columns.items() if k in details.columns])
    rebuilt=deepcopy(summary)
    paid=updated.filter(pl.col('status')=='distribute')
    split=allocated_outputs(paid,production=False);production=allocated_outputs(paid,production=True)
    for person in rebuilt.get('people',[]):
        part=paid.filter(pl.col('person_id')==person['person_id'])
        person['amount']=money_float(part['amount'].sum() or 0)
        person['base']=money_float(part['base'].sum() or 0)
        for field,col in [('sales','participation_sales'),('gross','participation_gross'),('profit','participation_profit')]:
            if person.get(field) is not None:person[field]=money_float(part[col].sum() or 0)
            person['allocated_'+field]=(split or {}).get(person['person_id'],{}).get(field)
    for product in rebuilt.get('products',[]):
        part=paid.filter(pl.col('product_id')==product['product_id'])
        for person in product.get('people',[]):person['amount']=money_float(part.filter(pl.col('person_id')==person['person_id'])['amount'].sum() or 0)
        product['amount']=money_float(part['amount'].sum() or 0)
        product['base']=money_float(updated.filter(pl.col('product_id')==product['product_id']).unique(subset=['spine_row'])['base'].sum() or 0)
    rebuilt['total']=money_float(paid['amount'].sum() or 0)
    rebuilt['base_total']=money_float(updated.unique(subset=['spine_row'])['original_base'].sum() or 0)
    rebuilt['production_outputs']=production
    if 'managed' in updated.columns:
        rebuilt['managed_sales']=managed_sales(updated)
    delta_totals={mid:sum((v.get(mid,Decimal(0)) for v in deltas.values()),Decimal(0)) for mid in controls}
    if any(money_float(value)!=0 for value in delta_totals.values()):
        raise WorkspaceError('历史更正未通过主订单金额守恒检查，不能发布')
    audit={'kind':'verified_legacy_ratio_correction','verified_orders':len({p['order_id'] for p in proof}),
           'pending_orders':pending,'evidence':proof,'rounding_deltas':{k:str(v) for k,v in delta_totals.items()},
           'untouched_metrics':[m.id for m in model.metrics if m.id not in controls]}
    return rebuilt,updated,audit


def publish(ws,registry,old_run_id,summary,details,audit,*,expected_calculation_sha,by,reason):
    """Append a corrected allocation, preserving every store statement amount.

    A separately confirmed payout/exclusion needs an explicit audited migration;
    this narrowly scoped operation refuses it rather than losing that decision.
    """
    from .finance_guard import guard
    from .commission_engine import persist
    from .commission_registry import json_text,now
    from . import commission_slice
    from .storage_integrity import seal,verified
    old=ws.conn.execute('SELECT * FROM run WHERE id=?',(old_run_id,)).fetchone()
    if old is None or not by.strip() or not reason.strip():raise WorkspaceError('更正需要原核算记录、操作人及原因')
    sid,period=old['store_id'],old['period']
    with guard(ws.root,sid,period):
        state=ws.state(sid,period)
        if not state or not state.closed or state.run_id!=old_run_id:raise WorkspaceError('原结账版本已变化，必须重新核对')
        raw=json.loads(old['result']);old_c=raw.get('commission') or {}
        with registry.connect() as conn:
            meta=conn.execute('SELECT * FROM calculation WHERE id=? AND finance_run=?',(old_c.get('calculation_id'),old_run_id)).fetchone()
            decisions=conn.execute('SELECT count(*) FROM payout_confirmation WHERE finance_run=?',(old_run_id,)).fetchone()[0]
            exclusions=conn.execute('SELECT count(*) FROM profit_exclusion WHERE finance_run=?',(old_run_id,)).fetchone()[0]
        if decisions or exclusions:raise WorkspaceError('本期有独立核定实发或剔除记录，必须先制定审计迁移，不能直接更换核算版本')
        if meta is None or meta['sha']!=expected_calculation_sha:raise WorkspaceError('原提成档案已变化')
        if not audit.get('verified_orders') or not audit.get('evidence'):raise WorkspaceError('没有通过核对的分配依据')
        if any(money_float(v)!=0 for v in audit.get('rounding_deltas',{}).values()):raise WorkspaceError('更正不能改变店铺原始入账总额')
        source=registry.root/'calculations'/Path(meta['path']).name
        if hashlib.sha256(source.read_bytes()).hexdigest()!=meta['sha']:raise WorkspaceError('原提成档案校验失败')
        source_facts=ws.facts_path(old_run_id)
        if not verified(source_facts):raise WorkspaceError('原订单流水校验失败')
        annotation={'kind':audit['kind'],'from_run':old_run_id,'from_calculation':meta['id'],
            'verified_orders':audit['verified_orders'],'pending_orders':audit['pending_orders'],
            'reason':reason,'at':now(),'by':by,'source_sha':meta['sha'],
            'basis_sha':hashlib.sha256(json_text(audit['evidence']).encode()).hexdigest()}
        annotation['code_sha']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        annotation['scope']='仅更正已核实的历史收入及同口径平台费分配；未核实组及其他费用保留原记录待复核'
        annotation['provenance']=audit.get('provenance',{})
        cid=str(uuid.uuid4());updated=deepcopy(summary);updated['calculation_id']=cid
        updated['allocation_correction']=annotation
        updated.setdefault('notes',[]).append('已按核实依据更正历史收入及平台费分配；原商品采购成本、其他支出和已核定实发不变')
        new_raw=deepcopy(raw);new_raw['commission']=updated;new_raw['allocation_correction']=annotation
        new_raw['has_allocation_evidence']=True
        fingerprint=hashlib.sha256((str(old['input_fingerprint'])+json_text(annotation)).encode()).hexdigest()
        new_id=ws.record(sid,period,new_raw,json.loads(old['shas']),evidence_ready=False,
                         model_revision=old['model_revision'] or '',input_fingerprint=fingerprint)
        new_meta=dict(meta);new_meta.update(id=cid,summary_json=json_text(updated))
        persist(registry,new_id,details,new_meta)
        for suffix in ('.parquet','.pricing.parquet','.coverage.parquet'):
            src=source_facts.with_suffix(suffix);dst=ws.facts_path(new_id).with_suffix(suffix)
            if src.exists():shutil.copyfile(src,dst);seal(dst)
        proof=pl.DataFrame(audit['evidence']).with_columns(pl.lit('trade_receipt').alias('metric_id'),
            pl.col('order_id').alias('link_key'),pl.col('new_factor').cast(pl.Float64).alias('factor'),
            (pl.col('source_amount').cast(pl.Float64)*pl.col('new_factor').cast(pl.Float64)).round(6).alias('amount'),
            pl.lit('历史冻结流水与核对后比例').alias('allocation_basis_source'),
            pl.col('spine_row').cast(pl.UInt32))
        proof_path=ws.facts_path(new_id).with_suffix('.allocation.parquet');proof.write_parquet(proof_path);seal(proof_path)
        ws.mark_evidence(new_id,ready=True)
        manual=ws.conn.execute('SELECT * FROM manual_finance WHERE run_id=?',(old_run_id,)).fetchone()
        with ws.conn as conn:
            conn.execute('BEGIN IMMEDIATE')
            current=conn.execute('SELECT state,run_id FROM period WHERE store_id=? AND period=?',(sid,period)).fetchone()
            if not current or current['state']!='closed' or current['run_id']!=old_run_id:raise WorkspaceError('结账版本在更正时发生变化')
            if manual:
                original=json.loads(manual['result_json']);shown=deepcopy(original);mc=shown['commission']
                new_people={p['person_id']:p for p in updated.get('people',[])}
                for p in mc.get('people',[]):
                    amount=p.get('amount');p.update(new_people.get(p['person_id'],{}));p['amount']=amount
                for key in ('calculation_id','products','production_outputs','base_total','managed_sales'):
                    if key in updated:mc[key]=updated[key]
                mc['allocation_correction']=annotation;shown['allocation_correction']=annotation;shown['has_allocation_evidence']=True
                decision=json.loads(manual['decision_json']);decision['source_run_id']=new_id;decision['allocation_correction']=annotation
                for key in ('manual_cost','manual_payout'):
                    if shown.get(key):shown[key]['source_run_id']=new_id
                if shown['statement']!=original['statement'] or [(p['person_id'],p.get('amount')) for p in mc.get('people',[])]!=[(p['person_id'],p.get('amount')) for p in original['commission'].get('people',[])]:
                    raise WorkspaceError('更正不能改变已结账损益或已核定实发')
                new_text=conn.execute('SELECT result FROM run WHERE id=?',(new_id,)).fetchone()[0]
                conn.execute('INSERT INTO manual_finance VALUES(?,?,?,?,?,?,?,?)',(new_id,sid,period,json_text(shown),json_text(decision),hashlib.sha256(new_text.encode()).hexdigest(),now(),by))
                commission_slice.save(conn,new_id,shown,kind='manual')
            labor=conn.execute('SELECT amount,by FROM run_labor WHERE run_id=?',(old_run_id,)).fetchone()
            if labor:conn.execute('INSERT INTO run_labor VALUES(?,?,?,?,?,?)',(new_id,sid,period,labor['amount'],now(),labor['by']))
            conn.execute('UPDATE period SET run_id=?,changed_at=?,by=?,note=? WHERE store_id=? AND period=? AND run_id=?',
                (new_id,now(),by,reason,sid,period,old_run_id))
            conn.execute('INSERT INTO config_log(at,by,kind,summary,before_json,after_json) VALUES(?,?,?,?,?,?)',
                (now(),by,'period-allocation-correction',reason,json_text({'run_id':old_run_id}),json_text({'run_id':new_id,**annotation})))
        return new_id
