"""One evidence set for classification warnings and their source-row drilldown.

Legacy recovery replays only classification against the archived model and
immutable source bytes, then reconciles to the saved warning. It never posts
money, changes a run, or uses today's dictionary to reinterpret history.
"""
import hashlib
import json
import sqlite3
from collections import OrderedDict
from contextlib import closing
from decimal import Decimal

import polars as pl

from .engine.types import ClassifyReport
from .model.schema import Model
from .snapshot_store import resolve
from .storage_integrity import verified, digest
from .workspace import WorkspaceError

KEYS = ['file_sha', 'sheet', 'row_no']
LABEL = 'unclassified_label'
AMOUNT = 'unclassified_amount'
_cache = OrderedDict()


def evidence_frame(report):
    records=[(sha,sheet,row,label,amount) for label,values in report.unmatched_rows.items()
             for (sha,sheet,row),amount in values.items()]
    result=pl.DataFrame(records,schema=[('file_sha',pl.String),('sheet',pl.String),('row_no',pl.String),
                                       (LABEL,pl.String),(AMOUNT,pl.Float64)],orient='row')
    if result.height and result.select(KEYS).n_unique()!=result.height:
        raise WorkspaceError('同一源行出现冲突的未归类依据，不能静默选择其中一条')
    return result


def attach(facts, report):
    if facts.is_empty():
        return facts.with_columns(pl.lit(None,dtype=pl.String).alias(LABEL),pl.lit(None,dtype=pl.Float64).alias(AMOUNT))
    evidence=evidence_frame(report)
    shadow=['__unclassified_sha','__unclassified_sheet','__unclassified_row']
    expressions=[]
    for name,alias in zip(KEYS,shadow):
        expr=pl.col(name).cast(pl.String).fill_null('')
        if name=='row_no' and facts.schema[name].is_numeric():
            expr=pl.when(pl.col(name)==0).then(pl.lit('')).otherwise(expr)
        expressions.append(expr.alias(alias))
    return facts.with_columns(expressions).join(evidence.rename(dict(zip(KEYS,shadow))),
        on=shadow,how='left',maintain_order='left').drop(shadow)


def _reconcile(report, saved):
    expected={r['label']:(int(r['count']),Decimal(str(r['amount']))) for r in saved}
    actual={label:(count,Decimal(str(amount))) for label,(count,amount) in report.unmatched.items()}
    if expected.keys()!=actual.keys() or any(expected[k][0]!=actual[k][0] or
        abs(expected[k][1]-actual[k][1])>Decimal('0.00000001') for k in expected):
        raise WorkspaceError('历史未归类明细与该次核算的笔数或金额不一致，不能显示为零；请核对原始归类留档')


def _archived_model(ws, state, current):
    cid=(state.result.get('commission') or {}).get('calculation_id')
    db=ws.root/'commission'/'registry.db'
    if cid and db.exists():
        with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            row=conn.execute('SELECT store_id,period,model_json FROM calculation WHERE id=?',(cid,)).fetchone()
            if row and row[:2]==(state.store_id,state.period):
                return Model.model_validate_json(resolve(conn,row[2]))
    row=ws.conn.execute('SELECT model_revision FROM run WHERE id=?',(state.run_id,)).fetchone()
    if row and row[0]==hashlib.sha256(current.model_dump_json().encode()).hexdigest():
        return current
    raise WorkspaceError('该次核算缺少历史归类规则留档，不能用当前规则猜测明细；原账未改动')


