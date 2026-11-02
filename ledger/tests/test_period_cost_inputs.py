"""Approved aggregate cost inputs never confirm payouts or close a period."""
import json,hashlib,sqlite3
from decimal import Decimal
from pathlib import Path
from copy import deepcopy
import pytest
from ledger import automation_actions as actions,automation_checker as checker,period_cost_inputs as costs
from ledger import automation_store as journal,replay_service as replay
from ledger.workspace import WorkspaceError
from test_replay_service import bundle,complete_bundle,seed,fixture_session
from test_replay_manual_finance import reference


def prepare(session,both=False):
    session.recompute('s1')
    if both:session.recompute('s2')
    session.drain_pending()
    return session.ws.latest_run('s1','2026-06')


def human(session):return replay._offline_actor(session.ws,session.registry)


def preview(session,request='cost-preview',amount='40.00',actor=None):
    row=session.ws.latest_run('s1','2026-06')
    return actions.decision_preview(session.ws,session.model,request_id=request,store_id='s1',period='2026-06',
        kind=costs.KIND,values={'run_id':row['id'],'costs':{'goods':amount,'dropship':'0.00','reshipment':'0.00'}},
        reason='独立人工批准本店当月完整三个成本科目',actor=human(session) if actor is None else actor)


def apply(session,proposal,request='cost-apply',actor=None):
    return actions.decision_apply(session.ws,session.model,request_id=request,proposal_id=proposal['proposal_id'],
        proposal_sha=proposal['proposal_sha'],input_sha=proposal['input_sha'],actor=human(session) if actor is None else actor)


def payload(session):return json.loads(session.ws.latest_run('s1','2026-06')['result'])
def value(body,node):return next(n['value'] for n in body['statement'] if n['id']==node)


def test_human_cost_input_persists_before_company_close_and_never_approves_people(bundle):
    path,_=bundle
    with fixture_session(path,seed()) as session:
        session.recompute('s1');original=session.ws.latest_run('s1','2026-06');original_binding=costs.raw_binding(session.ws,original)
        assert 'company_revenue_unknown' in {x['code'] for x in checker.check(session.ws,'s1','2026-06',model=session.model)['issues']}
        result=apply(session,preview(session))
        assert result['payouts_confirmed'] is False and result['period_closed'] is False
        assert not session.ws.conn.execute('SELECT 1 FROM manual_finance').fetchone()
        assert session.ws.state('s1','2026-06').state=='open'
        session.drain_pending()
        body=payload(session)
        assert value(body,'n_receipt')==200 and value(body,'gross_profit')==160 and value(body,'net_profit')==160
        assert body['period_cost_input']['status']=='applied'
        assert body['period_cost_input']['business_input_sha']==original_binding
        assert body['cost_review']['authority']=='approved_period_cost_input'
        assert body['cost_review']['individual_order_costs_proved'] is False
        assert body['commission']['amount_complete'] is False
        assert all(p['amount_complete'] is False and p['gross'] is None for p in body['commission']['people'])
        assert all(n['drillable'] is False and n['order_allocation_available'] is False for n in body['statement'] if n['id'] in {'gross_profit','net_profit'})
        codes={x['code'] for x in checker.check(session.ws,'s1','2026-06',model=session.model)['issues']}
        assert 'commission_incomplete' in codes
        assert 'required_cost_unresolved' not in codes and 'cost_decision_required' not in codes
        finding=next(f for f in body['findings'] if f['id']=='required_cost_unresolved')
        assert finding['passed'] is False and finding['blocking'] is False and finding['resolution']=='approved_period_total'
        assert body['raw_cost_findings'] and body['cost_coverage']['uncovered']>0
        assert not session.ws.state('s1','2026-06').closed
        with session.registry.connect() as db:assert not db.execute('SELECT 1 FROM payout_confirmation').fetchone()
        session.recompute('s1')
        assert payload(session)['period_cost_input']['status']=='applied'  # no self-invalidating approval fingerprint


