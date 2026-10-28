"""Immediate financial CAS across receive/enqueue and slice equivalence."""
import io
from ledger import automation_actions as actions,automation_checker as checker,automation_period_proof as proof
from test_automation_period_proof import fixed


def test_receive_changes_cas_before_enqueue_and_equivalence_restores_it(fixed):
    ws,model,store,sl,ing,payload,old,rid=fixed
    before=checker.check(ws,store.id,sl.period,model=model)
    batch=actions._scope_input(ws,model,sl.period)
    assert before['scope']['unverified_source_sha'] is None
    # The existing receipt writer commits before the separate Registry enqueue.
    ws.keep('orders.csv',io.BytesIO(b'declared other-month input'),store.id)
    changed=checker.check(ws,store.id,sl.period,model=model)
    assert changed['scope']['unverified_source_sha']
    assert changed['input_sha']!=before['input_sha']
    assert actions._scope_input(ws,model,sl.period)!=batch
    # The trusted original-equivalence path, not an HTTP-supplied epoch,
    # establishes that this month's source closure is unchanged.
    candidate=proof.prepare(ws,model,store,ing,sl,payload,None)
    assert proof.reusable(ws,store.id,sl.period,candidate)['id']==rid
    after=checker.check(ws,store.id,sl.period,model=model)
    assert after['scope']['unverified_source_sha'] is None
    assert after['input_sha']==before['input_sha']
    assert actions._scope_input(ws,model,sl.period)==batch
    assert ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]==1


def test_another_store_receipt_does_not_change_this_store_capture_identity(fixed):
    ws,model,store,sl,ing,payload,old,rid=fixed
    before=checker.check(ws,store.id,sl.period,model=model)
    ws.keep('other-store.csv',io.BytesIO(b'other store original'),'other-store')
    after=checker.check(ws,store.id,sl.period,model=model)
    assert after['scope']['unverified_source_sha'] is None
    assert after['input_sha']==before['input_sha']
