"""Real service/close/frozen-source regression; all inputs are synthetic.

No financial expected baseline or production authorization is generated. The
history is closed by the actual API; only identity-admission behavior changes.
"""
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from copy import deepcopy
import hashlib,json
import polars as pl
import pytest
from test_replay_service import complete_bundle,seed
from test_order_feed import _write
from test_jst_source_members import order,html
from conftest import write_xlsx,MODELS
from ledger import service,automation_actions as actions,automation_checker as checker,source_dates as dates,publication_scope as publication
from ledger.model.schema import Model,SourceContract,Template,Metric,ValueExpr,LinkRule,Overhead
from ledger.model.loader import load_model
from ledger.workspace import Workspace
from ledger.commission_registry import Registry
from ledger.order_feed import OrderFeed
from ledger.frozen_refund_guard import Context,STATUS


def _case(tmp_path,monkeypatch,*,legacy=False,native_seven=False,payout=False):
    tmp=tmp_path;mp=monkeypatch
    from ledger import frozen_refund_guard as guard_module
    original_annotate=Context.annotate
    original_index=guard_module.event_index
    if legacy:
        # Before any closed owner exists, only suppress the new metadata to
        # reproduce a legacy archive schema. No financial gate is bypassed.
        monkeypatch.setattr(Context,'annotate',lambda self,ing,facts,metrics:facts)
        monkeypatch.setattr(guard_module,'event_index',lambda *args:None)
    root = Path(tmp)
    root.mkdir(parents=True, exist_ok=True)
    setup = root / 'setup'
    setup.mkdir()
    bundle, _ = complete_bundle.__wrapped__(setup)
    base = Model.model_validate_json((bundle / 'model.json').read_text())
    full = load_model(MODELS / 'cn-ecommerce')
    ds = next((s for s in full.sources if s.id == 'refund_order_dates'))
    dt = next((t for t in full.templates if t.id == 'refund_order_dates_v1'))
    rt = Template(id='declared_refunds', source='refunds', name='退款原件', match_columns=['退款流水号', '退款原单', '退款金额', '发生时间'], event_namespace='declared_refunds', event_id_role='txn_id', time_slots={'settle_date': 'settle_time'}, bindings=[{'role': 'txn_id', 'columns': ['退款流水号']}, {'role': 'order_id', 'columns': ['退款原单']}, {'role': 'amount', 'columns': ['退款金额'], 'kind': 'number'}, {'role': 'settle_time', 'columns': ['发生时间'], 'kind': 'time'}, {'role':'sku','columns':['退款SKU'],'required':False}, {'role':'sub_order_id','columns':['退款子单'],'required':False}])
    rm = Metric(id='trade_refund', name='退款', source='refunds', value=ValueExpr(op='sum', of=['amount']), link=LinkRule(key='order_id', to='order.order_id', grain='order'), posting_basis='order_number', time_basis='order_date', orderless_time_basis='settle_date')
    model = base.model_copy(update={'stores': (base.store('s1'),), 'sources': (*base.sources, ds, SourceContract(id='refunds', name='退款原件', owner_role='finance', cadence='monthly', provides=['trade_refund'])), 'templates': (*base.templates, dt, rt), 'metrics': (*base.metrics, rm), 'overheads': (Overhead(period='2026-07', amount=0), Overhead(period='2026-09', amount=0)), 'statement': tuple((n.model_copy(update={'formula': n.formula.model_copy(update={'of': ('gross_profit', 'trade_refund')})}) if n.id == 'net_profit' else n for n in base.statement))})
    import shutil
    feed_root = root / 'feed'
    shutil.copytree(bundle / 'feed', feed_root)
    state = json.loads((bundle / 'feed-state.json').read_text())
    manifest = json.loads(state['manifest_json'])
    tables = {n: pl.read_parquet(feed_root / manifest['objects'][n]['path']) for n in ['orders.parquet', 'order_items.parquet', 'order_costs.parquet']}
    sample_id = tables['orders.parquet'].filter(pl.col('order_store_id') == '10')['order_id'][0]
    parent = '3319999999999999999'
    child = '3319999999999999000'
    header = tables['orders.parquet'].filter(pl.col('order_id') == sample_id).row(0, named=True)
    header.update(order_id='9000', online_order_no=parent, order_time='2026-09-02 10:00:00', pay_time='2026-09-02 10:01:00', ship_time='2026-09-02 11:00:00', tracking_no='W9000', platform_order_ids=[parent])
    item = tables['order_items.parquet'].filter(pl.col('order_id') == sample_id).row(0, named=True)
    item.update(order_id='9000', sub_order_id='9001', online_order_no=parent, sku_id='SKU9000', outer_sku=child, tracking_no='W9000')
    cost = tables['order_costs.parquet'].filter(pl.col('order_id') == sample_id).row(0, named=True)
    cost.update(order_id='9000', sub_order_id='9001', sku_id='SKU9000', cost_as_of='2026-09-02', pricing_evidence=json.dumps({'order_date': '2026-09-02'}))
    extra={}
    if native_seven:
        original=tables['orders.parquet'].filter((pl.col('order_store_id')=='10')&pl.col('order_time').cast(pl.String).str.starts_with('2026-07')).row(0,named=True)
        oid=original['order_id'];member=tables['order_items.parquet'].filter(pl.col('order_id')==oid).row(0,named=True);price=tables['order_costs.parquet'].filter(pl.col('order_id')==oid).row(0,named=True)
        original.update(order_id='9100',online_order_no='202600001',tracking_no='W9100',platform_order_ids=['202600001'])
        member.update(order_id='9100',sub_order_id='9101',online_order_no='202600001',sku_id='SKU9100',outer_sku='202600002',tracking_no='W9100')
        price.update(order_id='9100',sub_order_id='9101',sku_id='SKU9100')
        extra={'orders.parquet':original,'order_items.parquet':member,'order_costs.parquet':price}
    for name, row in [('orders.parquet', header), ('order_items.parquet', item), ('order_costs.parquet', cost)]:
        frame = pl.concat([tables[name], pl.from_dicts([row,*([extra[name]] if name in extra else [])], schema=tables[name].schema, strict=False)], how='vertical_relaxed')
        manifest['objects'][name] = _write(feed_root, name, frame)
    manifest['snapshot_id'] += '-declared-july-september-refund-case'
    if manifest.get('producer_query_scope'):
        manifest['producer_query_scope']['snapshot_id'] = manifest['snapshot_id']
        for name in ['orders.parquet', 'order_items.parquet', 'order_costs.parquet']:
            manifest['producer_query_scope']['objects'][name]['sha256'] = manifest['objects'][name]['sha256']
    ws = Workspace(root / 'workspace')
    feed = OrderFeed(ws.root, feed_root=feed_root, client=object())
    feed._install_snapshot(manifest, {'stores': [{'order_store_id': '10', 'ledger_store_id': 's1', 'mapping_status': 'confirmed'}]}, {'healthy': True}, source_revision=10, source_latest_seq=manifest['through_seq'], revision_etag='declared')
    raw = pl.scan_parquet(feed_root / manifest['objects']['orders.parquet']['path']).filter(pl.col('order_store_id') == '10').collect()
    raw_items = pl.read_parquet(feed_root / manifest['objects']['order_items.parquet']['path'])
    native = []
    for r in raw.to_dicts():
        i = raw_items.filter(pl.col('order_id') == r['order_id']).row(0, named=True)
        native.append([r['online_order_no'], i['outer_sku'], i['merchant_sku'], 10, datetime.fromisoformat(r['order_time']), r['tracking_no']])
    sheet = write_xlsx(root / 'declared-native.xlsx', [['主订单号', '子订单号', '宝贝ID', '买家实付', '下单时间', '物流单号'], *native])
    ws.keep(sheet.name, sheet, 's1')
    bank = write_xlsx(root / 'declared-refund.xlsx', [['退款流水号', '退款原单', '退款金额', '发生时间'], ['refund-event-one', '202600001', -20, datetime(2026, 9, 8)]])
    bank_ref = ws.keep(bank.name, bank, 's1')
    registry = Registry(ws.root)
    fixed = seed()
    for person in fixed['people']:
        registry.person_save(person, 'fixture', 'declared identity')
    for member in fixed['members']:
        if member['store_id'] == 's1':
            registry.save_store_member('s1', member['person_id'], member['duty'], '', 'fixture', 'declared relationship')
    for scheme in fixed['schemes']:
        if scheme['store_id'] == 's1':
            registry.save_scheme('s1', scheme['product_id'], scheme['body'], 'fixture', 'declared scheme', publish=True)
    for policy in fixed['policies']:
        registry.save_policy(policy['body'], policy['effective_from'], 'fixture', 'declared original policy')
    registry.operator_add('fixture-human', 'synthetic-password')
    actor = {**registry.actor(registry.login('fixture-human', 'synthetic-password')), 'kind': 'human', 'authenticated': True}
    mp.setenv('LEDGER_ORDER_FEED_ENABLED', '1')
    mp.setenv('LEDGER_ORDER_FEED_ROOT', str(feed_root))
    mp.setenv('LEDGER_ORDER_FEED_URL', 'http://127.0.0.1:1/offline')
    historical = publication.PublicationScope.for_isolated(ws.root, [('s1', '2026-07'), ('s1', '2026-09')], approval_reference='explicit-synthetic-historical-setup-only')
    publication.install_isolated(historical)
    checker.bind_model(ws, model)
    initial = service.recompute(ws, model, model.store('s1'), publication_scope=historical)
    assert not initial.failure, initial.failure
    actions.sync_month(ws, model, '2026-09')
    def publish(day, letter):
        actions.sync_month(ws, model, '2026-09')
        case = dict(ws.conn.execute("SELECT * FROM automation_case WHERE store_id='s1' AND period='2026-09'").fetchone())
        tid = 'source-' + letter * 40
        jid = letter * 32
        folder = ws.root / 'source-recovery' / tid / jid
        folder.mkdir(parents=True)
        body = html(order('100', [], order_date=day + ' 12:00:00', so_id='202600001', shop_id='10'))
        sha = hashlib.sha256(body).hexdigest()
        (folder / (sha + '.raw')).write_bytes(body)
        receipt = json.dumps({'nodes': [{'order_id': '100', 'sha256': sha}]}, separators=(',', ':')).encode()
        (folder / 'receipt.json').write_bytes(receipt)
        with feed._connect() as db:
            assert {r[0] for r in db.execute("SELECT order_store_id FROM feed_store WHERE ledger_store_id='s1' AND mapping_status='confirmed'")} == {'10'}
        return dates.publish_from_task(ws, model, case, {'platform_order_id': '202600001', 'shop_ids': ['10']}, tid, jid, hashlib.sha256(receipt).hexdigest(), {'id': 'fixture-source', 'kind': 'service', 'authenticated': True, 'scopes': ['business']})
    publish('2026-07-01', 'b')
    old = service.recompute(ws, model, model.store('s1'), publication_scope=historical)
    assert not old.failure, old.failure
    actions.deliver(ws)
    from ledger.commission_manager import Manager
    Manager(lambda: ws, lambda: model, publication_scope=historical).once()
    july = ws.state('s1', '2026-07')
    facts7 = pl.read_parquet(ws.facts_path(july.run_id))
    refund7 = facts7.filter((pl.col('metric_id') == 'trade_refund') & pl.col('counted'))
    if payout:
        from ledger import commission_confirm
        context=commission_confirm.context(ws,registry,model,'s1','2026-07',expected_run=july.run_id)
        commission_confirm.confirm(ws,registry,model,store_id='s1',period='2026-07',run_id=july.run_id,
            source_sha=context['source_sha'],expected_confirmation_id=context['latest']['id'] if context['latest'] else '',
            payouts=[{'person_id':v['person_id'],'amount':'1.00'} for v in context['people']],no_payout=not context['people'],
            reason='Declared synthetic already confirmed payout',actor=actor['id'])
    decision = actions.close_preview(ws, model, request_id='legitimate-historical-july-close', period='2026-07', store_ids=['s1'], actor=actor)
    assert decision.get('eligible'), 'historical fixture must close through the unchanged real gate'
    closed = actions.close_confirm(ws, model, request_id='legitimate-historical-july-confirm', proposal_id=decision['proposal_id'], proposal_sha=decision['proposal_sha'], input_sha=decision['input_sha'], actor=actor)
    old_run = ws.state('s1', '2026-07').run_id
    frozen_before = hashlib.sha256(ws.conn.execute('SELECT result FROM run WHERE id=?', (old_run,)).fetchone()[0].encode()).hexdigest()
    scope9 = publication.PublicationScope.for_isolated(ws.root, [('s1', '2026-09')], approval_reference='explicit-synthetic-new-september-only')
    publication.install_isolated(scope9, replace=True)
    if legacy:
        monkeypatch.setattr(Context,'annotate',original_annotate)
        monkeypatch.setattr(guard_module,'event_index',original_index)
    def replace_refunds(rows):
        file=write_xlsx(root/'new-refund-export.xlsx',[
            ['退款流水号','退款原单','退款金额','发生时间','退款SKU','退款子单'],*rows])
        ws.keep(bank.name,file,'s1')
        return file
    def compute():
        result=service.recompute(ws,model,model.store('s1'),publication_scope=scope9)
        assert not result.failure,result.failure
        return pl.read_parquet(ws.facts_path(ws.state('s1','2026-09').run_id))
    return SimpleNamespace(ws=ws,model=model,actor=actor,old_run=old_run,old_hash=frozen_before,
        publish=publish,compute=compute,replace=replace_refunds,scope=scope9,bank_sha=bank_ref.sha,registry=registry)


