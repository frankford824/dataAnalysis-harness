"""Paired freight experiments over one immutable input bundle, never financial writes."""
from collections import defaultdict
from decimal import Decimal
import csv
import io
import json
from pathlib import Path
import re
import uuid

import polars as pl

from . import freight_trial as trial, store_classification
from . import freight_experiments_inputs as frozen
from . import freight_experiments_lineage as lineage
from .freight_trial_inputs import InputError, digest, parse_sources, feed_frames, _materialize
from .freight_shipments import platform_bases, build_packages
from .freight_allocation import RULE_VERSION

SCHEMA_VERSION = 2
QUANTUM = Decimal('.000001')
CODE_ROOT = Path(__file__).resolve().parent


def code_identity():
    from .version import engine_version
    files=[{'path':path.relative_to(CODE_ROOT).as_posix(),'sha256':trial._hash_file(path)}
           for path in sorted(CODE_ROOT.rglob('*.py')) if '__pycache__' not in path.parts]
    if not files:raise InputError('实验缺少实际Python执行源码清单')
    package_sha=digest(files)
    production_manifest=CODE_ROOT.parent.parent/'PRODUCTION-CODE-MANIFEST.json'
    return {'engine':engine_version(),'experiment_code_sha':package_sha,'package_code_sha':package_sha,
            'dependency_policy':'all_recursive_ledger_python_sources','python_sources':files,
            'production_code_manifest_sha256':trial._hash_file(production_manifest) if production_manifest.is_file() else None}


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError) as exc:
        raise InputError('实验留档缺失或内容无效：'+Path(path).name) from exc


def _amount(value):
    return Decimal(str(value or 0))


