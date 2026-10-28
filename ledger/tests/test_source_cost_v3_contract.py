
"""V3 adoption contract over real raw JST/original bytes, never a money golden."""
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import json
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger import automation_source_cost_v3 as v3,automation_actions as actions,automation_checker as checker
from ledger import automation_period_proof as period_proof,automation_store as journal,source_recovery
from ledger.automation_api import install
from ledger.upstream_history import HistoryClient
from ledger.storage_integrity import seal
from test_source_recovery import source_case,PERIOD
from test_source_recovery_current import completed
from test_publication_scope import configure


@pytest.fixture
def v3_contract(source_case,monkeypatch):
    c=source_case
    configure(monkeypatch,c.ws.root,(('s1',PERIOD),))
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','1')
    monkeypatch.setenv('LEDGER_SOURCE_COST_V3_ENABLED','1')
    human={'id':'synthetic-finance','authenticated':True,'kind':'human'}
    stamp=datetime.now(timezone.utc)
    proposal=actions.decision_preview(c.ws,c.model,request_id='v3-grant-preview',period=PERIOD,store_id='s1',
        kind='rule_grant',values={'enabled':True,'rule_id':v3.RULE,'rule_sha':v3.RULE_SHA,
            'valid_from':(stamp-timedelta(days=1)).isoformat(),'valid_until':(stamp+timedelta(days=1)).isoformat()},
        reason='synthetic authenticated human v3 source-only grant',actor=human)
    grant=actions.decision_apply(c.ws,c.model,request_id='v3-grant-apply',proposal_id=proposal['proposal_id'],
        proposal_sha=proposal['proposal_sha'],input_sha=proposal['input_sha'],actor=human)
    # A NEW declared synthetic input/run has its own source and proof. Never
    # alter/reseal an existing immutable run to get the expected result.
    old=c.ws.latest_run('s1',PERIOD);old_id=old['id'];payload=json.loads(old['result'])
    facts=pl.read_parquet(c.ws.facts_path(old_id))
    coverage=pl.read_parquet(c.ws.coverage_gaps_path(old_id)).with_columns(pl.lit('C1').alias('sub_order_id'))
    rid=c.ws.record('s1',PERIOD,payload,json.loads(old['files_json']) if 'files_json' in old.keys() else [])
    coverage.write_parquet(c.ws.coverage_gaps_path(rid));seal(c.ws.coverage_gaps_path(rid))
    facts.write_parquet(c.ws.facts_path(rid));seal(c.ws.facts_path(rid))
    items=[]
    for row in c.ws.conn.execute('SELECT name,sha FROM slot WHERE store_id=?',('s1',)):
        role='order' if 'native' in row['name'] else 'cost'
        values={'order_id':['202600001'],'__sheet__':['证据'],'__row__':[3]} if role=='order' else {
            'original_order_id':['202600001'],'internal_order_id':['100'],'__sheet__':['证据'],'__row__':[3]}
        items.append(SimpleNamespace(ref=SimpleNamespace(sha256=row['sha']),template=SimpleNamespace(source=role),frame=pl.DataFrame(values)))
    ing=SimpleNamespace(model=c.model,items=items,known=items)
    sl=SimpleNamespace(period=PERIOD,facts=facts,pricing_gaps=pl.DataFrame(),coverage_gap_rows=coverage)
    proof=period_proof.prepare(c.ws,c.model,c.model.store('s1'),ing,sl,payload,None)
    period_proof.save(c.ws,rid,proof)
    actions.sync_month(c.ws,c.model,PERIOD)
    current=checker.check(c.ws,'s1',PERIOD,model=c.model)
    c.body={**c.body,'run_id':rid,'input_sha':current['input_sha']}
    completed(c)
    # Read raw response through the original provider's quote implementation.
    class Quotes(HistoryClient):
        def _fetch(self,*_):
            value={'data':[{'sku_id':'A','wms_co_id':'10','begin_date':'2026-05-01','end_date':'2026-05-31','cost_price':'3.335'}]}
            return json.dumps(value).encode(),value['data']
    # This file tests source/HTTP/quote contracts, not financial consumption
    # selection. The latter is tested against actual engine/run proof below.
    monkeypatch.setattr(v3,'used',lambda ws,sid,period:v3.active(ws.conn,sid,period))
    c.history=Quotes(c.ws.root)
    c.service={**c.actor,'capabilities':['approved_action']}
    c.grant_id=grant['decision_id'];c.human=human
    def request():
        actions.sync_month(c.ws,c.model,PERIOD)
        checked=checker.check(c.ws,'s1',PERIOD,model=c.model)
        return {**c.body,'input_sha':checked['input_sha'],'request_id':'v3-source-once','rule_id':v3.RULE,
            'store_id':'s1','period':PERIOD,'expected_line_revision':0}
    c.request=request
    # deliver is a persistent native handoff in production; don't create a
    # Registry merely as a side effect of this isolated source-contract test.
    monkeypatch.setattr(actions,'deliver',lambda *_:None)
    return c