@pytest.fixture
def calendar_case(tmp_path,monkeypatch):
    case=_case(tmp_path,monkeypatch)
    yield case
    case.ws.close()


def _refunds(frame):
    return frame.filter(pl.col('metric_id')=='trade_refund')


def _frozen_unchanged(c):
    assert c.ws.state('s1','2026-07').closed
    assert c.ws.state('s1','2026-07').run_id==c.old_run
    assert hashlib.sha256(c.ws.conn.execute('SELECT result FROM run WHERE id=?',(c.old_run,)).fetchone()[0].encode()).hexdigest()==c.old_hash


def test_same_original_event_is_not_reposted_and_both_periods_close(calendar_case):
    c=calendar_case;c.publish('2026-09-01','e');frame=_refunds(c.compute())
    assert frame.height==1 and frame['amount'][0]==-20
    assert not frame['counted'][0] and frame['contribution'][0]==0
    evidence=json.loads(frame['statement_evidence'][0])['frozen_refund']
    assert evidence['status']=='excluded' and evidence['owner']['period']=='2026-07'
    preview=actions.close_preview(c.ws,c.model,request_id='guarded-9-preview',period='2026-09',store_ids=['s1'],actor=c.actor)
    assert preview['eligible'] and not preview['blocked']
    actions.close_confirm(c.ws,c.model,request_id='guarded-9-confirm',proposal_id=preview['proposal_id'],proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=c.actor)
    assert c.ws.state('s1','2026-09').closed
    _frozen_unchanged(c)


