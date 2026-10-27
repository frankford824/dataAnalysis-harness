"""Trusted local tool receipts, never model-authored source diagnostics."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from ledger_agent.upstream import Collector,INTEGRATION
from test_upstream import CASE,setup,collect,action_source

BOUND={**CASE,'source_context_sha':'source-contract-a'}


def run_tool(setup,tool='collect_case_evidence',case=None,evidence=None):
    collector,_=setup
    return collector.tools(lambda:{'lease_active':True,'input_sha':CASE['input_sha'],'verified':False})[tool](
        deepcopy(case or BOUND),evidence)


@pytest.mark.parametrize('scenario,source,reason',[
    ('login','order','login_required'),('timeout','order','source_unavailable'),
    ('http503','order','source_unavailable'),('price_missing','history_cost','original_evidence_missing'),
    ('sku_zero','order','sku_contract_unknown'),('broken_contract','system','technical_failure'),
])
def test_source_wait_uses_actual_local_receipts_and_fixed_classification(setup,scenario,source,reason):
    collector,wire=setup
    wire.order['items'][0]['sku_type']='normal'
    path=INTEGRATION+'/orders/123'
    if scenario=='login':wire.override[path]=lambda r:httpx.Response(401,json={'error':'expired'})
    elif scenario=='timeout':
        def failed(request):raise httpx.ReadTimeout('private-order-credential',request=request)
        wire.override[path]=failed
    elif scenario=='http503':wire.override[path]=lambda r:httpx.Response(503,json={'error':'unavailable'})
    elif scenario=='price_missing':wire.history=[]
    elif scenario=='sku_zero':wire.order['items'][0]['sku_type']='0'
    elif scenario=='broken_contract':wire.override[path]=lambda r:httpx.Response(200,json=[])
    result=run_tool(setup)
    outcomes=collector.trusted_outcomes(BOUND)
    assert any(o['source_kind']==source and o['reason_code']==reason for o in outcomes),outcomes
    for outcome in outcomes:
        assert set(outcome)=={'tool_id','source_kind','reason_code','artifact_refs'}
        assert outcome['tool_id']=='collect_case_evidence'
        assert any(ref['path']==result['receipt'] for ref in outcome['artifact_refs'])
        for ref in outcome['artifact_refs']:
            assert set(ref)=={'path','sha256'}
            assert hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest()==ref['sha256']
    assert 'private-order-credential' not in json.dumps(outcomes)
    assert 'tool_outcomes' not in result


def test_mutating_model_visible_result_and_evidence_cannot_invent_a_diagnosis(setup):
    collector,wire=setup;wire.order['items'][0]['sku_type']='normal'
    result=run_tool(setup,evidence={'reason':'source_unavailable','tool_outcomes':[{'reason_code':'login_required'}]})
    assert result['status']=='found'
    result.update(status='login_required',reason='order_login_required',tool_outcomes=[{'amount':'3.34'}])
    assert collector.trusted_outcomes(BOUND)==[]


@pytest.mark.parametrize('change',[
    {'source_context_sha':'different-rule'},{'input_sha':'different-input'},{'run_id':8},
    {'id':'different-case'},{'store_id':'other-store'},{'period':'2026-07'},
])
def test_source_receipt_is_exactly_case_input_run_and_rule_bound(setup,change):
    collector,wire=setup
    wire.override[INTEGRATION+'/orders/123']=lambda r:httpx.Response(401,json={})
    run_tool(setup)
    assert collector.trusted_outcomes(BOUND)
    assert collector.trusted_outcomes({**BOUND,**change})==[]
    assert collector.trusted_outcomes(CASE)==[]  # No captured source context is unknown.


def test_receipt_survives_restart_but_hash_change_cannot_be_reclassified(setup):
    collector,wire=setup
    wire.override[INTEGRATION+'/orders/123']=lambda r:httpx.Response(401,json={})
    result=run_tool(setup);calls=len(wire.calls)
    reopened=Collector(collector.config,collector.root,client=collector.client)
    assert reopened.trusted_outcomes(BOUND)==collector.trusted_outcomes(BOUND)
    assert len(wire.calls)==calls  # Reading the handoff never probes production again.
    path=Path(result['receipt']);path.write_text('{"status":"login_required"}')
    assert reopened.trusted_outcomes(BOUND)==[]


def test_identical_configuration_restart_keeps_the_same_private_wait_epoch_without_credentials(setup):
    collector,wire=setup
    wire.override[INTEGRATION+'/orders/123']=lambda r:httpx.Response(401,json={})
    result=run_tool(setup);expected=collector.trusted_outcomes(BOUND)
    restarted=Collector(replace(collector.config),collector.root,client=collector.client)
    before=len(wire.calls)
    assert restarted.configuration_sha()==collector.configuration_sha()
    assert restarted.trusted_outcomes(BOUND)==expected and len(wire.calls)==before
    stored=json.loads(Path(result['receipt']).read_bytes())
    assert stored['case_scope']['configuration_sha']==collector.configuration_sha()
    assert len(collector.configuration_sha())==64
    all_files=''.join(path.read_text() for path in collector.root.rglob('*.json'))
    for token in (collector.config.ledger_token,collector.config.order_token,collector.config.cost_token):
        assert token not in all_files


def test_repaired_credentials_or_source_origin_cannot_replay_an_old_login_pointer(setup):
    collector,wire=setup
    wire.override[INTEGRATION+'/orders/123']=lambda r:httpx.Response(401,json={})
    run_tool(setup);assert collector.trusted_outcomes(BOUND)
    before=len(wire.calls)
    for change in ({'order_token':'new-private-order-credential'},{'order_base_url':'http://repaired-order-origin'}):
        repaired=Collector(replace(collector.config,**change),collector.root,client=collector.client)
        assert repaired.configuration_sha()!=collector.configuration_sha()
        assert repaired.trusted_outcomes(BOUND)==[]
    assert len(wire.calls)==before  # Configuration invalidation is local, not another source probe.


def test_source_artifact_hash_failure_does_not_trust_even_a_valid_receipt(setup):
    collector,wire=setup
    wire.override[INTEGRATION+'/orders/123']=lambda r:httpx.Response(401,json={})
    result=run_tool(setup);assert collector.trusted_outcomes(BOUND)
    Path(result['artifact_refs'][0]['path']).write_text('changed original artifact')
    assert collector.trusted_outcomes(BOUND)==[]


def test_new_success_replaces_old_login_wait_without_accumulating_history(setup):
    collector,wire=setup;wire.order['items'][0]['sku_type']='normal'
    wire.override[INTEGRATION+'/orders/123']=lambda r:httpx.Response(401,json={})
    run_tool(setup);first=collector.trusted_outcomes(BOUND)
    assert len(first)==1
    run_tool(setup);second=collector.trusted_outcomes(BOUND)
    assert len(second)==1  # Same reason across attempts is one current outcome.
    wire.override.clear();assert run_tool(setup)['status']=='found'
    assert collector.trusted_outcomes(BOUND)==[]


@pytest.mark.parametrize('consumed',[29,30])
def test_existing_feed_or_recompute_work_does_not_become_human_missing_material(setup,consumed):
    collector,wire=setup;wire.order['items'][0]['sku_type']='normal'
    wire.gap['reason']='member evidence missing'
    wire.queue.update(evidence_complete=True,status='complete',last_seq=30)
    wire.feed['consumed_seq']=consumed
    result=run_tool(setup,'poll_member_seller_evidence')
    assert result['items'][0]['request']['reason'] in {'await_ledger_feed','await_case_recompute'}
    assert collector.trusted_outcomes(BOUND)==[]


@pytest.mark.parametrize('reason',['component_or_unit_conversion_unproved','historical_amount_not_exact_cents',
                                  'partial_cost_requires_component_reconciliation'])
def test_generic_financial_exclusions_are_not_invented_source_or_sku_diagnoses(setup,reason):
    collector,wire=setup;action_source(wire)
    wire.action={'status':'pending','reason_codes':[reason],'source_refs':[],'recompute_required':False}
    run_tool(setup,'apply_approved_history_costs')
    assert collector.trusted_outcomes(BOUND)==[]


def test_accepted_standard_pipeline_publication_clears_older_blocking_observation(setup):
    collector,wire=setup;wire.order['items'][0]['sku_type']='normal'
    wire.override[INTEGRATION+'/orders/123']=lambda r:httpx.Response(401,json={})
    run_tool(setup);assert collector.trusted_outcomes(BOUND)
    action_source(wire)
    assert run_tool(setup,'apply_approved_history_costs')['recompute_required'] is True
    assert collector.trusted_outcomes(BOUND)==[]


def test_ambiguous_component_code_needs_exact_raw_sku_evidence(setup):
    collector,wire=setup;action_source(wire)
    wire.action={'status':'pending','reason_codes':['sku_type_contract_unknown'],'source_refs':[],'recompute_required':False}
    run_tool(setup,'apply_approved_history_costs')
    outcomes=collector.trusted_outcomes(BOUND)
    assert len(outcomes)==1 and outcomes[0]['source_kind']=='history_cost' and outcomes[0]['reason_code']=='sku_contract_unknown'


@pytest.mark.parametrize('price',['0','-1','NaN'])
def test_present_but_invalid_or_zero_price_is_not_misreported_as_missing_original_material(setup,price):
    collector,wire=setup;wire.order['items'][0]['sku_type']='normal'
    wire.history[0]['cost_price']=price
    result=run_tool(setup)
    assert result['status']=='pending' and result['items'][0]['history'][0]['invalid_price_observed']
    assert collector.trusted_outcomes(BOUND)==[]


def test_later_authorization_denial_cannot_erase_a_verified_source_login_problem(setup):
    collector,wire=setup;wire.order['items'][0]['sku_type']='normal'
    wire.override[INTEGRATION+'/orders/123']=lambda r:httpx.Response(401,json={})
    run_tool(setup)
    action_source(wire)
    wire.override['/api/automation/service-actions']=lambda r:httpx.Response(403,json={'detail':'grant required'})
    assert run_tool(setup,'apply_approved_history_costs')['status']=='denied'
    outcomes=collector.trusted_outcomes(BOUND)
    assert len(outcomes)==1 and outcomes[0]['source_kind']=='order' and outcomes[0]['reason_code']=='login_required'


def test_positive_original_order_replaces_order_login_with_only_current_history_problem(setup):
    collector,wire=setup;wire.order['items'][0]['sku_type']='normal'
    wire.override[INTEGRATION+'/orders/123']=lambda r:httpx.Response(401,json={})
    run_tool(setup)
    wire.override.clear();wire.history=[]
    run_tool(setup)
    outcomes=collector.trusted_outcomes(BOUND)
    assert len(outcomes)==1 and outcomes[0]['source_kind']=='history_cost'
    assert outcomes[0]['reason_code']=='original_evidence_missing'


def test_present_but_invalid_quote_retires_missing_material_without_claiming_a_financial_value(setup):
    collector,wire=setup;wire.order['items'][0]['sku_type']='normal'
    wire.history=[];run_tool(setup)
    assert collector.trusted_outcomes(BOUND)[0]['reason_code']=='original_evidence_missing'
    wire.history=[{'sku_id':'SKU-1','wms_co_id':9,'begin_date':'开始','end_date':'2026-06-30','cost_price':'0'}]
    assert run_tool(setup)['status']=='pending'
    assert collector.trusted_outcomes(BOUND)==[]


def test_backend_retained_original_and_quote_precision_rejection_clears_old_source_block_not_into_maintenance(setup):
    collector,wire=setup;wire.order['items'][0]['sku_type']='normal'
    wire.override[INTEGRATION+'/orders/123']=lambda r:httpx.Response(401,json={})
    run_tool(setup);assert collector.trusted_outcomes(BOUND)
    action_source(wire)
    wire.action={'status':'pending','reason_codes':['historical_amount_not_exact_cents'],
        'source_refs':[{'path':'server-original-quote','sha256':'a'*64}],'recompute_required':False}
    result=run_tool(setup,'apply_approved_history_costs')
    assert result['financial_applied'] is False
    assert collector.trusted_outcomes(BOUND)==[]


def test_interrupted_pointer_update_cannot_resurrect_old_missing_after_material_was_retained(setup,monkeypatch):
    collector,wire=setup;wire.order['items'][0]['sku_type']='normal'
    wire.override[INTEGRATION+'/orders/123']=lambda r:httpx.Response(401,json={})
    run_tool(setup);assert collector.trusted_outcomes(BOUND)
    wire.override.clear()
    monkeypatch.setattr(collector,'_remember_receipt',lambda case,path:None)
    assert run_tool(setup)['status']=='found'
    assert collector.trusted_outcomes(BOUND)==[]
