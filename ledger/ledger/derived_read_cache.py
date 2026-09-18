"""Checksummed, bounded, rebuildable cache; never a financial source of truth."""
import hashlib
import json
import sqlite3


def get(registry, key):
    try:
        with registry.connect() as conn:
            row=conn.execute('SELECT value,sha FROM derived_read_cache WHERE key=?',(key,)).fetchone()
        if row and hashlib.sha256(row['value'].encode()).hexdigest()==row['sha']:
            return json.loads(row['value'])
    except (sqlite3.OperationalError, ValueError):
        pass
    return None


def put(registry, key, value):
    text=json.dumps(value,ensure_ascii=False,separators=(',',':'))
    try:
        with registry.connect() as conn:
            conn.execute('PRAGMA busy_timeout=0')
            conn.execute('INSERT OR REPLACE INTO derived_read_cache(key,value,sha) VALUES(?,?,?)',
                         (key,text,hashlib.sha256(text.encode()).hexdigest()))
            if conn.execute('SELECT count(*) FROM derived_read_cache').fetchone()[0]>8192:
                conn.execute('DELETE FROM derived_read_cache WHERE rowid IN (SELECT rowid FROM derived_read_cache ORDER BY rowid LIMIT 1024)')
    except sqlite3.OperationalError:
        pass  # Read-only callers and optional-cache contention still get computed results.