def test_same_order_and_same_amount_but_different_refund_is_kept(calendar_case):
    c=calendar_case
    c.replace([['refund-event-one','202600001',-20,datetime(2026,9,8),'',''],
               ['refund-event-two','202600001',-20,datetime(2026,9,8),'','']])
    c.publish('2026-09-01','e');rows=_refunds(c.compute())
    assert rows.height==2
    assert rows.filter(pl.col('counted'))['contribution'].sum()==-20
    assert sorted(rows[STATUS])==['excluded','new']
    _frozen_unchanged(c)


def test_new_event_sku_parts_preserve_original_sum(calendar_case):
    c=calendar_case
    c.replace([['refund-event-one','202600001',-20,datetime(2026,9,8),'',''],
               ['refund-event-two','202600001',-7,datetime(2026,9,8),'A','C1'],
               ['refund-event-two','202600001',-13,datetime(2026,9,8),'B','C2']])
    c.publish('2026-09-01','e');rows=_refunds(c.compute())
    assert rows.filter(pl.col('counted')).height==2
    assert rows.filter(pl.col('counted'))['contribution'].sum()==-20
    assert rows.filter(pl.col(STATUS)=='excluded').height==1
    _frozen_unchanged(c)


def test_same_native_id_with_changed_amount_is_unknown_not_zero(calendar_case):
    c=calendar_case
    c.replace([['refund-event-one','202600001',-21,datetime(2026,9,8),'','']])
    c.publish('2026-09-01','e');rows=_refunds(c.compute())
    assert rows['amount'][0]==-21 and not rows['counted'][0]
    assert rows[STATUS][0]=='pending'
    state=c.ws.state('s1','2026-09');net=next(r for r in state.result['statement'] if r['id']=='net_profit')
    assert net['value'] is None and net['available'] is False
    checked=checker.check(c.ws,'s1','2026-09',model=c.model,verify_artifacts=True)
    assert not checked['allowed']
    assert 'frozen_refund_finance_conflict' in {r['code'] for r in checked['issues']}
    _frozen_unchanged(c)


