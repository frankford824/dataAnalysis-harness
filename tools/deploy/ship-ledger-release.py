"""Package a qualified commit; deploy only with pinned actual preimages and backup."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

REPO=Path(__file__).resolve().parents[2]


def ps_literal(value):return "'"+value.replace("'","''")+"'"


def encoded(script):return base64.b64encode(script.encode('utf-16-le')).decode('ascii')


def git(*args):
    return subprocess.check_output(['git','-C',str(REPO),*args],text=True).strip()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',required=True)
    parser.add_argument('--receipt',type=Path,required=True)
    parser.add_argument('--base-manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--deploy',action='store_true')
    parser.add_argument('--host',default='finance-win')
    parser.add_argument('--expected-version')
    parser.add_argument('--user-base-url')
    parser.add_argument('--period')
    parser.add_argument('--backup-manifest',help='Existing remote financial backup manifest, verified again before cutover')
    args=parser.parse_args(argv)
    if git('status','--porcelain','--','ledger','models','tools','ops','agent-worker','host-agent'):
        raise SystemExit('Uncommitted implementation cannot be shipped')
    version=git('rev-parse','HEAD')
    if args.deploy:
        if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]*',args.host):parser.error('Use a configured SSH host alias')
        if not re.fullmatch('[0-9a-f]{7,40}',args.expected_version or ''):parser.error('Explicit current deployed version required')
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',args.period or ''):parser.error('Explicit readback period required')
        url=urlsplit(args.user_base_url or '')
        if url.scheme not in {'http','https'} or not url.netloc or url.username or url.query or url.fragment:
            parser.error('Explicit user-entry HTTP(S) base URL required')
        if not args.backup_manifest or not re.fullmatch(r'D:[\\/]ledger[\\/]backups[\\/][A-Za-z0-9_./\\-]+\.json',args.backup_manifest):
            parser.error('A verified remote backup manifest below D:/ledger/backups is required')
        if '..' in re.split(r'[\\/]',args.backup_manifest):parser.error('Non-canonical backup path')
    # Network is unreachable until the real packager has accepted every check.
    result=subprocess.run([sys.executable,str(REPO/'tools/deploy/package-ledger-release.py'),str(REPO),
        args.base,version,str(args.output),'--receipt',str(args.receipt),'--base-manifest',str(args.base_manifest)],
        check=True,text=True,capture_output=True)
    package=json.loads(result.stdout)
    if not args.deploy:
        print(json.dumps({**package,'deployed':False}));return 0
    remote='D:/ledger/releases/incoming/'+package['sha256']
    remote_ps=remote+'/deploy-commission.ps1';remote_zip=remote+'/payload.zip'
    def ssh(script):
        subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',args.host,
            'powershell','-NoProfile','-NonInteractive','-EncodedCommand',encoded(script)],check=True)
    ssh("$ErrorActionPreference='Stop'; New-Item -ItemType Directory -Force -Path "+ps_literal(remote)+" | Out-Null")
    with tempfile.TemporaryDirectory(prefix='ledger-release-script-') as folder:
        script=Path(folder)/'deploy-commission.ps1'
        committed_script=subprocess.check_output(['git','-C',str(REPO),'show',version+':tools/deploy/win/deploy-commission.ps1'])
        script.write_bytes(b'\xef\xbb\xbf'+committed_script.removeprefix(b'\xef\xbb\xbf'))
        script_sha=hashlib.sha256(script.read_bytes()).hexdigest()
        for source,target in ((args.output,remote_zip),(script,remote_ps)):
            subprocess.run(['scp','-q','-o','BatchMode=yes',str(source),args.host+':'+target],check=True)
        parameters={'Payload':remote_zip,'ExpectedVersion':args.expected_version,'Version':version,
            'UserBaseUrl':args.user_base_url,'ReadbackPeriod':args.period,'PayloadSha256':package['sha256'],
            'BackupManifest':args.backup_manifest}
        command=("$ErrorActionPreference='Stop'; if ((Get-FileHash -LiteralPath "+ps_literal(remote_ps)+
            " -Algorithm SHA256).Hash.ToLower() -ne "+ps_literal(script_sha)+") { throw 'Release executor transport mismatch' }; & "+ps_literal(remote_ps)+
            ''.join(' -'+name+' '+ps_literal(value) for name,value in parameters.items()))
        ssh(command)
    print(json.dumps({**package,'deployed':True,'user_entry':args.user_base_url,'period':args.period}));return 0


if __name__=='__main__':raise SystemExit(main())
