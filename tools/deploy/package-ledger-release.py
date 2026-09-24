"""Package only committed code and explicitly approved model contracts."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

repo, base, version, output = sys.argv[1:]


def git(*args):
    return subprocess.check_output(['git', '-C', repo, *args], stderr=subprocess.DEVNULL)


for ref in (base, version):
    git('rev-parse', '--verify', ref + '^{commit}')
paths = ('ledger/ledger', 'models/cn-ecommerce/templates.yaml', 'models/cn-ecommerce/sources.yaml')
files = git('diff', '--no-renames', '--name-only', '--diff-filter=AM', base, version,
            '--', *paths).decode().splitlines()
manifest = []
with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
    for path in [*files, 'VERSION']:
        content = (version + '\n').encode() if path == 'VERSION' else git('show', version + ':' + path)
        try:
            before = (base + '\n').encode() if path == 'VERSION' else git('show', base + ':' + path)
        except subprocess.CalledProcessError:
            before = None
        archive.writestr(path, content)
        manifest.append(dict(path=path, sha256=hashlib.sha256(content).hexdigest(),
            before_sha256=hashlib.sha256(before).hexdigest() if before is not None and path != 'VERSION' else '',
            is_new=before is None))
    archive.writestr('manifest.json', json.dumps(dict(base=base, version=version, files=manifest)))
print(json.dumps(dict(version=version, files=len(manifest), bytes=Path(output).stat().st_size, zip=output)))
