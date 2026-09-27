import io,json,hashlib,sqlite3
from copy import deepcopy
from datetime import datetime
import pytest
import polars as pl
from conftest import MODELS
from ledger.model.loader import load_model
from ledger.model.schema import Model,Store,StatementNode
from ledger.workspace import Workspace,WorkspaceError
from ledger.engine.runtime import Ingestion,run,_dedupe_across_files
from ledger.engine.types import FileRef
from ledger.storage_integrity import seal
from ledger.view import slice_dict
from ledger.statement_review import context,confirm,apply
from test_statement_event_dedupe import item,douyin_row
from test_reship_period_and_cost_drill import item as order_item


def setup(tmp_path,second_amount=128.2):
    ws=Workspace(tmp_path/'workspace');builtin=load_model(MODELS/'cn-ecommerce')
    metric=builtin.metric('trade_receipt_douyin').model_copy(update={'major':None})
    model=Model(id='test',name='test',stores=(Store(id='s',name='shop',platform='douyin'),),
        sources=(builtin.source('order_detail'),builtin.source('settlement').model_copy(update={'provides':(metric.id,)})),
        metrics=(metric,),statement=(StatementNode(id='n_receipt',name='销售收入',formula={'op':'add','of':[metric.id]}),))
    first=item([douyin_row(base_order_id='A')],'first','douyin_settlement_v2')
    second=item([douyin_row(base_order_id='B',income=second_amount)],'second','douyin_settlement_v2')
    shas=[]
    for entry in [first,second]:
        kept=ws.keep(entry.ref.filename,io.BytesIO(entry.ref.filename.encode()),'s')
        entry.ref=FileRef(kept.sha,entry.ref.filename,'sheet');shas.append(kept.sha)
        entry.frame=entry.frame.with_columns(pl.lit(kept.sha).alias('__sha__'))
    rows=[dict(order_id=x,product_id='p',store_name='shop',order_time=datetime(2026,7,1),buyer_paid=128.2) for x in ['A','B']]
    ing=Ingestion(model=model,items=[first,second,order_item('order_detail',rows,rows[0].keys())])
    _dedupe_across_files(ing,model)
    result=run(ing,'douyin');sl=result.slice('shop','2026-07')
    payload=slice_dict(sl,model.store('s'),model)
    rid=ws.record('s','2026-07',payload,shas,model_revision=hashlib.sha256(model.model_dump_json().encode()).hexdigest())
    sl.facts.write_parquet(ws.facts_path(rid));seal(ws.facts_path(rid))
    return ws,model,ing,rid


def select(ws,model,rid):
    page=context(ws,'s','2026-07',rid,model);assert page['total']==1
    group=page['groups'][0]
    return group,group['members'][0]['anchor']


def test_confirmation_is_audited_and_recalculation_retains_one_original_amount(tmp_path):
    ws,model,ing,rid=setup(tmp_path)
    group,choice=select(ws,model,rid);assert group['can_confirm']
    before=ws.conn.execute('select result from run where id=?',(rid,)).fetchone()[0]
    receipt=confirm(ws,model,'s','2026-07',rid,group['group_id'],choice,'已查平台流水确认原订单归属',True,'财务甲')
    assert receipt['saved'] and receipt['recompute_required']
    assert ws.conn.execute('select result from run where id=?',(rid,)).fetchone()[0]==before
    apply(ing,ws,'s')
    result=run(ing,'douyin');sl=result.slice('shop','2026-07')
    assert sl.nodes['n_receipt'].value==128.2
    assert not any(d['status']=='conflict' for d in sl.deduplication)
    assert any('人工核对' in str(v) for v in sl.facts['source_note'])
    with pytest.raises(WorkspaceError,match='已保存'):
        confirm(ws,model,'s','2026-07',rid,group['group_id'],choice,'已查平台流水确认原订单归属',True,'财务甲')


def test_different_money_cannot_be_chosen_away(tmp_path):
    ws,model,ing,rid=setup(tmp_path,129.2)
    group,choice=select(ws,model,rid)
    assert not group['can_confirm'] and '金额' in group['reason']
    with pytest.raises(WorkspaceError,match='金额'):
        confirm(ws,model,'s','2026-07',rid,group['group_id'],choice,'随便选择一份金额是不允许的',True,'甲')


