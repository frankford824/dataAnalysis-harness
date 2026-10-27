"""The real shipping entry cannot reach SSH before qualified packaging."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_release_package import package_case,commit,seal

REPO=Path(__file__).resolve().parents[2]


@pytest.fixture
def ship_case(package_case,monkeypatch):
    case=package_case;repo=case['repo'];folder=repo/'tools/deploy';folder.mkdir()
    shutil.copyfile(REPO/'tools/deploy/package-ledger-release.py',folder/'package-ledger-release.py')
    (folder/'win').mkdir();(folder/'win/deploy-commission.ps1').write_text("throw 'synthetic transport fixture'\n")
    (repo/'.gitignore').write_text('__pycache__/\n')
    case['version']=commit(repo,'committed release tools')
    case['receipt']['code_sha']=case['version'];seal(case['receipt'])
    receipt=case['evidence']/'receipt.json'
    import json
    receipt.write_text(json.dumps(case['receipt']))
    baseline=case['evidence']/'production-base.json'
    from ledger.release_gate import file_sha
    baseline.write_text(json.dumps({'schema':1,'files':[{'path':'ledger/ledger/example.py','sha256':'b'*64}]}))
    spec=importlib.util.spec_from_file_location('real_ship_entry',REPO/'tools/deploy/ship-ledger-release.py')
    tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool);monkeypatch.setattr(tool,'REPO',repo)
    calls=[];real_run=subprocess.run
    def run(argv,**kwargs):
        if argv[0] in {'ssh','scp'}:calls.append(argv);raise AssertionError('A packaging-only test reached the network')
        return real_run(argv,**kwargs)
    monkeypatch.setattr(tool.subprocess,'run',run)
    args=['--base',case['base'],'--receipt',str(receipt),'--base-manifest',str(baseline),'--output',str(case['output'])]
    return case,tool,args,calls,receipt


def test_real_ship_without_deploy_only_produces_receipt_bound_package(ship_case):
    case,tool,args,calls,_=ship_case
    assert tool.main(args)==0
    assert case['output'].is_file() and not calls


def test_rejected_financial_receipt_cannot_reach_network_even_with_deploy_requested(ship_case):
    import json
    case,tool,args,calls,path=ship_case
    case['receipt']['checks']=[row for row in case['receipt']['checks'] if row['name']!='corpus']
    seal(case['receipt']);path.write_text(json.dumps(case['receipt']))
    with pytest.raises(subprocess.CalledProcessError):
        tool.main([*args,'--deploy','--expected-version','b'*40,'--period','2030-01',
            '--user-base-url','https://ledger.example.invalid','--backup-manifest','D:/ledger/backups/example/manifest.json'])
    assert not calls and not case['output'].exists()