def test_v3_raw_source_adoption_is_not_money_or_close_and_is_idempotent(v3_contract):
    c=v3_contract;body=c.request()
    first=v3.apply(c.ws,c.model,body,c.service,history_client=c.history)
    assert first['status']=='adopted' and first['financial_applied'] is False and first['recompute_required']
    assert v3.apply(c.ws,c.model,body,c.service,history_client=c.history)==first
    facts=v3.active(c.ws.conn,'s1',PERIOD)
    assert len(facts)==1 and facts[0]['members'][0]['quantity']=='1.25'
    assert facts[0]['members'][0]['observed_flags']['source_header']['status']['value']=='Split'
    assert facts[0]['quotes'][0]['unit_cost']=='3.335'
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()
    assert c.ws.state('s1',PERIOD).state=='open'
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='dispatch_pending' AND request_id='approved-physical-source:v3-source-once'").fetchone()[0]==1


def test_v3_disabled_is_pending_without_adoption_or_financial_write(v3_contract,monkeypatch):
    c=v3_contract;monkeypatch.setenv('LEDGER_SOURCE_COST_V3_ENABLED','0')
    result=v3.apply(c.ws,c.model,c.request(),c.service,history_client=c.history)
    assert result['status']=='pending' and result['reason_codes']==['source_cost_v3_disabled']
    assert not v3.active(c.ws.conn,'s1',PERIOD)
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()


