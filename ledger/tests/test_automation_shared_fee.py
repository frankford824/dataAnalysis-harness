"""Synthetic copies of the real source claim shape; never production histories."""
from copy import deepcopy
from decimal import Decimal
import hashlib,json
from pathlib import Path

import polars as pl
import pytest

from automation_fixtures import capture_synthetic_summary_fixture
from conftest import MODELS
from ledger import automation_checker as checker, automation_shared_fee as shared
from ledger.model.loader import load_model
from ledger.model.schema import Store,Overhead
from ledger.storage_integrity import seal
from ledger.workspace import Workspace,SHARED_STORE_ID

PERIOD='2026-06'


@pytest.fixture
def scope(tmp_path):
    model=load_model(MODELS/'cn-ecommerce').model_copy(update={'commission':(),
        'stores':(Store(id='s1',name='Synthetic One',platform='taobao'),Store(id='s2',name='Synthetic Two',platform='taobao')),
        'overheads':(Overhead(period=PERIOD,amount=0),)})
    ws=Workspace(tmp_path/'workspace');checker.bind_model(ws,model)
    raw=tmp_path/'synthetic-carrier.txt';raw.write_bytes(b'explicit synthetic carrier source, not a real invoice')
    source=ws.keep('synthetic-carrier.txt',raw,SHARED_STORE_ID)
    yield ws,model,source.sha
    ws.close()


def publish(scope,sid,*,ordinal=2,raw='-2',contribution=None,total=None,extra=(),period=PERIOD,source_sha=None):
    ws,model,sha=scope;sha=source_sha or sha;store=model.store(sid);value=Decimal(raw) if contribution is None else Decimal(contribution)
    rows=[] if ordinal is None else [{'metric_id':'freight_cost','source_id':'freight','store':store.name,'period':period,
        'file_sha':sha,'sheet':'Sheet1','row_no':ordinal,'amount':float(Decimal(raw)),
        'counted':True,'contribution':float(value),'link_key':'same-waybill','linked':True,'order_id':'synthetic-order'}]
    rows.extend(extra)
    booked=sum(Decimal(str(r['contribution'])) for r in rows) if total is None else Decimal(total)
    payload={'store_id':sid,'store':store.name,'period':period,'can_close':True,'missing_sources':[],'findings':[],
        'statement':[{'id':'n_receipt','value':100,'available':True},{'id':'net_profit','value':98,'available':True}],
        'calculation_inputs':{'metric_totals':{'freight_cost':float(booked)},'unavailable_metrics':[]}}
    rid=ws.record(sid,period,payload,[sha],model_revision=checker.journal.digest(model.model_dump(mode='json')))
    frame=pl.from_dicts(rows) if rows else pl.DataFrame(schema={'metric_id':pl.String})
    frame.write_parquet(ws.facts_path(rid));seal(ws.facts_path(rid))
    capture_synthetic_summary_fixture(ws,model,rid)
    shared.index_run(ws,rid,model=model)
    return rid


def check(scope,sid='s1'):
    ws,model,_=scope
    return checker.check(ws,sid,PERIOD,model=model,check_pending=False)


def test_two_full_current_source_claims_hard_block_open_scope(scope):
    ws,model,_=scope;publish(scope,'s1');publish(scope,'s2')
    result=check(scope)
    assert result['allowed'] is False
    problem=next(p for p in result['issues'] if p['code']=='shared_fee_source_conflict')
    assert problem['hard'] and problem['conflict_count']==1
    evidence=ws.root/problem['evidence_ref']['path']
    assert hashlib.sha256(evidence.read_bytes()).hexdigest()==problem['evidence_ref']['sha256']
    rows=json.loads(evidence.read_text())['conflicts']
    assert Decimal(rows[0]['effective_contribution'])==-4
    assert {r['store_id'] for r in rows[0]['claims']}=={'s1','s2'}
    assert all(ws.state(s,PERIOD).state=='open' for s in ('s1','s2'))


def test_different_real_fee_rows_on_same_waybill_are_not_merged(scope):
    publish(scope,'s1',ordinal=2,raw='-2');publish(scope,'s2',ordinal=3,raw='-3')
    assert check(scope)['shared_fee']['status']=='clear'
    assert check(scope)['allowed'] is True


def test_legitimate_split_and_submicro_source_arithmetic_do_not_block(scope):
    publish(scope,'s1',raw='-2',contribution='-.9999998',total='-1')
    publish(scope,'s2',raw='-2',contribution='-1.0000001',total='-1')
    result=check(scope)
    assert result['shared_fee']['status']=='clear' and result['allowed']


def test_source_raw_budget_identity_disagreement_is_blocked(scope):
    publish(scope,'s1',raw='-2',contribution='-1',total='-1')
    publish(scope,'s2',raw='-3',contribution='-1',total='-1')
    assert check(scope)['shared_fee']['status']=='blocked'


def test_old_run_versions_never_count_twice(scope):
    ws,model,_=scope
    old=publish(scope,'s2');publish(scope,'s1')
    assert check(scope)['shared_fee']['status']=='blocked'
    new=publish(scope,'s2',ordinal=3)
    assert new!=old
    assert check(scope)['shared_fee']['status']=='clear'


def test_counted_source_without_actual_metric_booking_is_not_false_ready(scope):
    publish(scope,'s1',raw='-2',total='0');publish(scope,'s2',ordinal=3)
    result=check(scope)
    assert result['allowed'] is False and result['shared_fee']['status']=='pending'
    assert any(p['code']=='shared_fee_evidence_unverified' for p in result['issues'])


