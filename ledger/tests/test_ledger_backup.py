"""Exercise actual SQLite/WAL backups, archive bytes and frozen restore proofs."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from test_release_readback import stored

REPO=Path(__file__).resolve().parents[2]
CODE='a'*40


def module():
    spec=importlib.util.spec_from_file_location('ledger_backup_test',REPO/'tools/ledger-backup.py')
    tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool);return tool


@pytest.fixture
def backup(stored,tmp_path):
    application=tmp_path/'application';(application/'ledger/ledger').mkdir(parents=True)
    (application/'ledger/ledger/api.py').write_text('# synthetic deployed application\n')
    (application/'VERSION').write_text(CODE+'\n')
    model=application/'models/cn-ecommerce';model.mkdir(parents=True)
    (model/'stores.yaml').write_text('stores: []\n')
    target=tmp_path/'backup';tool=module()
    # Keep committed data in a live WAL while the backup runs.
    ws=stored['ws'];ws.conn.execute('PRAGMA journal_mode=WAL')
    with ws.conn:
        ws.conn.execute('CREATE TABLE backup_live_wal(value INTEGER)')
        ws.conn.execute('INSERT INTO backup_live_wal VALUES(47)')
    result=tool.create(ws.root,application,model,target,CODE)
    assert result['passed'] is True
    return {**stored,'tool':tool,'target':target,'application':application,'model_root':model}


def reseal(case,body,proof=None):
    tool=case['tool'];root=case['target']
    if proof is not None:
        (root/'restore-check.json').write_text(json.dumps(proof))
        body['restore_check']['sha256']=tool.sha(root/'restore-check.json')
    (root/'manifest.json').write_text(json.dumps(body))


def test_real_backup_includes_committed_wal_and_keeps_original_financial_history(backup):
    case=backup;tool=case['tool'];root=case['target']
    assert tool.verify(root/'manifest.json',CODE)['roles']==['application','model','registry','workspace']
    with sqlite3.connect(root/'workspace.db') as restored:
        assert restored.execute('SELECT value FROM backup_live_wal').fetchone()==(47,)
    proof=json.loads((root/'restore-check.json').read_text())
    assert proof['source_frozen_history_sha']==proof['restored_frozen_history_sha']
    assert proof['live_restore_performed'] is False
    assert (case['application']/'VERSION').read_text()==CODE+'\n'


@pytest.mark.parametrize('mutation',['missing_role','wrong_role_path','stale','version','failed_restore','changed_archive'])
def test_missing_or_unverified_backup_cannot_enable_a_release(backup,mutation):
    case=backup;root=case['target'];tool=case['tool']
    body=json.loads((root/'manifest.json').read_text());proof=None
    if mutation=='missing_role':body['files'].pop()
    elif mutation=='wrong_role_path':body['files'][0]['path']='model.zip'
    elif mutation=='stale':body['completed_at']='2000-01-01T00:00:00+00:00'
    elif mutation=='version':body['code_sha']='b'*40
    elif mutation=='changed_archive':(root/'application.zip').write_bytes(b'not the verified backup')
    else:
        proof=json.loads((root/'restore-check.json').read_text());proof['checks']['frozen_history_equal']=False
    reseal(case,body,proof)
    with pytest.raises(ValueError):tool.verify(root/'manifest.json',CODE)


def test_rehashing_changed_database_does_not_replace_independent_restore_proof(backup):
    case=backup;root=case['target'];tool=case['tool']
    body=json.loads((root/'manifest.json').read_text());proof=json.loads((root/'restore-check.json').read_text())
    run=case['runs']['2030-01','s1']
    with sqlite3.connect(root/'workspace.db') as db:db.execute('UPDATE run SET result=? WHERE id=?',('{"changed":true}',run))
    changed=tool.sha(root/'workspace.db')
    for records in (body['files'],proof['files']):
        next(row for row in records if row['role']=='workspace')['sha256']=changed
    reseal(case,body,proof)
    with pytest.raises(ValueError,match='history differs'):tool.verify(root/'manifest.json',CODE)


def test_valid_backup_must_still_cover_financial_changes_since_it_was_taken(backup):
    case=backup;ws=case['ws'];tool=case['tool']
    with ws.conn:ws.conn.execute('UPDATE run SET result=? WHERE id=?',
        ('{"later_financial_record":true}',case['runs']['2030-01','s1']))
    assert tool.verify(case['target']/'manifest.json',CODE)['passed'] is True
    with pytest.raises(ValueError,match='Frozen financial history changed'):
        tool.verify(case['target']/'manifest.json',CODE,compare_root=ws.root)


def test_financial_changes_during_online_backup_leave_no_success_manifest(stored,tmp_path,monkeypatch):
    application=tmp_path/'application';(application/'ledger/ledger').mkdir(parents=True)
    (application/'ledger/ledger/api.py').write_text('# synthetic\n');(application/'VERSION').write_text(CODE)
    model=tmp_path/'model';model.mkdir();(model/'stores.yaml').write_text('stores: []')
    tool=module();original=tool.sqlite_copy;calls=[]
    def copy_then_source_changes(source,target):
        original(source,target);calls.append(source)
        if len(calls)==1:
            with stored['ws'].conn:
                stored['ws'].conn.execute('UPDATE run SET result=? WHERE id=?',
                    ('{"concurrent_financial_change":true}',stored['runs']['2030-01','s1']))
    monkeypatch.setattr(tool,'sqlite_copy',copy_then_source_changes)
    output=tmp_path/'backup'
    with pytest.raises(ValueError,match='Frozen financial history changed'):
        tool.create(stored['root'],application,model,output,CODE)
    assert output.is_dir() and not (output/'manifest.json').exists()


def test_backup_never_overwrites_existing_backup_or_writes_inside_source(backup):
    case=backup;tool=case['tool']
    for target in (case['target'],case['root']/'new-backup',case['model_root']/'new-backup'):
        with pytest.raises(ValueError,match='new backup directory'):
            tool.create(case['root'],case['application'],case['model_root'],target,CODE)


def test_extra_backup_artifact_cannot_replace_a_core_restore_proof(backup):
    case=backup;tool=case['tool'];root=case['target']
    (root/'source-snapshot.evidence').write_text('synthetic extra source artifact')
    body=json.loads((root/'manifest.json').read_text())
    body['files'].append({'role':'source_snapshot','path':'source-snapshot.evidence','sha256':tool.sha(root/'source-snapshot.evidence')})
    reseal(case,body)
    with pytest.raises(ValueError,match='Extra backup roles'):tool.verify(root/'manifest.json',CODE)
    actual=tool.verify(root/'manifest.json',CODE,allow_extra_roles=True)
    assert actual['restore_verified_roles']==['application','model','registry','workspace']
    assert actual['extra_roles_hash_checked']==['source_snapshot']
    (root/'source-snapshot.evidence').write_text('changed after recorded backup')
    with pytest.raises(ValueError,match='artifact changed'):tool.verify(root/'manifest.json',CODE,allow_extra_roles=True)