def formal_baseline(bundle, model, inputs, charges, aliases, scratch, progress):
    """Execute the original ingestion, feed adapter and accounting engine.

    Only whole-file bill aliases are canonicalized in the common source pool.
    The original metric, lookup, filtering, allocation and posting rules are unchanged.
    Source anchors and original projection factors recover per-charge destinations.
    """
    from .engine.runtime import ingest, run
    from .workspace import SHARED_STORE_ID
    feed = frozen.materialize_feed(bundle, scratch / 'adapter')
    by_anchor = {(c['file_sha'],c['sheet'],c['row_no']):c for c in charges}
    result = {c['charge_id']:{'allocations':[], 'source_facts':[], 'reason':'formal_unbooked'} for c in charges}
    records = {r['sha']:r for r in inputs['files']}
    # A executes the complete captured bill pool. The selected month only
    # chooses output charges; other-month originals remain engine inputs.
    canonical_bills = sorted(set(aliases.values()))
    mapped = {r['ledger_store_id'] for r in inputs['mapping'] if r['mapping_status']=='confirmed'}
    partial = inputs.get('baseline_policy') == 'confirmed_entities_with_explicit_unavailable'
    availability = []; original_groups={}
    for index, store in enumerate(model.stores):
        progress('原正式引擎 A：' + store.name, index, len(model.stores))
        # Match Workspace.active_files' store/shared filename and SHA precedence.
        slots = sorted((r for r in inputs['slots'] if r['store_id'] in {store.id,SHARED_STORE_ID}),
                       key=lambda r:(r['store_id']!=store.id,r['name']))
        named = {}; seen = set(); paths = []
        for slot in slots:
            named.setdefault(slot['name'],slot)
        for slot in named.values():
            if slot['sha'] in seen or slot['sha'] in aliases:
                continue
            seen.add(slot['sha'])
            paths.append(_materialize(bundle, scratch / 'raw', {**records[slot['sha']], 'name':slot['name']}))
        if store.id not in mapped and not paths:
            availability.append({'store_id':store.id,'status':'baseline_unavailable',
                'reason':'confirmed_mapping_and_native_input_missing','source_shas':[]})
            continue
        if store.id not in mapped:
            if partial:
                availability.append({'store_id':store.id,'status':'baseline_unavailable',
                    'reason':'confirmed_mapping_missing','source_shas':sorted(seen)})
                continue
            raise InputError('正式引擎 A 缺少已确认店铺映射：' + store.id)
        paths.extend(_materialize(bundle, scratch / 'raw', records[sha]) for sha in canonical_bills)
        ingestion = ingest(paths, model, [store.name,*store.aliases],
                           cache_root=scratch / 'parse', default_store=store.name)
        # Original service excludes declared derivative/empty helper sheets;
        # they are not failed financial sources. Keep genuine parse errors hard.
        unknowns=[{'file_sha':i.ref.sha256,'sheet_ref':digest(i.ref.sheet or ''),
            'source_id':i.recognition.source_id,'error_ref':digest(i.error or i.recognition.reason)}
            for i in ingestion.unknown if i.derivative is None]
        # service.recompute records unknown_tables then still calls run on
        # the intact ingestion. Unrecognized auxiliary files cannot become a
        # wrapper-only abort; known financial sources that failed parsing do.
        bad = [i.ref.label() + '：' + i.error for i in ingestion.unknown
            if i.error and i.derivative is None and i.recognition.source_id]
        if bad:
            if partial:
                availability.append({'store_id':store.id,'status':'baseline_unavailable',
                    'reason':'original_source_parse_error','source_shas':sorted(seen),
                    'error_refs':[digest(message) for message in bad]})
                continue
            raise InputError('正式引擎 A 原始输入无法读取：' + '；'.join(bad[:5]))
        feed.append_to(ingestion, store)
        computed = run(ingestion, store.platform)
        source = computed.facts
        projection = computed.projections.get('freight_cost')
        if 'metric_id' not in source.columns or projection is None:
            availability.append({'store_id':store.id,'status':'baseline_unavailable',
                'reason':'formal_spine_or_freight_projection_missing','source_shas':sorted(seen)})
            continue
        evidence = scratch.parent.parent / 'formal_evidence' / digest(store.id)[:24]
        evidence.mkdir(parents=True,exist_ok=True)
        source.filter(pl.col('metric_id')=='freight_cost').write_parquet(evidence / 'source.parquet')
        projection.facts.write_parquet(evidence / 'projection.parquet')
        computed.spine.write_parquet(evidence / 'spine.parquet')
        trial._write_json(evidence / 'scope.json',{'store_id':store.id,'platform':store.platform,
            'allocation_pending':projection.allocation_pending})
        availability.append({'store_id':store.id,
            'status':'available' if computed.spine.height else 'baseline_unavailable',
            'reason':'' if computed.spine.height else 'formal_spine_empty',
            'spine_rows':computed.spine.height,'source_shas':sorted(seen),
            'original_service_unknowns':unknowns,'original_unknowns_preserved':True})
        own=source.filter(pl.col('metric_id')=='freight_cost')
        scope_groups=lineage.scope_groups(own,projection.facts,computed.spine,store)
        original_groups.update(scope_groups)
        by_source=defaultdict(set)
        for gid,group in scope_groups.items():
            for item in group['source_anchors']:
                by_source[lineage.anchor(item)].add(gid)
        for row in own.iter_rows(named=True):
            charge=by_anchor.get((row.get('file_sha'),row.get('sheet'),row.get('row_no')))
            if charge is None or row.get('store') not in {store.name,*store.aliases}:
                continue
            record=result[charge['charge_id']]
            if row.get('counted') or row.get('linked'):
                record['source_facts'].append({k:row.get(k) for k in
                    ('file_sha','sheet','row_no','link_key','period','store','amount','counted','contribution')})
            record.setdefault('group_ids',[]).extend(sorted(by_source[lineage.anchor(row)]))
    trial._write_json(scratch.parent.parent / 'original-groups.json',original_groups)
    trial._write_json(scratch.parent.parent / 'formal_scope_availability.json',
        {'policy':inputs.get('baseline_policy','strict_complete'), 'scopes':availability,
         'unavailable_is_zero':False})
    for charge in charges:
        record=result[charge['charge_id']]
        record['source_facts'].sort(key=digest)
        lineage.finish(charge,record,original_groups)

    return result


def _b_results(charges, packages, allow_zero_equal):
    for packet in packages.values():
        packet['allow_zero_equal'] = bool(allow_zero_equal and packet.get('allow_zero_equal'))
    bases = trial.refund_bases(charges,packages)
    output = {}
    for charge, result in trial.allocate_bill_groups(charges,packages,bases):
        packet = packages.get(charge['tracking_no'])
        if result['status']=='allocated' and sum((_amount(a['amount']) for a in result['allocations']),Decimal(0)) != _amount(charge['amount']):
            raise InputError('B 分支逐笔金额不守恒：' + charge['charge_id'])
        output[charge['charge_id']] = {**result, 'packet_id':packet['packet_id'] if packet else '',
            'store_ids':packet['store_ids'] if packet else [],
            'erp_shop_keys':packet.get('erp_shop_keys',[]) if packet else [],
            'packet_issue':packet['issue'] if packet else 'no_shipment',
            'refund_basis':bases.get(charge['charge_id']),
            'allow_zero_equal':packet.get('allow_zero_equal',False) if packet else False}
    return output