def test_approved_total_detail_and_export_never_present_order_rows_as_statement_cost(bundle,monkeypatch):
    from urllib.parse import unquote
    from fastapi.testclient import TestClient
    from ledger import api
    path,_=bundle
    with fixture_session(path,seed()) as session:
        session.recompute('s1');apply(session,preview(session));session.drain_pending()
        run=session.ws.latest_run('s1','2026-06')['id']
        monkeypatch.setattr(api,'workspace',lambda:session.ws);monkeypatch.setattr(api,'_model',lambda:session.model)
        client=TestClient(api.app)
        scoped=[n['id'] for n in payload(session)['statement'] if n.get('amount_scope')=='approved_store_period_total']
        assert {'gross_profit','net_profit'}<=set(scoped)
        for node in scoped:
            drilled=client.get(f'/api/runs/{run}/drill/{node}')
            assert drilled.status_code==409 and '整期成本总额' in drilled.json()['detail']
        assert client.get(f'/api/runs/{run}/drill/n_receipt').status_code==200
        exported=client.get(f'/api/runs/{run}/fees.csv')
        assert exported.status_code==200
        rows=exported.content.decode('utf-8-sig').splitlines()
        assert rows[0].endswith(',核对状态') and len(rows)>1
        assert all('人工批准的整期总额：商品成本40' in row and '不能相加核对' in row for row in rows[1:])
        assert '费项明细-成本为整期批准总额' in unquote(exported.headers['content-disposition'])


def test_same_request_returns_same_receipt_and_single_input(bundle):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);p=preview(s);a=apply(s,p);b=apply(s,p)
        assert a==b
        assert s.ws.conn.execute('SELECT count(*) FROM period_cost_input').fetchone()[0]==1
        with pytest.raises((ValueError,PermissionError)):
            apply(s,{**p,'proposal_sha':'a'*64})


def test_lost_response_replay_after_preview_expiry_returns_original_receipt(bundle):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);p=preview(s);a=apply(s,p)
        s.ws.conn.execute('UPDATE automation_proposal SET expires_at=0 WHERE id=?',(p['proposal_id'],));s.ws.conn.commit()
        assert apply(s,p)==a
        assert s.ws.conn.execute('SELECT count(*) FROM period_cost_input').fetchone()[0]==1
        assert s.ws.conn.execute("SELECT count(*) FROM automation_decision WHERE kind='period_cost_input'").fetchone()[0]==1


@pytest.mark.parametrize('actor',[{'id':'service','kind':'service','authenticated':True},{'id':'human','kind':'human','authenticated':False},{}])
def test_nonhuman_actor_cannot_create_cost_approval(bundle,actor):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s)
        with pytest.raises(PermissionError):preview(s,actor=actor)
        assert not s.ws.conn.execute('SELECT 1 FROM period_cost_input').fetchone()


@pytest.mark.parametrize('bad',[{'goods':'40'}, {'goods':'40','dropship':'0','reshipment':None},
    {'goods':True,'dropship':'0','reshipment':'0'}, {'goods':'40.001','dropship':'0','reshipment':'0'}])
def test_partial_invalid_costs_never_become_zero(bad):
    with pytest.raises(WorkspaceError):costs.costs(bad)


def test_expired_or_changed_input_proposal_cannot_apply(bundle):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);p=preview(s)
        s.ws.conn.execute('UPDATE automation_proposal SET expires_at=0 WHERE id=?',(p['proposal_id'],));s.ws.conn.commit()
        with pytest.raises(WorkspaceError):apply(s,p)
        assert not s.ws.conn.execute('SELECT 1 FROM period_cost_input').fetchone()
        p=preview(s,request='new-preview')
        s.ws.note_external_version('s1','cost-input-race','new-snapshot')
        with pytest.raises(WorkspaceError):apply(s,p,request='new-apply')
        assert not s.ws.conn.execute('SELECT 1 FROM period_cost_input').fetchone()


def test_failure_before_receipt_rolls_back_input_authority_and_outbox(bundle,monkeypatch):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);p=preview(s)
        before=s.ws.conn.execute('SELECT count(*) FROM automation_attempt').fetchone()[0]
        original=journal.finish
        def fail(conn,key,action,*args):
            if action=='period_cost_input.apply':raise RuntimeError('declared transactional crash')
            return original(conn,key,action,*args)
        monkeypatch.setattr(journal,'finish',fail)
        with pytest.raises(RuntimeError):apply(s,p)
        assert not s.ws.conn.execute('SELECT 1 FROM period_cost_input').fetchone()
        assert s.ws.conn.execute('SELECT count(*) FROM automation_attempt').fetchone()[0]==before
        assert not s.ws.conn.execute("SELECT 1 FROM automation_authorization WHERE action='period_cost_input.apply'").fetchone()


