"""Verify a captured finite feed prefix without chasing an unrelated live head.

Only trusted run/validation manifests supply boundaries. No HTTP/model chooses
these fields. Missing proofs retain the legacy global synchronization gate.
"""
from __future__ import annotations

import json
import sqlite3
import contextvars
from contextlib import contextmanager

from . import automation_store as journal

BOUNDARY_SCHEMA='ledger_feed_boundary.v1'
IDENTITY_SCHEMA='ledger_scoped_feed_identity.v1'
_task=contextvars.ContextVar('ledger_feed_task_boundary',default=None)


@contextmanager
def hold_task(root,store_id,snapshot_id,required_through_seq):
    if not isinstance(snapshot_id,str) or not snapshot_id or not _integer(required_through_seq):
        raise ValueError('可信任务的有限前缀无效')
    token=_task.set((str(root.resolve()),store_id,snapshot_id,required_through_seq))
    try:yield
    finally:_task.reset(token)


def task_boundary(root,store_id,snapshot_id,consumed,observed_head,mapping):
    task=_task.get()
    if task and task[:3]!=(str(root.resolve()),store_id,snapshot_id):
        raise ValueError('可信任务的来源快照或店铺已变化')
    return {'schema':BOUNDARY_SCHEMA,'store_id':store_id,'snapshot_id':snapshot_id,
        'required_through_seq':task[3] if task else observed_head,'captured_through_seq':consumed,
        'observed_head_seq':observed_head,'order_store_ids':sorted(mapping)}


_close=contextvars.ContextVar('ledger_close_feed_boundaries',default={})


@contextmanager
def hold_close(boundaries):
    token=_close.set(boundaries or {})
    try:yield
    finally:_close.reset(token)


ACCOUNTING_ENTITIES=frozenset({'order','order_item','order_cost','order_relation','after_sale','after_sale_item'})


def _integer(value):
    return type(value) is int and value>=0


def _month(value):
    return isinstance(value,str) and len(value)==7 and value[4]=='-' and value[:4].isdigit() and value[5:].isdigit()


def _legacy(value,reason):
    fields=('snapshot_id','consumed_seq','source_latest_seq','last_error')
    identity={key:value.get(key) for key in fields} if value else None
    blocked=bool(value and (not value.get('snapshot_id') or value.get('last_error')
        or not _integer(value.get('consumed_seq')) or not _integer(value.get('source_latest_seq'))
        or value['consumed_seq']<value['source_latest_seq']))
    return {'identity':identity,'blocked':blocked,'mode':'legacy_global','reason':reason,'proof':None}


def evaluate(ws,store_id,period,recorded):
    """Read one SQLite snapshot; diagnostics never become financial identity.

    A boundary proves the task's finite prefix, not an unread future tail. Known
    later changes for this store or unknown-shop accounting rows remain blocked
    until a new trusted calculation/equivalence validation consumes them.
    """
    path=ws.root/'order-feed.db'
    boundary=(recorded or {}).get('feed_boundary')
    if not path.exists():
        return {'identity':{'schema':IDENTITY_SCHEMA,'state':'source_missing'},'blocked':True,
            'mode':'captured_prefix','reason':'captured_feed_source_missing','proof':None} if boundary else _legacy(None,'feed_not_configured')
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as conn:
            conn.row_factory=sqlite3.Row;conn.execute('BEGIN')
            state_row=conn.execute('SELECT * FROM feed_state WHERE id=1').fetchone()
            state=dict(state_row) if state_row else None
            if not isinstance(boundary,dict) or boundary.get('schema')!=BOUNDARY_SCHEMA:
                return _legacy(state,'captured_prefix_proof_missing')
            expected={'schema','store_id','snapshot_id','required_through_seq','captured_through_seq','order_store_ids'}
            valid=(expected<=set(boundary) and boundary.get('store_id')==store_id
                and isinstance(boundary.get('snapshot_id'),str) and bool(boundary['snapshot_id'])
                and _integer(boundary.get('required_through_seq')) and _integer(boundary.get('captured_through_seq'))
                and boundary['captured_through_seq']>=boundary['required_through_seq']
                and isinstance(boundary.get('order_store_ids'),list) and bool(boundary['order_store_ids'])
                and all(isinstance(v,str) and bool(v.strip()) and v==v.strip() for v in boundary['order_store_ids'])
                and len(set(boundary['order_store_ids']))==len(boundary['order_store_ids']))
            from .version import assert_code_integrity,CodeIntegrityError
            try:code=assert_code_integrity()['source_code_sha']
            except CodeIntegrityError:return _legacy(state,'captured_prefix_code_unverified')
            if not valid or recorded.get('source_code_sha')!=code:
                return {'identity':{'schema':IDENTITY_SCHEMA,'state':'captured_prefix_contract_unverified'},
                        'blocked':True,'mode':'captured_prefix','reason':'captured_prefix_contract_unverified','proof':None}
            identity={'schema':IDENTITY_SCHEMA,'store_id':store_id,'period':period,'state':'verified'}
            def reject(reason,extra=None):
                return {'identity':{**identity,'state':reason,**(extra or {})},'blocked':True,
                    'mode':'captured_prefix','reason':reason,'proof':boundary}
            if not state or state.get('last_error'):return reject('source_unavailable')
            if state.get('snapshot_id')!=boundary['snapshot_id']:return reject('source_snapshot_changed')
            if not _integer(state.get('consumed_seq')) or state['consumed_seq']<boundary['captured_through_seq']:
                return reject('captured_prefix_not_consumed')
            close=_close.get().get(store_id)
            if close:
                if close.get('period')!=period or close.get('snapshot_id')!=state['snapshot_id']:
                    return reject('close_prefix_snapshot_changed')
                if not _integer(close.get('required_through_seq')) or state['consumed_seq']<close['required_through_seq']:
                    return reject('close_prefix_not_consumed')
            mapping=sorted(str(row[0]) for row in conn.execute(
                "SELECT order_store_id FROM feed_store WHERE ledger_store_id=? AND mapping_status='confirmed'",(store_id,)))
            if mapping!=sorted(boundary['order_store_ids']):return reject('source_mapping_changed')
            # 每家店只走自己的索引段、只数条数和最新序号：每次变更都会拿到新序号，
            # 这两样足以标出「边界之后有没有新变更」。逐行读载荷算摘要要扫 2GB 的表，
            # 订单台大批回填时一家店十几秒，57 家店的整月检查就是十几分钟。
            # 变更只会影响它订单所在月及以后的月份（按下单日或按发生日入账）。
            # 认不出月份的变更照旧挡住所有月份。
            placeholders=','.join('?' for _ in mapping)
            types=','.join('?' for _ in ACCOUNTING_ENTITIES)
            tail=' AND entity_type IN ('+types+') AND seq>?'
            args=[*sorted(ACCOUNTING_ENTITIES),boundary['captured_through_seq']]
            if _month(period):
                tail+=' AND (business_month IS NULL OR business_month<=?)'
                args.append(period)
            changes,through=conn.execute('SELECT count(*),max(seq) FROM ('
                'SELECT seq FROM feed_entity WHERE order_store_id IN ('+placeholders+')'+tail+
                ' UNION ALL SELECT seq FROM feed_entity WHERE order_store_id IS NULL'+tail+
                " UNION ALL SELECT seq FROM feed_entity WHERE order_store_id=''"+tail+')',
                [*mapping,*args,*args,*args]).fetchone()
            # 店铺待通知行和它的变更同一事务写入，按月份数变更已经覆盖它；
            # 快照更换另有 source_snapshot_changed 挡住。
            pending=None if _month(period) else conn.execute('SELECT 1 FROM feed_pending_store WHERE ledger_store_id=? AND seq>?',
                (store_id,boundary['captured_through_seq'])).fetchone()
            if changes or pending:
                return reject('source_changes_not_revalidated',{'changes':changes,'changes_through_seq':through,'store_pending':bool(pending)})
            return {'identity':identity,'blocked':False,'mode':'captured_prefix','reason':None,
                'proof':{**boundary,'observed_consumed_seq':state['consumed_seq'],
                         'observed_head_seq':state.get('source_latest_seq'),
                         'future_tail_outside_boundary':bool(_integer(state.get('source_latest_seq')) and state['source_latest_seq']>boundary['required_through_seq'])}}
    except (sqlite3.Error,OSError,ValueError,TypeError,KeyError):
        return {'identity':{'schema':IDENTITY_SCHEMA,'state':'source_proof_unreadable'},'blocked':True,
                'mode':'captured_prefix','reason':'source_proof_unreadable','proof':None}