def test_missing_frozen_archive_does_not_mean_no_history(calendar_case):
    c=calendar_case;path=c.ws.facts_path(c.old_run);lost=path.with_suffix('.unavailable')
    path.rename(lost)
    try:
        c.publish('2026-09-01','e');rows=_refunds(c.compute())
        assert rows[STATUS][0]=='pending' and not rows['counted'][0]
        assert not checker.check(c.ws,'s1','2026-09',model=c.model)['allowed']
    finally:lost.rename(path)
    _frozen_unchanged(c)


def test_legacy_archive_without_new_identity_metadata_is_recovered(tmp_path,monkeypatch):
    c=_case(tmp_path,monkeypatch,legacy=True)
    try:
        old=pl.read_parquet(c.ws.facts_path(c.old_run));old_refund=_refunds(old)
        assert not any(json.loads(v or '{}').get('frozen_refund') for v in old_refund['statement_evidence'])
        c.replace([['refund-event-one','202600001',-20,datetime(2026,9,8),'','']])
        c.publish('2026-09-01','e');rows=_refunds(c.compute())
        assert rows[STATUS][0]=='excluded' and rows['contribution'][0]==0
        _frozen_unchanged(c)
    finally:c.ws.close()


def test_multiple_historical_owners_are_not_resolved_by_first_or_amount(calendar_case):
    c=calendar_case;context=Context(c.ws,c.model,c.model.store('s1'));context.history()
    records=next(iter(context.exact.values()));second=deepcopy(records[0]);second['owner']={'period':'2026-08','run_id':999999}
    status,reason,owner=context.assess([records[0],second],[{**records[0]['row'],'period':'2026-09'}])
    assert status=='pending' and reason=='multiple_frozen_owners' and owner is None


