"""Publish new run artifacts through one directory link, before evidence is ready.

Existing flat artifacts are never moved. A NAS outage during directory setup
keeps the established local-write/archive path. An outage after publication of
the directory is an explicit evidence failure; it must not become a zero cost.
"""
import json
import uuid
from pathlib import Path


def policy(root):
    try:
        value = json.loads((Path(root) / 'storage-policy.json').read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def prepare(ws, run_id):
    settings = policy(ws.root)
    if not (settings.get('archive_all_completed') is True
            and settings.get('run_directory_bundles') is True):
        return
    folder = ws.root / 'runs' / str(run_id)
    flat = ws.root / 'runs' / f'{run_id}.parquet'
    if folder.is_symlink() or folder.exists() or flat.is_symlink() or flat.exists():
        return
    row = ws.conn.execute('select evidence_ready from run where id=?', (run_id,)).fetchone()
    if row is None or row['evidence_ready']:
        raise ValueError('Only an unpublished run can create an artifact bundle')
    archive = Path(settings.get('archive_root') or '')
    if not archive.is_absolute():
        raise ValueError('Artifact archive must be an absolute path')
    target = archive / 'run-bundles' / (str(run_id) + '-' + uuid.uuid4().hex)
    try:
        resolved_archive = archive.resolve()
        resolved_root = ws.root.resolve()
        if resolved_archive.is_relative_to(resolved_root) or resolved_root.is_relative_to(resolved_archive):
            raise ValueError('Artifact archive overlaps live workspace')
        # mkdir(exist_ok=False) gives each attempt an isolated namespace. The
        # only local link is created before any run can become evidence-ready.
        target.mkdir(parents=True)
        folder.parent.mkdir(parents=True, exist_ok=True)
        folder.symlink_to(target, target_is_directory=True)
    except OSError:
        # Never remove a nonempty NAS directory or an existing local path.
        try:
            target.rmdir()
        except OSError:
            pass
        return


def path(root, run_id):
    folder = Path(root) / 'runs' / str(run_id)
    # is_symlink also recognizes an unavailable NAS target. Do not silently
    # redirect a historical lookup to an unrelated/new local flat file.
    if folder.is_symlink() or folder.is_dir():
        return folder / f'{run_id}.parquet'
    return Path(root) / 'runs' / f'{run_id}.parquet'


def finalize(ws, run_id):
    """Register one completed directory and its checksum manifest, never each file."""
    import hashlib
    import re
    import time
    from contextlib import closing
    from .storage_maintenance import manifest
    folder = ws.root / 'runs' / str(run_id)
    if not folder.is_symlink():
        return
    archive = Path(policy(ws.root)['archive_root']) / 'run-bundles'
    target = folder.resolve()
    if not target.is_relative_to(archive.resolve()):
        raise ValueError('Run bundle target is outside configured archive')
    files = []
    for artifact in sorted(folder.glob('*.parquet')):
        sha = artifact.with_name(artifact.name + '.sha256').read_text(encoding='ascii').strip()
        if not re.fullmatch('[0-9a-f]{64}', sha):
            raise ValueError('Run bundle checksum seal is invalid')
        files.append({'name': artifact.name, 'sha256': sha, 'bytes': artifact.stat().st_size})
    raw = json.dumps({'run_id': run_id, 'files': files}, sort_keys=True).encode('utf-8')
    temporary = folder / ('.manifest-' + uuid.uuid4().hex + '.tmp')
    temporary.write_bytes(raw)
    temporary.replace(folder / 'manifest.json')
    with closing(manifest(ws.root)) as conn, conn:
        conn.execute('INSERT OR REPLACE INTO artifact_archive VALUES(?,?,?,?,?,?)',
                     (f'runs/{run_id}', hashlib.sha256(raw).hexdigest(),
                      sum(item['bytes'] for item in files), str(target), 'archived', str(time.time())))
