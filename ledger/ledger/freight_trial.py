"""On-demand, immutable freight proposal jobs, separate from financial runs."""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import threading
import uuid

import polars as pl

from .freight_allocation import allocate, RULE_VERSION, REASONS, METHODS
from .engine.rules import _norm
from .freight_shipments import platform_bases, build_packages
from .freight_trial_inputs import capture, parse_sources, feed_frames, digest, InputError

SESSION=uuid.uuid4().hex
RULE_HASH=hashlib.sha256(b''.join((Path(__file__).parent/name).read_bytes() for name in
    ('freight_allocation.py','freight_shipments.py','freight_trial_inputs.py','freight_trial.py','order_lifecycle.py'))).hexdigest()
_managers={};_guard=threading.Lock()


def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name('.'+path.name+'-'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,default=str),encoding='utf-8');os.replace(temp,path)


def _hash_file(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


@lru_cache(maxsize=512)
def _checked(path,mtime,size,expected):
    if _hash_file(Path(path))!=expected:raise InputError('试算留档校验未通过，请重新生成')
    return True


def _batch(folder,rows,index):
    if not rows:return
    folder.mkdir(parents=True,exist_ok=True)
    schema={k:pl.String for k in ('basis_total','basis_unit','carrier','ship_time') if k in rows[0]}
    if 'store_ids' in rows[0]:schema['store_ids']=pl.List(pl.String)
    pl.from_dicts(rows,infer_schema_length=None,schema_overrides=schema).write_parquet(folder/f'{index:06d}.parquet',compression='zstd')


def _charge_time(charge):
    try:return datetime.fromisoformat(str(charge.get('scan_time') or '').replace('/','-'))
    except ValueError:return None


def refund_bases(charges,packages):
    """Bind credits to prior, successfully allocated charges of the same parcel.

    Input order and filename order are not chronology. Multiple originals are
    usable only when they agree on the complete target/factor map; no guessed
    original is selected. All positive/negative source rows remain separate.
    """
    credits=[c for c in charges if Decimal(c['amount'])<0 and not c['source_issue']]
    wanted={c['tracking_no'] for c in credits}
    originals=defaultdict(list)
    for charge in charges:
        if charge['tracking_no'] not in wanted or Decimal(charge['amount'])<=0 or charge['source_issue']:continue
        when=_charge_time(charge);packet=packages.get(charge['tracking_no'])
        if when is None or not packet or packet['issue']:continue
        result=allocate(charge['amount'],packet['members'],allow_zero_equal=packet.get('allow_zero_equal',False))
        if result['status']!='allocated':continue
        factors={a['target_key']:a['factor'] for a in result['allocations']}
        reference={k:charge[k] for k in ('charge_id','amount','scan_time','file_sha','file','sheet','row_no')}
        originals[charge['tracking_no']].append((when,factors,reference))
    bases={}
    for credit in credits:
        when=_charge_time(credit);packet=packages.get(credit['tracking_no'])
        if when is None or not packet or packet['issue']:continue
        eligible=[r for r in originals.get(credit['tracking_no'],[]) if r[0].tzinfo==when.tzinfo and r[0]<=when]
        if not eligible or len({tuple(sorted(r[1].items())) for r in eligible})!=1:continue
        eligible.sort(key=lambda r:(r[0],r[2]['charge_id']))
        bases[credit['charge_id']]={'tracking_no':credit['tracking_no'],'packet_id':packet['packet_id'],
            'factors':eligible[0][1],'original_charges':[r[2] for r in eligible]}
    return bases


def build(root,feed_root,model,period,target,progress=lambda *_:None,names=None):
    root, target=Path(root),Path(target)
    target.mkdir(parents=True,exist_ok=True)
    progress('固定输入版本',0,1)
    inputs,deltas=capture(root,model,period)
    _write_json(target/'inputs.json',inputs)
    shadow_root=root/'freight-shadow'
    charges,order_frames,aliases,warnings=parse_sources(root,shadow_root,model,inputs,progress)
    children,controls,native_rows=platform_bases(order_frames)
    del order_frames
    tracking=sorted({r['tracking_no'] for r in charges})
    headers,items,relations=feed_frames(feed_root,inputs,deltas,tracking,progress)
    packages=build_packages(headers,items,relations,inputs['mapping'],model,children,controls,native_rows,progress)
    del headers,items,relations,children,controls,native_rows,deltas
    # Persist normalized package evidence once; charge rows reference it.
    chunk=[];part=0
    for package in packages.values():
        chunk.append({'packet_id':package['packet_id'],'tracking_no':package['tracking_no'],
            'carrier':package['carrier'],'ship_time':package['ship_time'],
            'internal_order_ids':json.dumps(package['internal_order_ids']),
            'members_json':json.dumps(package['members'],ensure_ascii=False,default=str)})
        if len(chunk)>=5000:_batch(target/'packets',chunk,part);chunk=[];part+=1
    _batch(target/'packets',chunk,part)
    bases=refund_bases(charges,packages)
    rows=[];part=0
    charges.sort(key=lambda r:r['charge_id'])
    for index,charge in enumerate(charges):
        if index%5000==0:progress('按实际包裹试算运费',index,len(charges))
        package=packages.get(charge['tracking_no'])
        issue=charge['source_issue'] or (package['issue'] if package else '')
        basis=bases.get(charge['charge_id'])
        result=allocate(charge['amount'],package['members'] if package else [],issue=issue,
            allow_zero_equal=package.get('allow_zero_equal',False) if package else False,
            original_factors=basis['factors'] if basis else None)
        rows.append({**charge,'packet_id':package['packet_id'] if package else '',
            'store_ids':package['store_ids'] if package else [],
            'carrier':package['carrier'] if package else '',
            'status':result['status'],'reason':result['reason'],'method':result['method'],
            'basis_total':result.get('basis_total'),'basis_unit':result.get('basis_unit',''),
            'member_count':len(package['members']) if package else 0,
            'refund_basis_json':json.dumps(basis if result['status']=='allocated' else None,ensure_ascii=False),
            'allocations_json':json.dumps(result['allocations'],ensure_ascii=False)})
        if len(rows)>=5000:_batch(target/'charges',rows,part);rows=[];part+=1
    _batch(target/'charges',rows,part)
    stores=[{'id':s.id,'name':(names or {}).get(s.id,s.name),'platform':s.platform} for s in model.stores]
    manifest={'period':period,'created_at':now(),'rule_version':RULE_VERSION,'rule_hash':RULE_HASH,
        'schema_version':1,'input_pending':bool(inputs['source_latest_seq'] is not None and inputs['consumed_seq']<inputs['source_latest_seq']),
        'input_fingerprint':digest({k:v for k,v in inputs.items() if k!='captured_at'}),
        'input_captured_at':inputs.get('captured_at'),
        'snapshot_id':inputs['snapshot_id'],'consumed_seq':inputs['consumed_seq'],
        'source_latest_seq':inputs['source_latest_seq'],'stores':stores,'files':inputs['files'],
        'source_aliases':aliases,'warnings':warnings,'original_runs':inputs['runs'],
        'charge_count':len(charges),'packet_count':len(packages),'scope_basis':'freight_scan_month',
        'notice':'按运费账单月份试算；商品分摊结果不改原账和提成，不等于已确认应补扣。',
        'artifact_files':[]}
    for path in sorted(target.rglob('*.parquet')):
        sha=_hash_file(path)
        manifest['artifact_files'].append({'path':path.relative_to(target).as_posix(),'sha256':sha})
        stat=path.stat();_checked(str(path.resolve()),stat.st_mtime_ns,stat.st_size,sha)
    _write_json(target/'manifest.json',manifest)
    return manifest


def manager(root):
    key=str(Path(root).resolve())
    with _guard:
        if key not in _managers:_managers[key]=Jobs(Path(root))
        return _managers[key]


class Jobs:
    def __init__(self,root):
        self.root=root;self.area=root/'freight-shadow';self.lock=threading.RLock()
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='freight-shadow')

    def path(self,job_id):
        if not re.fullmatch(r'[a-f0-9]{32}',job_id or ''):raise InputError('试算记录编号无效')
        return self.area/'jobs'/job_id

    def read(self,job_id):
        path=self.path(job_id)/'job.json'
        if not path.is_file():raise InputError('试算记录不存在')
        value=json.loads(path.read_text(encoding='utf-8'))
        if value['state'] in {'queued','running'} and value.get('session')!=SESSION:
            value.update(state='interrupted',error='服务重启前的试算未完成，请重新生成',updated_at=now())
            _write_json(path,value)
        return {k:v for k,v in value.items() if k!='session'}

    def latest(self,period):
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',period or ''):raise InputError('请选择有效月份')
        path=self.area/'latest'/f'{period}.json'
        if not path.is_file():return {'job':None,'result':None}
        pointer=json.loads(path.read_text(encoding='utf-8'))
        return {'job':self.read(pointer['job_id']),
                'result':self.read(pointer['result_id']) if pointer.get('result_id') else None}

    def start(self,period,model,feed_root,names=None):
        with self.lock:
            previous=self.latest(period)
            if previous['job'] and previous['job']['state'] in {'queued','running'}:return previous['job']
            job_id=uuid.uuid4().hex;path=self.path(job_id);path.mkdir(parents=True)
            value={'job_id':job_id,'period':period,'state':'queued','stage':'等待试算','done':0,'total':0,
                'created_at':now(),'updated_at':now(),'session':SESSION,'rule_version':RULE_VERSION}
            _write_json(path/'job.json',value)
            _write_json(self.area/'latest'/f'{period}.json',{'job_id':job_id,'result_id':previous['result']['job_id'] if previous['result'] else ''})
            self.pool.submit(self._run,value,model,Path(feed_root),names)
            return {k:v for k,v in value.items() if k!='session'}

    def _run(self,value,model,feed_root,names):
        value=dict(value);path=self.path(value['job_id'])
        def progress(stage,done,total):
            with self.lock:
                value.update(state='running',stage=stage,done=done,total=total,updated_at=now())
                _write_json(path/'job.json',value)
        try:
            manifest=build(self.root,feed_root,model,value['period'],path/'result',progress,names)
            with self.lock:
                value.update(state='ready',stage='试算已生成',updated_at=now(),input_fingerprint=manifest['input_fingerprint'],
                    snapshot_id=manifest['snapshot_id'],consumed_seq=manifest['consumed_seq'],warnings=len(manifest['warnings']))
                _write_json(path/'job.json',value)
                _write_json(self.area/'latest'/f"{value['period']}.json",{'job_id':value['job_id'],'result_id':value['job_id']})
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception('Freight shadow failed job=%s',value['job_id'])
            with self.lock:
                value.update(state='failed',stage='试算未完成',error=str(exc),updated_at=now())
                _write_json(path/'job.json',value)

    def manifest(self,job_id):
        job=self.read(job_id)
        if job['state']!='ready':raise InputError('该次试算尚未完成，不能读取部分结果')
        path=self.path(job_id)/'result'
        manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('schema_version',1)!=1:raise InputError('该试算格式暂不支持，请使用原导出文件或重新生成')
        for record in manifest['artifact_files']:
            file=(path/record['path']).resolve()
            if not file.is_relative_to(path.resolve()) or not file.is_file():raise InputError('试算留档不完整，请重新生成')
            stat=file.stat();_checked(str(file),stat.st_mtime_ns,stat.st_size,record['sha256'])
        return manifest,path


