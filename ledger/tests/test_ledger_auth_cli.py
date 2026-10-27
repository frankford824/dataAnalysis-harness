"""Credential setup uses temporary local DB/files; never current accounts."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
from types import SimpleNamespace
import time

import pytest
from starlette.requests import Request

from ledger.commission_registry import Registry
from ledger import write_auth

SCRIPT=Path(__file__).resolve().parents[2]/'tools/ledger-auth.py'
spec=importlib.util.spec_from_file_location('local_ledger_auth_cli',SCRIPT)
auth=importlib.util.module_from_spec(spec);spec.loader.exec_module(auth)


def cli(*args):
    return subprocess.run([sys.executable,str(SCRIPT),*map(str,args)],capture_output=True,text=True,timeout=30)


def password(tmp_path,text='synthetic-long-password'):
    path=tmp_path/'password.txt';path.write_text(text);path.chmod(0o600)
    return path


def issue(tmp_path,identifier='worker-one',*,rotate=False,expiry=3600,extra=()):
    return cli('service-issue','--identity-file',tmp_path/'identity.json','--token-file',tmp_path/(identifier+'.token'),
        '--id',identifier,'--capabilities','agent_control,approved_action','--scopes','business',
        '--expires-in',expiry,*(['--rotate'] if rotate else []),*extra)


def actor(token):
    return write_auth.service_identity(Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())]}))


def test_new_operator_uses_registry_and_duplicate_never_changes_existing_password(tmp_path):
    home=tmp_path/'home';secret='synthetic-long-password';path=password(tmp_path,secret)
    first=cli('operator-add','--home',home,'--name','fixture-human','--password-file',path)
    assert first.returncode==0,first.stdout+first.stderr
    assert secret not in first.stdout+first.stderr
    registry=Registry(home)
    with registry.connect() as db:before=dict(db.execute('SELECT * FROM operator').fetchone())
    assert before['admin']==0 and before['disabled']==0 and secret not in json.dumps(before)
    assert home.stat().st_mode&0o777==0o700 and (home/'commission').stat().st_mode&0o777==0o700
    assert (home/'commission/registry.db').stat().st_mode&0o777==0o600
    assert registry.actor(registry.login('fixture-human',secret))['id']=='fixture-human'
    path.write_text('another-synthetic-password')
    duplicate=cli('operator-add','--home',home,'--name','fixture-human','--password-file',path,'--admin')
    assert duplicate.returncode!=0 and 'another-synthetic-password' not in duplicate.stdout+duplicate.stderr
    with registry.connect() as db:assert dict(db.execute('SELECT * FROM operator').fetchone())==before


def test_interactive_getpass_adds_only_explicit_temporary_home(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(auth.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(auth.getpass,'getpass',lambda _: 'synthetic-getpass-password')
    assert auth.main(['operator-add','--home',str(tmp_path/'home'),'--name','fixture-admin','--admin'])==0
    assert 'synthetic-getpass-password' not in capsys.readouterr().out
    with Registry(tmp_path/'home').connect() as db:
        assert db.execute("SELECT admin FROM operator WHERE id='fixture-admin'").fetchone()[0]==1


def test_service_server_has_only_hash_client_private_and_runtime_accepts_exact_token(tmp_path,monkeypatch):
    response=issue(tmp_path)
    assert response.returncode==0,response.stdout+response.stderr
    token=(tmp_path/'worker-one.token').read_text().strip()
    body=json.loads((tmp_path/'identity.json').read_text());entry=body['identities'][0]
    assert len(token)>=64 and token not in response.stdout+response.stderr
    assert token not in json.dumps(body) and 'token' not in entry and len(entry['token_sha256'])==64
    for path in (tmp_path/'worker-one.token',tmp_path/'identity.json'):
        assert path.stat().st_mode&0o777==0o600
    monkeypatch.setenv('LEDGER_SERVICE_IDENTITIES',str(tmp_path/'identity.json'))
    assert actor(token)['id']=='worker-one' and actor('wrong-token') is None
    assert entry['capabilities']==['agent_control','approved_action']
    assert auth.CAPABILITIES=={cap for _,_,cap in write_auth.SERVICE_ROUTES}


def test_issue_preserves_other_identities_rotate_revokes_old_without_exposing_tokens(tmp_path,monkeypatch):
    assert issue(tmp_path,'first').returncode==0
    one=deepcopy(json.loads((tmp_path/'identity.json').read_text())['identities'][0])
    assert issue(tmp_path,'second').returncode==0
    entries=json.loads((tmp_path/'identity.json').read_text())['identities']
    assert entries[0]==one and len(entries)==2
    original=(tmp_path/'first.token').read_text().strip();before=(tmp_path/'identity.json').read_bytes()
    duplicate=issue(tmp_path,'first')
    assert duplicate.returncode!=0 and 'explicit_rotate' in duplicate.stdout
    assert (tmp_path/'identity.json').read_bytes()==before
    assert (tmp_path/'first.token').read_text().strip()==original
    rotated=issue(tmp_path,'first',rotate=True)
    assert rotated.returncode==0,rotated.stdout+rotated.stderr
    current=(tmp_path/'first.token').read_text().strip()
    assert current!=original and current not in rotated.stdout+rotated.stderr and original not in rotated.stdout+rotated.stderr
    monkeypatch.setenv('LEDGER_SERVICE_IDENTITIES',str(tmp_path/'identity.json'))
    assert actor(original) is None and actor(current)['id']=='first'
    assert json.loads((tmp_path/'identity.json').read_text())['identities'][1]==entries[1]


def test_doctor_is_readonly_reports_expiry_without_password_hash_or_token(tmp_path):
    home=tmp_path/'home';path=password(tmp_path)
    assert cli('operator-add','--home',home,'--name','fixture','--password-file',path).returncode==0
    assert issue(tmp_path).returncode==0
    identity=tmp_path/'identity.json';token=(tmp_path/'worker-one.token').read_text().strip()
    before=identity.read_bytes();db=home/'commission/registry.db';database=db.read_bytes()
    result=cli('doctor','--home',home,'--identity-file',identity)
    assert result.returncode==0,result.stdout+result.stderr
    assert before==identity.read_bytes() and database==db.read_bytes()
    assert token not in result.stdout+result.stderr and 'password_hash' not in result.stdout
    assert json.loads(before)['identities'][0]['token_sha256'] not in result.stdout
    assert json.loads(result.stdout)['checks'][1]['valid_count']==1
    body=json.loads(before);body['identities'][0]['expires_at']=time.time()-1
    identity.write_text(json.dumps(body));before=identity.read_bytes()
    expired=cli('doctor','--identity-file',identity)
    assert expired.returncode!=0 and json.loads(expired.stdout)['checks'][0]['expired_enabled_ids']==['worker-one']
    assert identity.read_bytes()==before and actor_from_file_expired(identity,token)


def actor_from_file_expired(path,token):
    # The CLI validation agrees with the actual middleware, without setting
    # permanent process/service environment or touching an existing identity.
    from unittest.mock import patch
    with patch.dict('os.environ',{'LEDGER_SERVICE_IDENTITIES':str(path)}):return actor(token) is None


def test_missing_doctor_configuration_never_initializes_account_database(tmp_path):
    home=tmp_path/'missing-home';identity=tmp_path/'missing.json'
    result=cli('doctor','--home',home,'--identity-file',identity)
    assert result.returncode!=0 and not home.exists() and not identity.exists()
    assert not list(tmp_path.iterdir())
    assert cli('doctor').returncode!=0


def test_doctor_does_not_create_source_wal_or_shm_and_reads_live_committed_wal(tmp_path):
    home=tmp_path/'home';path=password(tmp_path)
    assert cli('operator-add','--home',home,'--name','fixture','--password-file',path).returncode==0
    folder=home/'commission';database=folder/'registry.db'
    assert not database.with_name('registry.db-wal').exists()
    before={p.name:p.read_bytes() for p in folder.iterdir() if p.is_file()}
    assert cli('doctor','--home',home).returncode==0
    assert before=={p.name:p.read_bytes() for p in folder.iterdir() if p.is_file()}
    writer=sqlite3.connect(database)
    try:
        writer.execute('PRAGMA journal_mode=WAL');writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('UPDATE operator SET disabled=1');writer.commit()
        assert database.with_name('registry.db-wal').is_file()
        before={p.name:p.read_bytes() for p in folder.iterdir() if p.is_file()}
        result=cli('doctor','--home',home)
        assert result.returncode!=0 and json.loads(result.stdout)['checks'][0]['enabled_count']==0
        assert before=={p.name:p.read_bytes() for p in folder.iterdir() if p.is_file()}
    finally:writer.close()


def test_doctor_unknown_if_snapshot_changes_instead_of_certifying_stale_operator(tmp_path,monkeypatch,capsys):
    home=tmp_path/'home';Registry(home).operator_add('fixture','synthetic-long-password')
    monkeypatch.setattr(auth,'_source_digest',lambda _: 'changed-while-reading')
    assert auth.main(['doctor','--home',str(home)])!=0
    assert 'registry_snapshot_changed_retry' in capsys.readouterr().out


def test_operator_add_does_not_rewrite_preexisting_directory_or_database_permissions(tmp_path):
    home=tmp_path/'existing';Registry(home)
    home.chmod(0o750);(home/'commission').chmod(0o750);(home/'commission/registry.db').chmod(0o640)
    path=password(tmp_path)
    assert cli('operator-add','--home',home,'--name','fixture','--password-file',path).returncode==0
    assert home.stat().st_mode&0o777==0o750 and (home/'commission').stat().st_mode&0o777==0o750
    assert (home/'commission/registry.db').stat().st_mode&0o777==0o640


@pytest.mark.parametrize('field,value',[
    ('--capabilities','close'),('--capabilities','agent_control,unknown'),
    ('--capabilities','agent_control,agent_control'),('--scopes','admin'),
    ('--expires-in','0'),('--expires-in','-1'),('--id','../escape'),
])
def test_unknown_capability_scope_expiry_and_identity_rejected_without_files(tmp_path,field,value):
    result=issue(tmp_path,extra=(field,value))
    assert result.returncode!=0 and not (tmp_path/'identity.json').exists() and not list(tmp_path.glob('*.token'))


def test_private_file_permissions_are_required_before_secret_read_or_rotation(tmp_path):
    file=password(tmp_path);file.chmod(0o644)
    result=cli('operator-add','--home',tmp_path/'home','--name','fixture','--password-file',file)
    assert result.returncode!=0 and not (tmp_path/'home').exists()
    assert issue(tmp_path).returncode==0
    identity=tmp_path/'identity.json';before=identity.read_bytes();identity.chmod(0o644)
    assert issue(tmp_path,rotate=True).returncode!=0 and identity.read_bytes()==before
    assert cli('doctor','--identity-file',identity).returncode!=0
    identity.chmod(0o600);token=tmp_path/'worker-one.token';token.chmod(0o640)
    assert issue(tmp_path,rotate=True).returncode!=0 and identity.read_bytes()==before


def test_rotation_wrong_client_file_and_missing_identity_do_not_overwrite(tmp_path):
    assert issue(tmp_path).returncode==0
    token=tmp_path/'worker-one.token';token.write_text('different-private-synthetic-token')
    before=token.read_bytes();identity=(tmp_path/'identity.json').read_bytes()
    assert issue(tmp_path,rotate=True).returncode!=0
    assert token.read_bytes()==before and (tmp_path/'identity.json').read_bytes()==identity
    assert issue(tmp_path,'missing',rotate=True).returncode!=0 and not (tmp_path/'missing.token').exists()


def test_concurrent_different_service_issues_preserve_both_identities(tmp_path):
    def argv(identifier):return [sys.executable,str(SCRIPT),'service-issue','--identity-file',str(tmp_path/'identity.json'),
        '--token-file',str(tmp_path/(identifier+'.token')),'--id',identifier,'--capabilities','agent_control',
        '--scopes','business','--expires-in','3600']
    processes=[subprocess.Popen(argv(name),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for name in ('first','second')]
    for process in processes:
        output,error=process.communicate(timeout=30)
        assert process.returncode==0,output+error
    assert {r['id'] for r in json.loads((tmp_path/'identity.json').read_text())['identities']}=={'first','second'}


def test_two_file_publish_failure_restores_prior_client_and_preserves_server(tmp_path,monkeypatch,capsys):
    assert issue(tmp_path).returncode==0
    identity=tmp_path/'identity.json';client=tmp_path/'worker-one.token'
    previous=identity.read_bytes(),client.read_bytes();replace=auth.os.replace
    def failing(source,target):
        if Path(target)==identity:raise OSError('synthetic disk failure')
        return replace(source,target)
    monkeypatch.setattr(auth.os,'replace',failing)
    code=auth.main(['service-issue','--identity-file',str(identity),'--token-file',str(client),
        '--id','worker-one','--capabilities','agent_control','--scopes','business','--expires-in','3600','--rotate'])
    assert code!=0 and (identity.read_bytes(),client.read_bytes())==previous
    assert not list(tmp_path.glob('*.tmp')) and previous[1].decode().strip() not in capsys.readouterr().out


def test_secret_arguments_and_plaintext_identity_fields_are_never_echoed(tmp_path):
    secret='synthetic-secret-never-echo'
    response=cli('operator-add','--home',tmp_path/'home','--name','fixture','--password',secret)
    assert response.returncode!=0 and secret not in response.stdout+response.stderr
    assert issue(tmp_path).returncode==0
    identity=tmp_path/'identity.json';body=json.loads(identity.read_text());body['identities'][0]['token']=secret
    identity.write_text(json.dumps(body))
    result=cli('doctor','--identity-file',identity)
    assert result.returncode!=0 and secret not in result.stdout+result.stderr


def test_symlink_and_non_local_paths_rejected_without_following(tmp_path):
    destination=tmp_path/'outside.json';link=tmp_path/'identity.json';link.symlink_to(destination)
    assert issue(tmp_path).returncode!=0 and not destination.exists()
    assert cli('doctor','--identity-file','http://example.invalid/identity.json').returncode!=0
    assert cli('doctor','--home','relative-home').returncode!=0
