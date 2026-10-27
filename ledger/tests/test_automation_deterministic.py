import hashlib
import io
import json
from datetime import datetime,timezone,timedelta

import polars as pl
import pytest

from conftest import MODELS
from ledger import automation_store as journal,statement_review
from ledger.automation_deterministic import preview_statement,apply_statement,RULE,RULE_SHA
from ledger.engine.runtime import Ingestion,run,_dedupe_across_files
from ledger.engine.types import FileRef
from ledger.model.loader import load_model
from ledger.model.schema import Model,Store,StatementNode,Overhead
from ledger.storage_integrity import seal
from ledger.view import slice_dict
from ledger.workspace import Workspace,WorkspaceError
from test_statement_event_dedupe import item,douyin_row
from test_reship_period_and_cost_drill import item as order_item


@pytest.fixture(autouse=True)
def rollout(monkeypatch):
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','1')


def source(tmp_path,ambiguous=False):
    ws=Workspace(tmp_path/'ws');builtin=load_model(MODELS/'cn-ecommerce')
    # A transaction-month statement can certify its month even when one copy's
    # order reference is unlinked. Unknown/cross-month groups stay ineligible.
    metric=builtin.metric('trade_receipt_douyin').model_copy(update={'major':None,'posting_basis':'transaction','time_basis':'settle_time'})
    model=Model(id='exact',name='exact',stores=(Store(id='s',name='shop',platform='douyin'),),
        sources=(builtin.source('order_detail'),builtin.source('settlement').model_copy(update={'provides':(metric.id,)})),
        metrics=(metric,),statement=(StatementNode(id='n_receipt',name='销售收入',headline='revenue',formula={'op':'add','of':[metric.id]}),),
        overheads=(Overhead(period='2026-07',amount=0),))
    entries=[item([douyin_row(base_order_id=ref,settle_time=datetime(2026,7,14,9,14,6))],name,'douyin_settlement_v2')
             for ref,name in [('A','first'),('B','second')]]
    shas=[]
    for entry in entries:
        kept=ws.keep(entry.ref.filename,io.BytesIO(entry.frame.write_ndjson().encode()),'s')
        entry.ref=FileRef(kept.sha,entry.ref.filename,'sheet');shas.append(kept.sha)
        entry.frame=entry.frame.with_columns(pl.lit(kept.sha).alias('__sha__'))
    rows=[dict(order_id=oid,product_id='p',store_name='shop',order_time=datetime(2026,7,1),buyer_paid=128.2)
          for oid in (['A','B'] if ambiguous else ['A'])]
    ing=Ingestion(model=model,items=[*entries,order_item('order_detail',rows,rows[0].keys())])
    _dedupe_across_files(ing,model);result=run(ing,'douyin');sl=result.slice('shop','2026-07')
    rid=ws.record('s','2026-07',slice_dict(sl,model.store('s'),model),shas,
        model_revision=hashlib.sha256(model.model_dump_json().encode()).hexdigest())
    sl.facts.write_parquet(ws.facts_path(rid));seal(ws.facts_path(rid))
    group=statement_review.context(ws,'s','2026-07',rid,model)['groups'][0]
    from ledger import automation_actions as actions
    human={'id':'finance','kind':'human','authenticated':True}
    now=datetime.now(timezone.utc)
    preview=actions.decision_preview(ws,model,request_id='grant-preview',period='2026-07',store_id='s',kind='rule_grant',
        values={'rule_id':RULE,'rule_sha':RULE_SHA,'enabled':True,
                'valid_from':(now-timedelta(days=1)).isoformat(),'valid_until':(now+timedelta(days=1)).isoformat()},
        reason='批准固定规则在本店本月灰度处理，财务终态仍人工确认',actor=human)
    grant=actions.decision_apply(ws,model,request_id='grant-apply',proposal_id=preview['proposal_id'],
        proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=human)
    aid=grant['decision_id']
    return ws,model,ing,rid,group,aid


def test_exact_unique_source_link_is_applied_once_without_model_or_edited_money(tmp_path):
    ws,model,ing,rid,group,aid=source(tmp_path)
    plan=preview_statement(ws,model,'s','2026-07',rid,group['group_id'])
    first=apply_statement(ws,model,plan,aid)
    assert apply_statement(ws,model,plan,aid)==first
    assert ws.conn.execute('SELECT count(*) FROM automation_decision WHERE kind=?',(RULE,)).fetchone()[0]==1
    decision=json.loads(ws.conn.execute("SELECT after_json FROM config_log WHERE kind='statement-review'").fetchone()[0])
    assert decision['actor']=='service:deterministic-ledger'
    assert decision['chosen']==plan['chosen']
    statement_review.apply(ing,ws,'s');result=run(ing,'douyin')
    assert result.slice('shop','2026-07').nodes['n_receipt'].value==128.2
    assert any('确定性规则核对' in str(n) for n in result.facts['source_note'])
    assert ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='dispatch_pending'").fetchone()[0]==1
    assert ws.state('s','2026-07').state=='open'


def test_two_proven_candidates_are_not_selected_automatically(tmp_path):
    ws,model,ing,rid,group,aid=source(tmp_path,ambiguous=True)
    with pytest.raises(WorkspaceError,match='只能有一个'):
        preview_statement(ws,model,'s','2026-07',rid,group['group_id'])
    assert not ws.conn.execute('SELECT 1 FROM automation_decision WHERE kind=?',(RULE,)).fetchone()