def test_strong_native_seven_date_still_wins_over_new_fallback(tmp_path,monkeypatch):
    c=_case(tmp_path,monkeypatch,native_seven=True)
    try:
        c.publish('2026-09-01','e');rows=_refunds(c.compute())
        assert rows.filter(pl.col('counted')).is_empty()
        _frozen_unchanged(c)
    finally:c.ws.close()


def test_same_native_id_but_changed_original_order_is_hard_conflict(calendar_case):
    c=calendar_case
    c.replace([['refund-event-one','3319999999999999999',-20,datetime(2026,9,8),'','']])
    rows=_refunds(c.compute())
    assert rows[STATUS][0]=='pending' and not rows['counted'][0]
    assert 'frozen_original_order_identity_conflict' in rows['source_note'][0]
    assert not checker.check(c.ws,'s1','2026-09',model=c.model)['allowed']
    _frozen_unchanged(c)


def test_missing_current_event_identity_is_not_assumed_new(calendar_case):
    c=calendar_case
    c.replace([['','202600001',-20,datetime(2026,9,8),'','']])
    c.publish('2026-09-01','e');rows=_refunds(c.compute())
    assert rows[STATUS][0]=='pending' and not rows['counted'][0]
    _frozen_unchanged(c)


def test_unrelated_metrics_and_store_namespaces_are_not_suppressed(calendar_case):
    from dataclasses import replace
    from ledger.model.schema import Store
    from ledger.engine.runtime import Ingestion,ingest
    c=calendar_case;c.publish('2026-09-01','e');frame=_refunds(c.compute())
    other=Store(id='other',name='独立账户',platform='taobao')
    model=c.model.model_copy(update={'stores':(*c.model.stores,other)})
    raw=next(i for i in ingest(c.ws.active_files('s1'),model,[c.model.store('s1').name],default_store=c.model.store('s1').name).known if i.template.id=='declared_refunds')
    other_frame=frame.with_columns(pl.lit(other.name).alias('store'))
    result=Context(c.ws,model,other).annotate(SimpleNamespace(known=[raw]),other_frame,model.metrics)
    assert result[STATUS][0]=='new'
    unrelated=frame.with_columns(pl.lit('annual_fee').alias('metric_id'),pl.lit('independent_source').alias('source_id'))
    result=Context(c.ws,c.model,c.model.store('s1')).annotate(SimpleNamespace(known=[raw]),unrelated,c.model.metrics)
    assert result['metric_id'][0]=='annual_fee' and result['amount'][0]==unrelated['amount'][0]
    # This is an admission-level namespace unit, not a claimed other-store run.
    _frozen_unchanged(c)


def test_confirmed_payout_stays_unchanged_when_new_period_excludes_duplicate(tmp_path,monkeypatch):
    c=_case(tmp_path,monkeypatch,payout=True)
    try:
        def rows():
            with c.registry.connect() as db:return [dict(v) for v in db.execute('SELECT * FROM payout_confirmation ORDER BY id')]
        original=rows();assert original
        c.publish('2026-09-01','e');c.compute()
        preview=actions.close_preview(c.ws,c.model,request_id='payout-protected-9-preview',period='2026-09',store_ids=['s1'],actor=c.actor)
        assert preview['eligible']
        actions.close_confirm(c.ws,c.model,request_id='payout-protected-9-confirm',proposal_id=preview['proposal_id'],proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=c.actor)
        assert rows()==original
        _frozen_unchanged(c)
    finally:c.ws.close()