def summarize(charges, results):
    totals = defaultdict(Decimal)
    counts = defaultdict(int)
    for charge in charges:
        amount = _amount(charge['amount']); record = results[charge['charge_id']]
        allocations = record['allocations']
        projected = sum((_amount(a['amount']) for a in allocations),Decimal(0))
        booked = _amount(record['source_booked_amount']) if 'source_booked_amount' in record else projected
        identified = sum((_amount(a['amount']) for a in allocations if a.get('identity_verified', True)),Decimal(0))
        covered = (abs(amount) if record.get('source_matched') else Decimal(0)) if 'source_booked_amount' in record else (min(abs(identified),abs(amount)) if amount*identified>0 else Decimal(0))
        sign = 'positive' if amount>0 else 'negative' if amount<0 else 'zero'
        counts['source_count'] += 1; counts[sign+'_count'] += 1
        counts['allocated_count'] += int(record['status']=='allocated')
        counts['group_matched_count'] += int(record['status']=='group_matched')
        counts['pending_count'] += int(record['status'] not in {'allocated','group_matched'})
        counts['projection_rounding_residue_count'] += int('source_booked_amount' in record and record['status']=='allocated' and abs(projected-booked)>Decimal('0.000000000001'))
        counts['overbooked_count'] += int(abs(booked)>abs(amount)+QUANTUM or amount*booked<0)
        counts['allocated_charge_violation_count'] += int(record['status']=='allocated' and abs(booked-amount)>QUANTUM)
        totals['source_net'] += amount; totals['source_abs'] += abs(amount)
        totals[sign+'_source_abs'] += abs(amount)
        totals['booked_net'] += booked; totals['booked_abs'] += abs(booked)
        totals['covered_abs'] += covered; totals[sign+'_covered_abs'] += covered
        totals['unallocated_abs'] += abs(amount)-covered
        totals['pending_net'] += amount-booked
        totals['overbooked_abs'] += max(abs(booked)-abs(amount),Decimal(0))
    value = {k:str(totals[k]) for k in ('source_net','source_abs','positive_source_abs','negative_source_abs',
        'booked_net','booked_abs','covered_abs','positive_covered_abs','negative_covered_abs','unallocated_abs','pending_net','overbooked_abs')}
    for prefix in ('','positive_','negative_'):
        denominator = totals[prefix+'source_abs']
        value[prefix+'coverage'] = str(totals[prefix+'covered_abs']/denominator) if denominator else None
    value.update({k:counts[k] for k in ('source_count','positive_count','negative_count','zero_count','allocated_count','group_matched_count','pending_count','overbooked_count','allocated_charge_violation_count','projection_rounding_residue_count')})
    value['conservation_delta'] = str(totals['source_net']-totals['booked_net']-totals['pending_net'])
    value['coverage_basis']=('original_counted_source_replay' if any('source_booked_amount' in r for r in results.values()) else 'candidate_allocated_charge')
    value['original_source_match_count']=sum(bool(r.get('source_matched')) for r in results.values())
    return value