def test_queue_failure_keeps_committed_cost_receipt_and_recovers(bundle,monkeypatch):
    from ledger.commission_registry import Registry
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);p=preview(s)
        with monkeypatch.context() as patch:
            patch.setattr(Registry,'enqueue_automation',lambda *_:(_ for _ in ()).throw(RuntimeError('queue offline')))
            saved=apply(s,p)
            assert saved['status']=='applied'
            assert s.ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state='dispatch_pending'").fetchone()
        s.drain_pending()
        assert payload(s)['period_cost_input']['status']=='applied'
        assert not s.ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state='dispatch_pending'").fetchone()


def test_existing_order_lines_block_period_override_and_reverse_overlap(bundle):
    path,missing=bundle
    with fixture_session(path,seed()) as s:
        prepare(s)
        s.supplement('s1','2026-06',amount='2.00',reason='独立逐笔成本原件',order_id=missing['s1/2026-06']);s.drain_pending()
        with pytest.raises(WorkspaceError,match='逐笔'):preview(s)
    with fixture_session(path,seed()) as s:
        prepare(s);apply(s,preview(s));s.drain_pending()
        with pytest.raises(WorkspaceError,match='整期'):s.supplement('s1','2026-06',amount='2.00',reason='不得叠加',order_id=missing['s1/2026-06'])


def test_human_revocation_removes_authority_without_zeroing_or_faking_completion(bundle):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);saved=apply(s,preview(s));s.drain_pending()
        row=s.ws.conn.execute("SELECT * FROM automation_decision WHERE kind=? AND action='apply'",(costs.KIND,)).fetchone()
        sha=actions._decision_input(s.ws,s.model,costs.KIND,'s1','2026-06',json.loads(row['values_json']))
        actions.decision_revoke(s.ws,s.model,row['id'],request_id='revoke-cost',reason='撤回原人工成本批准',input_sha=sha,actor=human(s))
        s.drain_pending();body=payload(s)
        assert body['period_cost_input']['status']=='revoked'
        assert value(body,'net_profit') is None
        assert body['cost_review']['amount_complete']['goods'] is False
        assert s.ws.conn.execute('SELECT count(*) FROM period_cost_input').fetchone()[0]==1


def test_changed_source_invalidates_cost_input_and_keeps_financial_unknown(bundle):
    from test_replay_service import write_xlsx
    from datetime import datetime
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);apply(s,preview(s));s.drain_pending()
        # A real changed original source, not an engine-generated golden.
        original=next(x for x in s.inputs['slots'] if x['store_id']=='s1')
        from openpyxl import load_workbook
        import io
        book=load_workbook(io.BytesIO((path/'files'/original['sha'][:2]/original['sha']).read_bytes()))
        book.active.cell(2,4,11)
        f=s.root/'changed-source.xlsx';book.save(f)
        s.ws.keep(original['name'],f,'s1');s.recompute('s1')
        body=payload(s)
        assert body['period_cost_input']['status']=='stale'
        assert value(body,'net_profit') is None
        assert s.ws.conn.execute('SELECT count(*) FROM period_cost_input').fetchone()[0]==1


def test_approved_input_evidence_cannot_be_changed(bundle):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);apply(s,preview(s));row=costs.latest(s.ws,'s1','2026-06')
        ref=json.loads(row['evidence_json'])['artifacts'][0];(s.root/ref['path']).write_text('tampered')
        with pytest.raises(WorkspaceError,match='凭据'):s.recompute('s1')


