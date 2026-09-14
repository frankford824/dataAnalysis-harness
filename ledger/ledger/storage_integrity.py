"""Checksums for safely reusing unchanged calculation artifacts."""
import hashlib
import re
import uuid


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''):h.update(block)
    return h.hexdigest()


def seal(path):
    target=path.with_name(path.name+'.sha256')
    temp=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(digest(path)+'\n',encoding='ascii')
    temp.replace(target)


def verified(path):
    try:
        expected=path.with_name(path.name+'.sha256').read_text(encoding='ascii').strip()
        return bool(re.fullmatch('[0-9a-f]{64}',expected)) and digest(path)==expected
    except (OSError,ValueError):return False


def comparable(payload):
    value=dict(payload)
    if isinstance(value.get('commission'),dict):
        value['commission']=dict(value['commission'])
        value['commission'].pop('calculation_id',None)
    return value



def registered_archive_link(root, source, expected_sha):
    """Accept an outside target only for a checksum-bound local archive record."""
    import sqlite3
    from pathlib import Path
    from contextlib import closing
    try:
        relative=source.relative_to(root)
        if '..' in relative.parts or not source.is_symlink():return False
        db=root/'storage.db'
        if not db.is_file():return False
        with closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as c:
            row=c.execute('SELECT target,sha,state FROM artifact_archive WHERE source=?',(str(relative),)).fetchone()
        return bool(row and row[1]==expected_sha and row[2] in {'copied','archived'}
                    and Path(row[0]).resolve()==source.resolve())
    except (OSError,ValueError,sqlite3.Error):return False
