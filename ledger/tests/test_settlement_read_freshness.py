import json
import sqlite3

import pytest

from ledger import commission_sales, store_display
from ledger.commission_registry import Registry, RevisionConflict
from ledger.model.schema import Store
from ledger.workspace_read_version import clock
from test_commission import _model
from test_commission_profit import _persist
from test_commission_reports import fixture, record
from test_commission_sales import frame
from test_api import client


def feed(root, rows):
    with sqlite3.connect(root / 'order-feed.db') as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS feed_store(order_store_id TEXT,ledger_store_id TEXT,mapping_status TEXT,payload_json TEXT)')
        conn.execute('DELETE FROM feed_store')
        conn.executemany('INSERT INTO feed_store VALUES(?,?,?,?)',
                         [(str(i),sid,state,json.dumps({'shop_name':name})) for i,(sid,state,name) in enumerate(rows)])


def test_display_names_confirmed_unambiguous_and_not_financial_model(tmp_path):
    model = _model(stores=(Store(id='s1',name='账务旧名',platform='taobao',aliases=('历史别名',)),))
    feed(tmp_path,[('s1','confirmed','订单台新名'),('s2','unconfirmed','未确认')])
    rev,_ = store_display.snapshot(tmp_path)
    assert store_display.names(tmp_path,model)['s1'] == '订单台新名'
    shown = store_display.store_dict(tmp_path,model.store('s1'))
    assert shown['accounting_name'] == '账务旧名' and shown['aliases'] == ['账务旧名','历史别名']
    assert model.store('s1').name == '账务旧名'
    feed(tmp_path,[('s1','confirmed','再次改名')])
    assert store_display.snapshot(tmp_path)[0] != rev
    assert store_display.names(tmp_path,model)['s1'] == '再次改名'
    feed(tmp_path,[('s1','confirmed','甲'),('s1','confirmed','乙')])
    assert store_display.names(tmp_path,model)['s1'] == '账务旧名'


def test_report_rename_invalidates_cache_without_financial_write(tmp_path):
    ws,reg,people,client = fixture(tmp_path)
    record(ws,people,'s1','2026-06',[10])
    selection={'start':'2026-06','end':'2026-06'}
    before=client.post('/api/commission-v2/reports/query',json=selection).json()
    generation=ws.generation()
    feed(tmp_path,[('s1','confirmed','订单新名')])
    after=client.post('/api/commission-v2/reports/query',json=selection).json()
    assert after['rows'][0]['store']=='订单新名'
    assert after['rows'][0]['amount']==before['rows'][0]['amount']
    assert ws.generation()==generation and reg.revision()==3


def test_navigation_and_detail_etags_follow_metadata_only_rename(client):
    import ledger.api as api
    first=client.get('/api/navigation')
    sid=first.json()['stores'][0]['id']
    canonical=api._model().store(sid).name
    detail=client.get('/api/stores/'+sid)
    feed(api.workspace().root,[(sid,'confirmed','已同步新店名')])
    second=client.get('/api/navigation',headers={'If-None-Match':first.headers['etag']})
    assert second.status_code==200
    assert second.json()['data_revision']!=first.json()['data_revision']
    assert next(s for s in second.json()['stores'] if s['id']==sid)['name']=='已同步新店名'
    changed=client.get('/api/stores/'+sid,headers={'If-None-Match':detail.headers['etag']})
    assert changed.status_code==200 and changed.json()['store']['name']=='已同步新店名'
    # Configuration editors retain the accounting name; it is not silently
    # replaced by a presentation label when saving an unrelated setting.
    assert next(s for s in client.get('/api/stores').json()['stores'] if s['id']==sid)['name']==canonical
    assert api._model().store(sid).name==canonical


def test_closed_background_run_does_not_invalidate_visible_report(tmp_path,monkeypatch):
    from ledger import commission_reports
    ws,reg,people,client=fixture(tmp_path)
    record(ws,people,'s1','2026-06',[10])
    ws.close_period('s1','2026-06',by='test',note='test')
    selection={'start':'2026-06','end':'2026-06'}
    before=client.post('/api/commission-v2/reports/query',json=selection).json()
    generation=clock(ws.conn,start='2026-06',end='2026-06',report=True)
    record(ws,people,'s1','2026-06',[999])
    assert clock(ws.conn,start='2026-06',end='2026-06',report=True)==generation
    original=commission_reports.build
    def unexpected(*args,**kwargs): raise AssertionError('invisible run rebuilt report')
    monkeypatch.setattr(commission_reports,'build',unexpected)
    assert client.post('/api/commission-v2/reports/query',json=selection).json()==before
    monkeypatch.setattr(commission_reports,'build',original)
    ws.reopen_period('s1','2026-06',by='test',note='test')
    assert clock(ws.conn,start='2026-06',end='2026-06',report=True)!=generation
    assert client.post('/api/commission-v2/reports/query',json=selection).json()['rows'][0]['amount']==999


