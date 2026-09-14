"""Lossless, content-addressed storage for immutable calculation evidence."""
from __future__ import annotations
import hashlib
import json
import re
import zlib

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_blob (
 sha TEXT PRIMARY KEY, codec TEXT NOT NULL, raw_bytes INTEGER NOT NULL,
 payload BLOB NOT NULL
);
CREATE TRIGGER IF NOT EXISTS evidence_blob_no_update BEFORE UPDATE ON evidence_blob
 BEGIN SELECT RAISE(ABORT,'calculation evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS evidence_blob_no_delete BEFORE DELETE ON evidence_blob
 BEGIN SELECT RAISE(ABORT,'calculation evidence is immutable'); END;
"""
PREFIX = '{"$ledger_snapshot_v1":'


def reference_sha(text: str) -> str | None:
    if not text.startswith(PREFIX):
        return None
    value=json.loads(text)
    sha=value.get('$ledger_snapshot_v1')
    if set(value)!={'$ledger_snapshot_v1'} or not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):
        raise ValueError('Invalid calculation evidence reference')
    return sha


def resolve(conn, text: str) -> str:
    sha=reference_sha(text)
    if sha is None:return text
    row=conn.execute('SELECT codec,raw_bytes,payload FROM evidence_blob WHERE sha=?',(sha,)).fetchone()
    if row is None:raise ValueError('Calculation evidence is missing: '+sha)
    codec,size,payload=row
    if codec!='zlib' or size<0 or size>256*1024*1024:raise ValueError('Invalid calculation evidence encoding')
    decoder=zlib.decompressobj()
    raw=decoder.decompress(payload,size+1)
    if len(raw)!=size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('Calculation evidence length mismatch')
    if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('Calculation evidence digest mismatch')
    return raw.decode('utf-8')


def put(conn, text: str) -> str:
    existing=reference_sha(text)
    if existing:
        resolve(conn,text)
        return text
    raw=text.encode('utf-8')
    if len(raw)>256*1024*1024:raise ValueError('Calculation evidence exceeds size limit')
    sha=hashlib.sha256(raw).hexdigest()
    if not conn.execute('SELECT 1 FROM evidence_blob WHERE sha=?',(sha,)).fetchone():
        conn.execute('INSERT INTO evidence_blob VALUES(?,?,?,?)',(sha,'zlib',len(raw),zlib.compress(raw,6)))
    return json.dumps({'$ledger_snapshot_v1':sha},separators=(',',':'))
