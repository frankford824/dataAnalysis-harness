"""Lossless, content-addressed storage for immutable calculation evidence."""
from __future__ import annotations
import hashlib
import json
import re
import zlib
import os
import sqlite3
from pathlib import Path

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
        # Independent writer connections can both observe a missing shared
        # blob. The immutable hash key makes the insert idempotent after wait.
        conn.execute('INSERT OR IGNORE INTO evidence_blob VALUES(?,?,?,?)',(sha,'zlib',len(raw),zlib.compress(raw,6)))
    return json.dumps({'$ledger_snapshot_v1':sha},separators=(',',':'))


def compact_enabled(root) -> bool:
    """Reader support is unconditional; compact writes are enabled after rollout."""
    configured = os.getenv('LEDGER_COMPACT_SNAPSHOTS')
    if configured is not None:
        return configured == '1'
    try:
        policy = json.loads((Path(root) / 'storage-policy.json').read_text(encoding='utf-8-sig'))
        return isinstance(policy, dict) and policy.get('compact_snapshots') is True
    except (OSError, ValueError):
        return False


def store(conn, text, root):
    # Small scalars stay inline. Original UTF-8 bytes, including formatting and
    # calculation IDs, are retained; no money or evidence field is discarded.
    if isinstance(text, str) and len(text) >= 1024 and compact_enabled(root):
        return put(conn, text)
    return text


def install_reader(conn):
    """Expand selected reference cells and explicitly wrapped SQL JSON inputs.

    Never cache decoded evidence across transactions: a rollback must not make
    a missing blob appear valid. Every expansion verifies length and SHA-256.
    """
    def expand(value):
        return resolve(conn, value) if isinstance(value, str) and value.startswith(PREFIX) else value

    json_columns = {'result', 'shown_result', 'model_json', 'rules_json', 'summary_json',
                    'overview_json', 'commission_json', 'products_slim_json',
                    'products_json', 'statement_json', 'manual_cost_json'}

    def row(cursor, values):
        # User-supplied names/notes are free text even if they happen to start
        # with the reserved marker. Only internal JSON cells are references.
        return sqlite3.Row(cursor, tuple(
            expand(value) if column[0] in json_columns else value
            for column, value in zip(cursor.description, values)))

    conn.row_factory = row
    conn.create_function('snapshot_json', 1, expand, deterministic=True)
