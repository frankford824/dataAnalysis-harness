import hashlib
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledger.commission_registry import Registry
from ledger.workspace import Workspace
from ledger import write_auth


@pytest.fixture
def surface(tmp_path, monkeypatch):
    monkeypatch.setenv('LEDGER_COMMISSION_AUTH_MODE', 'open')
    ws = Workspace(tmp_path / 'workspace')
    registry = Registry(ws.root)
    registry.operator_add('finance', 'synthetic-password')
    token = registry.login('finance', 'synthetic-password')
    identities = tmp_path / 'identities.json'
    identities.write_text(json.dumps({'identities': [{'id': 'worker', 'name': 'service',
        'enabled': True, 'expires_at': time.time() + 60, 'capabilities': ['intake'],
        'token_sha256': hashlib.sha256(b'synthetic-service-token').hexdigest()}]}))
    monkeypatch.setenv('LEDGER_SERVICE_IDENTITIES', str(identities))
    app = FastAPI()
    write_auth.install(app, lambda: ws)

    @app.get('/api/read')
    def read(): return {'ok': True}

    @app.post('/api/upload')
    def upload(): return {'by': write_auth.current_actor_name()}

    @app.post('/api/stores/s/periods/p/close')
    def close(): return {'by': write_auth.current_actor_name()}

    yield TestClient(app), token, identities
    ws.close()


def test_intranet_open_mode_does_not_authorize_writes(surface):
    client, _, _ = surface
    assert client.get('/api/read').status_code == 200
    assert client.post('/api/upload').status_code == 401
    assert client.post('/api/upload', headers={'x-commission-actor': 'pretend-human'}).status_code == 401


def test_identified_human_and_same_origin(surface):
    client, token, _ = surface
    client.cookies.set('commission_session', token)
    assert client.post('/api/stores/s/periods/p/close').json() == {'by': 'finance'}
    assert client.post('/api/upload', headers={'origin': 'https://other.example'}).status_code == 403
    # Reset request context; no actor leaks into a later anonymous request.
    client.cookies.clear()
    assert client.post('/api/upload').status_code == 401


def test_service_is_scoped_and_cannot_confirm_financial_state(surface):
    client, _, path = surface
    header = {'authorization': 'Bearer synthetic-service-token'}
    assert client.post('/api/upload', headers=header).json() == {'by': 'service'}
    assert client.post('/api/stores/s/periods/p/close', headers=header).status_code == 403
    entries = json.loads(path.read_text())
    entries['identities'][0]['expires_at'] = time.time() - 1
    path.write_text(json.dumps(entries))
    assert client.post('/api/upload', headers=header).status_code == 401


def test_cutover_blocks_writes_until_receipt_readback_finishes(surface,tmp_path,monkeypatch):
    client,token,_=surface
    marker=tmp_path/'release.json'
    monkeypatch.setenv('LEDGER_RELEASE_MARKER',str(marker))
    client.cookies.set('commission_session',token)
    marker.write_text('{}')
    assert client.post('/api/upload').status_code==503
    assert client.get('/api/read').status_code==200
    marker.unlink()
    assert client.post('/api/upload').status_code==200


def test_actual_source_drift_blocks_human_and_service_writes_but_preserves_reading(surface,tmp_path,monkeypatch):
    from ledger import version
    from test_code_integrity import make_release
    monitor,source,target,value=make_release(tmp_path/'application')
    monkeypatch.setattr(version,'_CODE_MONITOR',monitor)
    client,token,_=surface;client.cookies.set('commission_session',token)
    assert client.post('/api/upload').status_code==200
    source.write_text('VALUE = 2\n')
    for method,path,headers in [('POST','/api/upload',{}),('POST','/api/stores/s/periods/p/close',{}),
        ('POST','/api/upload',{'authorization':'Bearer synthetic-service-token'}),
        ('DELETE','/api/commission-v2/session',{}),('PATCH','/api/some-business-state',{})]:
        response=client.request(method,path,headers=headers)
        assert response.status_code==503
        assert response.json()['kind']=='maintenance'
        assert response.json()['code']=='code_source_drift'
        assert '技术维护' in response.json()['detail']
    assert client.get('/api/read').status_code==200


def test_runtime_receipt_tamper_is_not_a_login_or_financial_decision(surface,tmp_path,monkeypatch):
    from ledger import version
    from test_code_integrity import make_release
    monitor,source,target,value=make_release(tmp_path/'application')
    monkeypatch.setattr(version,'_CODE_MONITOR',monitor)
    client,token,_=surface;client.cookies.set('commission_session',token)
    target.unlink()
    response=client.post('/api/upload')
    assert response.status_code==503 and response.json()['kind']=='maintenance'
    assert response.json()['code']=='code_source_drift'
