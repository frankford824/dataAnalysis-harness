"""Actual adopted/used source IDs fence a post-revoke delivery crash gap."""
from datetime import datetime,timedelta,timezone
import json
import pytest
from ledger import automation_actions as actions,automation_checker as checker,automation_store as journal,automation_deterministic as deterministic
from ledger import historical_cost_facts as v2,automation_source_cost_v3 as v3
from ledger.commission_registry import Registry
from conftest import scoped_manager as ScopedManager
from test_historical_cost_facts_engine import native_factory
from test_source_close_integration import source_close_case,bridge
from test_source_frozen_cost_basis import frozen_case


class AfterCommit(Exception):pass


def actor(reg):
    reg.operator_add('freshness-finance','synthetic-password')
    return {**reg.actor(reg.login('freshness-finance','synthetic-password')),'kind':'human','authenticated':True}


def revoke_gap(c,decision_id,monkeypatch):
    row=c.ws.conn.execute('SELECT * FROM automation_decision WHERE id=?',(decision_id,)).fetchone()
    sha=actions._decision_input(c.ws,c.model,row['kind'],row['store_id'],row['period'],json.loads(row['values_json']))
    original=actions.deliver
    def crash(*_):raise AfterCommit('declared post-commit/pre-Registry handoff failure')
    monkeypatch.setattr(actions,'deliver',crash)
    with pytest.raises(AfterCommit):
        actions.decision_revoke(c.ws,c.model,decision_id,request_id='withdraw-'+decision_id,reason='declared authenticated source withdrawal',input_sha=sha,actor=c.human)
    assert c.ws.conn.execute("SELECT 1 FROM automation_decision WHERE action='revoke' AND ref_id=?",(decision_id,)).fetchone()
    assert c.ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state='dispatch_pending'").fetchone()
    with c.reg.connect() as db:assert db.execute('SELECT count(*) FROM pending').fetchone()[0]==0
    return original


def assert_rejected(c):
    checked=checker.check(c.ws,'s1',c.period,model=c.model,verify_artifacts=True)
    assert not checked['allowed'] and 'source_fact_input_unverified' in {r['code'] for r in checked['issues']}
    state=next(r for r in actions.month(c.ws,c.model,c.period)['stores'] if r['store_id']=='s1')
    assert state['status']=='processing' and state['stage']=='calculating'
    run=c.ws.latest_run('s1',c.period)
    view=checker.close_view(c.ws,c.model,'s1',c.period,{**json.loads(run['result']),'run_id':run['id']})
    assert not view['can_close'] and 'source_fact_input_unverified' in {r['code'] for r in view['close_verification']['issues']}
    preview=actions.close_preview(c.ws,c.model,request_id='fresh-after-source-withdrawal',period=c.period,store_ids=['s1'],actor=c.human)
    assert not preview.get('eligible')
    if preview.get('proposal_id'):
        result=actions.close_confirm(c.ws,c.model,request_id='fresh-confirm-after-source-withdrawal',proposal_id=preview['proposal_id'],proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=c.human)
        assert not result['completed']
    assert not c.ws.state('s1',c.period).closed


@pytest.fixture
def actual_v2(native_factory):
    from types import SimpleNamespace
    ws,model,reg,calls,_hashes,_feed=native_factory(1,'3.335')
    reg.enqueue_files({'s1'},'nas:'+ws.conn.execute("SELECT sha FROM slot WHERE store_id='s1'").fetchone()[0])
    manager=ScopedManager(lambda:ws,lambda:model);manager.once();manager.once()
    active=v2.active(ws.conn,'s1','2026-06');assert len(active)==1
    rid=ws.latest_run('s1','2026-06')['id'];proof=json.loads(ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone()[0])
    assert any(s['decision_id']==active[0]['decision_id'] for s in proof['approved_cost_sources'])
    assert '/api/cost/history' in calls and checker.check(ws,'s1','2026-06',model=model)['allowed']
    return SimpleNamespace(ws=ws,model=model,reg=reg,manager=manager,period='2026-06',fact=active[0],human=actor(reg))


