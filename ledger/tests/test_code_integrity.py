"""Runtime source approval: real file bytes, immutable process anchor, no skip mode."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from ledger import version
from ledger.release_gate import digest
from test_release_gate import receipt,seal

CODE='a'*40


def source_tree(tmp_path,*,development=False):
    folder=tmp_path/'ledger/ledger';folder.mkdir(parents=True)
    source=folder/'example.py';source.write_text('VALUE = 1\n')
    (folder/'support.py').write_text('OTHER = 1\n')
    if development:subprocess.run(['git','init','-q',str(tmp_path)],check=True,capture_output=True)
    return source


def make_release(tmp_path):
    source=source_tree(tmp_path)
    evidence=tmp_path/'evidence';evidence.mkdir()
    value=receipt(evidence);manifest=version.source_code_manifest(tmp_path)
    value.update(source_code_manifest=manifest,source_code_sha=digest(manifest));seal(value)
    (tmp_path/'VERSION').write_text(CODE+'\n')
    target=tmp_path/'RELEASE-ACCEPTANCE.json';target.write_text(json.dumps(value,ensure_ascii=False))
    return version.CodeIntegrityMonitor(tmp_path),source,target,value


def test_approved_release_matches_actual_complete_source_and_early_anchor(tmp_path):
    monitor,source,target,value=make_release(tmp_path)
    state=monitor.state()
    assert state['healthy'] is True and state['source_code_status']=='approved'
    assert state['source_code_sha']==state['approved_source_code_sha']==value['source_code_sha']
    source.write_text('VALUE = 2\n')
    bad=monitor.state()
    assert bad['healthy'] is False and bad['source_code_status']=='source_drift'
    assert bad['approved_source_code_sha']==value['source_code_sha']
    source.write_text('VALUE = 1\n')
    assert monitor.state()['healthy'] is False  # Restored disk bytes do not repair possibly mixed imported code.
    assert version.CodeIntegrityMonitor(tmp_path).state()['healthy'] is True


@pytest.mark.parametrize('change',['add','delete','modify'])
def test_complete_source_inventory_detects_file_changes(tmp_path,change):
    monitor,source,target,value=make_release(tmp_path)
    if change=='add':source.with_name('unexpected.py').write_text('VALUE = 9\n')
    elif change=='delete':source.unlink()
    else:source.write_text('VALUE = 9\n')
    assert monitor.state()['healthy'] is False


@pytest.mark.parametrize('change',['missing','malformed','tampered','valid_reseal','version'])
def test_receipt_or_version_changes_never_rebaseline_a_running_process(tmp_path,change):
    monitor,source,target,value=make_release(tmp_path)
    assert monitor.state()['healthy']
    if change=='missing':target.unlink()
    elif change=='malformed':target.write_text('{broken')
    elif change=='tampered':
        value['source_code_sha']='f'*64;target.write_text(json.dumps(value))
    elif change=='valid_reseal':
        value['maintenance_note']='changed after startup';seal(value);target.write_text(json.dumps(value))
    else:(tmp_path/'VERSION').write_text('b'*40)
    assert monitor.state()['healthy'] is False
    assert monitor.state()['approved_source_code_sha']!='f'*64


@pytest.mark.parametrize('change',['missing_receipt','missing_stamp','short_stamp','bad_checksum','wrong_version','missing_check'])
def test_incomplete_release_is_not_accepted_at_startup(tmp_path,change):
    _monitor,source,target,value=make_release(tmp_path)
    if change=='missing_receipt':target.unlink()
    elif change=='missing_stamp':(tmp_path/'VERSION').unlink()
    elif change=='short_stamp':(tmp_path/'VERSION').write_text('22')
    elif change=='bad_checksum':value['source_code_sha']='f'*64;target.write_text(json.dumps(value))
    elif change=='wrong_version':
        value['code_sha']='b'*40;seal(value);target.write_text(json.dumps(value))
    else:
        value['checks'].pop();seal(value);target.write_text(json.dumps(value))
    assert version.CodeIntegrityMonitor(tmp_path).state()['healthy'] is False


def test_development_baseline_is_captured_before_first_state_call(tmp_path):
    source=source_tree(tmp_path,development=True)
    monitor=version.CodeIntegrityMonitor(tmp_path)
    source.write_text('VALUE = 2\n')
    assert monitor.state()['healthy'] is False
    fresh=version.CodeIntegrityMonitor(tmp_path)
    assert fresh.state()['source_code_status']=='development'
    assert fresh.state()['healthy'] is True


def test_non_checkout_without_approval_is_not_guessed_to_be_development(tmp_path):
    source_tree(tmp_path)
    status=version.CodeIntegrityMonitor(tmp_path).state()
    assert status['healthy'] is False and status['source_code_status']=='receipt_missing'


def test_actual_version_module_import_eagerly_captures_source_before_request(tmp_path):
    source=source_tree(tmp_path,development=True)
    module_path=tmp_path/'ledger/ledger/version.py';module_path.write_bytes(Path(version.__file__).read_bytes())
    spec=importlib.util.spec_from_file_location('isolated_eager_version',module_path)
    isolated=importlib.util.module_from_spec(spec);spec.loader.exec_module(isolated)
    source.write_text('VALUE = 99\n')
    assert isolated.code_integrity()['healthy'] is False
    assert isolated.code_integrity()['source_code_status']=='source_drift'


def test_assertion_uses_typed_maintenance_error_and_cannot_be_disabled_by_optimization(tmp_path,monkeypatch):
    monitor,source,target,value=make_release(tmp_path)
    monkeypatch.setattr(version,'_CODE_MONITOR',monitor)
    assert version.assert_code_integrity()['healthy'] is True
    source.write_text('VALUE = 2\n')
    with pytest.raises(version.CodeIntegrityError,match='技术维护'):
        version.assert_code_integrity()
    code="import importlib.util,sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from ledger import version;version._CODE_MONITOR=version.CodeIntegrityMonitor(Path(sys.argv[2]));version.assert_code_integrity();print('UNSAFE_ACCEPTED')"
    # A fresh process still rejects a receipt bound to the old source bytes.
    import os,sys
    result=subprocess.run([sys.executable,'-O','-c',code,str(Path(version.__file__).resolve().parents[1]),str(tmp_path)],capture_output=True,text=True)
    assert result.returncode!=0 and 'UNSAFE_ACCEPTED' not in result.stdout


def test_unlisted_symbolic_source_directory_cannot_hide_executable_python(tmp_path):
    monitor,source,target,value=make_release(tmp_path/'application')
    outside=tmp_path/'unapproved-code';outside.mkdir();(outside/'injected.py').write_text('VALUE = 99\n')
    (source.parent/'new_package').symlink_to(outside,target_is_directory=True)
    assert monitor.state()['healthy'] is False