def test_documentary_cost_only_restoration_never_creates_fake_human_or_closes(bundle):
    from ledger import freight_experiments_inputs as frozen
    path,_=bundle;ref,_=reference(path/'evidence');frozen.seal(path)
    with fixture_session(path,seed()) as s:
        prepare(s)
        out=s.restore_approved_manual_finance('s1','2026-06',reference=ref,approval_reference='documentary-fixture-source',fixture=True,cost_input_only=True)
        assert out['payouts_confirmed'] is False and out['period_closed'] is False
        s.drain_pending();assert value(payload(s),'net_profit')==160
        row=costs.latest(s.ws,'s1','2026-06')
        assert row['identity_kind']=='documentary_history_replay'
        auth=s.ws.conn.execute('SELECT * FROM automation_authorization WHERE id=?',(row['authorization_id'],)).fetchone()
        assert auth['identity_kind']=='documentary_history_replay'
        assert json.loads(auth['scope_json'])['historical_authenticated_actor_proved'] is False
        assert not s.ws.conn.execute('SELECT 1 FROM manual_finance').fetchone()
        assert not s.ws.state('s1','2026-06').closed
        assert s.restore_approved_manual_finance('s1','2026-06',reference=ref,approval_reference='documentary-fixture-source',fixture=True,cost_input_only=True)==out


def test_nonfinancial_code_and_person_changes_do_not_expire_cost_approval(bundle,monkeypatch):
    from ledger import service,automation_period_proof,recompute_reuse
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);apply(s,preview(s));s.drain_pending()
        binding=payload(s)['period_cost_input']['business_input_sha']
        # A relationship change changes commission evidence, not approved cost inputs.
        body=deepcopy(seed()['schemes'][0]['body']);body['segments'][0]['total_rate']='0.06';body['segments'][0]['allocations'][0]['rate']='0.06'
        s.replace_scheme('s1',seed()['schemes'][0]['product_id'],body,'独立人员比例修订，不改商品成本')
        s.recompute('s1')
        assert payload(s)['period_cost_input']['status']=='applied'
        assert payload(s)['period_cost_input']['business_input_sha']==binding
        # Simulate a code-version-only release. Full publication CAS remains a
        # separate integrity check, not persistent monetary authorization.
        with monkeypatch.context() as patch:
            for module in [service,automation_period_proof,recompute_reuse]:patch.setattr(module,'engine_version',lambda:'f'*40)
            s.recompute('s1')
        assert payload(s)['period_cost_input']['status']=='applied'
        assert payload(s)['period_cost_input']['business_input_sha']==binding


def test_other_store_or_other_month_input_does_not_expire_approval(bundle):
    from openpyxl import load_workbook
    import io
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);apply(s,preview(s));s.drain_pending();binding=payload(s)['period_cost_input']['business_input_sha']
        other=next(x for x in s.inputs['slots'] if x['store_id']=='s2')
        book=load_workbook(io.BytesIO((path/'files'/other['sha'][:2]/other['sha']).read_bytes()));book.active.cell(2,4,11)
        changed=s.root/'other-store.xlsx';book.save(changed);s.ws.keep(other['name'],changed,'s2')
        s.recompute('s1');assert payload(s)['period_cost_input']['status']=='applied'
        own=next(x for x in s.inputs['slots'] if x['store_id']=='s1')
        book=load_workbook(io.BytesIO((path/'files'/own['sha'][:2]/own['sha']).read_bytes()))
        # June has rows2..21; this only changes July business data.
        book.active.cell(22,4,11);changed=s.root/'other-month.xlsx';book.save(changed);s.ws.keep(own['name'],changed,'s1')
        s.recompute('s1');assert payload(s)['period_cost_input']['status']=='applied'
        assert payload(s)['period_cost_input']['business_input_sha']==binding


def test_evidence_directory_symlink_cannot_write_outside_workspace(bundle,tmp_path):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);p=preview(s);outside=tmp_path/'outside';outside.mkdir()
        (s.root/'period-cost-input-evidence').symlink_to(outside,target_is_directory=True)
        with pytest.raises(WorkspaceError,match='符号链接'):apply(s,p)
        assert not list(outside.iterdir())
        assert not s.ws.conn.execute('SELECT 1 FROM period_cost_input').fetchone()


def test_same_id_cost_allocation_policy_change_expires_approval_but_text_does_not(bundle):
    from ledger.model.schema import Allocation
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);apply(s,preview(s));s.drain_pending();binding=payload(s)['period_cost_input']['business_input_sha']
        original=s.model
        s.model=original.model_copy(update={'metrics':tuple(m.model_copy(update={'name':m.name+'说明','note':'仅补文案'}) if m.id=='goods_cost' else m for m in original.metrics)})
        s.recompute('s1')
        assert payload(s)['period_cost_input']['status']=='applied'
        assert payload(s)['period_cost_input']['business_input_sha']==binding
        s.model=original.model_copy(update={'metrics':tuple(m.model_copy(update={'allocate':Allocation(mode='ratio',by='buyer_paid') if not m.allocate or m.allocate.mode=='even' else Allocation(mode='even')}) if m.id=='goods_cost' else m for m in original.metrics)})
        s.recompute('s1')
        assert payload(s)['period_cost_input']['status']=='stale'
        assert value(payload(s),'net_profit') is None


