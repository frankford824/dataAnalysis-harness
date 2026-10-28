"""Documentary human totals use real preview/close in disposable sessions."""
from copy import deepcopy
from decimal import Decimal
import hashlib,json
from pathlib import Path

import pytest
from ledger import replay_service as replay
from test_replay_service import bundle,complete_bundle,fixture_session,seed,all_expected,complete_expected


def reference(tmp_path,*,store_id='s1',period='2026-06',source_run_id=9001,base='40.00',total='40.00',payout='8.00',method='declared_synthetic_manual_decisions'):
    tmp_path.mkdir(parents=True,exist_ok=True)
    decision={'source_run_id':source_run_id,'confirmed':{'goods':total,'dropship':'0.00','reshipment':'0.00'},
        'base_costs':{'goods':base,'dropship':'0.00','reshipment':'0.00'},
        'payouts':[{'person_id':'p1','amount':payout}],'no_payout':False,'reason':'合成已批准整期人工凭据',
        'observed':{'goods':999999.99},'line_revision':0,'line_reviews':[]}
    raw=json.dumps(decision,ensure_ascii=False,indent=1)
    body={'method':method,'no_engine_result_amounts_read':True,'rows':[{
        'store_id':store_id,'period':period,'run_id':source_run_id,'decision_json':raw,
        'original_sha':'b'*64,'at':'2026-08-02T11:00:00+08:00'}]}
    path=tmp_path/'human-inputs.json';path.write_text(json.dumps(body,ensure_ascii=False))
    ref={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'source_run_id':source_run_id,'decision_sha256':hashlib.sha256(raw.encode()).hexdigest(),
        'original_result_sha256':'b'*64,'decision_at':body['rows'][0]['at']}
    return ref,body


def prepare(session,*,both=True):
    session.recompute('s1')
    if both:session.recompute('s2')
    session.drain_pending()


def test_whole_period_human_total_closes_through_real_api_without_fake_order_lines(bundle,tmp_path):
    ref,_=reference(tmp_path);path,_=bundle
    with fixture_session(path,seed()) as session:
        prepare(session)
        before=session.ws.latest_run('s1','2026-06')
        assert not json.loads(before['result'])['can_close']
        result=session.restore_approved_manual_finance('s1','2026-06',reference=ref,approval_reference='fixture-documentary-approval',fixture=True)
        assert result['state']=='closed'
        row=session.ws.conn.execute('SELECT * FROM manual_finance WHERE store_id=? AND period=?',('s1','2026-06')).fetchone()
        decided=json.loads(row['decision_json'])
        assert decided['confirmed']=={'goods':40.0,'dropship':0.0,'reshipment':0.0}
        assert decided['source_run_id']==before['id'] and before['id']!=ref['source_run_id']
        assert decided['payouts']==[{'person_id':'p1','amount':8.0}]
        assert session.ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0]==0
        state=session.ws.state('s1','2026-06');net=next(x for x in state.result['statement'] if x['id']=='net_profit')
        assert net['value']==160 and state.result['commission']['total']==8
        trace=session.trace[-1]
        assert trace['line_mode']=='documented_whole_period_total_no_line_import'
        assert trace['document']['decision_sha256']==ref['decision_sha256']
        assert trace['historical_authenticated_actor_proved'] is False and trace['production_authority'] is False
        assert trace['input_sha'] and trace['bundle_fingerprint']==session.fingerprint


def test_real_company_gate_still_rejects_manual_total_before_other_store_income_exists(bundle,tmp_path):
    ref,_=reference(tmp_path);path,_=bundle
    with fixture_session(path,seed()) as session:
        session.recompute('s1')
        with pytest.raises(replay.ReplayError,match='真实人工预览或月结入口拒绝'):
            session.restore_approved_manual_finance('s1','2026-06',reference=ref,approval_reference='fixture-approval',fixture=True)
        assert not session.ws.state('s1','2026-06').closed
        assert not session.ws.conn.execute('SELECT 1 FROM manual_finance').fetchone()


def test_existing_approved_line_uses_original_base_and_never_double_counts(bundle,tmp_path):
    ref,_=reference(tmp_path,base='38.00');path,missing=bundle
    with fixture_session(path,seed()) as session:
        prepare(session)
        session.supplement('s1','2026-06',amount='2.00',reason='合成独立逐笔原件',order_id=missing['s1/2026-06'])
        session.drain_pending()
        session.restore_approved_manual_finance('s1','2026-06',reference=ref,approval_reference='fixture-approval',fixture=True)
        row=session.ws.conn.execute('SELECT decision_json FROM manual_finance').fetchone()
        decision=json.loads(row[0]);assert decision['base_costs']['goods']=='38.00'
        assert decision['confirmed']['goods']==40.0 and len(decision['line_reviews'])==1
        assert session.trace[-1]['line_mode']=='original_base_plus_replayed_lines'


