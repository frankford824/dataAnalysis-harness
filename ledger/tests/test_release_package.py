"""Exercise the real packager against real temporary Git commits and receipts."""
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from ledger.release_gate import REQUIRED_CHECKS, digest, file_sha

CANONICAL_CHECKS={'backend','corpus','service_replay','invariants','browser','frontend_build','agent_worker','ops'}

REPO=Path(__file__).resolve().parents[2]
PACKAGER=REPO/'tools/deploy/package-ledger-release.py'


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args],text=True,stderr=subprocess.STDOUT).strip()


def commit(repo,message):
    git(repo,'add','-A')
    git(repo,'-c','commit.gpgsign=false','commit','-m',message)
    return git(repo,'rev-parse','HEAD')


def seal(receipt):
    receipt['receipt_sha']=digest({key:value for key,value in receipt.items() if key!='receipt_sha'})
    return receipt


def sources(case):
    repo=case['repo'];version=case['version']
    import hashlib
    paths=sorted(git(repo,'ls-tree','-r','--name-only',version,'--','ledger/ledger').splitlines())
    return [{'path':path,'sha256':hashlib.sha256(subprocess.check_output(['git','-C',str(repo),'show',version+':'+path])).hexdigest()}
            for path in paths if path.endswith('.py')]


@pytest.fixture
def package_case(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    git(repo,'init','-q');git(repo,'config','user.name','Package integration fixture');git(repo,'config','user.email','package@example.invalid')
    for folder in ('ledger/ledger/static/assets','models/cn-ecommerce','tools'):
        (repo/folder).mkdir(parents=True)
    (repo/'ledger/ledger/__init__.py').write_text('')
    for relative in ('ledger/ledger/release_gate.py','ledger/ledger/snapshot_store.py',
                     'ledger/ledger/storage_integrity.py','ledger/ledger/run_artifacts.py',
                     'tools/verify-ledger-release.py','tools/bootstrap-ledger-agent.py','tools/ledger-auth.py','tools/ledger-backup.py','tools/ledger_acceptance_inputs.py'):
        shutil.copyfile(REPO/relative,repo/relative)
    for name,body in {'model.yaml':'id: package-fixture\n','templates.yaml':'templates: []\n','sources.yaml':'sources: []\n','metrics.yaml':'metrics: []\n'}.items():
        (repo/'models/cn-ecommerce'/name).write_text(body)
    (repo/'ledger/ledger/example.py').write_text('VALUE = 1\n')
    (repo/'ledger/ledger/static/index.html').write_text('<!doctype html><div id="app"></div><script src="assets/app.js"></script>')
    (repo/'ledger/ledger/static/assets/app.js').write_text('document.querySelector("#app").textContent="base";')
    base=commit(repo,'base release')
    (repo/'ledger/ledger/example.py').write_text('VALUE = 2\n')
    (repo/'ledger/ledger/static/assets/app.js').write_text('document.querySelector("#app").textContent="accepted";')
    version=commit(repo,'accepted release')
    corpus=tmp_path/'corpus';corpus.mkdir()
    (corpus/'source.xlsx').write_bytes(b'synthetic input identity for package integration')
    evidence=tmp_path/'evidence';evidence.mkdir()
    checks=[]
    for name in sorted(CANONICAL_CHECKS):
        command=['synthetic-package-check',name]
        if name in {'browser','frontend_build'}:
            log=evidence/(name+'.log');log.write_text('synthetic package contract passed\n')
            report=evidence/(name+'.json')
            report.write_text(json.dumps({'command':command,'exit_code':0,'log_sha256':file_sha(log)}))
        else:
            report=evidence/(name+'.xml')
            report.write_text(f'<testsuites><testsuite name="{name}" tests="1" failures="0" errors="0" skipped="0"><testcase name="synthetic_package_contract"/></testsuite></testsuites>')
        checks.append({'name':name,'command':command,'exit_code':0,'tests':1,'failures':0,'errors':0,'skipped':0,'report':report.name,'report_sha256':file_sha(report)})
    models=[{'path':path.relative_to(repo/'models/cn-ecommerce').as_posix(),'sha256':file_sha(path)} for path in sorted((repo/'models/cn-ecommerce').rglob('*')) if path.is_file()]
    frontend=[{'path':path.relative_to(repo/'ledger/ledger/static').as_posix(),'sha256':file_sha(path)} for path in sorted((repo/'ledger/ledger/static').rglob('*')) if path.is_file()]
    inputs=[{'path':'source.xlsx','sha256':file_sha(corpus/'source.xlsx')}]
    receipt=seal({'schema':1,'code_sha':version,'model_sha':digest(models),'model_manifest':models,'inputs':inputs,'input_sha':digest(inputs),
                  'corpus_root':str(corpus),'frontend_artifacts':frontend,'checks':checks,'unresolved_differences':0,'inputs_unchanged':True})
    manifest=sources({'repo':repo,'version':version})
    receipt.update(source_code_manifest=manifest,source_code_sha=digest(manifest));seal(receipt)
    return {'repo':repo,'base':base,'version':version,'corpus':corpus,'evidence':evidence,'receipt':receipt,'output':tmp_path/'release.zip'}


def package(case,*,receipt=True,extra=()):
    args=[sys.executable,str(PACKAGER),str(case['repo']),case['base'],case['version'],str(case['output'])]
    if receipt:
        target=case['evidence']/'acceptance-receipt.json'
        target.write_text(json.dumps(case['receipt'],ensure_ascii=False,indent=2))
        args.extend(['--receipt',str(target)])
    return subprocess.run([*args,*extra],text=True,capture_output=True)


@pytest.mark.parametrize('mutation',['no_receipt','version','model','missing_check','skipped','source_changed','frontend_changed','missing_index','report_changed','source_code_manifest'])
def test_real_packager_refuses_unqualified_receipts_without_output(package_case,mutation):
    case=package_case;receipt=case['receipt']
    if mutation=='version':receipt['code_sha']=case['base']
    elif mutation=='model':receipt['model_sha']='f'*64
    elif mutation=='missing_check':receipt['checks'].pop()
    elif mutation=='skipped':receipt['checks'][0]['skipped']=1
    elif mutation=='source_changed':(case['corpus']/'source.xlsx').write_bytes(b'changed after acceptance')
    elif mutation=='frontend_changed':(case['repo']/'ledger/ledger/static/index.html').write_text('<html>unaccepted frontend</html>')
    elif mutation=='missing_index':receipt['frontend_artifacts']=[row for row in receipt['frontend_artifacts'] if row['path']!='index.html']
    elif mutation=='report_changed':(case['evidence']/receipt['checks'][0]['report']).write_text('changed report')
    elif mutation=='source_code_manifest':
        receipt['source_code_manifest'].pop();receipt['source_code_sha']=digest(receipt['source_code_manifest'])
    seal(receipt)
    result=package(case,receipt=mutation!='no_receipt')
    assert result.returncode!=0,result.stdout
    assert not case['output'].exists(),result.stderr


def test_real_packager_binds_every_byte_receipt_and_unique_entry(package_case):
    case=package_case
    result=package(case)
    assert result.returncode==0,result.stderr
    assert len(case['version'])==40 and case['version']!=case['base']
    with zipfile.ZipFile(case['output']) as archive:
        names=archive.namelist()
        assert len(names)==len(set(names))
        manifest=json.loads(archive.read('manifest.json'))
        receipt=json.loads(archive.read('acceptance-receipt.json'))
        assert REQUIRED_CHECKS==CANONICAL_CHECKS
        assert {check['name'] for check in receipt['checks']}==CANONICAL_CHECKS
        assert archive.read('acceptance-receipt.json')==(case['evidence']/'acceptance-receipt.json').read_bytes()
        assert archive.read('RELEASE-ACCEPTANCE.json')==archive.read('acceptance-receipt.json')
        assert manifest['version']==case['version'] and manifest['base']==case['base']
        assert manifest['model_sha']==case['receipt']['model_sha']
        assert manifest['acceptance_receipt_sha']==receipt['receipt_sha']==case['receipt']['receipt_sha']
        assert archive.read('VERSION').decode()==case['version']+'\n'
        paths=[row['path'] for row in manifest['files']]
        assert len(paths)==len(set(paths))
        assert {'ledger/ledger/static/index.html','ledger/ledger/static/assets/app.js','ledger/ledger/release_gate.py','tools/verify-ledger-release.py'}<=set(paths)
        import hashlib
        for item in manifest['files']:
            assert hashlib.sha256(archive.read(item['path'])).hexdigest()==item['sha256']


def test_missing_committed_verifier_never_leaves_a_partial_release(package_case):
    case=package_case
    (case['repo']/'tools/verify-ledger-release.py').unlink()
    case['version']=commit(case['repo'],'missing required verifier')
    case['receipt']['code_sha']=case['version'];seal(case['receipt'])
    result=package(case)
    assert result.returncode!=0,result.stdout
    assert not case['output'].exists(),'Rejected package left a release.zip artifact: '+result.stderr


def test_existing_release_is_never_overwritten_or_deleted(package_case):
    case=package_case;case['output'].write_bytes(b'keep-existing-release')
    result=package(case)
    assert result.returncode!=0
    assert case['output'].read_bytes()==b'keep-existing-release'


@pytest.mark.parametrize('omitted',sorted(CANONICAL_CHECKS))
def test_each_canonical_check_is_mandatory_at_the_real_entrypoint(package_case,omitted):
    case=package_case
    case['receipt']['checks']=[row for row in case['receipt']['checks'] if row['name']!=omitted]
    seal(case['receipt'])
    result=package(case)
    assert result.returncode!=0,omitted+' was not required by the package gate'
    assert not case['output'].exists()


def test_actual_production_preimages_override_stale_git_version(package_case):
    case=package_case
    deployed_sha='b'*64
    baseline=case['evidence']/'actual-production-files.json'
    baseline.write_text(json.dumps({'schema':1,'files':[{'path':'ledger/ledger/example.py','sha256':deployed_sha}]}))
    result=package(case,extra=('--base-manifest',str(baseline)))
    assert result.returncode==0,result.stderr
    with zipfile.ZipFile(case['output']) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        entry=next(row for row in manifest['files'] if row['path']=='ledger/ledger/example.py')
        assert entry['before_sha256']==deployed_sha
        assert manifest['production_base_manifest_sha256']==file_sha(baseline)
        assert manifest['production_base_files']==[{'path':'ledger/ledger/example.py','sha256':deployed_sha}]


def test_unknown_deployed_module_cannot_be_silently_dropped(package_case):
    case=package_case
    baseline=case['evidence']/'actual-production-files.json'
    baseline.write_text(json.dumps({'schema':1,'files':[{'path':'ledger/ledger/unknown_production_hotfix.py','sha256':'b'*64}]}))
    result=package(case,extra=('--base-manifest',str(baseline)))
    assert result.returncode!=0 and not case['output'].exists()
    assert 'Actual deployed code deletion' in result.stderr


def test_accepted_external_runtime_model_is_preserved_without_shipping_business_csv(package_case):
    case=package_case;runtime=case['evidence']/'private-runtime-model'
    shutil.copytree(case['repo']/'models/cn-ecommerce',runtime)
    (runtime/'personnel.csv').write_text('synthetic private runtime relation\n')
    identities=[{'path':p.relative_to(runtime).as_posix(),'sha256':file_sha(p)} for p in sorted(runtime.rglob('*')) if p.is_file()]
    case['receipt'].update(model_root=str(runtime),model_manifest=identities,model_sha=digest(identities));seal(case['receipt'])
    result=package(case)
    assert result.returncode==0,result.stderr
    with zipfile.ZipFile(case['output']) as archive:
        assert not any(path.startswith('models/') for path in archive.namelist())
        assert json.loads(archive.read('manifest.json'))['model_sha']==digest(identities)


def test_worker_source_layout_is_complete_even_when_unchanged_since_git_base(package_case):
    case=package_case;repo=case['repo']
    for path in ('host-agent/finance_agent/__init__.py','host-agent/pyproject.toml','host-agent/README.md',
                 'agent-worker/ledger_agent/__init__.py','agent-worker/pyproject.toml','agent-worker/requirements.lock',
                 'agent-worker/skills/evidence/SKILL.md','ops/ledger_sentinel/__init__.py'):
        target=repo/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text('# synthetic package layout\n')
    case['base']=commit(repo,'unchanged runtime source already in Git')
    (repo/'ledger/ledger/example.py').write_text('VALUE = 3\n')
    case['version']=commit(repo,'new service with existing runtime code')
    source=sources(case)
    case['receipt'].update(code_sha=case['version'],source_code_manifest=source,source_code_sha=digest(source));seal(case['receipt'])
    result=package(case)
    assert result.returncode==0,result.stderr
    with zipfile.ZipFile(case['output']) as archive:
        entries={item['path']:item for item in json.loads(archive.read('manifest.json'))['files']}
        for path in ('host-agent/finance_agent/__init__.py','host-agent/pyproject.toml','agent-worker/requirements.lock'):
            assert archive.read(path)==(repo/path).read_bytes()
            assert entries[path]['allow_absent'] is True
        assert {'tools/bootstrap-ledger-agent.py','tools/ledger-auth.py','ledger/ledger/snapshot_store.py',
                'ledger/ledger/run_artifacts.py','ledger/ledger/storage_integrity.py'}<=entries.keys()