def test_closed_and_payout_approved_period_reject_cost_input(complete_bundle):
    path,_=complete_bundle
    with fixture_session(path,seed()) as s:
        prepare(s,both=True)
        s.confirm('s1','2026-06',payouts=[{'person_id':'p1','amount':'8.00'}],reason='独立实发核定')
        with pytest.raises(WorkspaceError,match='实发'):preview(s)
        assert not s.ws.conn.execute('SELECT 1 FROM period_cost_input').fetchone()
        s.close_approved('s1','2026-06',approval_reference='synthetic original close approval',reason='合成闭账确认')
        with pytest.raises(WorkspaceError,match='冻结'):preview(s,request='closed-input')
        assert not s.ws.conn.execute('SELECT 1 FROM period_cost_input').fetchone()


def test_permanent_outbox_failure_is_not_reported_as_drained(bundle,monkeypatch):
    from ledger.commission_registry import Registry
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);p=preview(s)
        monkeypatch.setattr(Registry,'enqueue_automation',lambda *_:(_ for _ in ()).throw(RuntimeError('queue offline')))
        apply(s,p)
        with pytest.raises(replay.ReplayError,match='未清空'):s.drain_pending(max_passes=2)
        assert s.ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state='dispatch_pending'").fetchone()


def test_signed_net_return_cost_uses_existing_amount_contract(bundle):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);out=apply(s,preview(s,amount='-40.00'));s.drain_pending()
        assert out['revoke_input_sha']
        assert value(payload(s),'gross_profit')==240 and value(payload(s),'net_profit')==240
        assert payload(s)['period_cost_input']['costs']['goods']=='-40.00'
        assert payload(s)['commission']['amount_complete'] is False


def install_complete_price_source(session,goods_total='40.00'):
    """A new declared source snapshot, never overwrite the sealed input bundle."""
    import shutil,polars as pl
    from unittest.mock import patch
    from test_order_feed import _write
    source=session.root/'new-cost-source'
    shutil.copytree(session.bundle/'feed',source)
    with session.feed._connect() as db:state=dict(db.execute('SELECT * FROM feed_state').fetchone())
    manifest=json.loads(state['manifest_json']);entry=manifest['objects']['order_costs.parquet']
    frame=pl.read_parquet(source/entry['path']);chosen=pl.col('order_id').cast(pl.Int64)<=20
    unit=str(Decimal(goods_total)/20)
    frame=frame.with_columns(pl.when(chosen).then(pl.lit(float(unit))).otherwise(pl.col('unit_cost')).alias('unit_cost'),
        pl.when(chosen).then(pl.lit(float(unit))).otherwise(pl.col('cost_amount')).alias('cost_amount'),
        pl.when(chosen).then(pl.lit('history')).otherwise(pl.col('cost_source')).alias('cost_source'),
        pl.when(chosen).then(pl.lit('priced')).otherwise(pl.col('cost_status')).alias('cost_status'))
    manifest['objects']['order_costs.parquet']=_write(source,'order_costs.parquet',frame)
    manifest['snapshot_id']='complete-source-next';manifest['through_seq']=11;manifest['revision']=int(manifest['revision'])+1
    session.feed.feed_root=source
    mapping=[json.loads(row['payload_json']) if 'payload_json' in row else row for row in session.inputs['mapping']]
    session.feed._install_snapshot(manifest,{'stores':mapping},{'healthy':True},source_revision=11,source_latest_seq=11,revision_etag='complete-source-next')
    session.stack.enter_context(patch.dict('os.environ',{'LEDGER_ORDER_FEED_ROOT':str(source)}))


