"""Package only committed code and explicitly approved model contracts."""
import hashlib
import argparse
import json
from pathlib import Path
import subprocess
import sys
import zipfile
import os
import tempfile
import re

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('repo')
parser.add_argument('base')
parser.add_argument('version')
parser.add_argument('output')
parser.add_argument('--receipt', required=True, type=Path)
parser.add_argument('--base-manifest',type=Path,help='Reviewed actual deployed file identities when VERSION drifted')
args = parser.parse_args()
repo, base, version, output = args.repo, args.base, args.version, args.output
sys.path.insert(0, str(Path(repo) / 'ledger'))
sys.path.insert(0, str(Path(repo) / 'tools'))
from ledger.release_gate import digest, file_sha, validate
from ledger_acceptance_inputs import verify as verify_inputs


def git(*args):
    return subprocess.check_output(['git', '-C', repo, *args], stderr=subprocess.DEVNULL)


for ref in (base, version):
    git('rev-parse', '--verify', ref + '^{commit}')
base = git('rev-parse', base + '^{commit}').decode().strip()
version = git('rev-parse', version + '^{commit}').decode().strip()
receipt_bytes = args.receipt.read_bytes()
receipt = validate(json.loads(receipt_bytes), version=version,evidence_root=args.receipt.parent)
committed_sources=[{'path':path,'sha256':hashlib.sha256(git('show',version+':'+path)).hexdigest()}
                   for path in sorted(git('ls-tree','-r','--name-only',version,'--','ledger/ledger').decode().splitlines()) if path.endswith('.py')]
if (not committed_sources or receipt.get('source_code_manifest')!=committed_sources
        or receipt.get('source_code_sha')!=digest(committed_sources)):
    raise SystemExit('Accepted complete Python source identity differs from the committed release')
runtime_model=Path(receipt.get('model_root',Path(repo)/'models/cn-ecommerce')).resolve()
model_identity=[{'path':p.relative_to(runtime_model).as_posix(),'sha256':file_sha(p)}
                for p in sorted(runtime_model.rglob('*')) if p.is_file() and not p.name.startswith('.')]
if not model_identity or digest(model_identity)!=receipt['model_sha']:
    raise SystemExit('Accepted runtime model snapshot changed or is unavailable')
baseline_files=None
if args.base_manifest:
    baseline=json.loads(args.base_manifest.read_text(encoding='utf-8'))
    if baseline.get('schema')!=1 or not baseline.get('files'):raise SystemExit('Invalid reviewed production baseline')
    baseline_files={row['path']:row['sha256'] for row in baseline['files']}
    if len(baseline_files)!=len(baseline['files']) or any(not re.fullmatch('[0-9a-f]{64}',value) for value in baseline_files.values()):
        raise SystemExit('Invalid or duplicated production baseline identity')
    if any(not path.startswith('ledger/ledger/') or '\\' in path or '..' in Path(path).parts or Path(path).is_absolute()
           or path.startswith('ledger/ledger/static/') for path in baseline_files):
        raise SystemExit('Production baseline must contain normalized source paths only')
verify_inputs(receipt)
frontend = receipt.get('frontend_artifacts', [])
if not frontend or not any(p['path'] == 'index.html' for p in frontend):
    raise SystemExit('Accepted frontend build is missing')
for item in frontend:
    source = (Path(repo) / 'ledger/ledger/static' / item['path']).resolve()
    if not source.is_relative_to((Path(repo) / 'ledger/ledger/static').resolve()) or file_sha(source) != item['sha256']:
        raise SystemExit('Frontend changed after acceptance')
runtime_roots=('ops/ledger_sentinel','agent-worker/ledger_agent','agent-worker/skills','agent-worker/pyproject.toml',
               'agent-worker/requirements.lock','host-agent/finance_agent','host-agent/pyproject.toml','host-agent/README.md')
paths = ('ledger/ledger',*runtime_roots,'models/cn-ecommerce/templates.yaml','models/cn-ecommerce/sources.yaml','models/cn-ecommerce/metrics.yaml','models/cn-ecommerce/dictionary.csv')
removed = git('diff','--name-only','--diff-filter=D',base,version,'--',*paths).decode().splitlines()
code_removed = [p for p in removed if not p.startswith('ledger/ledger/static/')]
if code_removed:
    raise SystemExit('Deletion requires an explicit rollback/migration manifest: '+', '.join(code_removed))
allowed_models = {'models/cn-ecommerce/templates.yaml','models/cn-ecommerce/sources.yaml','models/cn-ecommerce/metrics.yaml','models/cn-ecommerce/dictionary.csv'}
changed_models = git('diff','--name-only',base,version,'--','models/cn-ecommerce').decode().splitlines()
if set(changed_models)-allowed_models:
    raise SystemExit('Model changes outside approved release contracts: '+', '.join(sorted(set(changed_models)-allowed_models)))