def close_boundaries(ws,period,store_ids,request_id,actor):
    from .finance_guard import authority
    with authority(ws.root):
        return _close_boundaries(ws,period,store_ids,request_id,actor)


def _close_boundaries(ws,period,store_ids,request_id,actor):
    """Persist a server-observed finite prefix before any financial proposal.

    Waiting does not roll this technical checkpoint back. Reusing its request
    identity with another actor/scope is rejected. No amount authority is made.
    """
    key='close-feed-boundary:'+request_id
    signature=journal.digest([period,sorted(store_ids),actor['id']])
    row=ws.conn.execute("SELECT result_json FROM automation_attempt WHERE request_id=? AND state='close_feed_boundary'",(key,)).fetchone()
    if row:
        value=json.loads(row[0])
        if value['signature']!=signature:raise ValueError('结账同步边界请求编号与操作人或范围不匹配')
        boundaries=value['boundaries']
    else:
        from .pricing_status import read_feed
        feed=read_feed(ws.root)
        # 边界用已经吃进台账的序号。订单台头部还在往前走时，不能因为差几条新单
        # 就把整月结账预览作废；受影响的店仍由各店自己的来源复核拦住。
        caught=feed.get('consumed_seq') if feed else None
        if feed and not _integer(caught):
            raise ValueError('订单同步前缀还没有可读的已消费序号，请稍后重试')
        boundaries={sid:{'period':period,'snapshot_id':feed['snapshot_id'],
            'required_through_seq':caught,'observed_head_seq':feed.get('source_latest_seq')}
            for sid in store_ids} if feed else {}
        with ws.transaction() as conn:
            conn.execute('INSERT INTO automation_attempt VALUES(?,?,?,?,?,?,?)',
                (journal.ident(),'__company__',key,signature,'close_feed_boundary',
                 journal.encoded({'signature':signature,'boundaries':boundaries}),journal.now()))
    if boundaries:
        from .pricing_status import read_feed
        current=read_feed(ws.root)
        if (not current or current.get('last_error') or any(not b.get('snapshot_id') or b['snapshot_id']!=current['snapshot_id']
            or not _integer(b.get('required_through_seq')) or not _integer(current.get('consumed_seq'))
            or current['consumed_seq']<b['required_through_seq'] for b in boundaries.values())):
            raise ValueError('本次结账捕获的订单同步前缀尚未追平或来源已变化，请按同一请求重试')
    return boundaries


def current(ws,store_id,period):
    run=ws.latest_run(store_id,period)
    row=ws.conn.execute('SELECT manifest_json FROM automation_run_validation WHERE run_id=? ORDER BY rowid DESC LIMIT 1',(run['id'],)).fetchone() if run else None
    if row is None and run:row=ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(run['id'],)).fetchone()
    return evaluate(ws,store_id,period,json.loads(row['manifest_json']) if row else None)