def differences(charges, baseline, candidate, period):
    output=[]
    for charge in charges:
        left,right=baseline[charge['charge_id']],candidate[charge['charge_id']]
        maps=[];periods=[];children=[]
        for result in (left,right):
            values=defaultdict(Decimal);dates=defaultdict(set);child=defaultdict(set)
            for allocation in result['allocations']:
                target=lineage.key(allocation);values[target]+=_amount(allocation['amount'])
                if allocation.get('period'):dates[target].add(allocation['period'])
                if allocation.get('child_id'):child[target].add(allocation['child_id'])
            maps.append(values);periods.append(dates);children.append(child)
        known_a=left.get('attribution_status')=='single_source_actual_projection'
        known_b=right['status']=='allocated'
        for index,target in enumerate(sorted(set(maps[0])|set(maps[1])) or [('', '', '')]):
            av=maps[0][target] if known_a else None;bv=maps[1][target] if known_b else None
            output.append({**charge,'store_id':target[0],'order_id':target[1],'child_id':'','product_id':target[2],
                'posting_period':'','a_posting_periods':sorted(periods[0][target]),'b_posting_periods':sorted(periods[1][target]),
                'a_child_ids':sorted(children[0][target]),'b_child_ids':sorted(children[1][target]),'source_first':index==0,
                'a_amount':str(av) if av is not None else None,'b_amount':str(bv) if bv is not None else None,
                'delta':str(bv-av) if av is not None and bv is not None else None,
                'a_status':left['status'],'a_reason':left['reason'],'a_attribution_status':left.get('attribution_status'),
                'a_source_booked_amount':left.get('source_booked_amount'),'a_source_matched':left.get('source_matched',False),
                'a_group_ids':left.get('group_ids',[]),'b_status':right['status'],'b_reason':right['reason'],'b_method':right['method'],
                'packet_issue':right.get('packet_issue',''),'allow_zero_equal':right.get('allow_zero_equal',False),
                'store_ids':right.get('store_ids',[]) or sorted({v[0] for v in set(maps[0])|set(maps[1]) if v[0]}),
                'erp_shop_keys':right.get('erp_shop_keys',[])})
    return output