@pytest.fixture
def actual_v3(source_close_case):
    from test_replay_service import seed
    c=source_close_case;c.reg=Registry(c.ws.root);fixed=seed()
    for person in fixed['people']:c.reg.person_save(person,'declared fixture','synthetic identity')
    c.reg.save_store_member('s1','p1','produce','','declared fixture','synthetic roster',valid_from='2026-05-01')
    scheme=fixed['schemes'][0]['body'];scheme['segments'][0]['valid_from']='2026-05-01'
    c.reg.save_scheme('s1','123456789001',scheme,'declared fixture','synthetic May 5 percent scheme',publish=True)
    c.reg.save_policy(fixed['policies'][0]['body'],'2026-05-01','declared fixture','synthetic policy')
    for _ in range(8):
        with c.reg.connect() as db:pending=db.execute('SELECT count(*) FROM pending').fetchone()[0]
        if not pending:break
        c.manager.once()
    bridge(c)  # Real source capture + actual HTTP quote refresh before baseline readiness.
    assert checker.check(c.ws,'s1','2026-05',model=c.model,verify_artifacts=True)['allowed'],checker.check(c.ws,'s1','2026-05',model=c.model,verify_artifacts=True)['issues']
    used=v3.used(c.ws,'s1','2026-05');assert len(used)==1
    c.period='2026-05';c.fact=used[0];c.human=actor(c.reg)
    return c


@pytest.mark.parametrize('target',['fact','grant'])
def test_actual_v2_source_revoke_committed_before_delivery_blocks_fresh_preview(actual_v2,monkeypatch,target):
    c=actual_v2;old_run=c.ws.latest_run('s1',c.period)['id']
    row=c.ws.conn.execute('SELECT request_id FROM automation_decision WHERE id=?',(c.fact['decision_id'],)).fetchone()
    auth=json.loads(c.ws.conn.execute('SELECT scope_json FROM automation_authorization WHERE request_id=?',(row[0],)).fetchone()[0])
    decision=c.fact['decision_id'] if target=='fact' else auth['approved_rule_grant']
    original=revoke_gap(c,decision,monkeypatch)
    assert_rejected(c)
    assert c.ws.latest_run('s1',c.period)['id']==old_run
    monkeypatch.setattr(actions,'deliver',original);actions.deliver(c.ws)
    c.manager.once()
    current=checker.check(c.ws,'s1',c.period,model=c.model,verify_artifacts=True)
    assert not current['allowed']
    if target=='fact':assert 'source_fact_input_unverified' not in {r['code'] for r in current['issues']}


@pytest.mark.parametrize('target',['fact','grant'])
def test_actual_v3_raw_used_revoke_committed_before_delivery_blocks_fresh_preview(actual_v3,monkeypatch,target):
    c=actual_v3;old_run=c.ws.latest_run('s1',c.period)['id']
    decision=c.fact['decision_id'] if target=='fact' else c.fact['authorization']['approved_rule_grant']
    original=revoke_gap(c,decision,monkeypatch)
    assert_rejected(c)
    assert c.ws.latest_run('s1',c.period)['id']==old_run
    monkeypatch.setattr(actions,'deliver',original);actions.deliver(c.ws);c.manager.once()
    current=checker.check(c.ws,'s1',c.period,model=c.model,verify_artifacts=True)
    assert not current['allowed']
    if target=='fact':assert 'source_fact_input_unverified' not in {r['code'] for r in current['issues']}


@pytest.mark.parametrize('fixture_name',['actual_v2','actual_v3'])
def test_natural_grant_expiry_and_kill_switch_leave_established_used_basis_valid(request,fixture_name,monkeypatch):
    c=request.getfixturevalue(fixture_name)
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','0');monkeypatch.setenv('LEDGER_SOURCE_COST_V3_ENABLED','0')
    class Future(datetime):
        @classmethod
        def now(cls,tz=None):return datetime.now(tz or timezone.utc)+timedelta(days=30)
    monkeypatch.setattr(deterministic,'datetime',Future)
    rule,sha=(v2.RULE,v2.RULE_SHA) if fixture_name=='actual_v2' else (v3.RULE,v3.RULE_SHA)
    with pytest.raises(PermissionError):deterministic.approved_rule(c.ws,'s1',c.period,rule_id=rule,rule_sha=sha)
    current=checker.check(c.ws,'s1',c.period,model=c.model,verify_artifacts=True)
    assert current['allowed'] and 'source_fact_input_unverified' not in {r['code'] for r in current['issues']}


