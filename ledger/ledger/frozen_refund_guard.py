"""Whole original-month refunds cannot be counted again across frozen periods.

Read-only frozen facts are the authority for historical posting. The current
raw amount/date remain evidence; this is admission control, not a new amount
calculator or a historical correction. No database migration is required.
"""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal,InvalidOperation
import hashlib,json
from pathlib import Path
import polars as pl
from . import automation_store as journal
from .storage_integrity import verified,digest

SCHEMA='ledger_frozen_refund_guard.v1'
STATUS='__frozen_refund_guard__'
REFUNDS=frozenset({'trade_refund','trade_refund_douyin'})
EPS=Decimal('.0000005')

class HistoryChanged(RuntimeError):pass

def money(value):
    if isinstance(value,bool):raise ValueError('boolean money')
    number=Decimal(str(value))
    if not number.is_finite():raise ValueError('nonfinite money')
    return number

def same(a,b):return abs(a-b)<=EPS

def physical(row):
    return (str(row['metric_id']),str(row['file_sha']),str(row['sheet']),int(row['row_no']))

def doc(row):
    try:
        value=json.loads(row.get('statement_evidence') or '{}')
        return value if isinstance(value,dict) else {}
    except (ValueError,TypeError):return {}

def native_key(row,identity):
    value=identity.get('event_id')
    return (str(row['metric_id']),str(row['source_id']),identity.get('namespace'),str(value),identity.get('leg','event')) if value and identity.get('namespace') and identity.get('leg','event') is not None else None

def valid_id(value):
    return str(value).strip() if isinstance(value,(str,int)) and not isinstance(value,bool) and str(value).strip() else None