def test_line_total_mismatch_rejects_before_manual_finance_writer(bundle,tmp_path):
    ref,_=reference(tmp_path,base='40.00');path,missing=bundle
    with fixture_session(path,seed()) as session:
        prepare(session)
        session.supplement('s1','2026-06',amount='2.00',reason='合成独立逐笔原件',order_id=missing['s1/2026-06'])
        session.drain_pending()
        with pytest.raises(replay.ReplayError,match='不能重复计入'):
            session.restore_approved_manual_finance('s1','2026-06',reference=ref,approval_reference='fixture-approval',fixture=True)
        assert not session.ws.conn.execute('SELECT 1 FROM manual_finance').fetchone()
        assert not session.ws.state('s1','2026-06').closed


def test_preview_input_race_cannot_close_or_write_manual_decision(bundle,tmp_path,monkeypatch):
    from ledger import api
    ref,_=reference(tmp_path);path,_=bundle
    with fixture_session(path,seed()) as session:
        prepare(session);original=api.manual_cost_preview
        def racing(*args,**kwargs):
            value=original(*args,**kwargs)
            session.ws.note_external_version('s1','declared-financial-race','changed')
            return value
        monkeypatch.setattr(api,'manual_cost_preview',racing)
        with pytest.raises(replay.ReplayError,match='输入CAS已变化'):
            session.restore_approved_manual_finance('s1','2026-06',reference=ref,approval_reference='fixture-approval',fixture=True)
        assert not session.ws.conn.execute('SELECT 1 FROM manual_finance').fetchone()
        assert not session.ws.state('s1','2026-06').closed


@pytest.mark.parametrize('fault',['capture_sha','decision_sha','source_run','result_sha','decision_at','payout_only','fixture_promotion'])
def test_documentary_reference_rejects_wrong_identity_and_payout_only(tmp_path,fault):
    ref,body=reference(tmp_path)
    if fault=='capture_sha':ref['sha256']='a'*64
    elif fault=='decision_sha':ref['decision_sha256']='a'*64
    elif fault=='source_run':ref['source_run_id']+=1
    elif fault=='result_sha':ref['original_result_sha256']='a'*64
    elif fault=='decision_at':ref['decision_at']='2026-08-03'
    elif fault=='fixture_promotion':pass
    else:
        raw=json.loads(body['rows'][0]['decision_json']);raw.pop('confirmed');body['rows'][0]['decision_json']=json.dumps(raw)
        path=Path(ref['path']);path.write_text(json.dumps(body));ref['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        ref['decision_sha256']=hashlib.sha256(body['rows'][0]['decision_json'].encode()).hexdigest()
    with pytest.raises(replay.ReplayError):
        replay.load_manual_finance_decision(ref,'s1','2026-06',fixture=fault!='fixture_promotion')


def test_descriptor_executes_explicit_manual_step_without_weakening_positive_contract(complete_bundle,tmp_path):
    path,_=complete_bundle;ref,_=reference(tmp_path)
    expected=complete_expected();expected['scopes']['s1/2026-06'].update(state='closed',can_close=False)
    case={'schema_version':1,'case_id':'documentary-period-total-service-fixture','kind':'service_fixture',
        'approval_reference':'synthetic-documentary-case-approval','bundle':str(path),'registry':seed(),
        'expected':expected,'steps':[{'operation':'drain_pending'},
            {'operation':'restore_approved_manual_finance','store_id':'s1','period':'2026-06',
             'reference':ref,'approval_reference':'synthetic-documentary-case-approval'}]}
    report=replay.execute(case)
    assert report['status']=='passed',report['differences']
    assert report['actual']['scopes']['s1/2026-06']['state']=='closed'
    assert report['actual']['scopes']['s1/2026-06']['cost_line_total']==0
    assert any(t['operation']=='manual_finance.restore_and_close' for t in report['actual']['trace'])


@pytest.mark.parametrize('fault',['source_rejection','fixture_override','approval_mismatch','historical_fixture_promotion'])
def test_descriptor_cannot_use_manual_totals_as_rejection_or_fake_historical_credit(complete_bundle,tmp_path,fault):
    path,_=complete_bundle;ref,_=reference(tmp_path)
    step={'operation':'restore_approved_manual_finance','store_id':'s1','period':'2026-06',
          'reference':ref,'approval_reference':'synthetic-case-approval'}
    case={'schema_version':1,'case_id':'forbidden-manual-step','kind':'service_fixture',
          'approval_reference':'synthetic-case-approval','bundle':str(path),'registry':seed(),
          'steps':[step],'expected':complete_expected()}
    if fault=='source_rejection':case['kind']='source_rejection'
    elif fault=='fixture_override':step['fixture']=True
    elif fault=='approval_mismatch':step['approval_reference']='different-approval'
    else:
        case['kind']='historical_golden';case['source_evidence']={'manual_finance_decisions_sha256':ref['sha256']}
    with pytest.raises(replay.ReplayError):replay.execute(case)


def test_real_decision_file_must_belong_to_sealed_bundle_protected_root(bundle,tmp_path):
    path,_=bundle;ref,_=reference(tmp_path,method='sqlite_mode_ro_query_only_transaction')
    with fixture_session(path,seed()) as session:
        with pytest.raises(replay.ReplayError,match='必须纳入新封存bundle'):
            session.restore_approved_manual_finance('s1','2026-06',reference=ref,approval_reference='documentary-replay-review')
        assert not session.ws.conn.execute('SELECT 1 FROM manual_finance').fetchone()