def test_missing_archive_does_not_block_unrelated_new_native_event(calendar_case):
    c=calendar_case
    c.replace([['new-unique-refund','202600001',-20,datetime(2026,9,8),'','']])
    original=c.ws.facts_path(c.old_run);lost=original.with_suffix('.lost')
    original.rename(lost)
    try:
        c.publish('2026-09-01','e');rows=_refunds(c.compute())
        assert rows[STATUS][0]=='new' and rows['counted'][0]
        assert rows['contribution'][0]==-20
        assert checker.check(c.ws,'s1','2026-09',model=c.model,verify_artifacts=True)['allowed']
    finally:lost.rename(original)
    _frozen_unchanged(c)


def test_legacy_missing_archive_can_use_complete_original_input_identities(tmp_path,monkeypatch):
    c=_case(tmp_path,monkeypatch,legacy=True)
    try:
        c.replace([['new-unique-refund','202600001',-20,datetime(2026,9,8),'','']])
        original=c.ws.facts_path(c.old_run);lost=original.with_suffix('.lost');original.rename(lost)
        try:
            c.publish('2026-09-01','e');rows=_refunds(c.compute())
            assert rows[STATUS][0]=='new' and rows['counted'][0]
        finally:lost.rename(original)
        _frozen_unchanged(c)
    finally:c.ws.close()


def test_reexported_old_whole_event_as_source_sku_parts_is_not_rebooked(calendar_case):
    c=calendar_case
    c.replace([['refund-event-one','202600001',-7,datetime(2026,9,8),'A','C1'],
               ['refund-event-one','202600001',-13,datetime(2026,9,8),'B','C2']])
    c.publish('2026-09-01','e');rows=_refunds(c.compute())
    assert rows.height==2 and not rows['counted'].any()
    assert rows['amount'].sum()==-20 and rows['contribution'].sum()==0
    assert set(rows[STATUS])=={'excluded'}
    _frozen_unchanged(c)


def test_independent_financial_namespace_can_reuse_provider_transaction_id(calendar_case,tmp_path):
    c=calendar_case;c.publish('2026-09-01','e')
    original=c.model.template('declared_refunds')
    alt=original.model_copy(update={'id':'independent_refunds','event_namespace':'independent_provider',
        'match_columns':('其他渠道流水','退款原单','退款金额','发生时间'),
        'bindings':tuple(b.model_copy(update={'columns':('其他渠道流水',)}) if b.role=='txn_id' else b for b in original.bindings)})
    model=c.model.model_copy(update={'templates':(*c.model.templates,alt)})
    file=write_xlsx(tmp_path/'independent-provider.xlsx',[
        ['其他渠道流水','退款原单','退款金额','发生时间'],['refund-event-one','202600001',-20,datetime(2026,9,8)]])
    c.ws.keep(file.name,file,'s1')
    result=service.recompute(c.ws,model,model.store('s1'),publication_scope=c.scope)
    assert not result.failure
    rows=_refunds(pl.read_parquet(c.ws.facts_path(c.ws.state('s1','2026-09').run_id)))
    assert rows.filter(pl.col('template_id')=='independent_refunds')['contribution'].sum()==-20
    assert rows.filter(pl.col('template_id')=='declared_refunds')['contribution'].sum()==0
    _frozen_unchanged(c)


def test_resealing_changed_frozen_facts_does_not_establish_a_new_expected_hash(calendar_case):
    from ledger.storage_integrity import seal
    c=calendar_case;path=c.ws.facts_path(c.old_run);side=path.with_name(path.name+'.sha256')
    original,checksum=path.read_bytes(),side.read_bytes()
    try:
        frame=pl.read_parquet(path).with_columns(pl.when(pl.col('metric_id')=='trade_refund').then(-99.).otherwise(pl.col('contribution')).alias('contribution'))
        frame.write_parquet(path);seal(path)
        c.publish('2026-09-01','e');rows=_refunds(c.compute())
        assert rows[STATUS][0]=='pending' and not rows['counted'][0]
        assert not checker.check(c.ws,'s1','2026-09',model=c.model)['allowed']
    finally:path.write_bytes(original);side.write_bytes(checksum)
    _frozen_unchanged(c)