class Context:
    def __init__(self,ws,model,store):
        self.ws,self.model,self.store=ws,model,store
        self.snapshot=self.signature();self.loaded=False
        self.exact=defaultdict(list);self.native=defaultdict(list);self.uncertain=[]
        self.original_cache={};self.identities={};self.potential_cache={}
        self.current_items=None;self.current_indexes={}

    def signature(self):
        return [tuple(r) for r in self.ws.conn.execute("SELECT p.period,p.run_id,r.evidence_ready,r.evidence_error FROM period p LEFT JOIN run r ON r.id=p.run_id WHERE p.store_id=? AND p.state='closed' ORDER BY p.period",(self.store.id,))]

    def validate(self):
        if self.signature()!=self.snapshot:raise HistoryChanged('冻结退款归属刚发生变化，本次等待重新核算')

    @property
    def identity(self):return journal.digest([SCHEMA,self.store.id,self.snapshot])

    def template(self,name):
        try:return self.model.template(name)
        except (KeyError,ValueError,StopIteration):return None

    def namespace(self,template):return template.event_namespace or 'template:'+template.id

    def current_identity_index(self,row,ingestion):
        """Index original identity cells once, without parsing unrelated money.

        Duplicate physical anchors retain the old selected.height != 1
        behavior: no native identity can be inferred for either duplicate.
        The last matching known item wins, as in the prior per-row lookup.
        """
        if self.current_items is None:
            self.current_items={}
            for item in ingestion.known:
                if item.template and item.frame is not None:
                    self.current_items[(str(item.ref.sha256),item.template.id)]=item
        key=(str(row['file_sha']),row['template_id'])
        if key not in self.current_indexes:
            item=self.current_items.get(key)
            if item is None:self.current_indexes[key]=({},None,{})
            else:
                template=item.template;role=template.event_id_role
                value={'namespace':self.namespace(template),'leg':'event'}
                indexed={}
                if role=='txn_id' and {role,'__sheet__','__row__'}<=set(item.frame.columns):
                    columns=[name for name in ('__sheet__','__row__',role,'sub_order_id','sku','income','outgo') if name in item.frame.columns]
                    for original in item.frame.select(columns).iter_rows(named=True):
                        anchor=(original['__sheet__'],original['__row__'])
                        indexed[anchor]=None if anchor in indexed else original
                self.current_indexes[key]=(value,template,indexed)
        return self.current_indexes[key]

    def current_identity(self,row,ingestion):
        if getattr(ingestion,'frozen_archived_identity',False):
            return self.old_identity(row)
        key=(str(row['file_sha']),str(row['sheet']),int(row['row_no']),row['template_id'])
        if key not in self.identities:
            base,template,indexed=self.current_identity_index(row,ingestion)
            value=dict(base);original=indexed.get((row['sheet'],row['row_no']))
            if original is not None:
                value['event_id']=valid_id(original['txn_id'])
                value['component']=next((str(original[k]) for k in ('sub_order_id','sku') if k in original and original[k] not in (None,'')),'')
                if template.event_directional:
                    if not {'income','outgo'} & set(original):value['leg']=None
                    else:
                        incoming=money(original['income'] or 0)!=0 if 'income' in original else False;outgoing=money(original['outgo'] or 0)!=0 if 'outgo' in original else False
                        value['leg']='both' if incoming and outgoing else 'income' if incoming else 'outgo' if outgoing else 'zero'
            self.identities[key]=value
        return self.identities[key]

    def old_identity(self,row):
        recorded=doc(row).get('frozen_refund') or {}
        if recorded.get('schema')==SCHEMA and recorded.get('namespace'):
            return {'namespace':recorded['namespace'],'event_id':recorded.get('event_id'),'leg':recorded.get('leg','event'),'component':recorded.get('component') or row.get('sku') or ''}
        template=self.template(row['template_id'])
        if template is None or template.source!=row['source_id']:return {}
        result={'namespace':self.namespace(template),'leg':'event'}
        # Only the approved native transaction-ID contract is used, never an
        # order number or an amount match. This derives identity from original
        # bytes; it does not reinterpret historical monetary/posting policy.
        if template.event_id_role!='txn_id':return result
        if template.event_directional:result['leg']=None
        key=(row['file_sha'],template.id)
        if key not in self.original_cache:
            identities={};path=self.ws.path_of(row['file_sha'])
            if path.is_file() and digest(path)==row['file_sha']:
                from .engine.parse import parse
                from .engine.normalize import _bind_columns
                try:
                    for table in parse(path,template.parse,sha=row['file_sha']):
                        bound=_bind_columns(table.headers,template,[])
                        if 'txn_id' not in bound:continue
                        index=bound['txn_id']
                        for original in table.rows:
                            if index<len(original.cells):identities[(table.ref.sheet,original.row_no)]=valid_id(original.cells[index])
                except Exception:
                    identities={}  # Missing/unsupported identity stays pending.
            self.original_cache[key]=identities
        result['event_id']=self.original_cache[key].get((row['sheet'],int(row['row_no'])))
        return result

    def history(self):
        if self.loaded:return
        self.loaded=True
        needed={'metric_id','source_id','template_id','counted','contribution','amount','file_sha','sheet','row_no','period'}
        from .automation_period_proof import valid_artifact
        for period,run_id,ready,error in self.snapshot:
            owner={'period':period,'run_id':run_id}
            manifest={}
            proof=self.ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(run_id,)).fetchone() if run_id else None
            if proof:
                try:manifest=json.loads(proof[0])
                except (ValueError,TypeError):manifest={}
            try:
                if not run_id or not ready or error:raise ValueError('frozen_run_not_verified')
                path=self.ws.facts_path(run_id)
                if not path.is_file() or not verified(path):raise ValueError('frozen_facts_missing_or_unsealed')
                if proof:
                    m=manifest;rel=path.resolve().relative_to(self.ws.root.resolve()).as_posix()
                    artifact=next((a for a in m.get('artifacts',[]) if a['path']==rel),None)
                    if not artifact or not valid_artifact(self.ws,artifact):raise ValueError('frozen_facts_expected_hash_mismatch')
                    assurance='run_proof_bound_archive'
                else:assurance='legacy_frozen_run_and_verified_seal'
                schema=pl.scan_parquet(path).collect_schema()
                if not needed<=set(schema):raise ValueError('frozen_facts_identity_schema_missing')
                rows=pl.scan_parquet(path).filter(pl.col('metric_id').is_in(REFUNDS)&pl.col('counted')&(pl.col('period')==period)).collect().to_dicts()
                frozen=self.ws.state(self.store.id,period).result or {}
                calculated=frozen.get('calculation_inputs')
                if isinstance(calculated,dict):
                    totals=calculated.get('metric_totals',{})
                    for metric in {row['metric_id'] for row in rows}:
                        booked=sum((money(row['contribution']) for row in rows if row['metric_id']==metric),Decimal(0))
                        if metric not in totals or not same(booked,money(totals[metric])):
                            raise ValueError('frozen_refund_facts_and_snapshot_conflict')
                shas=set(json.loads(self.ws.conn.execute('SELECT shas FROM run WHERE id=?',(run_id,)).fetchone()[0]))
                archive={'path':path.resolve().relative_to(self.ws.root.resolve()).as_posix(),'sha':digest(path),'sealed':True}
                for row in rows:
                    if not row.get('file_sha') or not row.get('sheet') or int(row['row_no'])<1:raise ValueError('frozen_original_anchor_missing')
                    if not proof and row['file_sha'] not in shas:raise ValueError('legacy_frozen_original_not_bound_to_run')
                    identity=self.old_identity(row)
                    record={'owner':owner,'row':row,'identity':identity,'artifact':archive,'assurance':assurance}
                    self.exact[physical(row)].append(record)
                    key=native_key(row,identity)
                    if key:self.native[key].append(record)
                    else:self.uncertain.append({'source':row['source_id'],'namespace':identity.get('namespace'),'potential_event_id':identity.get('event_id'),'physical':physical(row),'reason':'legacy_native_event_identity_unverified','owner':owner})
            except Exception as exc:
                self.uncertain.append({'source':None,'namespace':None,'reason':str(exc) if isinstance(exc,ValueError) else 'frozen_archive_unreadable','owner':owner,'manifest':manifest})

    def potential_original_index(self,uncertain):
        """Recover only event identity from complete original input evidence.

        This is a slow missing-facts recovery path, not a monetary replay. The
        known acquisition contract supplies financial data from registered
        files; generated OrderFeed templates are explicitly nonfinancial.
        """
        run_id=uncertain['owner']['run_id']
        if run_id in self.potential_cache:return self.potential_cache[run_id]
        self.potential_cache[run_id]=None
        manifest=uncertain.get('manifest') or {}
        used=manifest.get('template_ids')
        if not isinstance(used,list):return None
        from .order_feed import OrderFeed
        generated={t.id:t for t in (OrderFeed._order_template(),OrderFeed._cost_template(),OrderFeed._after_template())}
        source_ids={m.source for m in self.model.metrics if m.id in REFUNDS}
        selected=[]
        for name in used:
            template=self.template(name) or generated.get(name)
            if template is None:return None
            if template.source in source_ids:selected.append(template)
        result={'schema':SCHEMA,'complete':True,'store_id':self.store.id,'period':uncertain['owner']['period'],'events':[]}
        if not selected:
            result['event_count']=0;self.potential_cache[run_id]=result;return result
        from .engine.parse import parse
        from .engine.normalize import _bind_columns
        from .model.schema import normalize_header
        run=self.ws.conn.execute('SELECT shas FROM run WHERE id=?',(run_id,)).fetchone()
        if not run:return None
        seen=set();parsed={}
        for sha in set(json.loads(run[0])):
            # Financial originals have an immutable file-catalog identity.
            registered=self.ws.conn.execute('SELECT 1 FROM file WHERE sha=?',(sha,)).fetchone()
            if not registered:continue  # known OrderFeed acquisition is not a bank source
            path=self.ws.path_of(sha)
            if not path.is_file() or digest(path)!=sha:return None
            for template in selected:
                options=template.parse.model_dump_json()
                try:
                    if (sha,options) not in parsed:parsed[(sha,options)]=parse(path,template.parse,sha=sha)
                    for table in parsed[(sha,options)]:
                        if not all(normalize_header(column) in {normalize_header(h) for h in table.headers} for column in template.match_columns):continue
                        seen.add(template.id)
                        bound=_bind_columns(table.headers,template,[])
                        columns={bound['txn_id']} if 'txn_id' in bound else set()
                        for raw in table.rows:
                            event=valid_id(raw.cells[next(iter(columns))]) if template.event_id_role=='txn_id' and len(columns)==1 and next(iter(columns))<len(raw.cells) else None
                            identity={'namespace':self.namespace(template),'event_id':event,'leg':'event'}
                            if template.event_directional:identity['leg']=None
                            for metric in self.model.metrics:
                                if metric.id in REFUNDS and metric.source==template.source:
                                    result['events'].append({'metric_id':metric.id,'source_id':template.source,
                                        'physical':[metric.id,sha,table.ref.sheet,raw.row_no],'identity':identity})
                except Exception:return None
        if seen!={t.id for t in selected}:return None
        result['event_count']=len(result['events']);self.potential_cache[run_id]=result;return result

    def uncertainty_applies(self,uncertain,row,identity):
        if uncertain['source'] not in (None,row['source_id']):return False
        if uncertain['namespace'] not in (None,identity.get('namespace')):return False
        if uncertain.get('physical')==physical(row):return True
        if uncertain.get('potential_event_id') and identity.get('event_id') and uncertain['potential_event_id']!=identity['event_id']:return False
        manifest=uncertain.get('manifest') or {}
        index=manifest.get('frozen_refund_event_index')
        if index is None:index=self.potential_original_index(uncertain)
        if (isinstance(index,dict) and index.get('schema')==SCHEMA and index.get('complete') is True
            and index.get('store_id')==self.store.id and index.get('period')==uncertain['owner']['period']
            and isinstance(index.get('events'),list)
            and index.get('event_count')==len(index['events'])
            and all(isinstance(e,dict) and {'metric_id','source_id','physical','identity'}<=set(e) and isinstance(e['identity'],dict) for e in index['events'])):
            # A sealed full list is positive absence evidence, not a missing
            # archive silently treated as empty. Matching entries still need
            # their counted amount archive before automatic exclusion.
            for event in index['events']:
                if tuple(event['physical'])==physical(row):return True
                if event['metric_id']!=row['metric_id'] or event['source_id']!=row['source_id']:continue
                old=event['identity']
                if old.get('namespace') and identity.get('namespace') and old['namespace']!=identity['namespace']:continue
                if old.get('event_id') and identity.get('event_id'):
                    if old['event_id']!=identity['event_id']:continue
                    if old.get('leg','event') is not None and identity.get('leg','event') is not None and old.get('leg','event')!=identity.get('leg','event'):continue
                return True
            return False
        # Legacy full input-dependency sets allow a narrower candidate query.
        # This only excludes a different original-order identity when the
        # immutable financial occurrence month also excludes orderless posting
        # into the old period. It never proves equality from PID or amount.
        if row['file_sha'] in manifest.get('source_refs',[]):return True
        dependencies=manifest.get('original_date_dependencies')
        if isinstance(dependencies,dict) and isinstance(dependencies.get('potential_parent_ids'),list):
            parent=str(row.get('link_key') or '').strip()
            occurrence=str(row.get('source_period') or '')
            if parent and parent not in set(map(str,dependencies['potential_parent_ids'])) and occurrence and occurrence!=uncertain['owner']['period']:
                return False
        return True

    def assess(self,records,current_rows):
        owners={(r['owner']['period'],r['owner']['run_id']) for r in records}
        if len(owners)!=1:return 'pending','multiple_frozen_owners',None
        old_parents={str(r['row'].get('link_key') or '') for r in records}-{''}
        new_parents={str(r.get('link_key') or '') for r in current_rows}-{''}
        if old_parents and new_parents and old_parents!=new_parents:
            return 'pending','frozen_original_order_identity_conflict',None
        old_occurrence={str(r['row'].get('source_period') or '') for r in records}-{''}
        new_occurrence={str(r.get('source_period') or '') for r in current_rows}-{''}
        if old_occurrence and new_occurrence and old_occurrence!=new_occurrence:
            return 'pending','frozen_original_occurrence_conflict',None
        unique={}
        for record in records:
            row=record['row'];key=physical(row)
            amount=money(row['amount'])
            if key in unique and not same(unique[key],amount):return 'pending','frozen_original_amount_conflict',None
            unique[key]=amount
        if len(unique)>1:
            components={physical(r['row']):str(r['identity'].get('component') or r['row'].get('sku') or '') for r in records}
            if not all(components.values()) or len(set(components.values()))!=len(unique):
                return 'pending','frozen_event_component_identity_unverified',None
        current_anchors={physical(r) for r in current_rows}
        if len(current_anchors)>1:
            # A whole event may have explicit source SKU lines. Repeated
            # export rows without a component identity are not a larger event.
            components={physical(r):str(doc(r).get('frozen_refund',{}).get('component') or r.get('sku') or '') for r in current_rows}
            if not all(components.values()) or len(set(components.values()))!=len(current_anchors):
                return 'pending','current_event_component_identity_unverified',None
        booked=sum((money(r['row']['contribution']) for r in records),Decimal(0))
        original=sum(unique.values(),Decimal(0));incoming=sum((money(r['amount']) for r in current_rows),Decimal(0))
        if not same(booked,original):return 'pending','frozen_event_not_fully_booked',None
        if not same(original,incoming):return 'pending','frozen_and_current_amount_conflict',None
        owner=records[0]['owner']
        return 'same_period' if all(r['period']==owner['period'] for r in current_rows) else 'excluded','already_fully_booked_in_frozen_period',owner

    def annotate(self,ingestion,facts,metrics):
        targets={m.id for m in metrics if m.id in REFUNDS and m.posting_basis=='order_number'}
        if not targets or facts.is_empty():return facts
        if facts.filter(pl.col('metric_id').is_in(targets)&(pl.col('store')==self.store.name)).is_empty():return facts
        self.history();rows=facts.to_dicts();identities={};native_groups=defaultdict(list)
        for index,row in enumerate(rows):
            if row['metric_id'] not in targets or row['store']!=self.store.name:continue
            identity=self.current_identity(row,ingestion);identities[index]=identity
            key=native_key(row,identity)
            if key:
                candidate=dict(row);evidence=doc(candidate);evidence['frozen_refund']={'component':identity.get('component','')};candidate['statement_evidence']=journal.encoded(evidence)
                native_groups[key].append(candidate)
        for index,identity in identities.items():
            row=rows[index];status='new';reason=None;owner=None;records=[]
            namespace=identity.get('namespace');uncertain=[u for u in self.uncertain if self.uncertainty_applies(u,row,identity)]
            exact=self.exact.get(physical(row),[]);nkey=native_key(row,identity);native=self.native.get(nkey,[]) if nkey else []
            try:
                if exact:
                    if any(r['row']['source_id']!=row['source_id'] or r['identity'].get('namespace') not in (None,namespace) for r in exact):
                        status,reason='pending','same_original_namespace_conflict'
                    else:status,reason,owner=self.assess(exact,[row]);records=exact
                    if status!='pending' and native:
                        nstatus,nreason,nowner=self.assess(native,native_groups[nkey])
                        if nstatus=='pending' or nowner!=owner:status,reason,owner='pending',nreason,None
                elif native:
                    status,reason,owner=self.assess(native,native_groups[nkey]);records=native
                elif uncertain:
                    status,reason='pending',uncertain[0]['reason']
                elif not nkey and any(k[0]==row['metric_id'] and k[1]==row['source_id'] and k[2]==namespace and (not identity.get('event_id') or k[3]==identity['event_id']) for k in self.native):
                    status,reason='pending','current_native_event_identity_unverified'
                if uncertain and status in ('excluded','same_period'):
                    # A missing old owner archive cannot prove uniqueness.
                    missing=[u for u in uncertain if u['source'] is None]
                    if missing:status,reason,owner='pending',missing[0]['reason'],None
            except (ValueError,TypeError,InvalidOperation,KeyError):status,reason,owner='pending','refund_amount_or_identity_unverified',None
            missing=[]
            for entry in uncertain:
                run_id=entry['owner']['run_id'];relative=self.ws.facts_path(run_id).relative_to(self.ws.root).as_posix()
                artifact=next((a for a in (entry.get('manifest') or {}).get('artifacts',[]) if a.get('path')==relative),None)
                missing.append({'owner':entry['owner'],'reason':entry['reason'],'required_archive':relative,
                                'expected_sha':artifact.get('sha') if artifact else None})
            metadata={'schema':SCHEMA,**identity,'status':status,'reason':reason,'owner':owner,
                'uncertain_sources':missing,
                'current_period':row['period'],'original_amount':str(row['amount']),
                'frozen_sources':[{'owner':r['owner'],'artifact':r['artifact'],'amount':str(r['row']['amount']),
                    'contribution':str(r['row']['contribution']),'assurance':r['assurance']} for r in records]}
            evidence=doc(row);evidence['frozen_refund']=metadata;row['statement_evidence']=journal.encoded(evidence)
            row[STATUS]=status
            if status=='excluded':row['source_note']='；'.join(filter(None,[row.get('source_note'),f"该原退款已在{owner['period']}结账，本期不重复计入；新源原日和原金额保留，历史归属run {owner['run_id']}见证据"]))
            elif status=='pending':row['source_note']='；'.join(filter(None,[row.get('source_note'),'已结退款身份或金额待财务核对；争议行未计入，原账不变：'+str(reason)]))
        return pl.from_dicts(rows,schema={**facts.schema,STATUS:pl.String},strict=False)