def run_bundle(bundle, target, progress=lambda *_:None):
    """Replay solely from the bundle. No live roots, current model or classification reads."""
    from .model.schema import Model
    bundle, target = Path(bundle), Path(target)
    inputs, fingerprint = frozen.verify(bundle)
    if inputs.get('execution_identity') != code_identity():
        raise InputError('冻结输入要求原引擎和实验代码版本，当前版本不符')
    target.mkdir(parents=True,exist_ok=False)
    model = Model.model_validate(_json(bundle / 'model.json'))
    if digest(json.loads(model.model_dump_json())) != inputs['model_hash']:
        raise InputError('冻结模型身份不符')
    catalog = _json(bundle / 'classification.json')
    if digest(catalog) != inputs['classification_hash']:
        raise InputError('冻结分类身份不符')
    deltas = _json(bundle / 'deltas.json')
    if digest(deltas) != inputs['delta_hash']:
        raise InputError('冻结增量身份不符')
    charges, order_frames, aliases, warnings = parse_sources(bundle,target / 'scratch',model,inputs,progress,retain_unidentified=True)
    children,controls,native = platform_bases(order_frames)
    headers,items,relations = feed_frames(bundle / 'feed',inputs,deltas,sorted({c['tracking_no'] for c in charges}),progress)
    for label,frame,required in [('发货头',headers,{'order_id','order_store_id'}),
                                ('发货商品',items,{'order_id','order_store_id','sub_order_id'})]:
        missing = required-set(frame.columns)
        if missing:
            raise InputError('实际包裹快照缺少'+label+'字段：'+'、'.join(sorted(missing)))
    packages = build_packages(headers,items,relations,inputs['mapping'],model,children,controls,native,progress)
    candidate = _b_results(charges,packages,inputs['allow_zero_equal'])
    baseline = formal_baseline(bundle,model,inputs,charges,aliases,target / 'scratch' / 'formal',progress)
    if set(baseline)!=set(candidate) or set(candidate)!={c['charge_id'] for c in charges}:
        raise InputError('实验分支源收费集合不一致')
    rows = differences(charges,baseline,candidate,inputs['period'])
    component_rows=lineage.components(charges,baseline,candidate,_json(target/'original-groups.json'))
    for row in component_rows:
        row['billing_month']=inputs['period']
        row['store_ids']=sorted({g['store_id'] for cid in row['charge_ids'] for g in baseline[cid]['allocations']
            if g.get('store_id')}|{s for cid in row['charge_ids'] for s in candidate[cid].get('store_ids',[])})
        row['erp_shop_keys']=sorted({s for cid in row['charge_ids'] for s in candidate[cid].get('erp_shop_keys',[])})
    trial._write_json(target/'comparison-components.json',component_rows)
    schema = {'store_ids':pl.List(pl.String),'erp_shop_keys':pl.List(pl.String),
              'a_amount':pl.String,'b_amount':pl.String,'delta':pl.String}
    lazy = pl.from_dicts(rows,infer_schema_length=None,schema_overrides=schema).lazy()
    # Apply the captured catalogue only; filters never recompute allocation weights.
    rows = store_classification.annotate(lazy,catalog).collect().to_dicts()
    group_frame=pl.from_dicts(component_rows,infer_schema_length=None,schema_overrides=schema).lazy()
    store_classification.annotate(group_frame,catalog).collect().write_parquet(target/'component-diff.parquet',compression='zstd')
    trial._write_json(target / 'charges.json',charges)
    trial._write_json(target / 'a.json',baseline)
    trial._write_json(target / 'b.json',candidate)
    trial._write_json(target / 'packets.json',packages)
    pl.from_dicts(rows,infer_schema_length=None,schema_overrides=schema).write_parquet(target / 'diff.parquet',compression='zstd')
    manifest = {'schema_version':SCHEMA_VERSION,'period':inputs['period'],'created_at':trial.now(),
        'input_fingerprint':fingerprint,'same_input':True,'baseline':'original_engine',
        'baseline_scope':{'metric_id':'freight_cost','rules':'frozen_model_unchanged',
                          'billing_pool':'canonical_all_captured_company_bill_files','historical_run_amounts':False,
                          'availability':_json(target / 'formal_scope_availability.json')},
        'a_coverage_basis':'original_source_counted_replay_not_per_product_attribution_or_published_history',
        'a_coverage_notice':'A金额覆盖表示原规则重放已计入的来源收费；逐商品归因需要独立组凭据。生产历史入账状态另行核验。',
        'comparison_grain':'nonoverlapping_source_components_store_parent_product',
        'source_component_count':len({r['component_id'] for r in component_rows}),
        'source_component_comparable_count':len({r['component_id'] for r in component_rows if r['comparable']}),
        'candidate':RULE_VERSION,'allow_zero_equal':inputs['allow_zero_equal'],
        'code_identity':code_identity(),'snapshot_id':inputs['snapshot_id'],
        'consumed_seq':inputs['consumed_seq'],'source_latest_seq':inputs['source_latest_seq'],
        'classification_version':catalog['version'],'classification':catalog,
        'stores':[{'id':s.id,'name':s.name,'platform':s.platform} for s in model.stores],
        'source_aliases':aliases,'warnings':warnings,'original_runs':inputs['runs'],
        'old_runs_used_for_calculation':False,'input_pending':False,
        'a_summary':summarize(charges,baseline),'b_summary':summarize(charges,candidate),
        'checks':{'same_source_charge_ids':True,'input_bundle_verified':True,
                  'candidate_each_allocated_charge_conserved':True,'unknown_sources_in_denominator':True},
        'changed_charge_count':len({r['charge_id'] for r in rows if _amount(r['delta']) or r['a_status']!=r['b_status']}),
        'notice':'同一冻结原始输入的 A/B 隔离提案。差额不是应补扣金额；未修改业务账、历史结账或实发。'}
    manifest['artifacts'] = [{'path':p.relative_to(target).as_posix(),'sha256':trial._hash_file(p)}
        for p in sorted(target.rglob('*')) if p.is_file() and 'scratch' not in p.relative_to(target).parts]
    trial._write_json(target / 'manifest.json',manifest)
    return manifest