def test_v3_requires_service_capability_distinct_human_grant_and_current_cas(v3_contract):
    c=v3_contract;body=c.request()
    with pytest.raises(PermissionError):v3.apply(c.ws,c.model,body,c.actor,history_client=c.history)
    with pytest.raises(actions.Conflict):v3.apply(c.ws,c.model,{**body,'input_sha':'stale'},c.service,history_client=c.history)
    with c.ws.conn:
        c.ws.conn.execute('INSERT INTO automation_decision VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('withdraw-grant','withdraw-grant', 'rule_grant',PERIOD,'s1','revoke',c.grant_id,'{}','synthetic withdrawal','x','','finance',journal.now()))
    with pytest.raises(PermissionError):v3.apply(c.ws,c.model,body,c.service,history_client=c.history)


def test_v3_http_does_not_accept_money_units_or_arbitrary_source_binding(v3_contract):
    c=v3_contract;app=FastAPI();install(app,lambda:c.ws,lambda:c.model,lambda _:c.service)
    with TestClient(app) as client:
        for extra in ({'amount':1},{'quantity':'1'},{'source_binding':{}},{'path':'/tmp/source'}):
            response=client.post('/api/automation/service-physical-facts',json={**c.request(),**extra})
            assert response.status_code==422

def test_quote_refresh_is_a_real_provider_capture_and_confirm_binds_one_generation(v3_contract,monkeypatch):
    c=v3_contract;v3.apply(c.ws,c.model,c.request(),c.service,history_client=c.history)
    calls=[];fetch=c.history._fetch
    def counted(*args):calls.append(args);return fetch(*args)
    monkeypatch.setattr(c.history,'_fetch',counted)
    task=v3.ensure_quote_refresh(c.ws,c.model,PERIOD,['s1'],'close-source-one',c.human)
    assert task['status']=='processing' and calls==[]
    assert v3.quote_close_reasons(c.ws,'s1',PERIOD)==['physical_quote_refresh_pending']
    v3.process_quote_refresh(c.ws,c.model,'s1',PERIOD,history_client=c.history)
    ready=v3.quote_refresh_view(c.ws,task['refresh_id'])
    assert ready['status']=='verified' and len(calls)==1
    assert v3.verify_quote_refresh(c.ws,c.model,PERIOD,['s1'],ready['refresh_id'],ready['quote_sha'])
    assert v3.quote_close_reasons(c.ws,'s1',PERIOD)==[]
    # Another preview creates its own explicit active generation, never a TTL
    # guess or a re-use of local raw SHA as if it were an upstream query.
    next_task=v3.ensure_quote_refresh(c.ws,c.model,PERIOD,['s1'],'close-source-two',c.human)
    assert next_task['refresh_id']!=ready['refresh_id'] and len(calls)==1
    with pytest.raises(actions.Conflict,match='代际'):
        v3.verify_quote_refresh(c.ws,c.model,PERIOD,['s1'],ready['refresh_id'],ready['quote_sha'])
    assert len(v3.active(c.ws.conn,'s1',PERIOD))==1


def test_provider_price_correction_is_pending_without_editing_old_financial_fact(v3_contract,monkeypatch):
    c=v3_contract;v3.apply(c.ws,c.model,c.request(),c.service,history_client=c.history)
    before=v3.fingerprint(c.ws,'s1',PERIOD)
    task=v3.ensure_quote_refresh(c.ws,c.model,PERIOD,['s1'],'close-price-change',c.human)
    def changed(*_):
        value={'data':[{'sku_id':'A','wms_co_id':'10','begin_date':'2026-05-01','end_date':'2026-05-31','cost_price':'4'}]}
        return json.dumps(value).encode(),value['data']
    monkeypatch.setattr(c.history,'_fetch',changed)
    v3.process_quote_refresh(c.ws,c.model,'s1',PERIOD,history_client=c.history)
    view=v3.quote_refresh_view(c.ws,task['refresh_id'])
    assert view['status']=='pending' and view['reason_codes']==['physical_source_quote_changed']
    assert v3.fingerprint(c.ws,'s1',PERIOD)==before
    assert v3.active(c.ws.conn,'s1',PERIOD)[0]['quotes'][0]['unit_cost']=='3.335'
    with pytest.raises(actions.Conflict):v3.verify_quote_refresh(c.ws,c.model,PERIOD,['s1'],view['refresh_id'],view['quote_sha'])
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()


def test_provider_outage_is_pending_and_keeps_actual_retained_failure_refs(v3_contract,monkeypatch):
    from ledger.upstream_history import HistoryUnavailable
    c=v3_contract;v3.apply(c.ws,c.model,c.request(),c.service,history_client=c.history)
    task=v3.ensure_quote_refresh(c.ws,c.model,PERIOD,['s1'],'close-price-outage',c.human)
    def failed(*_):raise HistoryUnavailable('history_source_login_required')
    monkeypatch.setattr(c.history,'_fetch',failed)
    v3.process_quote_refresh(c.ws,c.model,'s1',PERIOD,history_client=c.history)
    view=v3.quote_refresh_view(c.ws,task['refresh_id'])
    assert view['status']=='pending' and view['reason_codes']==['history_source_login_required']


def test_manager_quote_progress_never_reparses_or_recomputes_the_store(v3_contract,monkeypatch):
    from ledger.commission_manager import Manager
    from ledger import service
    c=v3_contract;v3.apply(c.ws,c.model,c.request(),c.service,history_client=c.history)
    task=v3.ensure_quote_refresh(c.ws,c.model,PERIOD,['s1'],'close-manager-read',c.human)
    monkeypatch.setattr(HistoryClient,'from_env',classmethod(lambda cls,root:c.history))
    def forbidden(*args,**kwargs):raise AssertionError('a quote read must not run the financial engine')
    monkeypatch.setattr(service,'recompute',forbidden)
    before=c.ws.latest_run('s1',PERIOD)['id']
    Manager(lambda:c.ws,lambda:c.model).once()
    assert v3.quote_refresh_view(c.ws,task['refresh_id'])['status']=='verified'
    assert c.ws.latest_run('s1',PERIOD)['id']==before
    assert not c.ws.conn.execute('SELECT 1 FROM cost_line_log').fetchone()


def test_confirmed_payout_blocks_new_source_adoption(v3_contract):
    from ledger.commission_registry import Registry
    c=v3_contract;body=c.request();registry=Registry(c.ws.root)
    with registry.transaction() as conn:
        conn.execute('INSERT INTO payout_confirmation VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('protected','s1',PERIOD,body['run_id'],journal.now(),'finance','synthetic confirmed payout','source-sha','[]','[]','0'))
    result=v3.apply(c.ws,c.model,body,c.service,history_client=c.history)
    assert result['status']=='pending' and result['reason_codes']==['confirmed_payout_preserved']
    assert not v3.active(c.ws.conn,'s1',PERIOD)
    with registry.connect() as conn:
        assert conn.execute('SELECT count(*) FROM payout_confirmation').fetchone()[0]==1


def test_v3_real_write_middleware_requires_dedicated_capability(v3_contract,monkeypatch):
    from hashlib import sha256
    from pathlib import Path
    from ledger import write_auth
    from ledger.commission_registry import Registry
    c=v3_contract;Registry(c.ws.root)
    token='synthetic-v3-service-token'
    identities=Path(c.tmp_path)/'v3-identities.json'
    entry={'id':'synthetic-v3-worker','enabled':True,'token_sha256':sha256(token.encode()).hexdigest(),
        'expires_at':datetime.now(timezone.utc).timestamp()+3600,'scopes':['business'],'capabilities':['approved_action']}
    identities.write_text(json.dumps({'identities':[entry]}))
    monkeypatch.setenv('LEDGER_SERVICE_IDENTITIES',str(identities))
    monkeypatch.setenv('LEDGER_SOURCE_COST_V3_ENABLED','0')
    app=FastAPI();write_auth.install(app,lambda:c.ws)
    install(app,lambda:c.ws,lambda:c.model,lambda request:write_auth.strict_actor(request,c.ws))
    with TestClient(app) as client:
        body=c.request()
        assert client.post('/api/automation/service-physical-facts',json=body).status_code==401
        response=client.post('/api/automation/service-physical-facts',json=body,headers={'Authorization':'Bearer '+token})
        assert response.status_code==200,response.text
        assert response.json()['status']=='pending' and response.json()['reason_codes']==['source_cost_v3_disabled']
        entry['capabilities']=['recompute'];identities.write_text(json.dumps({'identities':[entry]}))
        assert client.post('/api/automation/service-physical-facts',json=body,headers={'Authorization':'Bearer '+token}).status_code==403


def test_explicit_withdrawal_is_not_silently_undone_by_automatic_retry(v3_contract):
    c=v3_contract;body=c.request()
    adopted=v3.apply(c.ws,c.model,body,c.service,history_client=c.history)
    row=c.ws.conn.execute('SELECT * FROM automation_decision WHERE id=?',(adopted['decision_id'],)).fetchone()
    input_sha=actions._decision_input(c.ws,c.model,v3.RULE,'s1',PERIOD,json.loads(row['values_json']))
    revoked=actions.decision_revoke(c.ws,c.model,adopted['decision_id'],request_id='withdraw-source-once',
        reason='synthetic human withdrawal',input_sha=input_sha,actor=c.human)
    assert revoked['status']=='revoked' and not v3.active(c.ws.conn,'s1',PERIOD)
    with pytest.raises(actions.Conflict,match='撤回'):
        v3.apply(c.ws,c.model,body,c.service,history_client=c.history)
    result=v3.apply(c.ws,c.model,{**c.request(),'request_id':'different-auto-retry'},c.service,history_client=c.history)
    assert result['status']=='pending' and result['reason_codes']==['withdrawn_physical_source_requires_new_human_grant']
    assert not v3.active(c.ws.conn,'s1',PERIOD)
    assert c.ws.conn.execute('SELECT count(*) FROM automation_decision WHERE id=?',(adopted['decision_id'],)).fetchone()[0]==1


def test_later_valid_human_order_cost_takes_priority_over_an_unconsumed_fact(v3_contract,monkeypatch):
    from ledger.model.schema import Store
    from test_source_cost_posting import sample
    c=v3_contract;v3.apply(c.ws,c.model,c.request(),c.service,history_client=c.history)
    preview=actions.decision_preview(c.ws,c.model,request_id='human-cost-preview',period=PERIOD,store_id='s1',kind='cost_line',
        values={'run_id':c.body['run_id'],'coverage_key':c.body['coverage_key'],'context_sha':c.body['context_sha'],
            'amount':9,'expected_line_revision':0},reason='synthetic human source-backed order cost',actor=c.human)
    actions.decision_apply(c.ws,c.model,request_id='human-cost-apply',proposal_id=preview['proposal_id'],
        proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=c.human)
    assert c.ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0]==1
    def forbidden(*args,**kwargs):raise AssertionError('current valid human authority must be checked before source consumption')
    monkeypatch.setattr(source_recovery,'verified_fact_source',forbidden)
    ing,baseline,store,*_=sample()
    store=store.model_copy(update={'id':'s1','platform':'taobao'})
    ing.model=ing.model.model_copy(update={'stores':(store,)})
    result=v3.consume(c.ws,ing.model,store,ing,baseline)
    assert not ing.physical_cost_evidence
    assert result is baseline