def for_run(ws, run_id, current):
    state=ws.state_by_run(run_id)
    if not state or 'unclassified' not in (state.result or {}):
        raise WorkspaceError('该次核算缺少未归类检查依据，不能把缺失明细当作零笔')
    path=ws.facts_path(run_id)
    if not verified(path):
        raise WorkspaceError('原始核算明细留档校验未通过，未归类明细暂不可用；原账未改动')
    facts=pl.scan_parquet(path)
    columns=set(facts.collect_schema().names())
    saved=state.result['unclassified']
    if not saved and any(not f.get('passed') and (f.get('id')=='chk_no_unclassified' or
            f.get('kind')=='no_unclassified') for f in state.result.get('findings',[])):
        raise WorkspaceError('原检查提示有未归类费项，但归档清单为空；不能显示为零，请核对该次留档')
    if LABEL in columns and AMOUNT in columns:
        pending=facts.filter(pl.col(LABEL).is_not_null()).unique(subset=KEYS,maintain_order=True).collect()
        report=ClassifyReport()
        for row in pending.select(*KEYS,LABEL,AMOUNT).iter_rows(named=True):
            report.note_unmatched(row[LABEL],tuple(str(row[k] or '') for k in KEYS),row[AMOUNT])
        _reconcile(report,saved)
        return pending
    if not saved:
        return attach(facts.limit(0).collect(),ClassifyReport())
    model=_archived_model(ws,state,current)
    from .engine.classify import classify, effective_classify_rules, merge_reports
    from .engine.rules import resolve_classify_field
    from .engine.runtime import _ingest_file_cached, _header_row_candidates, _with_row_amount, ROW_AMOUNT
    from .engine.types import ANCHOR_ROW
    platform=state.result.get('platform') or model.store(state.store_id).platform
    templates={}
    for t in model.templates:
        roles={b.role for b in t.bindings}|set(t.time_slots)
        if 'subject' in roles or any(r.when and resolve_classify_field(r.when.field,roles)
                for r in effective_classify_rules(model,platform,t)):
            templates[t.id]=t
    candidates=facts.filter(pl.col('template_id').is_in(list(templates)) &
                            (pl.col('major').fill_null('')==''))
    if {'store','period'}<=columns:
        candidates=candidates.filter((pl.col('store')==(state.result.get('store') or model.store(state.store_id).name)) &
                                     (pl.col('period')==state.period))
    candidates=candidates.collect()
    sources=[]
    for sha in sorted(candidates['file_sha'].unique().to_list()):
        source=ws.root/'files'/sha[:2]/sha
        if not source.exists() or digest(source)!=sha:
            raise WorkspaceError('未归类流水的原文件缺失或校验不符，不能恢复原行；原账未改动')
        sources.append((sha,source))
    # Cache only the small, reconciled pending subset. Every hit still checks
    # the facts and raw file checksums, so a missing/tampered original is visible.
    key=(str(ws.root.resolve()),run_id,state.store_id,state.period,state.result.get('store'),platform,
         path.with_name(path.name+'.sha256').read_text(),
         hashlib.sha256(model.model_dump_json().encode()).hexdigest(),
         json.dumps(saved,sort_keys=True,ensure_ascii=False),tuple(sha for sha,_ in sources))
    def build():
        reports=[]
        for sha,source in sources:
            items=_ingest_file_cached(source,model,[],_header_row_candidates(model),None,'',None)
            own=candidates.filter(pl.col('file_sha')==sha)
            for item in items:
                if item.ref.sha256!=sha:
                    raise WorkspaceError('读取期间原文件发生变化，未归类明细未展示；请核对留档后重试')
                if not item.ok or not item.template:continue
                selected=own.filter((pl.col('sheet').fill_null('')==(item.ref.sheet or '')) &
                                    (pl.col('template_id')==item.template.id))
                for mid in selected['metric_id'].unique().to_list():
                    metric=model.metric(mid).for_platform(platform)
                    if metric is None:continue
                    anchors=selected.filter(pl.col('metric_id')==mid)['row_no'].cast(pl.String).unique()
                    raw=item.frame.filter(pl.col(ANCHOR_ROW).cast(pl.String).is_in(anchors.implode()))
                    if raw.is_empty():continue
                    _, report=classify(_with_row_amount(raw,metric),model,platform,ROW_AMOUNT,item.template)
                    reports.append(report)
        report=merge_reports(reports)
        # The saved warning, not newly discoverable replay labels, owns scope.
        labels={row['label'] for row in saved}
        report.unmatched_rows={label:values for label,values in report.unmatched_rows.items() if label in labels}
        _reconcile(report,saved)
        return attach(candidates,report).filter(pl.col(LABEL).is_not_null()).unique(subset=KEYS,maintain_order=True)
    from .read_cache import cached
    return cached(_cache,key,build,8)