@pytest.mark.parametrize('kind',['other_metric','other_store'])
def test_no_current_store_refund_does_not_load_closed_archives(tmp_path,monkeypatch,kind):
    ws=Workspace(tmp_path/'no-refund-workspace')
    try:
        context=Context(ws,SimpleNamespace(),SimpleNamespace(id='s1',name='Our store'))
        def forbidden(*args):
            raise AssertionError('No current-store refund must load a closed archive')
        monkeypatch.setattr(context,'history',forbidden)
        facts=pl.DataFrame({'metric_id':['trade_receipt' if kind=='other_metric' else 'trade_refund'],
                            'store':['Other store' if kind=='other_store' else 'Our store']})
        metric=SimpleNamespace(id='trade_refund',posting_basis='order_number')
        assert context.annotate(SimpleNamespace(known=()),facts,[metric]) is facts
        assert context.loaded is False
    finally:ws.close()


@pytest.mark.parametrize('refund_count',[64,128])
def test_current_native_identity_builds_one_linear_original_index(tmp_path,monkeypatch,refund_count):
    ws=Workspace(tmp_path/'indexed-refunds')
    try:
        context=Context(ws,SimpleNamespace(),SimpleNamespace(id='s1',name='Our store'))
        original_count=2*refund_count
        frame=pl.DataFrame({'__sheet__':['Sheet1']*original_count,'__row__':list(range(2,original_count+2)),
            'txn_id':['event-'+str(i) for i in range(original_count)],'sku':['SKU-'+str(i) for i in range(original_count)],
            'income':['0']*original_count,'outgo':['1']*refund_count+['not-an-amount']*refund_count})
        template=SimpleNamespace(id='bank',event_namespace='bank-refunds',event_id_role='txn_id',event_directional=True)
        ingestion=SimpleNamespace(known=[SimpleNamespace(ref=SimpleNamespace(sha256='a'*64),template=template,frame=frame)])
        counts={'filters':0,'index_enumerations':0,'original_rows':0}
        original_iter=pl.DataFrame.iter_rows
        def forbidden_filter(self,*args,**kwargs):
            counts['filters']+=1
            raise AssertionError('Native lookup must not scan/filter a whole original for each refund')
        def counted_iter(self,*args,**kwargs):
            counts['index_enumerations']+=1
            for value in original_iter(self,*args,**kwargs):
                counts['original_rows']+=1
                yield value
        monkeypatch.setattr(pl.DataFrame,'filter',forbidden_filter)
        monkeypatch.setattr(pl.DataFrame,'iter_rows',counted_iter)
        for repeat in range(2):
            for i in range(refund_count):
                row={'file_sha':'a'*64,'template_id':'bank','sheet':'Sheet1','row_no':i+2}
                assert context.current_identity(row,ingestion)=={
                    'namespace':'bank-refunds','leg':'outgo','event_id':'event-'+str(i),'component':'SKU-'+str(i)}
        # Doubling the original doubles enumerated cells; repeated lookups do
        # no new scans. Malformed unrequested money was not interpreted.
        assert counts=={'filters':0,'index_enumerations':1,'original_rows':original_count}
        assert len(context.current_indexes)==1
    finally:ws.close()


def test_index_preserves_duplicate_anchor_missing_identity_and_last_item_rules(tmp_path):
    ws=Workspace(tmp_path/'index-identity-rules')
    try:
        context=Context(ws,SimpleNamespace(),SimpleNamespace(id='s1',name='Our store'))
        template=SimpleNamespace(id='bank',event_namespace='bank-refunds',event_id_role='txn_id',event_directional=True)
        # Two physical rows with one anchor never authorize either native ID.
        frame=pl.DataFrame({'__sheet__':['Sheet1']*4,'__row__':[2,2,3,4],
            'txn_id':['first','second','unique','both'],'sku':['A','B','C','D'],
            'income':[0,0,0,2],'outgo':[1,1,0,3]})
        def item(data,t=template):return SimpleNamespace(ref=SimpleNamespace(sha256='a'*64),template=t,frame=data)
        ingestion=SimpleNamespace(known=[item(frame)])
        def identity(number):return context.current_identity({'file_sha':'a'*64,'template_id':'bank','sheet':'Sheet1','row_no':number},ingestion)
        assert identity(2)=={'namespace':'bank-refunds','leg':'event'}
        assert identity(3)=={'namespace':'bank-refunds','leg':'zero','event_id':'unique','component':'C'}
        assert identity(4)=={'namespace':'bank-refunds','leg':'both','event_id':'both','component':'D'}
        assert identity(5)=={'namespace':'bank-refunds','leg':'event'}
        # Keep the prior last-matching-item rule, even if that item's source
        # does not have the approved transaction-ID column.
        later=Context(ws,SimpleNamespace(),SimpleNamespace(id='s1',name='Our store'))
        missing=SimpleNamespace(known=[item(frame),item(frame.drop('txn_id'))])
        assert later.current_identity({'file_sha':'a'*64,'template_id':'bank','sheet':'Sheet1','row_no':3},missing)=={'namespace':'bank-refunds','leg':'event'}
        # Same bytes with another template are a separate namespace/index.
        other=SimpleNamespace(id='other-bank',event_namespace='other-refunds',event_id_role='txn_id',event_directional=False)
        separate=Context(ws,SimpleNamespace(),SimpleNamespace(id='s1',name='Our store'))
        both=SimpleNamespace(known=[item(frame),item(frame,other)])
        assert separate.current_identity({'file_sha':'a'*64,'template_id':'bank','sheet':'Sheet1','row_no':3},both)['leg']=='zero'
        assert separate.current_identity({'file_sha':'a'*64,'template_id':'other-bank','sheet':'Sheet1','row_no':3},both)=={'namespace':'other-refunds','leg':'event','event_id':'unique','component':'C'}
        assert len(separate.current_indexes)==2
    finally:ws.close()