def _query(manifest,path,store_id='',platform='',reason='',q=''):
    stores={s['id']:s for s in manifest['stores']}
    if store_id and store_id not in stores:raise InputError('请选择已登记店铺')
    if platform and platform not in {s['platform'] for s in stores.values()}:raise InputError('请选择已登记平台')
    query=pl.scan_parquet(str(path/'charges'/'*.parquet'),missing_columns='insert')
    if store_id:query=query.filter(pl.col('store_ids').list.contains(store_id))
    if platform:
        allowed=[s['id'] for s in stores.values() if s['platform']==platform]
        query=query.filter(pl.col('store_ids').list.eval(pl.element().is_in(allowed)).list.any())
    if reason:
        if reason=='allocated':query=query.filter(pl.col('status')=='allocated')
        elif reason in REASONS:query=query.filter(pl.col('reason')==reason)
        else:raise InputError('请选择有效问题组')
    if q:
        value=_norm(q)
        if not value:raise InputError('请输入有效运单号')
        query=query.filter(pl.col('tracking_no').str.contains(value,literal=True)
            |pl.col('allocations_json').str.contains(value,literal=True))
    return query


def _summary(query):
    money=pl.col('amount').cast(pl.Decimal(38,12))
    value=query.select(pl.len().alias('charge_count'),money.sum().alias('source_amount'),
        (pl.col('status')=='allocated').sum().alias('allocated_count'),
        money.filter(pl.col('status')=='allocated').sum().alias('allocated_amount'),
        (-money).filter((pl.col('status')=='allocated') & (money<0)).sum().alias('allocated_refund'),
        (pl.col('status')=='pending').sum().alias('pending_count'),
        money.filter(pl.col('status')=='pending').sum().alias('pending_amount'),
        money.filter((pl.col('status')=='pending') & (money>=0)).sum().alias('pending_expense'),
        (-money).filter((pl.col('status')=='pending') & (money<0)).sum().alias('pending_refund'),
        money.abs().filter(pl.col('status')=='pending').sum().alias('pending_abs_amount')).collect().row(0,named=True)
    return {k:format(v,'f') if isinstance(v,Decimal) else v for k,v in value.items()}