def clear_contribution(facts):
    if STATUS not in facts.columns:return facts
    blocked=pl.col(STATUS).is_in(['excluded','pending']).fill_null(False)
    return facts.with_columns(pl.when(blocked).then(False).otherwise(pl.col('counted')).alias('counted'),
        pl.when(blocked).then(0.).otherwise(pl.col('contribution')).alias('contribution'))

def blocked_metrics(facts):
    return set(facts.filter(pl.col(STATUS)=='pending')['metric_id']) if STATUS in facts.columns else set()

def finish_slice(sliced,model):
    pending=blocked_metrics(sliced.facts)
    if not pending:return
    from .engine.types import Finding
    sources={m.source for m in model.metrics if m.id in pending}
    for node in sliced.nodes.values():
        if not node.available and sources & set(node.missing_sources):
            node.value=None;node.unavailable_reason=node.unavailable_reason or '已结退款原事件归属或金额尚未核实，争议行未作为零确认'
    sliced.audit.findings.append(Finding('frozen_refund_finance_conflict','已结退款归属待核',passed=False,blocking=True,
        message='历史冻结退款身份、原金额或档案存在待核事项；原账不变，当前争议退款未计入，请财务集中核对依据',
        detail={'responsible':'finance','metric_ids':sorted(pending)}))


