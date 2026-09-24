"""Capture/verify immutable closed runs and confirmed payouts around a release.

Business databases are read-only. Only the supplied diagnostic receipt is written.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
mode, root_arg, receipt_arg = sys.argv[1:4]
root, receipt = Path(root_arg), Path(receipt_arg)


def digest(row):
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, default=str).encode()).hexdigest()


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def snapshot():
    with readonly(root / 'workspace.db') as db:
        closed = {f'{s}:{p}': {'run': r, 'result_sha': digest(payload)} for s, p, r, payload in db.execute(
            "select p.store_id,p.period,p.run_id,r.result from period p join run r on p.run_id=r.id where p.state='closed'")}
    with readonly(root / 'commission/registry.db') as db:
        immutable = {table: {row[0]: digest(row) for row in db.execute(f'SELECT * FROM {table}')}
                     for table in ('payout_confirmation', 'settlement', 'profit_exclusion')}
    with urllib.request.urlopen('http://127.0.0.1:8000/api/version', timeout=20) as response:
        version = json.load(response)
    return dict(version=version, closed=closed, immutable=immutable)


current = snapshot()
if mode == 'capture':
    with receipt.open('x', encoding='utf-8') as stream:
        json.dump(current, stream, ensure_ascii=False, indent=2)
    print(json.dumps(dict(version=current['version'], closed=len(current['closed']),
        immutable={k: len(v) for k, v in current['immutable'].items()}, receipt=str(receipt))))
elif mode == 'verify':
    baseline = json.loads(receipt.read_text(encoding='utf-8'))
    changes = {k: [v, current['closed'].get(k)] for k, v in baseline['closed'].items()
               if v != current['closed'].get(k)}
    records = {table: [key for key, value in previous.items() if current['immutable'][table].get(key) != value]
               for table, previous in baseline['immutable'].items()}
    print(json.dumps(dict(version=current['version'], changed_closed=changes, changed_immutable=records,
        new_records={k: len(v) - len(baseline['immutable'][k]) for k,v in current['immutable'].items()}), ensure_ascii=False))
    assert not changes and not any(records.values()), 'Historical record changed; inspect audit before proceeding'
else:
    raise ValueError('capture or verify required')