def query(jobs,job_id,*,store_id='',platform='',reason='',q='',offset=0,limit=30):
    manifest,path=jobs.manifest(job_id)
    base=_query(manifest,path,store_id,platform)
    summary=_summary(base)
    groups=base.group_by('reason','status').agg(pl.len().alias('count'),
        pl.col('amount').cast(pl.Decimal(38,12)).sum().alias('amount'),
        pl.col('amount').cast(pl.Decimal(38,12)).abs().sum().alias('abs_amount')).collect().to_dicts()
    for group in groups:
        group.update(id=group['reason'] or 'allocated',title=REASONS.get(group['reason'],'可分摊'),
                     amount=str(group['amount']),abs_amount=str(group['abs_amount']))
    groups.sort(key=lambda g:(g['status']=='allocated',-Decimal(g['abs_amount']),g['id']))
    rows=_query(manifest,path,store_id,platform,reason,q).sort('charge_id').slice(max(0,offset),min(100,max(1,limit))).collect().to_dicts()
    count=_query(manifest,path,store_id,platform,reason,q).select(pl.len()).collect().item()
    names={s['id']:s['name'] for s in manifest['stores']}
    for row in rows:
        row.pop('allocations_json',None);row.pop('refund_basis_json',None)
        row['stores']=[names.get(s,s) for s in row['store_ids']]
        row['reason_label']=REASONS.get(row['reason'],'');row['method_label']=METHODS.get(row['method'],'')
    scope={'job_id':job_id,'store_id':store_id,'platform':platform,'reason':reason,'q':q}
    return {'job_id':job_id,'period':manifest['period'],'created_at':manifest['created_at'],
        'input_captured_at':manifest.get('input_captured_at'),
        'rule_version':manifest['rule_version'],'input_fingerprint':manifest['input_fingerprint'],
        'rule_hash':manifest.get('rule_hash'),'rules_current':manifest.get('rule_hash')==RULE_HASH,'input_pending':manifest.get('input_pending',False),
        'snapshot_id':manifest['snapshot_id'],'consumed_seq':manifest['consumed_seq'],
        'summary':summary,'groups':groups,'rows':rows,'count':count,'offset':max(0,offset),
        'scope':scope,'scope_fingerprint':digest(scope),'warnings':manifest['warnings'],'notice':manifest['notice']}


