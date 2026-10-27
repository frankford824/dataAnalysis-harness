"""V2 HTTP contracts over a real loopback quote server, never production/goldens."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import polars as pl
import pytest

from automation_fixtures import capture_synthetic_summary_fixture
from ledger import historical_cost_facts as facts
from ledger import automation_store as journal
from ledger.storage_integrity import seal
from ledger.upstream_history import HistoryUnavailable
from test_automation_cost_actions import contract,PERIOD,SERVICE_TOKEN,_replace_source_quantities


def missing_source(c):
    c.upstream.detail['order']['order_status_raw']='Sent'
    c.upstream.detail['items'][0]['cost']={
        'unit_cost':None,'cost_amount':None,'cost_status':'missing_price','cost_source':None}


def grant(c,rule_id=None,rule_sha=None):
    stamp=datetime.now(timezone.utc);rule_id=rule_id or facts.RULE;rule_sha=rule_sha or facts.RULE_SHA
    request='v2-grant-'+hashlib.sha256(rule_id.encode()).hexdigest()[:12]
    response=c.human.post('/api/automation/decisions/preview',json={
        'request_id':request,'period':PERIOD,'store_id':'s1','kind':'rule_grant',
        'values':{'rule_id':rule_id,'rule_sha':rule_sha,'enabled':True,
            'valid_from':(stamp-timedelta(days=1)).isoformat(),'valid_until':(stamp+timedelta(days=1)).isoformat()},
        'reason':'明确合成HTTP合同：仅批准可信原始历史报价事实，不含月结或实发授权'})
    assert response.status_code==200,response.text
    value=response.json()
    response=c.human.post('/api/automation/decisions/apply',json={
        'request_id':request+'-apply','proposal_id':value['proposal_id'],
        'proposal_sha':value['proposal_sha'],'input_sha':value['input_sha']})
    assert response.status_code==200,response.text


def body(c,rows=None,**changes):
    old=c.request()
    if rows is None:
        rows=[{'coverage_key':old['coverage_key'],'context_sha':old['context_sha']}]
    value={key:old[key] for key in ('case_id','store_id','period','run_id','input_sha')}
    return {**value,'request_id':'source-facts-once','rule_id':facts.RULE,
        'items':[{'coverage_key':r['coverage_key'],'context_sha':r['context_sha'],'expected_line_revision':0} for r in rows],
        **changes}


def post(c,payload,headers=None):
    return c.client.post('/api/automation/service-facts',json=payload,
        headers=headers if headers is not None else {'Authorization':'Bearer '+SERVICE_TOKEN})


def adopted(c):
    return facts.active(c.ws.conn,'s1',PERIOD)


def assert_no_manual_money(c):
    assert c.ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0]==0


def fork_sources(c,coverage,pricing):
    """New declared synthetic run; never bless a resealed original run."""
    c.run_id=c.ws.record('s1',PERIOD,c.original,[],model_revision=journal.digest(c.model.model_dump(mode='json')))
    coverage.write_parquet(c.ws.coverage_gaps_path(c.run_id));seal(c.ws.coverage_gaps_path(c.run_id))
    pricing.write_parquet(c.ws.pricing_gaps_path(c.run_id));seal(c.ws.pricing_gaps_path(c.run_id))
    capture_synthetic_summary_fixture(c.ws,c.model,c.run_id)


def test_v2_retains_noncent_quote_as_source_fact_without_manual_money_or_financial_close(contract):
    c=contract;missing_source(c);rows=_replace_source_quantities(c,'1');grant(c)
    response=post(c,body(c,rows))
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='applied' and result['applied_count']==1 and result['recompute_required'] is True
    assert result['decision_id'] and result['receipt_id']
    assert_no_manual_money(c)
    values=adopted(c);assert len(values)==1
    assert Decimal(values[0]['unit_cost'])==Decimal('3.3350')
    assert Decimal(values[0]['exact_amount'])==Decimal('3.3350') and Decimal(values[0]['quantity'])==1
    assert c.ws.state('s1',PERIOD).state=='open'
    assert json.loads(c.ws.latest_run('s1',PERIOD)['result'])==c.original
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE request_id='approved-source-facts:source-facts-once' AND state='queued'").fetchone()[0]==1
    for reference in result['source_refs']:
        path=Path(reference['path'])
        assert path.resolve().is_relative_to(c.ws.root.resolve()) and path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest()==reference['sha256']
    auth=c.ws.conn.execute('SELECT identity_kind,scope_json FROM automation_authorization WHERE action=?',(facts.RULE,)).fetchone()
    assert auth['identity_kind']=='delegated_service' and json.loads(auth['scope_json'])['approved_rule_grant']


def test_v2_success_id_replay_does_not_requery_or_duplicate_facts(contract):
    c=contract;missing_source(c);grant(c);payload=body(c)
    first=post(c,payload);assert first.status_code==200 and first.json()['status']=='applied',first.text
    before=len(c.upstream.calls);c.upstream.status=503
    again=post(c,payload)
    assert again.status_code==200 and again.json()==first.json()
    assert len(c.upstream.calls)==before and len(adopted(c))==1
    assert_no_manual_money(c)


def test_v2_pending_original_id_can_succeed_after_source_recovers(contract):
    c=contract;missing_source(c);grant(c);payload=body(c);c.upstream.status=503
    first=post(c,payload);assert first.status_code==200,first.text
    assert first.json()['status']=='pending' and first.json()['applied_count']==0
    assert not adopted(c);assert_no_manual_money(c)
    assert c.ws.conn.execute("SELECT 1 FROM automation_receipt WHERE request_id='approved-source-facts:source-facts-once'").fetchone() is None
    c.upstream.status=200
    second=post(c,payload)
    assert second.status_code==200 and second.json()['status']=='applied',second.text
    assert len(adopted(c))==1;assert_no_manual_money(c)


def test_v2_needs_separate_human_grant_and_does_not_widen_v1_noncent_rule(contract):
    c=contract;missing_source(c);rows=_replace_source_quantities(c,'1');c.grant()
    v1=c.post(c.request(coverage_key=rows[0]['coverage_key'],context_sha=rows[0]['context_sha']))
    assert v1.status_code==200 and v1.json()['status']=='pending',v1.text
    assert v1.json()['reason_codes']==['historical_amount_not_exact_cents']
    response=post(c,body(c,rows))
    assert response.status_code==403,response.text
    assert not adopted(c);assert_no_manual_money(c)


@pytest.mark.parametrize('location,key,value',[
    ('top','amount','99.00'),('top','url','https://invalid.example'),
    ('item','amount','99.00'),('item','source_ref','self-verified')])
def test_v2_request_cannot_supply_money_urls_or_claimed_source(contract,location,key,value):
    c=contract;missing_source(c);grant(c);payload=body(c)
    (payload if location=='top' else payload['items'][0])[key]=value
    response=post(c,payload)
    assert response.status_code==422,response.text
    assert not adopted(c);assert_no_manual_money(c)


def test_v2_service_identity_and_current_input_are_required(contract):
    c=contract;missing_source(c);grant(c);payload=body(c)
    assert post(c,payload,headers={}).status_code in (401,403)
    assert post(c,{**payload,'input_sha':'0'*64}).status_code==409
    stale=deepcopy(payload);stale['items'][0]['expected_line_revision']=1
    assert post(c,stale).status_code==409
    assert not adopted(c);assert_no_manual_money(c)


@pytest.mark.parametrize('field,value',[
    ('quantity','3'),('cost_company_id','10'),('original_order_date','2026-06-03'),
    ('sku_type','combine'),('is_gift',True)])
def test_v2_live_identity_or_component_disagreement_never_adopts(contract,field,value):
    c=contract;missing_source(c);grant(c);c.upstream.detail['items'][0][field]=value
    response=post(c,body(c))
    assert response.status_code==200,response.text
    assert response.json()['status']=='pending' and response.json()['applied_count']==0
    assert response.json()['reason_codes'] and not adopted(c)
    assert_no_manual_money(c)


def test_v2_reship_and_already_priced_sources_are_preserved(contract):
    c=contract;missing_source(c);grant(c);payload=body(c)
    c.upstream.detail['order']['order_type']='补发'
    blocked=post(c,payload)
    assert blocked.status_code==200 and blocked.json()['status']=='pending',blocked.text
    c.upstream.detail['order']['order_type']='普通订单'
    c.upstream.detail['items'][0]['cost']={'unit_cost':'1.25','cost_amount':'2.50','cost_status':'priced'}
    preserved=post(c,payload)
    assert preserved.status_code==200 and preserved.json()['reason_codes']==['existing_source_cost_preserved'],preserved.text
    assert not adopted(c);assert_no_manual_money(c)


@pytest.mark.parametrize('marker',[{'status':'priced'},{'frozen':True},{'cost_status':'priced'}])
def test_v2_real_api_priced_or_frozen_marker_preserves_existing_source(contract,marker):
    c=contract;missing_source(c);grant(c)
    c.upstream.detail['items'][0]['cost'].update(marker)
    response=post(c,body(c))
    assert response.status_code==200,response.text
    assert response.json()['reason_codes']==['existing_source_cost_preserved']
    assert not adopted(c);assert_no_manual_money(c)


@pytest.mark.parametrize('status',['Cancelled',None])
def test_v2_live_not_sent_source_remains_pending(contract,status):
    c=contract;missing_source(c);grant(c)
    c.upstream.detail['order']['order_status_raw']=status
    response=post(c,body(c))
    assert response.status_code==200,response.text
    assert response.json()['reason_codes']==['live_sale_status_unproved']
    assert not adopted(c);assert_no_manual_money(c)


def test_v2_partial_success_receipt_is_immutable_and_pending_rest_needs_new_batch(contract):
    c=contract;missing_source(c);rows=_replace_source_quantities(c,'1',count=2);grant(c)
    handler=c.upstream.handle
    def one_conflict(path,headers):
        status,payload=handler(path,headers)
        if path.split('?',1)[0].endswith('/orders/124'):
            payload=deepcopy(payload);payload['items'][0]['cost_company_id']='10'
        return status,payload
    c.upstream.handle=one_conflict;payload=body(c,rows)
    first=post(c,payload)
    assert first.status_code==200,first.text
    value=first.json()
    assert value['status']=='applied' and value['applied_count']==1
    assert sorted(r['status'] for r in value['outcomes'])==['adopted','pending']
    count=len(c.upstream.calls);c.upstream.handle=handler
    replay=post(c,payload)
    assert replay.status_code==200 and replay.json()==value
    assert len(c.upstream.calls)==count and len(adopted(c))==1
    assert_no_manual_money(c)


def test_v2_partial_cost_requires_component_reconciliation(contract):
    c=contract;missing_source(c);grant(c)
    coverage=pl.read_parquet(c.ws.coverage_gaps_path(c.run_id)).with_columns(pl.lit(True).alias('partial_cost'))
    pricing=pl.read_parquet(c.ws.pricing_gaps_path(c.run_id))
    fork_sources(c,coverage,pricing)
    response=post(c,body(c))
    assert response.status_code==200,response.text
    assert response.json()['reason_codes']==['partial_cost_component_inventory_incomplete']
    assert not adopted(c);assert_no_manual_money(c)


def test_v2_resign_of_modified_gap_does_not_replace_original_capture(contract):
    c=contract;missing_source(c);grant(c);payload=body(c)
    path=c.ws.coverage_gaps_path(c.run_id)
    frame=pl.read_parquet(path).with_columns(pl.lit('3').alias('quantities'))
    frame.write_parquet(path);seal(path)
    response=post(c,payload)
    assert response.status_code==200,response.text
    assert response.json()['reason_codes']==['pricing_or_coverage_capture_invalid']
    assert not adopted(c);assert_no_manual_money(c)


def test_v2_history_client_construction_failure_is_audited_pending(contract,monkeypatch):
    c=contract;missing_source(c);grant(c)
    def unavailable(*args,**kwargs):raise HistoryUnavailable('history_source_not_configured')
    monkeypatch.setattr(facts.HistoryClient,'from_env',unavailable)
    response=post(c,body(c))
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='pending' and result['reason_codes']==['history_source_not_configured']
    assert not adopted(c);assert_no_manual_money(c)


@pytest.mark.parametrize('count',[0,101])
def test_v2_batch_is_bounded_before_any_source_or_write(contract,count):
    c=contract;missing_source(c);grant(c);payload=body(c)
    payload['items']=[{'coverage_key':'key-'+str(i),'context_sha':'a'*64,'expected_line_revision':0} for i in range(count)]
    response=post(c,payload)
    assert response.status_code==422,response.text
    assert not adopted(c);assert_no_manual_money(c)