def test_actual_return_derivation_preserves_frozen_refund_context(tmp_path,monkeypatch):
    from test_after_sales_cost import AFTER_HEADER
    c=_case(tmp_path,monkeypatch,payout=True)
    try:
        def payout_rows():
            with c.registry.connect() as db:return [tuple(r) for r in db.execute('SELECT * FROM payout_confirmation ORDER BY id')]
        confirmed=payout_rows();assert confirmed
        c.publish('2026-09-01','e')
        full=load_model(MODELS/'cn-ecommerce')
        after_templates=tuple(t for t in full.templates if t.source=='after_sales')
        model=c.model.model_copy(update={
            'stores':tuple(s.model_copy(update={'cost_return_posting':'transaction'}) for s in c.model.stores),
            'sources':(*c.model.sources,full.source('cost_return')),
            'templates':(*c.model.templates,*after_templates),
            'metrics':(*c.model.metrics,full.metric('goods_return_cost')),
            'statement':tuple(n.model_copy(update={'formula':n.formula.model_copy(update={
                'of':('gross_profit','trade_refund','goods_return_cost')})}) if n.id=='net_profit' else n for n in c.model.statement)})
        original_store=model.store('s1')
        after=write_xlsx(tmp_path/'declared-actual-return.xlsx',[
            AFTER_HEADER+['实退数量','进仓时间'],
            ['declared-actual-return','9000',original_store.name,'3319999999999999999',
             '已确认','退款成功','卖家已收到退货','SKU9000','3319999999999999000',1,1,'2026-09-09']])
        c.ws.keep(after.name,after,'s1')
        checker.bind_model(c.ws,model)
        actual_run=service.run;observed=[]
        def guarded_run(ingestion,*args,**kwargs):
            context=ingestion.frozen_refund_context
            result=actual_run(ingestion,*args,**kwargs)
            observed.append((ingestion,result))
            # A real actual-receipt row triggers dataclasses.replace. The
            # same read-only Workspace-bound coordinator must survive it.
            assert result.ingestion is not ingestion
            assert result.ingestion.frozen_refund_context is context
            return result
        monkeypatch.setattr(service,'run',guarded_run)
        result=service.recompute(c.ws,model,original_store,publication_scope=c.scope)
        assert not result.failure,result.failure
        assert observed
        rows=pl.read_parquet(c.ws.facts_path(c.ws.state('s1','2026-09').run_id))
        refund=_refunds(rows)
        assert refund.height==1 and refund['amount'][0]==-20
        assert not refund['counted'][0] and refund['contribution'][0]==0
        assert json.loads(refund['statement_evidence'][0])['frozen_refund']['owner']['period']=='2026-07'
        returns=rows.filter(pl.col('metric_id')=='goods_return_cost')
        assert returns.height==1 and returns['counted'][0]
        # Original captured unit price 2 * actual received quantity 1; the
        # receipt is September, while the old frozen refund remains July.
        assert returns['amount'][0]==2 and returns['contribution'][0]==2
        assert returns['period'][0]=='2026-09'
        assert 'declared-actual-return' in returns['source_note'][0]
        preview=actions.close_preview(c.ws,model,request_id='return-and-refund-9-preview',period='2026-09',store_ids=['s1'],actor=c.actor)
        assert preview['eligible'] and not preview['blocked']
        actions.close_confirm(c.ws,model,request_id='return-and-refund-9-confirm',proposal_id=preview['proposal_id'],
            proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=c.actor)
        assert c.ws.state('s1','2026-09').closed
        _frozen_unchanged(c)
        assert payout_rows()==confirmed
    finally:c.ws.close()