def test_already_complete_sources_are_not_degraded_by_matching_control(complete_bundle):
    path,_=complete_bundle
    with fixture_session(path,seed()) as s:
        prepare(s,both=True);before=deepcopy(payload(s))
        apply(s,preview(s));s.drain_pending();after=payload(s)
        assert after['period_cost_input']['status']=='superseded_by_complete_sources'
        assert after['period_cost_input']['old_override_applied'] is False
        assert after['statement']==before['statement']
        assert after['commission']['people']==before['commission']['people']
        assert after['commission']['amount_complete'] is True
        with s.registry.connect() as db:assert not db.execute('SELECT 1 FROM payout_confirmation').fetchone()


@pytest.mark.parametrize('source_total,approved',[('39.9998','40.00'),('0.004','0.00')])
def test_complete_new_sources_match_cent_control_and_retain_actual_micro_amount(bundle,source_total,approved):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);out=apply(s,preview(s,amount=approved));s.drain_pending()
        old=costs.latest(s.ws,'s1','2026-06');old_hash=journal.digest(old)
        install_complete_price_source(s,source_total);s.recompute('s1');after=payload(s)
        assert after['period_cost_input']['status']=='superseded_by_complete_sources', {'source_pending':after['source_sync_pending'],'cost_review':after['cost_review'],'coverage':after['cost_coverage'],'pricing':after['pricing_pending_count'],'findings':[(f['id'],f['passed']) for f in after['findings']]}
        assert after['period_cost_input']['new_approval_created'] is False
        assert after['period_cost_input']['old_override_applied'] is False
        from ledger.money import money_float
        assert after['calculation_inputs']['metric_totals']['goods_cost']==-money_float(Decimal(source_total))
        raw=after['period_cost_input']['source_control_match']['raw_source_totals']['goods']
        assert abs(Decimal(raw)-Decimal(source_total))<Decimal('.00000001')
        assert Decimal(raw)!=0
        assert after['cost_review']['authority']=='source_evidence'
        assert after['commission']['amount_complete'] is True
        assert costs.latest(s.ws,'s1','2026-06')['source_binding_sha']==old['source_binding_sha']
        assert journal.digest(costs.latest(s.ws,'s1','2026-06'))==old_hash
        with s.registry.connect() as db:assert not db.execute('SELECT 1 FROM payout_confirmation').fetchone()


@pytest.mark.parametrize('source_total',['39.9949','40.005','40.01'])
def test_cross_half_cent_or_one_cent_difference_never_silently_converges(bundle,source_total):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);apply(s,preview(s));s.drain_pending()
        install_complete_price_source(s,source_total);s.recompute('s1');after=payload(s)
        assert after['period_cost_input']['status']=='stale'
        assert after['cost_review']['authority']=='source_evidence'
        from ledger.money import money_float
        assert after['calculation_inputs']['metric_totals']['goods_cost']==-money_float(Decimal(source_total))
        assert 'period_cost_input_stale' in {i['code'] for i in checker.check(s.ws,'s1','2026-06',model=s.model)['issues']}
        assert s.ws.conn.execute('SELECT count(*) FROM period_cost_input').fetchone()[0]==1


def test_partial_source_flags_or_aggregate_override_cannot_self_certify(bundle):
    import polars as pl
    from types import SimpleNamespace
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);raw=payload(s)
        empty=pl.DataFrame();sl=SimpleNamespace(pricing_gaps=empty,coverage_gap_rows=empty,order_issue_rows=empty,audit=SimpleNamespace(findings=[]))
        ingestion=SimpleNamespace(items=[]);approved={'goods':'40.00','dropship':'0.00','reshipment':'0.00'}
        # Even manually setting a misleading complete flag cannot make 95%
        # coverage / missing price a complete original-source proof.
        raw['cost_review']['amount_complete']={k:True for k in costs.KEYS}
        assert not costs.complete_source_control_match(s.model,s.model.store('s1'),raw,approved,sl,ingestion)
        raw['cost_coverage'].update(uncovered=0,passed=True)
        assert not costs.complete_source_control_match(s.model,s.model.store('s1'),raw,approved,sl,ingestion)
        raw['pricing_pending_count']=0;raw['period_cost_input']={'status':'applied'}
        assert not costs.complete_source_control_match(s.model,s.model.store('s1'),raw,approved,sl,ingestion)


