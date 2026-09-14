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