def test_first_capture_sha_cannot_be_replaced_by_resigning_source_facts(scope):
    ws,model,_=scope;rid=publish(scope,'s1');publish(scope,'s2',ordinal=3)
    source=ws.facts_path(rid)
    changed=pl.read_parquet(source).with_columns(pl.lit(-3.).alias('contribution'))
    changed.write_parquet(source);seal(source)
    result=check(scope)
    assert result['allowed'] is False and result['shared_fee']['status']=='pending'


def test_closed_history_only_gets_risk_and_new_open_scope_is_blocked(scope):
    ws,model,_=scope;rid=publish(scope,'s1');publish(scope,'s2',ordinal=3)
    ws.close_period('s1',PERIOD,expected_run_id=rid,model=model,by='synthetic-human')
    original=deepcopy(ws.state('s1',PERIOD).result)
    publish(scope,'s2',ordinal=2)
    closed=check(scope,'s1');opened=check(scope,'s2')
    assert closed['shared_fee']['status']=='risk_only' and closed['shared_fee']['issues']==[]
    assert closed['shared_fee']['risks'] and opened['shared_fee']['status']=='blocked'
    assert ws.state('s1',PERIOD).closed and ws.state('s1',PERIOD).result==original
    outward=checker.close_view(ws,model,'s1',PERIOD,original)
    assert outward['source_risks'] and outward['can_close'] is False


def test_same_source_different_period_claim_is_not_ignored(scope):
    ws,model,_=scope;publish(scope,'s1')
    publish(scope,'s2',period='2026-07')
    assert check(scope)['shared_fee']['status']=='blocked'


def test_nofreight_scope_does_not_scan_other_artifacts(scope,monkeypatch):
    ws,model,_=scope;rid=publish(scope,'s1',ordinal=None);publish(scope,'s2',ordinal=3)
    monkeypatch.setattr(shared,'warm',lambda *args,**kwargs:pytest.fail('zero-claim scope scanned neighbors'))
    result=shared.evaluate(ws,'s1',PERIOD,rid,model=model)
    assert result['status']=='clear'


def test_cross_store_evidence_is_one_shared_group_despite_other_unique_fees(scope):
    ws,model,sha=scope
    def additional(sid,ordinal,cost):
        return {'metric_id':'freight_cost','source_id':'freight','store':model.store(sid).name,'period':PERIOD,
            'file_sha':sha,'sheet':'Sheet1','row_no':ordinal,'amount':cost,'counted':True,'contribution':cost,
            'link_key':'another-real-fee','linked':True,'order_id':'another-order'}
    publish(scope,'s1',extra=[additional('s1',4,-5.)])
    publish(scope,'s2',extra=[additional('s2',5,-8.)])
    one=check(scope,'s1')['shared_fee']['issues'][0]
    two=check(scope,'s2')['shared_fee']['issues'][0]
    assert one['owner_scope']==two['owner_scope'] and one['evidence_ref']==two['evidence_ref']


def test_cold_related_neighbor_is_pending_then_reveals_conflict(scope,monkeypatch):
    ws,model,_=scope;publish(scope,'s1');other=publish(scope,'s2')
    with shared._connect(ws) as db:
        db.execute('DELETE FROM indexed_run WHERE run_id=?',(other,));db.execute('DELETE FROM claim WHERE run_id=?',(other,))
    original=shared.warm;monkeypatch.setattr(shared,'warm',lambda *args,**kwargs:[])
    assert check(scope)['shared_fee']['status']=='pending'
    monkeypatch.setattr(shared,'warm',original)
    assert check(scope)['shared_fee']['status']=='blocked'


def test_missing_unrelated_source_does_not_block_current_precise_claim(scope,tmp_path):
    ws,model,_=scope;publish(scope,'s1')
    file=tmp_path/'other-source.txt';file.write_bytes(b'explicit unrelated synthetic source')
    other_sha=ws.keep('other-source.txt',file,'s2').sha
    other=publish(scope,'s2',source_sha=other_sha)
    with shared._connect(ws) as db:
        db.execute('DELETE FROM indexed_run WHERE run_id=?',(other,));db.execute('DELETE FROM claim WHERE run_id=?',(other,))
    ws.facts_path(other).unlink()
    assert check(scope)['shared_fee']['status']=='clear'


def test_shared_source_conflict_cannot_be_ignored_as_a_soft_finding(scope):
    ws,model,_=scope;publish(scope,'s1');publish(scope,'s2')
    result=checker.check(ws,'s1',PERIOD,model=model,ignored_blockers=('shared_fee_source_conflict',))
    assert not result['allowed'] and any(p['code']=='shared_fee_source_conflict' and p['hard'] for p in result['issues'])


def test_sealed_file_of_an_unpublished_run_is_not_effective_ready_evidence(scope):
    ws,model,_=scope;rid=publish(scope,'s1');publish(scope,'s2',ordinal=3)
    ws.mark_evidence(rid,ready=False,error='explicit synthetic incomplete publication')
    result=check(scope)
    assert result['shared_fee']['status']=='pending' and not result['allowed']


def test_cached_neighbor_that_lost_publication_is_unknown_not_an_effective_claim(scope):
    ws,model,_=scope;publish(scope,'s1');other=publish(scope,'s2')
    ws.mark_evidence(other,ready=False,error='explicit synthetic incomplete neighbor')
    result=check(scope)
    assert result['shared_fee']['status']=='pending'
    assert result['shared_fee']['issues'][0]['code']=='shared_fee_index_pending'