def validate_run(ws,model,store,period,run_id):
    """A later close must not validate a cached result against an old world."""
    from types import SimpleNamespace
    context=Context(ws,model,store)
    empty={'schema':SCHEMA,'store_id':store.id,'period':period,'bindings':[]}
    if not context.snapshot or not any(m.id in REFUNDS and m.posting_basis=='order_number' for m in model.metrics):
        return {'sha':journal.digest(empty),'recompute_required':False}
    try:
        path=ws.facts_path(run_id)
        if not verified(path):raise ValueError('current_refund_archive_unverified')
        current=pl.read_parquet(path)
        rows=current.filter(pl.col('metric_id').is_in(REFUNDS))
        if rows.is_empty():return {'sha':journal.digest(empty),'recompute_required':False}
        refreshed=context.annotate(SimpleNamespace(known=(),frozen_archived_identity=True),rows,
            [m for original in model.metrics if (m:=original.for_platform(store.platform)) is not None])
        bindings=[];stale=False
        for before,after in zip(rows.to_dicts(),refreshed.to_dicts()):
            previous=doc(before).get('frozen_refund') or {};now=doc(after)['frozen_refund']
            bindings.append([physical(after),now])
            if now['status']=='pending' or (now['status']=='excluded' and before.get('counted')):
                stale=True
            if previous.get('status')=='pending':stale=True
        return {'sha':journal.digest({**empty,'bindings':bindings}),'recompute_required':stale}
    except Exception:
        return {'sha':journal.digest({**empty,'state':'current_archive_unverified'}),'recompute_required':True}


def protected_artifacts(facts):
    records={}
    if 'statement_evidence' not in facts.columns:return []
    for row in facts.to_dicts():
        metadata=doc(row).get('frozen_refund') or {}
        for entry in metadata.get('frozen_sources',[]):
            artifact=entry.get('artifact')
            if artifact:records[journal.encoded(artifact)]=artifact
    return list(records.values())


def event_index(ingestion,facts,store_id,period):
    context=getattr(ingestion,'frozen_refund_context',None)
    if context is None:return None
    events=[]
    selected=facts.filter(pl.col('metric_id').is_in(REFUNDS)&pl.col('counted'))
    for row in selected.to_dicts():
        events.append({'metric_id':row['metric_id'],'source_id':row['source_id'],
            'physical':list(physical(row)),'identity':context.current_identity(row,ingestion)})
    return {'schema':SCHEMA,'store_id':store_id,'period':period,'complete':True,
            'event_count':len(events),'events':events}