def test_store_clock_is_transactional_and_scoped(tmp_path):
    reg=Registry(tmp_path)
    person=reg.person_save({'name':'甲'},'test','test')['id']
    old=commission_sales.rule_revision(reg,'s1')
    reg.save_setting({'store_id':'s2','product_id':'p','valid_from':'2026-06-01',
                      'allocations':[{'person_id':person,'rate':'.02','duty':'produce'}]},'test')
    assert commission_sales.rule_revision(reg,'s1')==old
    saved=commission_sales.rule_revision(reg,'s2')
    reg.save_store_member('s1',person,'cut','','test','confirmed default',valid_from='2026-06-01')
    assert commission_sales.rule_revision(reg,'s1')!=old
    assert commission_sales.rule_revision(reg,'s2')==saved
    with reg.connect() as conn:
        conn.execute('BEGIN')
        conn.execute('UPDATE scheme SET active_version=NULL')
        assert commission_sales._revision(conn,'s2')[0]!=saved
        conn.rollback()
    assert commission_sales.rule_revision(reg,'s2')==saved
    with reg.connect() as conn:
        # Exercise defensive delete invalidation independently of the business
        # foreign-key prohibition on deleting an already published scheme.
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('DELETE FROM scheme')
    assert commission_sales.rule_revision(reg,'s2')!=saved


def test_configured_index_reuses_durable_cache_across_processes_and_audits(tmp_path,monkeypatch):
    from ledger import commission_reports
    reg=Registry(tmp_path)
    pid=reg.person_save({'name':'甲'},'test','test')['id']
    reg.save_setting({'store_id':'s1','product_id':'p','valid_from':'2026-06-01',
                      'allocations':[{'person_id':pid,'rate':'.02','duty':'produce'}]},'test')
    expected=commission_reports.configured_people(reg,'2026-06','2026-06')
    reg.person_save({'name':'乙'},'test','unrelated audit')
    commission_reports._configured_cache.clear()
    def fail(*args,**kwargs):raise AssertionError('unrelated audit or cold process rescanned every scheme')
    monkeypatch.setattr(commission_reports,'_scan_configured_rows',fail)
    assert commission_reports.configured_people(reg,'2026-06','2026-06')==expected


def test_cold_durable_sales_hit_does_not_load_rules(tmp_path,monkeypatch):
    reg=Registry(tmp_path)
    pid=reg.person_save({'name':'甲'},'test','test')['id']
    calculation=_persist(reg,pid,rows=frame(['produce','cut']).to_dict(as_series=False))
    outputs={'member':{'sales':1},'leader':{'sales':2}}
    expected=commission_sales.archived(reg,{'calculation_id':calculation},outputs)
    commission_sales._archive_cache.clear()
    commission_sales._cache.clear()
    def fail(*args,**kwargs):raise AssertionError('durable cache hit decoded rules')
    monkeypatch.setattr(commission_sales,'rules',fail)
    assert commission_sales.archived(reg,{'calculation_id':calculation},outputs)==expected


def test_concurrent_rules_never_persist_under_wrong_key(tmp_path,monkeypatch):
    reg=Registry(tmp_path)
    pid=reg.person_save({'name':'甲'},'test','test')['id']
    calculation=_persist(reg,pid,rows=frame(['produce','cut']).to_dict(as_series=False))
    original=commission_sales.rules
    def race(*args,**kwargs):
        reg.save_setting({'store_id':'s1','product_id':'p','valid_from':'2026-06-01',
                          'allocations':[{'person_id':pid,'rate':'.02','duty':'produce'}]},'test')
        return original(*args,**kwargs)
    monkeypatch.setattr(commission_sales,'rules',race)
    with pytest.raises(RevisionConflict):
        commission_sales.archived(reg,{'calculation_id':calculation},{'member':{'sales':1}})
    with reg.connect() as conn:
        assert conn.execute("SELECT count(*) FROM derived_read_cache WHERE key LIKE 'sales:%'").fetchone()[0]==0


def test_missing_mapping_is_blocked_before_recompute_and_resumes(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from ledger.commission_manager import Manager
    from ledger.workspace import Workspace
    ws=Workspace(tmp_path);reg=Registry(tmp_path)
    feed(tmp_path,[])
    with sqlite3.connect(tmp_path/'order-feed.db') as conn:
        conn.execute('CREATE TABLE feed_state(consumed_seq INTEGER,source_latest_seq INTEGER,snapshot_id TEXT)')
        conn.execute("INSERT INTO feed_state VALUES(10,10,'snapshot')")
    reg.enqueue_source({'s1'},'order-feed:snapshot:10')
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','1')
    calls=[]
    def compute(*args,**kwargs):calls.append(1);return SimpleNamespace(failure=None,periods=[])
    monkeypatch.setattr('ledger.commission_manager.service.recompute',compute)
    manager=Manager(lambda:ws,_model)
    manager.once()
    assert calls==[]
    with reg.connect() as conn:
        row=conn.execute('SELECT * FROM pending').fetchone()
        assert '已确认店铺映射' in row['error'] and row['next_attempt']>0
    feed(tmp_path,[('s1','confirmed','店铺一')])
    with reg.connect() as conn:conn.execute('UPDATE pending SET next_attempt=0')
    manager.once()
    assert calls==[1]
    with reg.connect() as conn:assert conn.execute('SELECT count(*) FROM pending').fetchone()[0]==0
