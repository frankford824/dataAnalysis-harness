"""Readback gates over real temporary financial history and real operations DTOs."""
from contextlib import contextmanager,nullcontext
from copy import deepcopy
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ledger import automation_actions,operations_api
from operations_fixture import create_fixture

REPO=Path(__file__).resolve().parents[2]
SCRIPT=REPO/'tools/verify-ledger-release.py'
CODE='a'*40
PERIOD='2030-01'


def module():
    spec=importlib.util.spec_from_file_location('release_readback_test',SCRIPT)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


@pytest.fixture
def stored(tmp_path,request):
    ws,model,registry,runs=create_fixture(tmp_path/'workspace',overhead=10)
    request.addfinalizer(ws.close)
    with ws.conn:
        for sid,amount in (('s1','2.50'),('s2','7.50')):
            rid=runs[PERIOD,sid]
            ws.conn.execute("UPDATE period SET state='closed',run_id=? WHERE store_id=? AND period=?",(rid,sid,PERIOD))
            ws.conn.execute('INSERT INTO run_labor VALUES(?,?,?,?,?,?)',(rid,sid,PERIOD,amount,'2030-02-01','approved-history-fixture'))
        rid=runs[PERIOD,'s1'];payload=ws.conn.execute('SELECT result FROM run WHERE id=?',(rid,)).fetchone()[0]
        ws.conn.execute('INSERT INTO manual_finance VALUES(?,?,?,?,?,?,?,?)',
            (rid,'s1',PERIOD,payload,'{"reason":"approved fixture"}',hashlib.sha256(payload.encode()).hexdigest(),'2030-02-01','approved-history-fixture'))
    with registry.transaction() as conn:
        conn.execute('INSERT INTO payout_confirmation VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('pay-1','s1',PERIOD,rid,'2030-02-01','approved-history-fixture','approved fixture','1'*64,'[]','[]','3.00'))
        conn.execute('INSERT INTO settlement VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('settle-1','2030-02-01','approved-history-fixture','approved fixture',PERIOD,PERIOD,'{}','[]','2'*64,'6.00','{}'))
        conn.execute('INSERT INTO profit_exclusion VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('exclude-1','s1',PERIOD,'doer',rid,'2030-02-01','approved-history-fixture','approved fixture','3'*64,'[]','16.00'))
    app=FastAPI();operations_api.install(app,lambda:ws,lambda:model)
    with TestClient(app) as client:
        response=client.get('/api/operations/month',params={'period':PERIOD,'compare':'false'})
        assert response.status_code==200,response.text
        report=response.json()
    month=automation_actions.month(ws,model,PERIOD)
    yield {'ws':ws,'registry':registry,'model':model,'runs':runs,'report':report,'month':month,'root':ws.root}


def responses(stored):
    return {'/api/version':{'version':CODE},'/api/automation/months/'+PERIOD:deepcopy(stored['month']),
            '/api/operations/month':deepcopy(stored['report'])}


def reader_for(routes):
    def reader(url):return deepcopy(routes[urlsplit(url).path])
    return reader


@contextmanager
def http(routes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            value=routes.get(urlsplit(self.path).path)
            if value is None:self.send_error(404);return
            body=json.dumps(value,ensure_ascii=False).encode()
            self.send_response(200);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}'
    finally:server.shutdown();server.server_close();thread.join(timeout=5)


def test_real_saved_operations_dto_reads_back_through_actual_http(stored):
    verify=module();routes=responses(stored)
    with http(routes) as base:
        result=verify.business_readback(base,PERIOD,CODE)
    assert result['formula_checks']==12
    assert result['store_rows']==2
    assert result['input_sha']==stored['report']['input_sha']
    assert result['business_status']==stored['month']['status']=='completed'
    assert result['business_complete'] is True


def test_frozen_snapshot_includes_all_financial_layers(stored):
    verify=module()
    before=verify.snapshot(stored['root'],reader=lambda url:{'version':'b'*40})
    after=verify.snapshot(stored['root'],reader=lambda url:{'version':CODE})
    assert set(before['workspace_immutable'])=={'manual_finance','run_labor'}
    assert set(before['immutable'])=={'payout_confirmation','settlement','profit_exclusion'}
    assert len(before['closed'])==2
    assert verify.compare_history(before,after,CODE)=={'changed_closed':0,'changed_financial_records':0}


@pytest.mark.parametrize('section,table',[('workspace_immutable','manual_finance'),('workspace_immutable','run_labor'),
    ('immutable','payout_confirmation'),('immutable','settlement'),('immutable','profit_exclusion')])
def test_missing_required_history_layer_cannot_silently_remove_protection(stored,section,table):
    verify=module();before=verify.snapshot(stored['root'],reader=lambda url:{'version':CODE});after=deepcopy(before)
    del before[section][table]
    with pytest.raises(ValueError):verify.compare_history(before,after,CODE)


@pytest.mark.parametrize('addition',['closed_period','run_labor'])
def test_release_cannot_add_unapproved_financial_closures_while_writes_are_fenced(stored,addition):
    verify=module();before=verify.snapshot(stored['root'],reader=lambda url:{'version':CODE})
    ws=stored['ws'];run=stored['runs']['2029-12','s1']
    with ws.conn:
        if addition=='closed_period':
            ws.conn.execute("UPDATE period SET state='closed',run_id=? WHERE store_id='s1' AND period='2029-12'",(run,))
        else:
            ws.conn.execute('INSERT INTO run_labor VALUES(?,?,?,?,?,?)',(run,'s1','2029-12','0.00','2030-02-01','unexpected-release'))
    after=verify.snapshot(stored['root'],reader=lambda url:{'version':CODE})
    with pytest.raises(ValueError):verify.compare_history(before,after,CODE)


@pytest.mark.parametrize('mutation',['manual_finance','run_labor','closed_result','closed_pointer','reopened','payout_confirmation','settlement','profit_exclusion'])
def test_mutated_real_frozen_history_is_rejected(stored,mutation):
    verify=module();before=verify.snapshot(stored['root'],reader=lambda url:{'version':CODE})
    ws,registry=stored['ws'],stored['registry']
    if mutation=='closed_result':
        with ws.conn:ws.conn.execute('UPDATE run SET result=? WHERE id=?',('{"changed":true}',stored['runs'][PERIOD,'s1']))
    elif mutation=='closed_pointer':
        with ws.conn:ws.conn.execute('UPDATE period SET run_id=? WHERE store_id=? AND period=?',(stored['runs']['2029-12','s1'],'s1',PERIOD))
    elif mutation=='reopened':
        with ws.conn:ws.conn.execute("UPDATE period SET state='open' WHERE store_id='s1' AND period=?",(PERIOD,))
    else:
        # Simulate an erroneous migration bypassing the immutable-table trigger.
        connection_context=nullcontext(ws.conn) if mutation in {'manual_finance','run_labor'} else registry.connect()
        with connection_context as connection:
            trigger=mutation+'_no_update'
            connection.execute('DROP TRIGGER IF EXISTS '+trigger)
            column={'manual_finance':'decision_json','run_labor':'amount','payout_confirmation':'confirmed_total','settlement':'total','profit_exclusion':'included_profit'}[mutation]
            value='{"reason":"bad release"}' if mutation=='manual_finance' else '999.00'
            connection.execute('UPDATE '+mutation+' SET '+column+'=?',(value,));connection.commit()
    after=verify.snapshot(stored['root'],reader=lambda url:{'version':CODE})
    with pytest.raises(ValueError,match='Frozen financial history changed'):
        verify.compare_history(before,after,CODE)


@pytest.mark.parametrize('mutation',['version','empty_rows','empty_refs','period','bad_derived','unknown_to_zero'])
def test_invalid_user_entry_readback_is_rejected(stored,mutation):
    routes=responses(stored);report=routes['/api/operations/month']
    if mutation=='version':routes['/api/version']['version']='b'*40
    elif mutation=='empty_rows':report['rows']=[]
    elif mutation=='empty_refs':report['run_refs']=[]
    elif mutation=='period':report['period']='2030-02'
    elif mutation=='bad_derived':report['summary']['profit_after_labor']='0.00'
    elif mutation=='unknown_to_zero':report['rows'][0]['metrics']['receipts']=None;report['rows'][0]['metrics']['net_income']='0.00'
    with pytest.raises(ValueError):module().business_readback('https://user-entry.example.invalid',PERIOD,CODE,reader=reader_for(routes))


def recompute(values):
    from decimal import Decimal
    rules={'net_income':(('receipts',1),('refunds',-1)),
           'gross_profit':(('net_income',1),('goods_cost',-1)),
           'profit':(('net_income',1),('goods_cost',-1),('promotion_cost',-1),('platform_cost',-1),('freight_cost',-1),('aftersales_cost',-1)),
           'profit_after_labor':(('profit',1),('labor_cost',-1))}
    for key,parts in rules.items():
        values[key]=None if any(values.get(name) is None for name,_ in parts) else str(sum((Decimal(str(values[name]))*sign for name,sign in parts),Decimal(0)))


def test_placeholder_runs_and_consistent_zero_values_do_not_certify_an_empty_scope(stored):
    routes=responses(stored);report=routes['/api/operations/month']
    for ref in report['run_refs']:ref['run_id']=None;ref['evidence_complete']=False
    for row in report['rows']:row['run_ids']=[]
    for values in [report['summary'],*[row['metrics'] for row in report['rows']]]:
        for key in ('receipts','refunds','goods_cost','promotion_cost','platform_cost','freight_cost','aftersales_cost','labor_cost'):values[key]='0.00'
        recompute(values)
    with pytest.raises(ValueError):module().business_readback('https://user-entry.example.invalid',PERIOD,CODE,reader=reader_for(routes))


def test_unknown_store_income_is_not_zeroed_in_a_consistent_company_summary(stored):
    routes=responses(stored);report=routes['/api/operations/month']
    unknown=next(row for row in report['rows'] if row['store_id']=='s1')['metrics']
    unknown['receipts']=None;recompute(unknown)
    report['summary']['receipts']='300.00';recompute(report['summary'])
    with pytest.raises(ValueError):module().business_readback('https://user-entry.example.invalid',PERIOD,CODE,reader=reader_for(routes))


def test_self_consistent_but_wrong_summary_is_rejected(stored):
    routes=responses(stored);report=routes['/api/operations/month']
    report['summary']['receipts']='401.00';recompute(report['summary'])
    with pytest.raises(ValueError):module().business_readback('https://user-entry.example.invalid',PERIOD,CODE,reader=reader_for(routes))


@pytest.fixture(params=[False,True],ids=['labelled_split','known_net_unknown_gross'])
def income_source(tmp_path,request):
    from test_operations_income_scopes import owned_case
    from ledger import operations_readmodel
    case=owned_case(tmp_path,unknown_subject=request.param)
    ws=case['ws'];period='2026-07'
    report=operations_readmodel.read_month(ws,case['model'],period)
    try:
        yield case,{'/api/version':{'version':CODE},'/api/automation/months/'+period:automation_actions.month(ws,case['model'],period),
                    '/api/operations/month':report}
    finally:ws.close()


def test_income_readback_uses_actual_saved_1688_source_without_inventing_gross(income_source):
    case,routes=income_source;verify=module()
    result=verify.business_readback('https://user-entry.example.invalid','2026-07',CODE,
        reader=reader_for(routes),source_lookup=lambda run:verify.saved_run(case['ws'].root,run))
    assert result['store_rows']==1 and result['formula_checks']>0
    assert result['business_complete'] is False


@pytest.mark.parametrize('mutation',['no_lookup','source_net','metric_total','source_model','source_identity','invented_split'])
def test_http_income_evidence_cannot_self_certify_a_different_source(income_source,mutation):
    case,routes=income_source;routes=deepcopy(routes);verify=module()
    report=routes['/api/operations/month'];income=report['income_evidence']['s1'];source=income['proof']['net_source']
    lookup=lambda run:verify.saved_run(case['ws'].root,run)
    if mutation=='no_lookup':lookup=None
    elif mutation=='source_net':income['net_income']='181.00'
    elif mutation=='metric_total':source['metric_total']='181.00'
    elif mutation=='source_model':source['source_contract']['model_sha']='f'*64
    elif mutation=='source_identity':source['store_id']='another-store';source['result_sha']=verify.canonical({key:source[key] for key in ('store_id','period','run_id','platform','statement_receipt')})
    elif mutation=='invented_split':
        income['status']='split_verified';income['receipts']='201.00';income['refunds']='21.00'
    with pytest.raises(ValueError):verify.business_readback('https://user-entry.example.invalid','2026-07',CODE,
        reader=reader_for(routes),source_lookup=lookup)


def test_income_split_in_http_rows_cannot_change_gross_and_refund_together(tmp_path):
    from test_operations_income_scopes import owned_case
    from ledger import operations_readmodel
    case=owned_case(tmp_path);ws=case['ws'];verify=module()
    try:
        report=operations_readmodel.read_month(ws,case['model'],'2026-07')
        for values in (report['summary'],report['rows'][0]['metrics']):
            values['receipts']='201.00';values['refunds']='21.00'
        routes={'/api/version':{'version':CODE},'/api/automation/months/2026-07':automation_actions.month(ws,case['model'],'2026-07'),'/api/operations/month':report}
        with pytest.raises(ValueError,match='Gross/refund row'):
            verify.business_readback('https://user-entry.example.invalid','2026-07',CODE,
                reader=reader_for(routes),source_lookup=lambda run:verify.saved_run(ws.root,run))
    finally:ws.close()


def test_compression_preserves_frozen_logical_identity_but_corrupt_blob_is_rejected(stored):
    from ledger.snapshot_store import put
    verify=module();ws=stored['ws'];run=stored['runs'][PERIOD,'s1']
    before=verify.snapshot(ws.root,reader=lambda _:{'version':CODE})
    text=ws.conn.execute('SELECT result FROM run WHERE id=?',(run,)).fetchone()[0]
    with ws.conn:ws.conn.execute('UPDATE run SET result=? WHERE id=?',(put(ws.conn,text),run))
    after=verify.snapshot(ws.root,reader=lambda _:{'version':CODE})
    assert verify.compare_history(before,after,CODE)['changed_closed']==0
    with ws.conn:
        ws.conn.execute('DROP TRIGGER evidence_blob_no_update')
        ws.conn.execute("UPDATE evidence_blob SET payload=x'00'")
    with pytest.raises((ValueError,zlib.error)):
        verify.snapshot(ws.root,reader=lambda _:{'version':CODE})


def test_python_optimization_does_not_disable_frozen_history_rejection(stored,tmp_path):
    verify=module();before=verify.snapshot(stored['root'],reader=lambda url:{'version':CODE})
    after=deepcopy(before);after['workspace_immutable']['run_labor'][str(stored['runs'][PERIOD,'s1'])]='f'*64
    fixture=tmp_path/'history.json';fixture.write_text(json.dumps({'before':before,'after':after}))
    code="import importlib.util,json,sys; s=importlib.util.spec_from_file_location('readback',sys.argv[1]); m=importlib.util.module_from_spec(s);s.loader.exec_module(m);v=json.load(open(sys.argv[2]));m.compare_history(v['before'],v['after'],sys.argv[3]);print('UNSAFE_ACCEPTED')"
    result=subprocess.run([sys.executable,'-c',code,str(SCRIPT),str(fixture),CODE],env={**os.environ,'PYTHONOPTIMIZE':'1'},capture_output=True,text=True)
    assert result.returncode!=0
    assert 'UNSAFE_ACCEPTED' not in result.stdout


def test_python_optimization_does_not_disable_business_amount_rejection(stored,tmp_path):
    routes=responses(stored);routes['/api/operations/month']['summary']['profit_after_labor']='0.00'
    fixture=tmp_path/'responses.json';fixture.write_text(json.dumps(routes))
    code="import importlib.util,json,sys,urllib.parse; s=importlib.util.spec_from_file_location('readback',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);v=json.load(open(sys.argv[2]));m.business_readback('https://user-entry.example.invalid',sys.argv[3],sys.argv[4],reader=lambda url:v[urllib.parse.urlsplit(url).path]);print('UNSAFE_ACCEPTED')"
    result=subprocess.run([sys.executable,'-c',code,str(SCRIPT),str(fixture),PERIOD,CODE],env={**os.environ,'PYTHONOPTIMIZE':'1'},capture_output=True,text=True)
    assert result.returncode!=0
    assert 'UNSAFE_ACCEPTED' not in result.stdout


def test_main_runs_real_history_and_http_readback(stored,tmp_path,monkeypatch,capsys):
    from test_release_gate import receipt,seal
    from ledger.version import source_code_manifest
    from ledger.release_gate import digest as source_digest
    verify=module();original=verify.snapshot
    verify.snapshot=lambda root:original(root,reader=lambda url:{'version':CODE})
    baseline=tmp_path/'before.json';baseline.write_text(json.dumps(verify.snapshot(stored['root'])))
    evidence=tmp_path/'accepted';evidence.mkdir()
    accepted_value=receipt(evidence);source_manifest=source_code_manifest(REPO)
    accepted_value.update(source_code_manifest=source_manifest,source_code_sha=source_digest(source_manifest));seal(accepted_value)
    accepted=evidence/'acceptance-receipt.json';accepted.write_text(json.dumps(accepted_value))
    routes=responses(stored);routes['/api/version'].update(source_code_sha=accepted_value['source_code_sha'],approved_source_code_sha=accepted_value['source_code_sha'],source_code_status='approved')
    with http(routes) as base:
        monkeypatch.setattr(sys,'argv',[str(SCRIPT),'verify',str(stored['root']),str(baseline),'--acceptance',str(accepted),'--readback-url',base,'--period',PERIOD])
        verify.main()
    output=json.loads(capsys.readouterr().out)
    assert output['financial_history']['changed_financial_records']==0
    assert output['business_readback']['formula_checks']==12


def test_stopped_server_capture_requires_no_network(stored,tmp_path,monkeypatch,capsys):
    verify=module();target=tmp_path/'stopped-history.json'
    def forbidden(*args,**kwargs):raise AssertionError('offline capture contacted HTTP')
    monkeypatch.setattr(verify.urllib.request,'urlopen',forbidden)
    monkeypatch.setattr(sys,'argv',[str(SCRIPT),'capture',str(stored['root']),str(target),'--offline-version',CODE])
    verify.main()
    saved=json.loads(target.read_text())
    assert saved['version']=={'version':CODE,'source':'stopped_VERSION'}
    assert saved['schema']==2 and len(saved['closed'])==2
    assert saved['workspace_immutable']['manual_finance']
    capsys.readouterr()


def test_rollback_cli_under_optimization_does_not_resume_over_changed_financial_history(stored,tmp_path):
    verify=module();old='b'*40
    before=verify.snapshot(stored['root'],reader=lambda url:{'version':old})
    target=tmp_path/'before-rollback.json';target.write_text(json.dumps(before))
    with stored['ws'].conn:
        stored['ws'].conn.execute('DROP TRIGGER run_labor_no_update')
        stored['ws'].conn.execute('UPDATE run_labor SET amount=? WHERE run_id=?',('99.00',stored['runs'][PERIOD,'s1']))
    result=subprocess.run([sys.executable,str(SCRIPT),'verify-history',str(stored['root']),str(target),'--expected-version',old,'--offline-version',old],
        env={**os.environ,'PYTHONOPTIMIZE':'1'},capture_output=True,text=True)
    assert result.returncode!=0
    assert 'UNSAFE_RESUME' not in result.stdout
    assert 'Frozen financial history changed' in result.stderr


@pytest.mark.parametrize('mutation',['omitted','unavailable','foreign_store'])
def test_actual_1688_source_cannot_be_hidden_to_self_declare_a_normal_receipt(income_source,mutation):
    case,routes=income_source;routes=deepcopy(routes);verify=module()
    report=routes['/api/operations/month'];income=report['income_evidence'].pop('s1')
    if mutation=='unavailable':
        income['status']='unavailable';report['income_evidence']['s1']=income
    elif mutation=='foreign_store':report['income_evidence']['foreign-store']=income
    for values in (report['summary'],report['rows'][0]['metrics']):
        values['receipts']=values['net_income'];values['refunds']='0.00';values['refund_rate']='0.00000000'
        recompute(values)
    with pytest.raises(ValueError):
        verify.business_readback('https://user-entry.example.invalid','2026-07',CODE,reader=reader_for(routes),
            source_lookup=lambda run:verify.saved_run(case['ws'].root,run))


@pytest.mark.parametrize('target',['row','summary'])
def test_refund_rate_cannot_be_zeroed_when_its_source_is_known_or_unknown(income_source,target):
    case,routes=income_source;routes=deepcopy(routes);verify=module()
    report=routes['/api/operations/month']
    values=report['summary'] if target=='summary' else report['rows'][0]['metrics']
    values['refund_rate']='0.00000000'
    with pytest.raises(ValueError):
        verify.business_readback('https://user-entry.example.invalid','2026-07',CODE,reader=reader_for(routes),
            source_lookup=lambda run:verify.saved_run(case['ws'].root,run))


@pytest.mark.parametrize('key,value',[('template_ids','prefix-alibaba1688_settlement_in_v1-suffix'),
    ('template_ids',[None]),('artifacts',[None]),('artifacts',{}),('store_id','foreign-store')])
def test_source_manifest_shapes_and_scope_do_not_certify_http_net(income_source,key,value):
    case,routes=income_source;verify=module()
    from test_operations_income import _corrupt_manifest
    _corrupt_manifest(case,key,value)
    with pytest.raises(ValueError):
        verify.business_readback('https://user-entry.example.invalid','2026-07',CODE,reader=reader_for(routes),
            source_lookup=lambda run:verify.saved_run(case['ws'].root,run))


def test_authoritative_lookup_reads_compressed_full_result_not_a_stale_projection(income_source):
    case,routes=income_source;verify=module();ws=case['ws'];run=case['run_id']
    from ledger.snapshot_store import put
    full=deepcopy(case['payload']);next(row for row in full['statement'] if row['id']=='n_receipt')['available']=False
    text=json.dumps(full,ensure_ascii=False)
    with ws.conn:ws.conn.execute('UPDATE run SET result=? WHERE id=?',(put(ws.conn,text),run))
    saved=verify.saved_run(ws.root,run)
    assert next(row for row in saved['payload']['statement'] if row['id']=='n_receipt')['available'] is False
    with pytest.raises(ValueError,match='not available'):
        verify.business_readback('https://user-entry.example.invalid','2026-07',CODE,reader=reader_for(routes),
            source_lookup=lambda run:verify.saved_run(ws.root,run))


def test_active_compressed_manual_result_overrides_raw_and_slim_income(income_source):
    case,routes=income_source;verify=module();ws=case['ws'];run=case['run_id']
    from ledger.snapshot_store import put
    full=deepcopy(case['payload']);next(row for row in full['statement'] if row['id']=='n_receipt')['available']=False
    text=json.dumps(full,ensure_ascii=False)
    with ws.conn:
        ws.conn.execute('INSERT INTO manual_finance VALUES(?,?,?,?,?,?,?,?)',
            (run,'s1','2026-07',put(ws.conn,text),'{}',hashlib.sha256(text.encode()).hexdigest(),'2030-02-01','isolated-approved-layer'))
        ws.conn.execute("UPDATE period SET state='closed',run_id=? WHERE store_id='s1' AND period='2026-07'",(run,))
    assert verify.saved_run(ws.root,run)['payload']==full
    with pytest.raises(ValueError,match='not available'):
        verify.business_readback('https://user-entry.example.invalid','2026-07',CODE,reader=reader_for(routes),
            source_lookup=lambda run:verify.saved_run(ws.root,run))


def test_readback_keeps_actual_subcent_totals_and_rejects_one_millionth_errors(tmp_path):
    from test_operations_precision import subcent_fixture
    from ledger import operations_readmodel
    from decimal import Decimal
    ws,model,client=subcent_fixture(tmp_path)
    try:
        report=operations_readmodel.read_month(ws,model,PERIOD)
        assert report['summary']['freight_cost']=='0.40'
        routes={'/api/version':{'version':CODE},'/api/automation/months/'+PERIOD:automation_actions.month(ws,model,PERIOD),'/api/operations/month':report}
        verify=module()
        assert verify.business_readback('https://user-entry.example.invalid',PERIOD,CODE,reader=reader_for(routes))['store_rows']==1
        for target in ('aggregate','derived'):
            bad=deepcopy(routes);metrics=bad['/api/operations/month']['summary']
            if target=='aggregate':metrics['freight_cost']=str(Decimal(metrics['freight_cost'])+Decimal('.000001'));recompute(metrics)
            else:metrics['profit_after_labor']=str(Decimal(metrics['profit_after_labor'])+Decimal('.000001'))
            with pytest.raises(ValueError):verify.business_readback('https://user-entry.example.invalid',PERIOD,CODE,reader=reader_for(bad))
    finally:client.close();ws.close()


def registered_income_bundle(tmp_path):
    """Real engine evidence, with the old bundle layout present before its M3 seal."""
    from test_operations_income_scopes import owned_case
    from ledger import service,operations_readmodel
    from ledger.automation_period_proof import prepare,save
    from ledger.storage_maintenance import manifest
    from ledger.storage_integrity import digest
    case=owned_case(tmp_path/'source');ws,model=case['ws'],case['model']
    store=model.store('s1');sl=case['result'].slice(store.name,'2026-07')
    proof=prepare(ws,model,store,case['ingestion'],sl,case['payload'],None)
    run=ws.record('s1','2026-07',case['payload'],[item.ref.sha256 for item in case['ingestion'].items],evidence_ready=False)
    target=tmp_path/'legacy-archive'/str(run);target.mkdir(parents=True)
    link=ws.root/'runs'/str(run);link.symlink_to(target,target_is_directory=True)
    service._keep_facts(ws,run,sl)
    files=[{'name':path.name,'sha256':digest(path),'bytes':path.stat().st_size}
        for path in sorted(target.glob('*.parquet'))]
    raw=json.dumps({'run_id':run,'files':files},sort_keys=True).encode();(target/'manifest.json').write_bytes(raw)
    with manifest(ws.root) as conn,conn:
        conn.execute('INSERT INTO artifact_archive VALUES(?,?,?,?,?,?)',
            (f'runs/{run}',hashlib.sha256(raw).hexdigest(),sum(row['bytes'] for row in files),str(target),'archived','isolated-legacy-fixture'))
    save(ws,run,proof)
    report=operations_readmodel.read_month(ws,model,'2026-07')
    assert report['income_evidence']['s1']['status']=='split_verified'
    case.update(run_id=run,bundle_target=target,report=report)
    return case


def income_routes(case,report=None):
    return {'/api/version':{'version':CODE},'/api/automation/months/2026-07':automation_actions.month(case['ws'],case['model'],'2026-07'),'/api/operations/month':case['report'] if report is None else report}


def test_real_registered_legacy_bundle_reads_back_without_creating_directories(tmp_path,monkeypatch):
    case=registered_income_bundle(tmp_path);ws=case['ws'];verify=module()
    try:
        def forbidden(*args,**kwargs):raise AssertionError('readback created a directory')
        monkeypatch.setattr(Path,'mkdir',forbidden)
        result=verify.business_readback('https://user-entry.example.invalid','2026-07',CODE,
            reader=reader_for(income_routes(case)),source_lookup=lambda run:verify.saved_run(ws.root,run))
        assert result['store_rows']==1
    finally:ws.close()


def test_resealed_registered_bundle_cannot_replace_first_m3_income_bytes(tmp_path):
    import polars as pl
    from ledger.storage_integrity import seal,digest
    from ledger.storage_maintenance import manifest
    case=registered_income_bundle(tmp_path);ws=case['ws'];run=case['run_id'];verify=module()
    try:
        path=case['bundle_target']/(str(run)+'.parquet');facts=pl.read_parquet(path)
        selected=(pl.col('metric_id')=='trade_receipt_1688') & (pl.col('link_key')=='A')
        for name in ('amount','contribution'):
            facts=facts.with_columns(pl.when(selected & (pl.col('subject')=='订单收入')).then(pl.col(name)+1)
                .when(selected & (pl.col('subject')=='订单售后退款')).then(pl.col(name)-1).otherwise(pl.col(name)).alias(name))
        facts.write_parquet(path);seal(path);changed=digest(path)
        bundle=json.loads((case['bundle_target']/'manifest.json').read_text())
        for row in bundle['files']:
            if row['name']==path.name:row.update(sha256=changed,bytes=path.stat().st_size)
        raw=json.dumps(bundle,sort_keys=True).encode();(case['bundle_target']/'manifest.json').write_bytes(raw)
        # Even a new archive catalog/seal cannot overrule the first M3 proof.
        with manifest(ws.root) as conn,conn:conn.execute('UPDATE artifact_archive SET sha=? WHERE source=?',
            (hashlib.sha256(raw).hexdigest(),f'runs/{run}'))
        report=deepcopy(case['report']);evidence=report['income_evidence']['s1'];evidence['proof']['facts_sha']=changed
        evidence.update(receipts='201.00',refunds='21.00')
        for values in (report['summary'],report['rows'][0]['metrics']):
            values.update(receipts='201.00',refunds='21.00',refund_rate=str(21/201));recompute(values)
        with pytest.raises(ValueError,match='original run proof'):
            verify.business_readback('https://user-entry.example.invalid','2026-07',CODE,
                reader=reader_for(income_routes(case,report)),source_lookup=lambda run:verify.saved_run(ws.root,run))
    finally:ws.close()


@pytest.mark.parametrize('mode',['unregistered','offline'])
def test_missing_legacy_bundle_does_not_fall_back_to_a_flat_copy(tmp_path,mode):
    from ledger.storage_maintenance import manifest
    from ledger.storage_integrity import seal
    case=registered_income_bundle(tmp_path);ws=case['ws'];run=case['run_id'];verify=module()
    try:
        flat=ws.root/'runs'/(str(run)+'.parquet');flat.write_bytes((case['bundle_target']/(str(run)+'.parquet')).read_bytes());seal(flat)
        if mode=='unregistered':
            with manifest(ws.root) as conn,conn:conn.execute('DELETE FROM artifact_archive')
        else:case['bundle_target'].rename(tmp_path/'offline-original')
        with pytest.raises(ValueError):
            verify.business_readback('https://user-entry.example.invalid','2026-07',CODE,
                reader=reader_for(income_routes(case)),source_lookup=lambda run:verify.saved_run(ws.root,run))
        if mode=='offline':assert not case['bundle_target'].exists()
    finally:ws.close()


def test_authoritative_and_history_connections_close_on_success_and_failure(stored,monkeypatch):
    verify=module();real_connect=sqlite3.connect;opened=[]
    def tracked(*args,**kwargs):
        assert '?mode=ro' in str(args[0]) and kwargs.get('uri') is True
        conn=real_connect(*args,**kwargs);opened.append(conn);return conn
    monkeypatch.setattr(verify.sqlite3,'connect',tracked)
    run=stored['runs'][PERIOD,'s1']
    assert verify.saved_run(stored['root'],run)['payload']
    with pytest.raises(ValueError):verify.saved_run(stored['root'],999999)
    verify.snapshot(stored['root'],reader=lambda _:{'version':CODE})
    assert len(opened)>=4
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):conn.execute('SELECT 1')


def test_absent_readback_root_is_never_initialized(tmp_path):
    absent=tmp_path/'never-opened';verify=module()
    with pytest.raises(sqlite3.OperationalError):verify.saved_run(absent,1)
    assert not absent.exists()


def test_month_operating_scope_detects_consistent_report_and_run_ref_omission(stored):
    routes=responses(stored);report=routes['/api/operations/month']
    report['rows']=[row for row in report['rows'] if row['store_id']=='s1']
    report['run_refs']=[row for row in report['run_refs'] if row['store_id']=='s1']
    report['summary']=deepcopy(report['rows'][0]['metrics'])
    if 'total' in report:report['total']=1
    with pytest.raises(ValueError,match='operating-store scope'):
        module().business_readback('https://user-entry.example.invalid',PERIOD,CODE,reader=reader_for(routes))


@pytest.mark.parametrize('mutation',['completed','processing','total','waiting','status','store_status','filtered_scope'])
def test_month_store_counts_and_completion_must_agree(stored,mutation):
    routes=responses(stored);month=routes['/api/automation/months/'+PERIOD]
    if mutation in {'completed','processing','total','waiting'}:month['counts'][mutation]+=1
    elif mutation=='status':month['status']='processing'
    elif mutation=='store_status':month['stores'][0]['status']='processing'
    else:routes['/api/operations/month']['scope']['store_id']='s1'
    with pytest.raises(ValueError):
        module().business_readback('https://user-entry.example.invalid',PERIOD,CODE,reader=reader_for(routes))


def test_explicit_excluded_scope_is_verified_without_readding_the_store(tmp_path):
    from ledger import operations_readmodel
    ws,model,registry,runs=create_fixture(tmp_path,overhead=10)
    actor={'id':'fixture-human','kind':'human','authenticated':True}
    try:
        proposal=automation_actions.decision_preview(ws,model,request_id='exclude-preview',period=PERIOD,store_id='s2',
            kind='store_scope',values={'included':False},reason='原始营业登记确认本月不营业',actor=actor)
        automation_actions.decision_apply(ws,model,request_id='exclude-apply',proposal_id=proposal['proposal_id'],
            proposal_sha=proposal['proposal_sha'],input_sha=proposal['input_sha'],actor=actor)
        report=operations_readmodel.read_month(ws,model,PERIOD);month=automation_actions.month(ws,model,PERIOD)
        assert {row['store_id'] for row in report['rows']}=={'s1'}
        assert next(row for row in month['stores'] if row['store_id']=='s2')['stage']=='excluded'
        routes={'/api/version':{'version':CODE},'/api/automation/months/'+PERIOD:month,'/api/operations/month':report}
        assert module().business_readback('https://user-entry.example.invalid',PERIOD,CODE,reader=reader_for(routes))['store_rows']==1
    finally:ws.close()


def test_business_run_must_match_the_monthly_current_reference(stored):
    routes=responses(stored)
    routes['/api/automation/months/'+PERIOD]['stores'][0]['run_id']+=1000
    with pytest.raises(ValueError,match='current monthly workflow'):
        module().business_readback('https://user-entry.example.invalid',PERIOD,CODE,reader=reader_for(routes))


def test_free_text_that_looks_like_a_snapshot_reference_stays_literal(stored):
    marker=json.dumps({'$ledger_snapshot_v1':'f'*64},separators=(',',':'))
    with stored['registry'].transaction() as conn:
        conn.execute('INSERT INTO payout_confirmation VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('literal-note','s1',PERIOD,stored['runs'][PERIOD,'s1'],'2030-02-01','fixture',marker,'4'*64,'[]','[]','0.00'))
    verify=module();before=verify.snapshot(stored['root'],reader=lambda _:{'version':CODE})
    after=verify.snapshot(stored['root'],reader=lambda _:{'version':CODE})
    assert 'literal-note' in before['immutable']['payout_confirmation']
    assert verify.compare_history(before,after,CODE)=={'changed_closed':0,'changed_financial_records':0}


def test_optimized_cli_cannot_hide_actual_1688_source_contract(income_source,tmp_path):
    case,routes=income_source;routes=deepcopy(routes);report=routes['/api/operations/month']
    report['income_evidence']={}
    for values in (report['summary'],report['rows'][0]['metrics']):
        values.update(receipts=values['net_income'],refunds='0.00',refund_rate='0.00000000');recompute(values)
    fixture=tmp_path/'http-response.json';fixture.write_text(json.dumps(routes,ensure_ascii=False))
    code="import importlib.util,json,sys;from pathlib import Path;from urllib.parse import urlsplit;s=importlib.util.spec_from_file_location('readback',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);routes=json.load(open(sys.argv[2]));m.business_readback('https://user-entry.example.invalid','2026-07',sys.argv[4],reader=lambda url:routes[urlsplit(url).path],source_lookup=lambda run:m.saved_run(Path(sys.argv[3]),run));print('UNSAFE_ACCEPTED')"
    result=subprocess.run([sys.executable,'-c',code,str(SCRIPT),str(fixture),str(case['ws'].root),CODE],
        env={**os.environ,'PYTHONOPTIMIZE':'1'},capture_output=True,text=True)
    assert result.returncode!=0 and 'UNSAFE_ACCEPTED' not in result.stdout
    assert 'Known 1688 net cannot hide' in result.stderr


@pytest.mark.parametrize('served',['missing','different'])
def test_same_version_cannot_hide_source_code_drift_over_real_http(stored,served):
    verify=module();routes=responses(stored)
    expected=hashlib.sha256(b'accepted-python-source-tree').hexdigest()
    if served=='different':routes['/api/version']['source_code_sha']=hashlib.sha256(b'drifted-python-source-tree').hexdigest()
    before=verify.snapshot(stored['root'],reader=lambda _:{'version':CODE})
    with http(routes) as base:
        with pytest.raises(ValueError,match='different Python source bytes'):
            verify.business_readback(base,PERIOD,CODE,expected_source_sha=expected,
                source_lookup=lambda run:verify.saved_run(stored['root'],run))
    after=verify.snapshot(stored['root'],reader=lambda _:{'version':CODE})
    assert verify.compare_history(before,after,CODE)['changed_financial_records']==0


def test_matching_source_code_identity_keeps_real_nonzero_http_readback(stored):
    verify=module();routes=responses(stored);expected=hashlib.sha256(b'accepted-python-source-tree').hexdigest()
    routes['/api/version'].update(source_code_sha=expected,approved_source_code_sha=expected,source_code_status='approved')
    assert routes['/api/operations/month']['summary']['net_income']=='320.00'
    with http(routes) as base:
        result=verify.business_readback(base,PERIOD,CODE,expected_source_sha=expected,
            source_lookup=lambda run:verify.saved_run(stored['root'],run))
    assert result['store_rows']==2 and result['formula_checks']==12


@pytest.mark.parametrize('status',['missing','development','source_drift','receipt_missing','receipt_invalid'])
def test_matching_disk_source_does_not_certify_an_unapproved_runtime(stored,status):
    verify=module();routes=responses(stored);expected=hashlib.sha256(b'accepted-python-source-tree').hexdigest()
    routes['/api/version']['source_code_sha']=expected
    if status!='missing':routes['/api/version'].update(approved_source_code_sha=expected,source_code_status=status)
    with http(routes) as base:
        with pytest.raises(ValueError,match='approved runtime'):
            verify.business_readback(base,PERIOD,CODE,expected_source_sha=expected,
                source_lookup=lambda run:verify.saved_run(stored['root'],run))