def detail(jobs,job_id,charge_id,*,store_id='',platform=''):
    if not re.fullmatch(r'[0-9a-f]{32}',charge_id or ''):raise InputError('收费记录编号无效')
    manifest,path=jobs.manifest(job_id)
    rows=_query(manifest,path,store_id,platform).filter(pl.col('charge_id')==charge_id).collect().to_dicts()
    if len(rows)!=1:raise InputError('该范围内没有找到收费记录')
    row=rows[0];allocations=json.loads(row.pop('allocations_json'))
    row['refund_basis']=json.loads(row.pop('refund_basis_json',None) or 'null')
    packet=None
    if row['packet_id'] and (path/'packets').exists():
        found=pl.scan_parquet(str(path/'packets'/'*.parquet')).filter(pl.col('packet_id')==row['packet_id']).collect().to_dicts()
        if found:
            packet=found[0];packet['members']=json.loads(packet.pop('members_json'));packet['internal_order_ids']=json.loads(packet['internal_order_ids'])
            platforms={s['id']:s['platform'] for s in manifest['stores']}
            for member in packet['members']:
                period=manifest['period']
                if platforms.get(member['store_id'])=='pdd':
                    match=re.match(r'^(\d{6})-\d{15}',member['order_id'])
                    if match:
                        try:period=datetime.strptime(match[1],'%y%m%d').strftime('%Y-%m')
                        except ValueError:pass
                member['original_period']=period
    row.update(allocations=allocations,packet=packet,reason_label=REASONS.get(row['reason'],''),method_label=METHODS.get(row['method'],''))
    return {'job_id':job_id,'period':manifest['period'],'created_at':manifest['created_at'],
        'input_fingerprint':manifest['input_fingerprint'],'charge':row,'notice':manifest['notice']}


