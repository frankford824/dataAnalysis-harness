"""Guarded, sequential refresh through the normal API; never reopens a period.

Plan/receipt files are diagnostic artifacts. Each refresh must preserve the
previously closed run pointers; API failures are recorded, not hidden or retried
as database writes. No payout, cost, source file or manual decision is edited.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
p = argparse.ArgumentParser()
p.add_argument('mode', choices=('plan', 'apply'))
p.add_argument('--root', required=True)
p.add_argument('--receipt', required=True)
p.add_argument('--start', default='2026-06')
p.add_argument('--end', default='2026-09')
p.add_argument('--version')
args = p.parse_args()
root, receipt = Path(args.root), Path(args.receipt)


def db():
    return sqlite3.connect((root / 'workspace.db').resolve().as_uri() + '?mode=ro', uri=True)


def request(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000' + path, data=data,
            headers={'Content-Type': 'application/json'}), timeout=7200) as response:
        return json.load(response)


def closed():
    with db() as conn:
        return {f'{s}:{m}': r for s, m, r in conn.execute("select store_id,period,run_id from period where state='closed'")}


if args.mode == 'plan':
    with db() as conn:
        stores = [r[0] for r in conn.execute("""select distinct r.store_id from run r
            left join period p on p.store_id=r.store_id and p.period=r.period
            where r.period between ? and ? and coalesce(p.state,'open')!='closed' order by r.store_id""", (args.start, args.end))]
        before = {}
        for sid in stores:
            records = conn.execute("select id,period,result from run where id in (select max(id) from run where store_id=? and period between ? and ? group by period)",
                (sid, args.start, args.end)).fetchall()
            before[sid] = [dict(run=r, period=m, statement=json.loads(j).get('statement')) for r, m, j in records]
    plan = dict(at=datetime.now(timezone.utc).isoformat(), version=request('/api/version'), stores=stores,
                start=args.start, end=args.end, closed=closed(), before=before)
    with receipt.open('x', encoding='utf-8') as stream:
        json.dump(plan, stream, ensure_ascii=False, indent=2)
    print(json.dumps(dict(stores=len(stores), receipt=str(receipt))))
else:
    if not args.version:
        raise ValueError('--version is required for applying a refresh')
    plan = json.loads(receipt.read_text(encoding='utf-8'))
    journal = receipt.with_suffix('.results.jsonl')
    if journal.exists():
        raise ValueError('Existing refresh journal: inspect results before retrying')
    targets = sorted(plan['stores'], key=lambda s: (s != 'taobao_msy387nx', s))
    for index, sid in enumerate(targets, 1):
        if request('/api/version')['version'] != args.version:
            raise ValueError('Production version changed, refresh stopped')
        frozen_before = closed()
        started = time.perf_counter()
        print(json.dumps(dict(store=sid, index=index, total=len(targets), state='running')), flush=True)
        try:
            result = request(f'/api/stores/{sid}/recompute', {})
            periods = [row for row in result['periods'] if plan['start'] <= row['period'] <= plan['end']]
            output = dict(store=sid, seconds=round(time.perf_counter() - started, 2), failure=result.get('failure'),
                          unknown=result.get('unknown_tables'), periods=periods)
        except Exception as exc:
            output = dict(store=sid, seconds=round(time.perf_counter() - started, 2), failure=str(exc))
        frozen_after = closed()
        changed = {key: [value, frozen_after.get(key)] for key, value in frozen_before.items() if frozen_after.get(key) != value}
        output['closed_changes'] = changed
        with journal.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(output, ensure_ascii=False) + '\n')
        print(json.dumps(dict(store=sid, seconds=output['seconds'], failure=output.get('failure'),
            periods=len(output.get('periods', [])), unknown=len(output.get('unknown') or []), closed_changes=changed), ensure_ascii=False), flush=True)
        if changed:
            raise ValueError('Closed pointer changed during refresh; inspect concurrent audit first')