class Jobs:
    """Shares the existing freight executor and lock; no second background worker."""
    def __init__(self, parent):
        self.parent = parent; self.root = parent.root; self.area = parent.area / 'experiments'
        self.lock = parent.lock

    def path(self, experiment_id):
        if not re.fullmatch('[0-9a-f]{32}',experiment_id or ''):
            raise InputError('实验编号无效')
        return self.area / experiment_id

    def read(self, experiment_id):
        with self.lock:
            file = self.path(experiment_id) / 'job.json'
            if not file.is_file():
                raise InputError('实验记录不存在')
            value = _json(file)
            if value['state'] in {'queued','running'} and value.get('session') != trial.SESSION:
                value.update(state='interrupted',error='服务重启前实验未完成；原输入包保留，可显式重放')
                trial._write_json(file,value)
            return {k:v for k,v in value.items() if k!='session'}

    def start(self, period, model, feed_root, names=None, *, replay_of='', evidence_parent_id='', allow_zero_equal=True):
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',period or ''):
            raise InputError('请选择有效月份')
        if replay_of and evidence_parent_id:
            raise InputError('重放与补证新版本不能混用')
        for source in (replay_of,evidence_parent_id):
            if source:
                previous,_path = self.manifest(source)
                if previous['period']!=period:
                    raise InputError('来源实验账期不一致')
                if replay_of and previous['code_identity']!=code_identity():
                    raise InputError('实验代码版本已变化，请使用原版本重放')
        with self.lock:
            # Coalesce only identical requests; an old failed result is never reused as success.
            request = {'period':period,'replay_of':replay_of,'evidence_parent_id':evidence_parent_id,
                       'allow_zero_equal':allow_zero_equal}
            for path in self.area.glob('*/job.json') if self.area.exists() else []:
                active = self.read(path.parent.name)
                if active['state'] in {'queued','running'} and active.get('request')==request:
                    return active
            identifier = uuid.uuid4().hex; path = self.path(identifier); path.mkdir(parents=True)
            value = {'experiment_id':identifier,'job_id':identifier,'period':period,'kind':'freight_experiment',
                'state':'queued','stage':'等待同输入 A/B 实验','created_at':trial.now(),
                'updated_at':trial.now(),'session':trial.SESSION,'request':request}
            trial._write_json(path / 'job.json',value)
            self.parent.pool.submit(self._run,value,model,Path(feed_root),names)
            return {k:v for k,v in value.items() if k!='session'}

    def _run(self, value, model, feed_root, names):
        value = dict(value); path = self.path(value['experiment_id']); request = value['request']
        def progress(stage,done=0,total=1):
            with self.lock:
                value.update(state='running',stage=stage,done=done,total=total,updated_at=trial.now())
                trial._write_json(path / 'job.json',value)
        try:
            if request['replay_of']:
                bundle = self.path(request['replay_of']) / 'inputs'
                import shutil
                frozen.verify(bundle)
                shutil.copytree(bundle,path / 'inputs')
            else:
                stores = [{'id':s.id,'name':(names or {}).get(s.id,s.name),'platform':s.platform} for s in model.stores]
                progress('完整固定原始输入、增量与模型')
                frozen.capture(self.root,feed_root,model,request['period'],path / 'inputs',
                               store_classification.resolve(stores),request['allow_zero_equal'])
            if request['evidence_parent_id']:
                _, parent_fingerprint = frozen.verify(self.path(request['evidence_parent_id']) / 'inputs')
                _, current_fingerprint = frozen.verify(path / 'inputs')
                if parent_fingerprint==current_fingerprint:
                    raise InputError('补证没有形成新的输入版本，不能申报补证收益')
            manifest = run_bundle(path / 'inputs',path / 'result',progress)
            value.update(state='ready',stage='同输入实验已验证',updated_at=trial.now(),
                input_fingerprint=manifest['input_fingerprint'],replay_of=request['replay_of'],
                result_manifest_sha=trial._hash_file(path / 'result' / 'manifest.json'),
                evidence_parent_id=request['evidence_parent_id'],
                comparison_kind='evidence_version' if request['evidence_parent_id'] else 'same_input_ab',
                cross_input_gain_claimed=False)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception('Freight experiment failed')
            value.update(state='failed',stage='实验未完成',error=str(exc),updated_at=trial.now())
        with self.lock:
            trial._write_json(path / 'job.json',value)

    def manifest(self, experiment_id):
        job = self.read(experiment_id)
        if job['state']!='ready':
            raise InputError('实验未完成，不能读取部分结果')
        path = self.path(experiment_id) / 'result'; manifest = _json(path / 'manifest.json')
        if not job.get('result_manifest_sha') or trial._hash_file(path / 'manifest.json')!=job['result_manifest_sha']:
            raise InputError('实验结果清单校验失败')
        if manifest.get('schema_version')!=SCHEMA_VERSION:
            raise InputError('旧实验逐费重建不作为金额差额依据；请审计原执行source/projection或以新版本试算')
        for record in manifest['artifacts']:
            file = path / record['path']
            if not file.resolve().is_relative_to(path.resolve()) or not file.is_file():
                raise InputError('实验结果留档校验失败')
            stat = file.stat()
            trial._checked(str(file.resolve()),stat.st_mtime_ns,stat.st_size,record['sha256'])
        _inputs,fingerprint = frozen.verify(self.path(experiment_id) / 'inputs',full=False)
        if fingerprint!=manifest['input_fingerprint']:
            raise InputError('实验结果与原始输入不一致')
        return manifest,path


