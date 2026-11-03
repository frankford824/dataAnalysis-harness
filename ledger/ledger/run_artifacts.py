"""Run-directory artifact bundles: policy-gated publication and verified reads.

Existing flat artifacts are never moved. A NAS outage during directory setup
keeps the established local-write/archive path. An outage after publication of
the directory is an explicit evidence failure; it must not become a zero cost.
Readers accept a bundle only through its original directory registration,
manifest and per-file seal.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import re
import uuid
from contextlib import closing
from pathlib import Path


def policy(root):
    try:
        value=json.loads((Path(root)/'storage-policy.json').read_text(encoding='utf-8-sig'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


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


def path(root,run_id):
    folder=Path(root)/'runs'/str(run_id)
    # A missing NAS must remain missing evidence, never a fallback flat file.
    if folder.is_symlink() or folder.is_dir():return folder/(str(run_id)+'.parquet')
    return Path(root)/'runs'/(str(run_id)+'.parquet')


def finalize(ws, run_id):
    """Register one completed directory and its checksum manifest, never each file."""
    import time
    from .storage_maintenance import manifest
    settings = policy(ws.root)
    folder = ws.root / 'runs' / str(run_id)
    # Legacy directory links keep their original registration; only bundles
    # published under the enabled policy are registered here.
    if not (folder.is_symlink() and settings.get('archive_all_completed') is True
            and settings.get('run_directory_bundles') is True):
        return
    archive = Path(settings['archive_root']) / 'run-bundles'
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


def registered_file(root,source,expected_sha):
    """Require the original directory registration, manifest and file identity."""
    root=Path(root);source=Path(source)
    try:
        relative=source.relative_to(root)
        if len(relative.parts)!=3 or relative.parts[0]!='runs' or not relative.parts[1].isdigit():return False
        folder=root/'runs'/relative.parts[1]
        if not (folder.is_symlink() or getattr(folder,'is_junction',lambda:False)()):return False
        database=root/'storage.db'
        if not database.is_file():return False
        with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            record=conn.execute('SELECT target,sha,state FROM artifact_archive WHERE source=?',
                ('runs/'+relative.parts[1],)).fetchone()
        if not record or record[2] not in {'copied','archived'}:return False
        target=folder.resolve()
        if not Path(record[0]).is_absolute() or Path(record[0]).resolve()!=target:return False
        if not source.resolve().is_relative_to(target) or source.resolve().parent!=target:return False
        raw=(folder/'manifest.json').read_bytes()
        if hashlib.sha256(raw).hexdigest()!=record[1]:return False
        manifest=json.loads(raw)
        if manifest.get('run_id')!=int(relative.parts[1]) or not isinstance(manifest.get('files'),list):return False
        matched=[item for item in manifest['files'] if isinstance(item,dict) and item.get('name')==source.name]
        return bool(len(matched)==1 and matched[0].get('sha256')==expected_sha
            and matched[0].get('bytes')==source.stat().st_size)
    except (OSError,ValueError,TypeError,sqlite3.Error):return False


def verified_path(root,run_id,suffix='.parquet',expected_sha=None):
    """Pure stdlib accessor for release/readback tools; never creates paths.

    Returns None for missing, unsealed, unregistered outside targets or changed
    bytes. A supplied first-manifest SHA remains authoritative over a new seal.
    """
    allowed={'.parquet','.pricing.parquet','.pricing-raw.parquet','.coverage.parquet',
        '.allocation.parquet','.operations.parquet','.order-issues.parquet'}
    if suffix not in allowed or isinstance(run_id,bool) or not str(run_id).isdigit() or int(run_id)<=0:
        return None
    root=Path(root);source=path(root,int(run_id)).with_suffix(suffix)
    try:
        sha=source.with_name(source.name+'.sha256').read_text(encoding='ascii').strip()
        if not re.fullmatch('[0-9a-f]{64}',sha) or (expected_sha is not None and sha!=expected_sha):return None
        if not source.resolve().is_relative_to(root.resolve()):
            registered=registered_file(root,source,sha)
            if not registered and source.is_symlink() and (root/'storage.db').is_file():
                with closing(sqlite3.connect((root/'storage.db').resolve().as_uri()+'?mode=ro',uri=True)) as conn:
                    row=conn.execute('SELECT target,sha,state FROM artifact_archive WHERE source=?',
                        (str(source.relative_to(root)),)).fetchone()
                registered=bool(row and row[1]==sha and row[2] in {'copied','archived'} and Path(row[0]).resolve()==source.resolve())
            if not registered:return None
        value=hashlib.sha256()
        with source.open('rb') as stream:
            for block in iter(lambda:stream.read(4*1024*1024),b''):value.update(block)
        return source if value.hexdigest()==sha else None
    except (OSError,ValueError,sqlite3.Error):return None