def test_source_changed_after_preview_cannot_be_automatically_confirmed(tmp_path):
    ws,model,ing,rid,group,aid=source(tmp_path)
    plan=preview_statement(ws,model,'s','2026-07',rid,group['group_id'])
    ws.keep('first.xlsx',io.BytesIO(b'changed source'),'s')
    with pytest.raises(WorkspaceError):apply_statement(ws,model,plan,aid)
    assert not ws.conn.execute('SELECT 1 FROM automation_decision WHERE kind=?',(RULE,)).fetchone()


def test_no_delegated_grant_cannot_write_business_evidence(tmp_path):
    ws,model,ing,rid,group,aid=source(tmp_path)
    plan=preview_statement(ws,model,'s','2026-07',rid,group['group_id'])
    with pytest.raises(PermissionError):apply_statement(ws,model,plan,'missing')
    assert not ws.conn.execute("SELECT 1 FROM config_log WHERE kind='statement-review'").fetchone()


def test_receipt_failure_rolls_back_link_decision_authorization_and_queue_handoff(tmp_path):
    ws,model,ing,rid,group,aid=source(tmp_path)
    plan=preview_statement(ws,model,'s','2026-07',rid,group['group_id'])
    ws.conn.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON automation_receipt BEGIN SELECT RAISE(ABORT,'receipt unavailable'); END")
    with pytest.raises(Exception,match='receipt unavailable'):apply_statement(ws,model,plan,aid)
    assert not ws.conn.execute("SELECT 1 FROM config_log WHERE kind='statement-review'").fetchone()
    assert not ws.conn.execute('SELECT 1 FROM automation_decision WHERE kind=?',(RULE,)).fetchone()
    assert ws.conn.execute('SELECT count(*) FROM automation_authorization').fetchone()[0]==2


def test_manager_source_arrival_runs_approved_exact_rule_then_existing_queue_recomputes(tmp_path,monkeypatch):
    import copy
    from ledger import service,order_feed,automation_checker
    from ledger.commission_registry import Registry
    from ledger.commission_manager import Manager
    from ledger.model.schema import Metric,Check
    ws,model,ing,rid,group,aid=source(tmp_path)
    cost=Metric(id='goods_cost',name='原表已证明成本',source='order_detail',
        value={'op':'sum','of':['known_cost']},sign='negate',link={'key':'order_id','to':'order.order_id','grain':'order'})
    model=model.model_copy(update={'metrics':(*model.metrics,cost),
        'checks':(Check(id='cost_coverage',name='成本完整',kind='spine_coverage',metric='goods_cost',threshold=1,blocking=True),),
        'statement':tuple(n.model_copy(update={'commission_base':True}) for n in model.statement)})
    ing.model=model
    ing.items[-1].frame=ing.items[-1].frame.with_columns(pl.lit(2.).alias('known_cost'))
    monkeypatch.setattr(order_feed,'enabled',lambda:False)
    # The parser is isolated by this component fixture; engine.run, service
    # publication, sealed evidence, Manager/outbox/second calculation are real.
    monkeypatch.setattr(service,'ingest',lambda *args,**kwargs:copy.deepcopy(ing))
    registry=Registry(ws.root)
    registry.enqueue_files({'s'},'nas:'+hashlib.sha256(b'nas-fixture-originals').hexdigest())
    manager=Manager(lambda:ws,lambda:model)
    manager.once()
    assert ws.conn.execute('SELECT count(*) FROM automation_decision WHERE kind=?',(RULE,)).fetchone()[0]==1
    assert not ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state='dispatch_pending'").fetchone()
    with registry.connect() as conn:
        assert conn.execute("SELECT 1 FROM pending WHERE store_id='s'").fetchone()
    manager.once()
    with registry.connect() as conn:
        assert not conn.execute("SELECT 1 FROM pending WHERE store_id='s'").fetchone()
    checked=automation_checker.check(ws,'s','2026-07',model=model,verify_artifacts=True)
    assert checked['allowed'],checked['issues']
    latest=ws.latest_run('s','2026-07')
    assert json.loads(latest['result'])['statement'][0]['value']==128.2
    assert ws.state('s','2026-07').state=='open'
    assert ws.conn.execute("SELECT stage FROM automation_case WHERE store_id='s' AND period='2026-07'").fetchone()[0]=='ready_for_close'


def test_rollout_off_keeps_source_pending_for_review_without_applying_rule(tmp_path,monkeypatch):
    from ledger.automation_deterministic import run_available
    ws,model,ing,rid,group,aid=source(tmp_path)
    monkeypatch.delenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED')
    assert run_available(ws,model,'2026-07')==[]
    assert not ws.conn.execute('SELECT 1 FROM automation_decision WHERE kind=?',(RULE,)).fetchone()


def test_human_revocation_removes_statement_effect_and_blocks_automatic_reapplication(tmp_path):
    from ledger import automation_actions as actions
    from ledger.automation_deterministic import run_available
    ws,model,ing,rid,group,aid=source(tmp_path)
    applied=apply_statement(ws,model,preview_statement(ws,model,'s','2026-07',rid,group['group_id']),aid)
    value=json.loads(ws.conn.execute('SELECT values_json FROM automation_decision WHERE id=?',(applied['decision_id'],)).fetchone()[0])
    sha=actions._decision_input(ws,model,RULE,'s','2026-07',value)
    result=actions.decision_revoke(ws,model,applied['decision_id'],request_id='withdraw-exact',
        reason='人工重新核对原始流水，需要撤回自动候选等待核实',input_sha=sha,
        actor={'id':'finance','kind':'human','authenticated':True})
    assert result['status']=='revoked'
    latest=json.loads(ws.conn.execute("SELECT after_json FROM config_log WHERE kind='statement-review' ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert latest['action']=='revoke'
    assert run_available(ws,model,'2026-07')==[]
    assert ws.conn.execute("SELECT count(*) FROM config_log WHERE kind='statement-review'").fetchone()[0]==2
