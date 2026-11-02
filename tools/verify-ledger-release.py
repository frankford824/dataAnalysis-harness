"""Read-only frozen-history protection and actual user-entry business readback."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from decimal import Decimal,InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import urllib.parse
import urllib.request


def canonical(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


def amount(value):
    if value is None or isinstance(value,bool):raise ValueError('Missing or invalid financial amount')
    try:result=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError) as error:raise ValueError('Invalid financial amount') from error
    if not result.is_finite():raise ValueError('Non-finite financial amount')
    return result


def equal_amount(first,second):
    # Same six-place accounting closure as the saved income contract; display
    # precision must never permit a cent-level discrepancy.
    return amount(first).quantize(Decimal('.000001'))==amount(second).quantize(Decimal('.000001'))


def mapping(value,label):
    if not isinstance(value,dict):raise ValueError(label+' must be an object')
    return value


def string_list(value,label):
    if not isinstance(value,list) or any(not isinstance(item,str) for item in value):
        raise ValueError(label+' must contain exact string elements')
    return value


def sha256(value,label):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise ValueError(label+' has no SHA256 identity')
    return value


def source_manifest(value):
    if value is None:return None
    value=mapping(value,'Saved source manifest')
    string_list(value.get('template_ids'),'Saved source templates')
    artifacts=value.get('artifacts')
    if not isinstance(artifacts,list):raise ValueError('Saved source artifacts must be a list')
    for artifact in artifacts:
        artifact=mapping(artifact,'Saved source artifact')
        if not isinstance(artifact.get('path'),str) or not artifact['path']:
            raise ValueError('Saved source artifact path is missing')
        sha256(artifact.get('sha'),'Saved source artifact')
    return value


def saved_run(root,run_id):
    """Independent full saved input, including the active confirmed overlay.

    This deliberately does not read run_report_slice: that cache is not the
    authority used to certify an HTTP claim about a booked amount.
    """
    if type(run_id) is not int or run_id<=0:raise ValueError('Invalid authoritative run identity')
    root=Path(root)
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ledger'))
    from ledger.snapshot_store import resolve
    with readonly(root/'workspace.db') as db:
        db.row_factory=sqlite3.Row
        db.execute('BEGIN')
        row=db.execute('''SELECT r.*,coalesce(mf.result_json,r.result) source_result
          FROM run r LEFT JOIN period p ON p.store_id=r.store_id AND p.period=r.period
          LEFT JOIN manual_finance mf ON mf.run_id=r.id AND p.state='closed' AND p.run_id=r.id
          WHERE r.id=?''',(run_id,)).fetchone()
        if row is None or not row['evidence_ready'] or row['evidence_error']:
            raise ValueError('Known net has no completed authoritative saved run')
        payload=mapping(json.loads(resolve(db,row['source_result'])),'Authoritative saved result')
        proof=(db.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(run_id,)).fetchone()
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_run_proof'").fetchone() else None)
        manifest=source_manifest(json.loads(proof[0])) if proof else None
        return {'store_id':row['store_id'],'period':row['period'],'run_id':row['id'],
                'payload':payload,'manifest':manifest,'root':root}


def saved_company(root,period):
    """Read the same legacy frozen-pay criterion as the backend, without writes."""
    try:
        with readonly(Path(root)/'workspace.db') as db:
            db.row_factory=sqlite3.Row
            db.execute('BEGIN')
            frozen=db.execute('SELECT * FROM automation_overhead_close WHERE period=? ORDER BY rowid DESC LIMIT 1',(period,)).fetchone()
            has_closed=bool(db.execute("SELECT 1 FROM period WHERE period=? AND state='closed' LIMIT 1",(period,)).fetchone())
            closed=db.execute("""SELECT p.store_id,p.period,p.run_id,l.amount,r.engine,r.model_revision,r.input_fingerprint
                FROM period p JOIN run_labor l ON l.run_id=p.run_id JOIN run r ON r.id=p.run_id
                WHERE p.period=? AND p.state='closed' ORDER BY p.store_id""",(period,)).fetchall()
            allocations=[dict(row) for row in closed]
            frozen_value=None if frozen is None else {'input_sha':frozen['input_sha'],'total':frozen['total'],
                'basis':json.loads(frozen['basis_json']),'shares':json.loads(frozen['shares_json']),'at':frozen['at']}
            legacy=not frozen and any(amount(row['amount'])!=0 for row in closed)
            unknown=bool(frozen and has_closed and any(row.get('revenue') is None for row in frozen_value['basis']))
            return {'period':period,'has_frozen_basis':bool(frozen),'closed_allocations':allocations,
                    'frozen':frozen_value,'required_reason':'overhead_frozen_basis_unverified' if unknown else
                    'overhead_legacy_frozen_basis' if legacy else None}
    except sqlite3.Error as error:
        raise ValueError('Independent company finance metadata is unavailable') from error


def income_artifact(record,run_id,suffix,declared_sha,*,required=False):
    """The first M3 manifest wins over a later seal or archive registration."""
    if declared_sha is None:
        if required:raise ValueError('Required income source artifact has no identity')
        return None
    declared_sha=sha256(declared_sha,'Income source artifact')
    from ledger.run_artifacts import path as artifact_path,verified_path
    root=Path(record['root']);path=artifact_path(root,run_id).with_suffix(suffix)
    manifest=source_manifest(record.get('manifest'))
    expected=declared_sha
    if manifest is not None:
        relative=path.relative_to(root).as_posix()
        matches=[item['sha'] for item in manifest['artifacts'] if item['path']==relative]
        if len(matches)!=1 or matches[0]!=declared_sha:
            raise ValueError('Income facts differ from the original run proof')
        expected=matches[0]
    verified=verified_path(root,run_id,suffix,expected_sha=expected)
    if verified is None:raise ValueError('Income source artifact is missing, unregistered or changed')
    return verified


def verify_income_source(evidence,store,period,run_id,source_lookup):
    if source_lookup is None:raise ValueError('Independent net requires an authoritative source lookup')
    evidence=mapping(evidence,'Income evidence')
    if evidence.get('contract')!='operations-income-1688.v1' or evidence.get('status') not in {'net_only','split_verified'}:
        raise ValueError('Unknown direct-net evidence contract')
    record=mapping(source_lookup(run_id),'Authoritative source');payload=mapping(record.get('payload'),'Saved source payload')
    if (record.get('store_id')!=store or record.get('period')!=period or record.get('run_id')!=run_id
            or payload.get('platform')!='alibaba1688'):
        raise ValueError('Known net source belongs to another store/platform/period')
    statement=payload.get('statement')
    if not isinstance(statement,list) or any(not isinstance(row,dict) for row in statement):
        raise ValueError('Saved statement is not a valid source')
    nodes={row.get('id'):row for row in statement}
    if len(nodes)!=len(statement):raise ValueError('Saved statement has duplicate identities')
    receipt=nodes.get('n_receipt',{})
    if receipt.get('available') is not True:raise ValueError('Known net source is not available')
    net=amount(receipt.get('value'))
    inputs=mapping(payload.get('calculation_inputs'),'Saved calculation inputs')
    totals=mapping(inputs.get('metric_totals'),'Saved metric totals')
    unavailable=string_list(inputs.get('unavailable_metrics',[]),'Saved unavailable metrics')
    if not equal_amount(totals.get('trade_receipt_1688'),net) or 'trade_receipt_1688' in unavailable:
        raise ValueError('Known net does not match its saved income metric')
    identity={'run_id':run_id,'store_id':store,'period':period,'platform':'alibaba1688',
              'statement_receipt':{key:receipt.get(key) for key in ('id','value','available')}}
    proof=mapping(evidence.get('proof'),'Income proof');source=mapping(proof.get('net_source'),'Known-net source')
    source_identity={key:source.get(key) for key in identity}
    if (source.get('kind')!='saved_1688_net_statement' or source.get('result_sha')!=canonical(identity)
            or canonical(source_identity)!=canonical(identity) or not equal_amount(evidence.get('net_income'),net)):
        raise ValueError('Direct net was self-declared or differs from the saved source')
    refunds={'trade_refund','trade_refund_douyin','trade_refund_jd','trade_refund_pdd'}
    absent=mapping(source.get('refund_not_applicable'),'Refund applicability proof')
    if set(string_list(absent.get('metric_ids'),'Refund applicability metrics'))!=refunds:
        raise ValueError('Incomplete refund non-applicability proof')
    refund=nodes.get('n_refund')
    if refund and (refund.get('available',True) is not True or amount(refund.get('value'))!=0):
        raise ValueError('Saved refund contradicts direct-net source')
    if absent.get('method')=='explicit_not_applicable':
        if not refund or refund.get('applicable') is not False:raise ValueError('Refund applicability source mismatch')
    elif absent.get('method')=='inapplicable_metrics':
        if not refunds.issubset(set(string_list(inputs.get('inapplicable_metrics'),'Saved inapplicable metrics'))):
            raise ValueError('Refund metrics are not inapplicable')
    else:raise ValueError('Unknown refund applicability method')
    contract=mapping(source.get('source_contract'),'Income source contract');manifest=source_manifest(record.get('manifest'))
    if manifest is not None and (manifest.get('store_id')!=store or manifest.get('period')!=period):
        raise ValueError('Captured income source belongs to another store/period')
    if (contract.get('template_id')!='alibaba1688_settlement_in_v1' or contract.get('source_id')!='settlement'
            or contract.get('metric_id')!='trade_receipt_1688' or source.get('metric_id')!='trade_receipt_1688'
            or not equal_amount(source.get('metric_total'),net)):
        raise ValueError('Direct net source contract differs from its saved metric')
    if contract.get('method')=='captured_template_allowlist':
        if (manifest is None or 'alibaba1688_settlement_in_v1' not in manifest['template_ids']
                or manifest.get('store_id')!=store or manifest.get('period')!=period
                or sha256(contract.get('model_sha'),'Income model')!=sha256(manifest.get('model_sha'),'Captured model')):
            raise ValueError('Direct net has no captured source-template contract')
    elif contract.get('method')!='sealed_source_template_allowlist':
        raise ValueError('Direct net source template is not approved')
    elif contract.get('facts_sha')!=proof.get('facts_sha'):
        raise ValueError('Direct net source contract has a different facts identity')
    facts_required=evidence['status']=='split_verified' or contract.get('method')=='sealed_source_template_allowlist'
    path=income_artifact(record,run_id,'.parquet',proof.get('facts_sha'),required=facts_required)
    income_artifact(record,run_id,'.operations.parquet',proof.get('projection_sha'))
    if facts_required:
        import polars as pl
        try:facts=pl.read_parquet(path)
        except (OSError,pl.exceptions.PolarsError) as error:raise ValueError('Income source artifact is unreadable') from error
        required={'metric_id','source_id','template_id','major','subject','store','period','link_key','counted','amount','contribution','file_sha','sheet','row_no'}
        if not required.issubset(facts.columns):raise ValueError('Income source artifact lacks original fields')
        booked=[row for row in facts.filter(pl.col('metric_id')=='trade_receipt_1688').to_dicts() if row.get('counted') is True]
        if not booked and net!=0:raise ValueError('Income source has no actual booked rows')
        gross=Decimal(0);returned=Decimal(0)
        for row in booked:
            if (row.get('source_id')!='settlement' or row.get('template_id')!='alibaba1688_settlement_in_v1'
                    or row.get('major')!='trade_receipt_1688'
                    or row.get('store')!=(payload.get('accounting_store') or payload.get('store')) or row.get('period')!=period
                    or not isinstance(row.get('sheet'),str) or not row['sheet']
                    or type(row.get('row_no')) is not int or row['row_no']<1):
                raise ValueError('Income source identity differs from its saved run')
            sha256(row.get('file_sha'),'Income original file')
            if evidence['status']=='split_verified':
                value=amount(row.get('contribution'));original=amount(row.get('amount'));subject=row.get('subject')
                if isinstance(subject,str) and subject.strip()=='订单收入' and value>=0 and original>=0:gross+=value
                elif isinstance(subject,str) and subject.strip()=='订单售后退款' and value<=0 and original<=0:returned-=value
                else:raise ValueError('Unknown source subject cannot prove gross/refund split')
        if evidence['status']=='split_verified' and (not equal_amount(gross-returned,net) or
                not equal_amount(evidence.get('receipts'),gross) or not equal_amount(evidence.get('refunds'),returned)):
            raise ValueError('Gross/refund values differ from original booked source rows')
    if evidence['status']=='net_only' and any(evidence.get(name) is not None for name in ('receipts','refunds')):
        raise ValueError('Net-only evidence cannot invent a gross/refund split')
    return net


def digest(row):
    return hashlib.sha256(json.dumps(row,ensure_ascii=False,default=str,sort_keys=True).encode()).hexdigest()


def readonly(path):
    return _readonly(path)


@contextmanager
def _readonly(path):
    connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:yield connection
    finally:connection.close()


def read_json(url):
    with urllib.request.urlopen(url,timeout=30) as response:
        body=response.read(8*1024*1024+1)
    if len(body)>8*1024*1024:raise ValueError('Readback response is not bounded')
    return json.loads(body)


def immutable_rows(db,table):
    from ledger.snapshot_store import resolve
    cursor=db.execute('SELECT * FROM '+table)
    names=[column[0] for column in cursor.description]
    def identity(row):
        if table=='automation_overhead_close':
            return json.dumps([row[names.index('period')],row[names.index('input_sha')]],separators=(',',':'))
        return str(row[0])
    return {identity(row):digest(tuple(resolve(db,value) if name.endswith('_json') and isinstance(value,str) else value
        for name,value in zip(names,row))) for row in cursor}


def snapshot(root,reader=read_json):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ledger'))
    from ledger.snapshot_store import resolve
    with readonly(root/'workspace.db') as db:
        db.execute('BEGIN')
        closed={f'{s}:{p}':{'run':r,'result_sha':digest(json.loads(resolve(db,payload)))} for s,p,r,payload in db.execute(
            "SELECT p.store_id,p.period,p.run_id,r.result FROM period p JOIN run r ON p.run_id=r.id WHERE p.state='closed'")}
        workspace_immutable={table:immutable_rows(db,table) for table in ('manual_finance','run_labor')}
        company_table=bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_overhead_close'").fetchone())
        workspace_immutable['automation_overhead_close']=immutable_rows(db,'automation_overhead_close') if company_table else {}
        workspace_schema={'automation_overhead_close':'present' if company_table else 'legacy_absent'}
        cost_table=bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='period_cost_input'").fetchone())
        workspace_immutable['period_cost_input']=immutable_rows(db,'period_cost_input') if cost_table else {}
        workspace_schema['period_cost_input']='present' if cost_table else 'legacy_absent'
    with readonly(root/'commission/registry.db') as db:
        db.execute('BEGIN')
        immutable={table:immutable_rows(db,table) for table in ('payout_confirmation','settlement','profit_exclusion')}
    return dict(schema=2,version=reader('http://127.0.0.1:8000/api/version'),closed=closed,
                workspace_immutable=workspace_immutable,workspace_schema=workspace_schema,immutable=immutable)


def compare_history(baseline,current,code_sha):
    required={'workspace_immutable':{'manual_finance','run_labor','automation_overhead_close','period_cost_input'},
              'immutable':{'payout_confirmation','settlement','profit_exclusion'}}
    for snapshot_value in (baseline,current):
        if snapshot_value.get('schema')!=2 or not isinstance(snapshot_value.get('closed'),dict):
            raise ValueError('Frozen-history receipt lacks required financial layers')
        for section,tables in required.items():
            if not isinstance(snapshot_value.get(section),dict) or not tables.issubset(snapshot_value[section]):
                raise ValueError('Frozen-history receipt lacks required financial layers')
        company_status=(snapshot_value.get('workspace_schema') or {}).get('automation_overhead_close')
        company_rows=snapshot_value['workspace_immutable']['automation_overhead_close']
        if company_status not in {'present','legacy_absent'} or not isinstance(company_rows,dict) or (company_status=='legacy_absent' and company_rows):
            raise ValueError('Frozen-history receipt lacks explicit company basis metadata')
        cost_status=(snapshot_value.get('workspace_schema') or {}).get('period_cost_input')
        cost_rows=snapshot_value['workspace_immutable']['period_cost_input']
        if cost_status not in {'present','legacy_absent'} or not isinstance(cost_rows,dict) or (cost_status=='legacy_absent' and cost_rows):
            raise ValueError('Frozen-history receipt lacks explicit period cost input metadata')
    if (baseline['workspace_schema']['automation_overhead_close']=='present'
            and current['workspace_schema']['automation_overhead_close']!='present'):
        raise ValueError('Frozen financial history changed: company basis table was removed')
    if (baseline['workspace_schema']['period_cost_input']=='present'
            and current['workspace_schema']['period_cost_input']!='present'):
        raise ValueError('Frozen financial history changed: period cost input table was removed')
    if current['version'].get('version')!=code_sha:raise ValueError('Live version differs from acceptance receipt')
    changed={k:[baseline['closed'].get(k),current['closed'].get(k)] for k in set(baseline['closed'])|set(current['closed'])
             if current['closed'].get(k)!=baseline['closed'].get(k)}
    records={}
    for section in ('workspace_immutable','immutable'):
        for table,previous in baseline[section].items():
            after=current[section].get(table,{})
            records[table]=[key for key in set(previous)|set(after) if previous.get(key)!=after.get(key)]
    if changed or any(records.values()):
        raise ValueError('Frozen financial history changed: '+json.dumps({'closed':changed,'records':records}))
    return {'changed_closed':0,'changed_financial_records':0}


def company_scope(month,report,period,company_source_lookup=None):
    """The independently requested monthly workflow fixes the company scope."""
    stores=month.get('stores')
    if not isinstance(stores,list) or not stores or any(not isinstance(row,dict) for row in stores):
        raise ValueError('Monthly workflow has no explicit operating-store scope')
    identities=[row.get('store_id') for row in stores]
    if any(not isinstance(store,str) or not store for store in identities) or len(set(identities))!=len(identities):
        raise ValueError('Monthly workflow store scope is duplicated or missing')
    statuses=('completed','processing','needs_decision')
    counts={key:0 for key in statuses};counts['total']=len(stores);counts['waiting']=0
    required=set();excluded=set()
    for row in stores:
        if row.get('period')!=period or row.get('status') not in statuses or not isinstance(row.get('stage'),str):
            raise ValueError('Monthly workflow store status/period is invalid')
        closed=row.get('stage') in {'closed','excluded'}
        if (row['status']=='completed')!=closed:
            raise ValueError('A waiting or unfinished store was represented as completed')
        if row.get('stage')=='excluded':excluded.add(row['store_id'])
        else:required.add(row['store_id'])
        counts[row['status']]+=1;counts['waiting']+=bool(row.get('waiting'))
        if row.get('waiting') and row['status']=='completed':raise ValueError('Waiting stores cannot count as completed')
    declared=mapping(month.get('counts'),'Monthly workflow counts')
    for key,value in counts.items():
        if type(declared.get(key)) is not int or declared[key]!=value:
            raise ValueError('Monthly workflow counts differ from its actual store statuses')
    overhead=mapping(month.get('overhead'),'Monthly company basis')
    finance=overhead.get('finance_decision')
    groups=month.get('decision_groups')
    if not isinstance(groups,list) or any(not isinstance(group,dict) for group in groups):
        raise ValueError('Monthly company decisions are invalid')
    company_groups=[group for group in groups if group.get('kind')=='company_finance']
    if finance is not None:
        finance=mapping(finance,'Company finance decision')
        if (finance.get('kind')!='company_finance' or finance.get('scope')!='company_month'
                or finance.get('period')!=period or finance.get('role')!='company_finance'
                or finance.get('reason_code') not in {'overhead_legacy_frozen_basis','overhead_frozen_input_changed','overhead_frozen_basis_unverified'}
                or finance.get('actions')!=[] or overhead.get('status')!='needs_decision'
                or len(company_groups)!=1):
            raise ValueError('Company finance decision has no valid monthly contract')
        group=company_groups[0]
        if (type(group.get('count')) is not int or group['count']!=1 or group.get('store_ids')!=identities
                or any(group.get(key)!=value for key,value in finance.items())):
            raise ValueError('Company finance card differs from its company basis')
    elif company_groups:
        raise ValueError('Company finance card has no underlying company decision')
    if type(declared.get('company_finance_decisions')) is not int or declared['company_finance_decisions']!=int(finance is not None):
        raise ValueError('Monthly company finance count differs from its decision')
    finance_only=finance is not None and all(row['status']=='completed' or row['stage']=='company_finance' for row in stores)
    if type(month.get('company_finance_only')) is not bool or month['company_finance_only']!=finance_only:
        raise ValueError('Monthly company finance scope differs from its actual decision')
    if company_source_lookup is not None:
        independent=mapping(company_source_lookup(period),'Independent company finance source')
        if (independent.get('period')!=period or type(independent.get('has_frozen_basis')) is not bool
                or independent.get('required_reason') not in {None,'overhead_legacy_frozen_basis','overhead_frozen_basis_unverified'}
                or not isinstance(independent.get('closed_allocations'),list) or 'frozen' not in independent
                or independent['has_frozen_basis']!=(independent['frozen'] is not None)):
            raise ValueError('Independent company finance metadata is incomplete')
        required_reason=independent['required_reason']
        if required_reason and (finance is None or finance.get('reason_code')!=required_reason):
            raise ValueError('Independent company finance decision is missing or changed')
        if finance is not None:
            if finance.get('frozen')!=independent['frozen'] or finance.get('closed_allocations')!=independent['closed_allocations']:
                raise ValueError('Company finance frozen records differ from their independent saved source')
            if not required_reason and finance.get('reason_code') in {'overhead_legacy_frozen_basis','overhead_frozen_basis_unverified'}:
                raise ValueError('Company finance reason differs from its independent saved source')
    expected_status='needs_decision' if counts['needs_decision'] or finance is not None else 'processing' if counts['processing'] else 'completed'
    if month.get('status')!=expected_status:raise ValueError('Monthly workflow status differs from its store statuses')
    scope=mapping(report.get('scope'),'Business report scope')
    if (scope.get('period')!=period or scope.get('level')!='store' or scope.get('managed')!='all'
            or any(scope.get(key) for key in ('platform','store_id','team_id','person_id','product_id'))):
        raise ValueError('User-entry report is filtered rather than a complete company scope')
    reported={row.get('store_id') for row in report.get('rows',[]) if isinstance(row,dict)}
    if reported!=required or reported & excluded:
        raise ValueError('Business report differs from the monthly operating-store scope')
    if 'total' in report and (type(report['total']) is not int or report['total']!=len(report.get('rows',[]))):
        raise ValueError('A partial or paginated company report cannot certify a release')


def business_readback(base,period,code_sha,reader=read_json,source_lookup=None,expected_source_sha=None,company_source_lookup=None):
    parsed=urllib.parse.urlsplit(base)
    if parsed.scheme not in {'http','https'} or not parsed.netloc or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('An explicit user-entry HTTP(S) base URL is required')
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',period):raise ValueError('Explicit readback period required')
    base=base.rstrip('/')
    served_version=mapping(reader(base+'/api/version'),'User-entry version')
    if served_version.get('version')!=code_sha:raise ValueError('User entry still serves another version')
    if expected_source_sha is not None:
        expected_source_sha=sha256(expected_source_sha,'Accepted Python source')
        if served_version.get('source_code_sha')!=expected_source_sha:
            raise ValueError('User entry serves different Python source bytes under the same version')
        if served_version.get('approved_source_code_sha')!=expected_source_sha or served_version.get('source_code_status')!='approved':
            raise ValueError('User entry has no healthy approved runtime source identity')
    month=reader(base+'/api/automation/months/'+period)
    report=reader(base+'/api/operations/month?'+urllib.parse.urlencode({'period':period,'compare':'false','limit':200}))
    if month.get('period')!=period or report.get('period')!=period or not report.get('input_sha') or not report.get('scope_sha'):
        raise ValueError('Business readback has no matching period/version identity')
    if not report.get('run_refs') or not report.get('rows'):
        raise ValueError('An empty business scope cannot certify a release')
    company_scope(month,report,period,company_source_lookup)
    primitives=('receipts','refunds','net_income','goods_cost','promotion_cost','platform_cost','freight_cost','aftersales_cost','labor_cost')
    references={row.get('store_id'):row for row in report['run_refs']}
    rows={row.get('store_id'):row for row in report['rows']}
    if (not all(references) or len(references)!=len(report['run_refs']) or len(rows)!=len(report['rows'])
            or set(references)!=set(rows)):
        raise ValueError('User-entry store scope is duplicated, filtered or incomplete')
    actual_runs=0
    for store,reference in references.items():
        run=reference.get('run_id');row=rows[store]
        if run is None:
            if row.get('run_ids') or any(row.get('metrics',{}).get(name) is not None for name in primitives):
                raise ValueError('A missing run was represented as known business amounts')
        elif type(run) is not int or run<=0 or row.get('run_ids')!=[run]:
            raise ValueError('Business rows do not reference their actual saved run')
        else:actual_runs+=1
    month_rows={row['store_id']:row for row in month['stores']}
    for store,reference in references.items():
        if reference.get('run_id')!=month_rows[store].get('run_id'):
            raise ValueError('Business run differs from the current monthly workflow')
    if actual_runs==0:raise ValueError('Placeholder runs cannot certify an empty business scope')
    income_evidence=mapping(report.get('income_evidence') or {},'Income evidence scope')
    if set(income_evidence)-set(rows):raise ValueError('Income evidence belongs to another business scope')
    authoritative={}
    if source_lookup is not None:
        for store,reference in references.items():
            if reference.get('run_id') is not None:
                record=mapping(source_lookup(reference['run_id']),'Authoritative source')
                if (record.get('store_id')!=store or record.get('period')!=period or record.get('run_id')!=reference['run_id']):
                    raise ValueError('Business run identity differs from its saved source')
                authoritative[reference['run_id']]=record
    lookup=(lambda run:authoritative[run]) if source_lookup is not None else None
    independent_net=set()
    for store,row in rows.items():
        values=mapping(row.get('metrics'),'Business metrics')
        income=income_evidence.get(store)
        actual=authoritative.get(references[store].get('run_id'))
        is_1688=actual is not None and mapping(actual.get('payload'),'Saved source payload').get('platform')=='alibaba1688'
        if is_1688 and values.get('net_income') is not None and (not isinstance(income,dict) or income.get('status') not in {'net_only','split_verified'}):
            raise ValueError('Known 1688 net cannot hide its independent source evidence')
        if isinstance(income,dict) and income.get('status') in {'net_only','split_verified'}:
            net=verify_income_source(income,store,period,references[store]['run_id'],lookup)
            if not equal_amount(values.get('net_income'),net):raise ValueError('Row net income differs from verified source')
            if income['status']=='split_verified' and any(not equal_amount(values.get(name),income.get(name)) for name in ('receipts','refunds')):
                raise ValueError('Gross/refund row differs from verified booked source')
            if income['status']=='net_only' and any(values.get(name) is not None for name in ('receipts','refunds','refund_rate')):
                raise ValueError('Net-only store invented a gross receipt/refund metric')
            independent_net.add(store)
    for name in primitives:
        amounts=[row.get('metrics',{}).get(name) for row in rows.values()]
        aggregate=report.get('summary',{}).get(name)
        if any(value is None for value in amounts):
            if aggregate is not None:raise ValueError('Unknown store amount was zeroed in company summary')
        else:
            expected=sum((amount(value) for value in amounts),Decimal(0))
            if aggregate is None or not equal_amount(aggregate,expected):
                raise ValueError('Company summary differs from the complete store scope: '+name)
    equations={
        'net_income':(('receipts',1),('refunds',-1)),
        'gross_profit':(('net_income',1),('goods_cost',-1)),
        'profit':(('net_income',1),('goods_cost',-1),('promotion_cost',-1),('platform_cost',-1),('freight_cost',-1),('aftersales_cost',-1)),
        'profit_after_labor':(('profit',1),('labor_cost',-1)),
    }
    checked=0
    for index,values in enumerate([report.get('summary',{}),*(row.get('metrics',{}) for row in report['rows'])]):
        for key,parts in equations.items():
            if any(values.get(name) is None for name,_ in parts):
                if key=='net_income' and values.get(key) is not None:
                    if (index==0 and independent_net) or (index>0 and report['rows'][index-1]['store_id'] in independent_net):
                        checked+=1;continue
                if values.get(key) is not None:raise ValueError('Unknown business inputs became a derived amount')
                continue
            expected=sum((amount(values[name])*sign for name,sign in parts),Decimal(0))
            if not equal_amount(values.get(key),expected):
                raise ValueError('User-entry business formula failed: '+key)
            checked+=1
    for values in [report.get('summary',{}),*(row.get('metrics',{}) for row in report['rows'])]:
        if values.get('receipts') is None or values.get('refunds') is None or amount(values['receipts'])==0:
            if values.get('refund_rate') is not None:raise ValueError('Unknown refund-rate inputs became a known rate')
        elif not equal_amount(values.get('refund_rate'),amount(values['refunds'])/amount(values['receipts'])):
            raise ValueError('Refund rate differs from its verified amounts')
    if checked==0:raise ValueError('No actual business amount could be verified at user entry')
    return {'period':period,'input_sha':report['input_sha'],'scope_sha':report['scope_sha'],
            'formula_checks':checked,'store_rows':len(report['rows']),
            'business_status':month.get('status'),'business_complete':month.get('status')=='completed'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['capture','verify','verify-history'])
    parser.add_argument('root',type=Path)
    parser.add_argument('receipt',type=Path)
    parser.add_argument('--acceptance',type=Path)
    parser.add_argument('--readback-url')
    parser.add_argument('--period')
    parser.add_argument('--offline-version')
    parser.add_argument('--expected-version')
    args=parser.parse_args()
    if args.mode=='verify':
        if not args.acceptance or not args.readback_url or not args.period:
            parser.error('verify requires the same --acceptance receipt, --readback-url and --period')
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ledger'))
        from ledger.release_gate import validate
        accepted=json.loads(args.acceptance.read_text(encoding='utf-8'))
        validate(accepted,version=accepted['code_sha'])
    if args.offline_version and (args.mode not in {'capture','verify-history'} or not re.fullmatch(r'[0-9a-f]{7,40}',args.offline_version)):
        parser.error('offline-version is only a stopped-server financial-history identity')
    current=snapshot(args.root,reader=(lambda _:{'version':args.offline_version,'source':'stopped_VERSION'})) if args.offline_version else snapshot(args.root)
    if args.mode=='capture':
        with args.receipt.open('x',encoding='utf-8') as stream:json.dump(current,stream,ensure_ascii=False,indent=2)
        print(json.dumps({'version':current['version'],'closed':len(current['closed']),
            'protected_layers':list(current['workspace_immutable'])+list(current['immutable'])}))
    elif args.mode=='verify-history':
        if not args.expected_version:parser.error('verify-history requires expected rollback version')
        baseline=json.loads(args.receipt.read_text(encoding='utf-8'))
        print(json.dumps(compare_history(baseline,current,args.expected_version)))
    else:
        baseline=json.loads(args.receipt.read_text(encoding='utf-8'))
        protected=compare_history(baseline,current,accepted['code_sha'])
        readback=business_readback(args.readback_url,args.period,accepted['code_sha'],
                                   source_lookup=lambda run_id:saved_run(args.root,run_id),
                                   company_source_lookup=lambda period:saved_company(args.root,period),
                                   expected_source_sha=accepted['source_code_sha'])
        print(json.dumps({'version':current['version'],'financial_history':protected,'business_readback':readback},ensure_ascii=False))


if __name__=='__main__':main()