@pytest.mark.parametrize('change',['closed','new_run','model','source','checksum'])
def test_stale_closed_and_corrupt_evidence_cannot_be_confirmed(tmp_path,change):
    ws,model,ing,rid=setup(tmp_path)
    group,choice=select(ws,model,rid)
    if change=='closed':
        with ws.conn:ws.conn.execute("update period set state='closed',run_id=? where store_id='s' and period='2026-07'",(rid,))
    elif change=='new_run':ws.record('s','2026-07',{},[])
    elif change=='model':model=model.model_copy(update={'name':'changed'})
    elif change=='source':ws.keep('first.xlsx',io.BytesIO(b'new source'),'s')
    else:ws.path_of(group['members'][0]['file_sha']).write_bytes(b'corrupt')
    with pytest.raises(WorkspaceError):
        confirm(ws,model,'s','2026-07',rid,group['group_id'],choice,'已查平台流水确认原订单归属',True,'财务甲')
    assert ws.conn.execute("select count(*) from config_log where kind='statement-review'").fetchone()[0]==0


def test_forged_anchor_and_missing_ack_are_rejected(tmp_path):
    ws,model,ing,rid=setup(tmp_path);group,choice=select(ws,model,rid)
    with pytest.raises(WorkspaceError,match='核对平台'):
        confirm(ws,model,'s','2026-07',rid,group['group_id'],choice,'已查平台流水确认原订单归属',False,'甲')
    with pytest.raises(WorkspaceError,match='只能选择'):
        confirm(ws,model,'s','2026-07',rid,group['group_id'],'made-up','已查平台流水确认原订单归属',True,'甲')


def test_audit_failure_does_not_create_a_decision(tmp_path):
    ws,model,ing,rid=setup(tmp_path);group,choice=select(ws,model,rid)
    ws.conn.execute("create trigger deny_review before insert on config_log when new.kind='statement-review' begin select raise(abort,'audit unavailable');end")
    with pytest.raises(sqlite3.IntegrityError):
        confirm(ws,model,'s','2026-07',rid,group['group_id'],choice,'已查平台流水确认原订单归属',True,'甲')
    assert ws.conn.execute("select count(*) from config_log where kind='statement-review'").fetchone()[0]==0


def test_revoke_is_append_only_and_restores_review_instead_of_deleting_history(tmp_path):
    from ledger.statement_review import revoke
    ws,model,ing,rid=setup(tmp_path);group,choice=select(ws,model,rid)
    confirm(ws,model,'s','2026-07',rid,group['group_id'],choice,'已查平台流水确认原订单归属',True,'甲')
    revoke(ws,'s','2026-07',rid,group['group_id'],'发现依据不完整撤销并重新核对','甲')
    assert ws.conn.execute("select count(*) from config_log where kind='statement-review'").fetchone()[0]==2
    apply(ing,ws,'s')
    assert any('待核对' in str(v) for v in ing.items[0].frame['source_note'])
    assert context(ws,'s','2026-07',rid,model)['groups'][0]['can_confirm']


def test_changed_event_group_cannot_reuse_a_past_decision(tmp_path):
    ws,model,ing,rid=setup(tmp_path);group,choice=select(ws,model,rid)
    confirm(ws,model,'s','2026-07',rid,group['group_id'],choice,'已查平台流水确认原订单归属',True,'甲')
    for entry in ing.items:
        if 'statement_evidence' in entry.frame.columns:
            values=[json.dumps({**json.loads(v),'group_id':'a'*64}) if v else None for v in entry.frame['statement_evidence']]
            entry.frame=entry.frame.with_columns(pl.Series('statement_evidence',values,dtype=pl.String))
    apply(ing,ws,'s')
    assert ing.items[0].frame.height==ing.items[1].frame.height==1
    assert '待核对' in ing.items[0].frame['source_note'][0]


@pytest.mark.parametrize('previous_closed',[False,True])
def test_changed_posting_month_invalidates_review_and_protects_closed_period(tmp_path,previous_closed):
    ws,model,ing,rid=setup(tmp_path);group,choice=select(ws,model,rid)
    confirm(ws,model,'s','2026-07',rid,group['group_id'],choice,'已查平台流水确认原订单归属',True,'甲')
    if previous_closed:
        with ws.conn:ws.conn.execute("update period set state='closed',run_id=? where store_id='s' and period='2026-07'",(rid,))
    ing.items[-1].frame=ing.items[-1].frame.with_columns(
        pl.lit(datetime(2026,6,1)).alias('order_time'),pl.lit(datetime(2026,6,1)).alias('order_date'))
    apply(ing,ws,'s');result=run(ing,'douyin');sl=result.slice('shop','2026-06')
    assert not sl.nodes['n_receipt'].available
    new_id=ws.record('s','2026-06',slice_dict(sl,model.store('s'),model),json.loads(ws.latest_run('s','2026-07')['shas']),
        model_revision=hashlib.sha256(model.model_dump_json().encode()).hexdigest())
    sl.facts.write_parquet(ws.facts_path(new_id));seal(ws.facts_path(new_id))
    new_group=context(ws,'s','2026-06',new_id,model)['groups'][0]
    assert new_group['group_id']!=group['group_id']
    assert new_group['can_confirm'] is (not previous_closed)
    if previous_closed:assert '其他已结账' in new_group['reason']