def test_immutable_cost_input_cannot_be_replaced_with_sqlite_replace(bundle):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);apply(s,preview(s))
        row=dict(s.ws.conn.execute('SELECT * FROM period_cost_input').fetchone());before=dict(row)
        row['reason']='unauthorized replacement'
        names=list(row)
        with pytest.raises(sqlite3.IntegrityError):
            s.ws.conn.execute('INSERT OR REPLACE INTO period_cost_input('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')',[row[n] for n in names])
        assert dict(s.ws.conn.execute('SELECT * FROM period_cost_input').fetchone())==before


def test_documentary_input_cannot_be_consumed_after_its_isolated_scope_is_lost(bundle,monkeypatch):
    from ledger import freight_experiments_inputs as frozen,publication_scope
    path,_=bundle;ref,_=reference(path/'evidence');frozen.seal(path)
    with fixture_session(path,seed()) as s:
        prepare(s)
        s.restore_approved_manual_finance('s1','2026-06',reference=ref,approval_reference='fixture documentary authority',fixture=True,cost_input_only=True)
        row=costs.latest(s.ws,'s1','2026-06');raw=payload(s)
        monkeypatch.setattr(publication_scope,'resolve',lambda *_args,**_kwargs:None)
        with pytest.raises(WorkspaceError,match='隔离'):
            costs.apply_payload(s.ws,s.model,'s1','2026-06',raw,row['source_binding_sha'])
        assert not raw.get('period_cost_input')


def test_documentary_archive_uses_verified_bytes_and_rejects_mid_write_source_change(bundle,monkeypatch):
    from ledger import freight_experiments_inputs as frozen
    path,_=bundle;ref,_=reference(path/'evidence');frozen.seal(path)
    with fixture_session(path,seed()) as s:
        prepare(s);original=costs._archive;source=Path(ref['path']);approved_bytes=source.read_bytes()
        def mutate_after_archive(ws,body):
            archived=original(ws,body)
            source.write_bytes(approved_bytes+b' ')
            return archived
        monkeypatch.setattr(costs,'_archive',mutate_after_archive)
        with pytest.raises(WorkspaceError,match='捕获'):
            s.restore_approved_manual_finance('s1','2026-06',reference=ref,approval_reference='fixture documentary authority',fixture=True,cost_input_only=True)
        assert not s.ws.conn.execute('SELECT 1 FROM period_cost_input').fetchone()
        assert not s.ws.conn.execute("SELECT 1 FROM automation_authorization WHERE action='period_cost_input.apply'").fetchone()
        artifact=s.root/'period-cost-input-evidence'/(ref['sha256']+'.json')
        assert hashlib.sha256(artifact.read_bytes()).hexdigest()==ref['sha256']
        assert artifact.read_bytes()==approved_bytes


def test_month_routes_pending_conflict_and_damaged_approval_without_reentering_costs(bundle,monkeypatch):
    path,_=bundle
    with fixture_session(path,seed()) as s:
        prepare(s);saved=apply(s,preview(s))
        row=next(r for r in actions.month(s.ws,s.model,'2026-06')['stores'] if r['store_id']=='s1')
        assert row['status']=='processing' and row['stage']=='calculating'
        assert '无需再次确认' in row['message']
        s.drain_pending();install_complete_price_source(s,'40.01');s.recompute('s1')
        row=next(r for r in actions.month(s.ws,s.model,'2026-06')['stores'] if r['store_id']=='s1')
        assert row['status']=='needs_decision' and row['stage']=='period_cost_conflict'
        conflict=row['period_cost_input_conflict']
        assert conflict['approved_costs']['goods']=='40.00'
        assert conflict['current_source_costs']['goods']=='40.01'
        assert conflict['requires_amount_reentry'] is False
        assert conflict['decision_id']==saved['decision_id'] and conflict['revoke_input_sha']
        # Losing an old approved document is maintenance, not a new amount form.
        fact=costs.latest(s.ws,'s1','2026-06');artifact=json.loads(fact['evidence_json'])['artifacts'][0]
        (s.root/artifact['path']).write_text('damaged capture')
        row=next(r for r in actions.month(s.ws,s.model,'2026-06')['stores'] if r['store_id']=='s1')
        assert row['status']=='processing' and row['stage']=='maintenance'
        assert row['period_cost_input_conflict'] is None
        assert not any(a['kind']=='advance' for a in row['actions'])
