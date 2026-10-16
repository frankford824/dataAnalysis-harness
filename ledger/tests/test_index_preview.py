import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.error import URLError

import polars as pl
import pytest

from ledger import index_client, index_preview


SHA='a'*64


def fixture_catalog(root):
    root.mkdir(exist_ok=True)
    (root/'parquet').mkdir()
    parquet=root/'parquet'/f'{SHA}.parquet'
    pl.DataFrame({'file_sha':[SHA]*4,'sheet':['甲','甲','甲','乙'],'row_no':[9,1,3,1],
        'cells_json':[json.dumps([text]) for text in ('long-id-1234567890123456789','表头','0','乙表头')]}).write_parquet(parquet)
    catalog=root/'catalog.db'
    with sqlite3.connect(catalog) as db:
        db.execute('CREATE TABLE file_catalog(sha256,path,platform,store_id,source,authority,state,rows,parquet_path)')
        db.execute('INSERT INTO file_catalog VALUES(?,?,?,?,?,?,?,?,?)',
            (SHA,'original.xlsx','淘宝天猫','shop','运费','calculation','ready',4,str(parquet)))
    return catalog


def test_preview_keeps_source_row_numbers_cells_and_sheet_while_paging(tmp_path):
    catalog=fixture_catalog(tmp_path)
    before=hashlib.sha256(catalog.read_bytes()).hexdigest()
    data=index_preview.local(catalog,SHA,sheet='甲',offset=1,limit=2)
    assert data['rows']==[{'sheet':'甲','row_no':3,'cells':['0']},
        {'sheet':'甲','row_no':9,'cells':['long-id-1234567890123456789']}]
    assert data['metadata']['path']=='original.xlsx' and data['metadata']['total_rows']==4
    assert data['metadata']['preview_rows']==3
    assert data['backend']=='local_parquet'
    assert index_preview.local(catalog,SHA,sheet='乙')['rows']==[{'sheet':'乙','row_no':1,'cells':['乙表头']}]
    assert hashlib.sha256(catalog.read_bytes()).hexdigest()==before


def test_end_of_file_is_a_valid_empty_page_not_an_unavailable_source(tmp_path):
    catalog=fixture_catalog(tmp_path)
    assert index_preview.local(catalog,SHA,sheet='甲',offset=100)['rows']==[]


def test_missing_catalog_is_not_created_and_unknown_files_use_existing_service(tmp_path):
    missing=tmp_path/'missing.db'
    assert index_preview.local(missing,SHA) is None and not missing.exists()
    catalog=fixture_catalog(tmp_path)
    assert index_preview.local(catalog,'b'*64) is None
    (tmp_path/'parquet'/f'{SHA}.parquet').unlink()
    assert index_preview.local(catalog,SHA) is None


def test_catalog_cannot_redirect_preview_to_an_unrelated_file(tmp_path,monkeypatch):
    catalog=fixture_catalog(tmp_path)
    with sqlite3.connect(catalog) as db:db.execute('UPDATE file_catalog SET parquet_path=?',(str(tmp_path/'other.parquet'),))
    monkeypatch.setattr(pl,'scan_parquet',lambda *_a,**_k:pytest.fail('Unrelated source must not be read'))
    assert index_preview.local(catalog,SHA) is None
    with pytest.raises(ValueError):index_preview.local(catalog,'../unrelated')


@pytest.mark.parametrize('exc',[TimeoutError('timed out'),URLError(TimeoutError('timed out'))])
def test_timeout_does_not_incorrectly_claim_the_whole_service_is_down(monkeypatch,exc):
    def fail(*_a,**_k):raise exc
    monkeypatch.setattr(index_client,'urlopen',fail)
    with pytest.raises(index_client.IndexerUnavailable,match='原文件预览超时'):
        index_client.get('/preview')
    with pytest.raises(index_client.IndexerUnavailable,match='原文件检索超时'):
        index_client.search('3315496369961031477',limit=30)


def test_api_uses_local_evidence_when_search_service_is_unavailable(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    import ledger.api as api
    catalog=fixture_catalog(tmp_path)
    monkeypatch.setattr(api,'WORKSPACE_ROOT',tmp_path/'space')
    monkeypatch.setattr(api,'_ws',None)
    monkeypatch.setenv('LEDGER_INDEX_CATALOG',str(catalog))
    monkeypatch.setattr(index_client,'get',lambda *_a,**_k:pytest.fail('Local preview must not wait on the index service'))
    with TestClient(api.app) as client:
        response=client.get('/api/index/preview',params={'sha':SHA,'sheet':'甲','offset':1,'limit':2})
    assert response.status_code==200 and response.json()['rows'][0]['row_no']==3
