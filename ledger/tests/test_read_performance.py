from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from ledger.read_cache import cached
from ledger.workspace import Workspace
from ledger.commission_registry import Registry
from ledger.commission_catalog import settings, cached_count


def test_cache_coalesces_same_key_without_blocking_other_keys():
    cache=OrderedDict();started=Event();release=Event();calls=[]
    def slow():
        calls.append(1);started.set();release.wait(3);return 'slow'
    with ThreadPoolExecutor(max_workers=3) as pool:
        first=pool.submit(cached,cache,'slow',slow,4)
        assert started.wait(1)
        second=pool.submit(cached,cache,'slow',slow,4)
        try:
            assert pool.submit(cached,cache,'fast',lambda:'fast',4).result(timeout=1)=='fast'
        finally:release.set()
        assert first.result()==second.result()=='slow'
    assert len(calls)==1


def test_read_clocks_isolate_other_shops_but_track_shared_month_inputs(tmp_path):
    ws=Workspace(tmp_path)
    ws.record('a','2026-06',{'can_close':True},[])
    own=ws.read_generation(store_id='a')
    period=ws.read_generation(store_id='a',period='2026-06')
    report=ws.read_generation(start='2026-06',end='2026-06')
    ws.record('b','2026-07',{},[])
    assert own==ws.read_generation(store_id='a')
    assert period==ws.read_generation(store_id='a',period='2026-06')
    assert report==ws.read_generation(start='2026-06',end='2026-06')
    ws.record('b','2026-06',{},[])
    assert own==ws.read_generation(store_id='a')
    assert period!=ws.read_generation(store_id='a',period='2026-06')
    assert report!=ws.read_generation(start='2026-06',end='2026-06')
    ws.close()


def test_person_index_current_future_and_transaction_rollback(tmp_path):
    reg=Registry(tmp_path)
    a=reg.person_save({'name':'A'},'test','create')
    b=reg.person_save({'name':'B'},'test','create')
    base={'store_id':'s','product_id':'123456789001','valid_from':'2026-01-01',
          'valid_to':'2026-06-01','allocations':[{'person_id':a['id'],'rate':'.03','duty':'produce'}]}
    reg.save_setting(base,'test')
    reg.save_setting({**base,'valid_from':'2026-07-01','valid_to':'','expected_revision':1,
                      'allocations':[{'person_id':b['id'],'rate':'.02','duty':'cut'}]},'test')
    assert cached_count(reg,person_id=a['id'],at='2026-03-01T00:00:00')==1
    assert cached_count(reg,person_id=b['id'],at='2026-03-01T00:00:00')==0
    assert cached_count(reg,person_id=b['id'],at='2026-06-15T00:00:00')==1
    assert settings(reg,person_id=b['id'],at='2026-06-15T00:00:00')['rows'][0]['state']=='scheduled'
    with reg.connect() as conn:
        conn.execute('BEGIN')
        conn.execute('UPDATE scheme SET active_version=NULL')
        assert conn.execute('SELECT count(*) FROM scheme_read_person').fetchone()[0]==0
        conn.rollback()
        assert conn.execute('SELECT count(*) FROM scheme_read_person').fetchone()[0]==2


def test_catalog_cache_tracks_catalog_writes_without_business_audit(tmp_path):
    reg=Registry(tmp_path)
    assert settings(reg)['rows']==[]
    with reg.connect() as conn:
        conn.execute("INSERT INTO catalog VALUES('s','123456789001','Name','','{}','2026-01-01')")
    assert len(settings(reg)['rows'])==1