for path in changed_models:
    relative=Path(path).relative_to('models/cn-ecommerce').as_posix()
    approved=next((row['sha256'] for row in model_identity if row['path']==relative),None)
    if hashlib.sha256(git('show',version+':'+path)).hexdigest()!=approved:
        raise SystemExit('Packaged model update is not the model actually exercised by acceptance')
files = git('diff', '--no-renames', '--name-only', '--diff-filter=AM', base, version,
            '--', *paths).decode().splitlines()
if baseline_files is not None:
    candidate_paths=git('ls-tree','-r','--name-only',version,'--','ledger/ledger').decode().splitlines()
    candidate_code={path for path in candidate_paths if not path.startswith('ledger/ledger/static/')}
    absent=set(baseline_files)-candidate_code
    if absent:raise SystemExit('Actual deployed code deletion needs an explicit migration manifest: '+', '.join(sorted(absent)))
    files=[path for path in files if not path.startswith('ledger/ledger/') or path.startswith('ledger/ledger/static/')]
    files += [path for path in sorted(candidate_code)
              if hashlib.sha256(git('show',version+':'+path)).hexdigest()!=baseline_files.get(path)]
# Runtime source layouts must be complete even when the previous production
# installer never shipped these unchanged repository packages.
files += git('ls-tree','-r','--name-only',version,'--',*runtime_roots).decode().splitlines()
manifest, contents = [], {}
# Resolve every committed object before any output is published.
required_helpers={'ledger/ledger/release_gate.py','ledger/ledger/snapshot_store.py',
    'ledger/ledger/storage_integrity.py','ledger/ledger/run_artifacts.py','tools/verify-ledger-release.py',
    'tools/bootstrap-ledger-agent.py','tools/ledger-auth.py','tools/ledger-backup.py','tools/ledger_acceptance_inputs.py'}
files = sorted({p for p in files if not p.startswith('ledger/ledger/static/') and '/tests/' not in p} | required_helpers)
for path in [*files, 'VERSION','RELEASE-ACCEPTANCE.json', *('ledger/ledger/static/'+item['path'] for item in frontend)]:
    if path.startswith('ledger/ledger/static/'):
        content = (Path(repo)/path).read_bytes()
        expected = next(item['sha256'] for item in frontend if path=='ledger/ledger/static/'+item['path'])
        if hashlib.sha256(content).hexdigest()!=expected:raise SystemExit('Frontend changed while packaging')
    else:
        content = ((version+'\n').encode() if path=='VERSION' else receipt_bytes if path=='RELEASE-ACCEPTANCE.json'
                   else git('show',version+':'+path))
    try:before = (base+'\n').encode() if path=='VERSION' else git('show',base+':'+path)
    except subprocess.CalledProcessError:before=None
    before_sha=hashlib.sha256(before).hexdigest() if before is not None and path!='VERSION' else ''
    is_new=before is None
    if baseline_files is not None and path.startswith('ledger/ledger/') and not path.startswith('ledger/ledger/static/'):
        before_sha=baseline_files.get(path,'');is_new=path not in baseline_files
    contents[path]=content
    manifest.append(dict(path=path,sha256=hashlib.sha256(content).hexdigest(),
        before_sha256=before_sha,is_new=is_new,allow_absent=path.startswith(('ops/ledger_sentinel/','agent-worker/','host-agent/'))))
target = Path(output).resolve()
if target.exists():raise SystemExit('Release output already exists; never overwrite it')
descriptor, name = tempfile.mkstemp(prefix='.ledger-package-',suffix='.zip',dir=target.parent)
os.close(descriptor)
temporary=Path(name)
try:
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
        for path,content in contents.items():archive.writestr(path,content)
        archive.writestr('acceptance-receipt.json',receipt_bytes)
        archive.writestr('manifest.json',json.dumps(dict(base=base,version=version,files=manifest,
            retained_old_static_assets=[p for p in removed if p.startswith('ledger/ledger/static/')],
            production_base_manifest_sha256=file_sha(args.base_manifest) if args.base_manifest else None,
            production_base_files=[{'path':path,'sha256':sha} for path,sha in sorted(baseline_files.items())] if baseline_files else [],
            acceptance_receipt_sha=receipt['receipt_sha'],model_sha=receipt['model_sha'])))
    # Same-directory hard link is atomic and refuses a concurrently created target.
    os.link(temporary,target)
finally:
    temporary.unlink(missing_ok=True)
print(json.dumps(dict(version=version, files=len(manifest), bytes=Path(output).stat().st_size, zip=output,
                      sha256=file_sha(Path(output)))))