def export(jobs,job_id,*,store_id='',platform='',reason='',q='',scope_fingerprint=''):
    scope={'job_id':job_id,'store_id':store_id,'platform':platform,'reason':reason,'q':q}
    if digest(scope)!=scope_fingerprint:raise InputError('导出范围与页面不一致，请重新读取结果')
    manifest,path=jobs.manifest(job_id);output=io.StringIO();writer=csv.writer(output)
    writer.writerow(['账单月份','收费记录号','运单号','店铺候选','状态','分摊方法','来源运费（每笔仅首行）','本包分摊基数（每笔仅首行）','待核原因',
        '主订单号','平台子订单号','商品ID','权重类型','分摊权重','分摊比例','商品分摊运费','来源文件','工作表','留档行号','试算编号','输入校验值','冲减依据收费记录'])
    names={s['id']:s['name'] for s in manifest['stores']}
    def safe(v):
        if isinstance(v,str) and (v.lstrip().startswith(('=','+','-','@')) or v.startswith(('\t','\r','\n')) or (v.isdigit() and len(v)>=15)):return "'"+v
        return v
    for row in _query(manifest,path,store_id,platform,reason,q).sort('charge_id').collect().iter_rows(named=True):
        allocations=json.loads(row['allocations_json']) or [{}]
        basis=json.loads(row.get('refund_basis_json') or 'null') or {}
        originals='；'.join(r['charge_id'] for r in basis.get('original_charges',[]))
        for index,allocation in enumerate(allocations):
            writer.writerow([safe(v) for v in [manifest['period'],row['charge_id'],row['tracking_no'],'；'.join(names.get(s,s) for s in row['store_ids']),
                '可分摊' if row['status']=='allocated' else '待核',METHODS.get(row['method'],''),
                Decimal(row['amount']) if index==0 else None,Decimal(row['basis_total']) if index==0 and row['basis_total'] is not None else None,REASONS.get(row['reason'],''),allocation.get('order_id',''),
                allocation.get('child_id',''),allocation.get('product_id',''),'核定比例' if row['basis_unit']=='ratio' else '金额基数',allocation.get('basis_weight'),
                Decimal(allocation['factor']) if allocation.get('factor') is not None else None,
                Decimal(allocation['amount']) if allocation.get('amount') is not None else None,row['file'],row['sheet'],row['row_no'],job_id,manifest['input_fingerprint'],originals]])
    return '\ufeff'+output.getvalue()