def manager(root):
    parent = trial.manager(root)
    with parent.lock:
        if not hasattr(parent,'experiments'):
            parent.experiments = Jobs(parent)
        return parent.experiments


def _rows(manifest,path,store_id='',only_category='',changed_only=False):
    rows = pl.scan_parquet(path / 'diff.parquet')
    if store_id:
        if store_id not in {s['id'] for s in manifest['stores']}:
            raise InputError('请选择已登记店铺')
        rows = rows.filter((pl.col('store_id')==store_id) | pl.col('store_ids').list.contains(store_id))
    if only_category:
        if only_category not in {r['id'] for r in manifest['classification']['categories']} | {store_classification.UNMATCHED}:
            raise InputError('请选择有效店铺分类')
        rows = rows.filter(pl.col('store_category')==only_category)
    if changed_only:
        rows = rows.filter((pl.col('delta').cast(pl.Decimal(38,12))!=0) | (pl.col('a_status')!=pl.col('b_status')))
    return rows


def query(jobs, experiment_id, *, offset=0, limit=50, store_id='', only_category='', changed_only=False):
    job = jobs.read(experiment_id)
    if job['state']!='ready':
        return {'job':job,'result':None}
    manifest,path = jobs.manifest(experiment_id)
    rows = _rows(manifest,path,store_id,only_category,changed_only)
    catalog = manifest['classification']
    if only_category and only_category not in {r['id'] for r in catalog['categories']} | {store_classification.UNMATCHED}:
        raise InputError('请选择有效店铺分类')
    scope = {'experiment_id':experiment_id,'store_id':store_id,'only_category':only_category,'changed_only':changed_only,
             'input_fingerprint':manifest['input_fingerprint'],'classification_version':catalog['version']}
    return {'job':job,'result':{k:v for k,v in manifest.items() if k not in {'artifacts','classification'}},
        'rows':rows.slice(max(0,offset),min(500,max(1,limit))).collect().to_dicts(),
        'count':rows.select(pl.len()).collect().item(),
        'offset':max(0,offset),'scope':scope,'scope_fingerprint':digest(scope),
        'denominator_scope':'all_original_source_charges_including_unknown_stores',
        'summary_scope':'whole_experiment_unchanged_by_display_filters'}