def test_closed_confirmed_actual_v2_record_refuses_fact_withdrawal_and_keeps_hash(actual_v2,monkeypatch):
    from ledger import commission_confirm
    c=actual_v2
    context=commission_confirm.context(c.ws,c.reg,c.model,'s1',c.period,expected_run=c.ws.latest_run('s1',c.period)['id'])
    commission_confirm.confirm(c.ws,c.reg,c.model,store_id='s1',period=c.period,run_id=context['run_id'],source_sha=context['source_sha'],
        expected_confirmation_id=context['latest']['id'] if context['latest'] else '',payouts=[{'person_id':'p1','amount':'0.33'}],no_payout=False,
        reason='Declared (10-3.335)*5 percent cent approval',actor=c.human['id'])
    preview=actions.close_preview(c.ws,c.model,request_id='close-used-v2-before-withdrawal',period=c.period,store_ids=['s1'],actor=c.human)
    assert preview['eligible'] and not preview['blocked']
    done=actions.close_confirm(c.ws,c.model,request_id='close-used-v2-before-withdrawal-confirm',proposal_id=preview['proposal_id'],proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=c.human)
    assert done['status']=='completed'
    def frozen():
        rid=c.ws.state('s1',c.period).run_id
        rows=[dict(c.ws.conn.execute('SELECT * FROM run WHERE id=?',(rid,)).fetchone()),dict(c.ws.conn.execute('SELECT * FROM period WHERE store_id=? AND period=?',('s1',c.period)).fetchone()),dict(c.ws.conn.execute('SELECT * FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone())]
        with c.reg.connect() as db:rows.append([dict(r) for r in db.execute('SELECT * FROM payout_confirmation')])
        return journal.digest(rows)
    before=frozen();row=c.ws.conn.execute('SELECT * FROM automation_decision WHERE id=?',(c.fact['decision_id'],)).fetchone()
    sha=actions._decision_input(c.ws,c.model,row['kind'],row['store_id'],row['period'],json.loads(row['values_json']))
    with pytest.raises(actions.Conflict):actions.decision_revoke(c.ws,c.model,c.fact['decision_id'],request_id='forbidden-history-withdrawal',reason='declared closed-source attempt',input_sha=sha,actor=c.human)
    assert frozen()==before


def test_unused_actual_engine_source_fact_is_not_a_recorded_dependency(actual_v3,monkeypatch):
    from ledger.model.schema import Predicate
    from ledger import service
    c=actual_v3
    goods=c.model.metric('goods_cost').model_copy(update={'where':(Predicate(field='sku',op='eq',value='declared-other-sku'),)})
    model=c.model.model_copy(update={'metrics':tuple(goods if m.id=='goods_cost' else m for m in c.model.metrics)})
    result=service.recompute(c.ws,model,model.store('s1'))
    assert result.failure is None,result.failure
    rid=c.ws.latest_run('s1',c.period)['id']
    proof=json.loads(c.ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(rid,)).fetchone()[0])
    assert proof['approved_cost_sources']==[] and len(v3.active(c.ws.conn,'s1'))==1
    assert checker.source_fact_dependencies_current(c.ws,'s1',proof)
    c.model=model
    original=revoke_gap(c,c.fact['decision_id'],monkeypatch)
    checked=checker.check(c.ws,'s1',c.period,model=model)
    assert 'source_fact_input_unverified' not in {r['code'] for r in checked['issues']}
    assert checker.source_fact_dependencies_current(c.ws,'s1',proof)