def export(jobs, experiment_id, *, scope_fingerprint='', store_id='', only_category='', changed_only=False):
    page = query(jobs,experiment_id,limit=1,store_id=store_id,only_category=only_category,changed_only=changed_only)
    if page['result'] is None or not scope_fingerprint or page['scope_fingerprint']!=scope_fingerprint:
        raise InputError('实验导出范围或版本不一致')
    manifest,path = jobs.manifest(experiment_id)
    rows = _rows(manifest,path,store_id,only_category,changed_only)
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(['实验编号','输入校验值','收费记录','来源文件SHA','来源文件','工作表','原始行号','运单号',
        '原始收费（每笔首行）','店铺ID','主订单','子订单','宝贝ID','记账月份','A金额','B金额','B减A',
        'A状态','A原因','B状态','B原因','B方法','来源问题','包裹问题','零基数等分许可','冻结分类','分类版本'])
    def safe(value):
        if isinstance(value,str) and (value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')) or (value.isdigit() and len(value)>=15)):
            return "'"+value
        return value
    emitted = set()
    for row in rows.collect(engine='streaming').iter_rows(named=True):
        first = row['charge_id'] not in emitted; emitted.add(row['charge_id'])
        writer.writerow([safe(v) for v in [experiment_id,manifest['input_fingerprint'],row['charge_id'],row['file_sha'],
            row['file'],row['sheet'],row['row_no'],row['tracking_no'],_amount(row['amount']) if first else '',
            row['store_id'],row['order_id'],row['child_id'],row['product_id'],row['posting_period'],
            _amount(row['a_amount']) if row['a_amount'] is not None else '',
            _amount(row['b_amount']) if row['b_amount'] is not None else '',
            _amount(row['delta']) if row['delta'] is not None else '',row['a_status'],row['a_reason'],
            row['b_status'],row['b_reason'],row['b_method'],row['source_issue'],row['packet_issue'],
            row['allow_zero_equal'],row['store_category'],manifest['classification_version']]])
    return '\ufeff'+output.getvalue()


def _component_rows(manifest,path,store_id='',only_category=''):
    rows=pl.scan_parquet(path/'component-diff.parquet')
    if store_id:
        if store_id not in {s['id'] for s in manifest['stores']}:raise InputError('请选择已登记店铺')
        rows=rows.filter((pl.col('store_id')==store_id)|pl.col('store_ids').list.contains(store_id))
    if only_category:
        if only_category not in {r['id'] for r in manifest['classification']['categories']}|{store_classification.UNMATCHED}:
            raise InputError('请选择有效店铺分类')
        rows=rows.filter(pl.col('store_category')==only_category)
    return rows


def group_query(jobs,experiment_id,*,offset=0,limit=50,store_id='',only_category=''):
    job=jobs.read(experiment_id)
    if job['state']!='ready':return {'job':job,'result':None}
    manifest,path=jobs.manifest(experiment_id)
    rows=_component_rows(manifest,path,store_id,only_category)
    scope={'experiment_id':experiment_id,'store_id':store_id,'only_category':only_category,
        'comparison_grain':'source_component','input_fingerprint':manifest['input_fingerprint'],
        'classification_version':manifest['classification_version']}
    return {'job':job,'result':{'period':manifest['period'],'comparison_grain':'source_component'},
        'rows':rows.slice(max(0,offset),min(500,max(1,limit))).collect().to_dicts(),
        'count':rows.select(pl.len()).collect().item(),'scope':scope,'scope_fingerprint':digest(scope),
        'notice':'每收费和原projection记录仅进入一个连通组件；不同子单命名空间不作硬比，不推定利润或实发。'}


def group_export(jobs,experiment_id,*,scope_fingerprint='',store_id='',only_category=''):
    page=group_query(jobs,experiment_id,limit=500,store_id=store_id,only_category=only_category)
    if page['result'] is None or not scope_fingerprint or page['scope_fingerprint']!=scope_fingerprint:
        raise InputError('实验组件导出范围或版本不一致')
    manifest,path=jobs.manifest(experiment_id)
    # All target rows of a selected component stay together for conservation.
    selected=_component_rows(manifest,path,store_id,only_category).select('component_id').unique()
    rows=pl.scan_parquet(path/'component-diff.parquet').join(selected,on='component_id',how='semi').collect()
    stream=io.StringIO();writer=csv.writer(stream)
    writer.writerow(['实验编号','输入校验值','组件','收费数','原收费（组件首行）','原收费绝对额（组件首行）',
        '店铺ID','平台主单','商品ID','A实际投影金额','B分摊金额','B减A','可比较','待核原因','A原账期','B声明账期'])
    emitted=set()
    def safe(value):
        if isinstance(value,str) and (value.lstrip().startswith(('=','+','-','@')) or value.isdigit() and len(value)>=15):return "'"+value
        return value
    for row in rows.iter_rows(named=True):
        first=row['component_id'] not in emitted;emitted.add(row['component_id'])
        writer.writerow([safe(v) for v in [experiment_id,manifest['input_fingerprint'],row['component_id'],row['source_charge_count'],
            _amount(row['component_source_amount']) if first else '',_amount(row['component_source_abs']) if first else '',
            row['store_id'],row['order_id'],row['product_id'],
            _amount(row['a_amount']) if row['a_amount'] is not None else '',
            _amount(row['b_amount']) if row['b_amount'] is not None else '',
            _amount(row['delta']) if row['delta'] is not None else '',
            row['comparable'],row['reason'],';'.join(row['a_original_periods']),';'.join(row['b_declared_periods'])]])
    return '\ufeff'+stream.getvalue()


def main(argv=None):
    """Offline replay: python -m ledger.freight_experiments --bundle ... --output ..."""
    import argparse
    parser = argparse.ArgumentParser(description='离线重放同输入运费 A/B 实验，仅写隔离提案')
    parser.add_argument('--bundle',required=True)
    parser.add_argument('--output',required=True)
    args = parser.parse_args(argv)
    try:
        manifest = run_bundle(args.bundle,args.output)
    except (InputError,OSError,ValueError) as exc:
        print(json.dumps({'status':'failed','error':str(exc)},ensure_ascii=False))
        return 2
    print(json.dumps({k:manifest[k] for k in ('input_fingerprint','a_summary','b_summary','changed_charge_count')},ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
