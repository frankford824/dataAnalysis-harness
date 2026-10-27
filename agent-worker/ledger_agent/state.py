"""Local evidence vault, model usage and supplier settlements; no monthly money cap."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path


class State:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root
        self.path = root / 'agent-audit.db'
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,input_sha TEXT NOT NULL,payload TEXT NOT NULL,sha TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS model_call(id TEXT PRIMARY KEY,case_id TEXT NOT NULL,model TEXT NOT NULL,
              input_tokens INTEGER,output_tokens INTEGER,cache_tokens INTEGER,estimated_cost TEXT,price_version TEXT,currency TEXT,
              request_sha TEXT NOT NULL,state TEXT NOT NULL,at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS supplier_settlement(id TEXT PRIMARY KEY,call_id TEXT NOT NULL REFERENCES model_call(id),
              amount TEXT NOT NULL,currency TEXT NOT NULL,reference TEXT NOT NULL UNIQUE,at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS outbound(id INTEGER PRIMARY KEY,case_id TEXT NOT NULL,payload TEXT NOT NULL,sha TEXT NOT NULL);
            ''')
            for table in ('evidence','model_call','supplier_settlement','outbound'):
                for operation in ('UPDATE','DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable agent audit'); END")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            self.path.chmod(0o600)
            with db:
                yield db
        finally:
            db.close()

    def evidence(self, payload, input_sha):
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        identifier = 'ev_' + hashlib.sha256((input_sha + ':' + raw).encode()).hexdigest()[:32]
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)',
                       (identifier,input_sha,raw,hashlib.sha256(raw.encode()).hexdigest()))
        return identifier

    def read_evidence(self, identifier, input_sha):
        with self.connect() as db:
            row = db.execute('SELECT * FROM evidence WHERE id=? AND input_sha=?',(identifier,input_sha)).fetchone()
        if row is None or hashlib.sha256(row['payload'].encode()).hexdigest() != row['sha']:
            raise ValueError('Unknown, changed or stale evidence')
        return json.loads(row['payload'])

    def outbound(self, case_id, payload):
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        with self.connect() as db:
            db.execute('INSERT INTO outbound(case_id,payload,sha) VALUES(?,?,?)',
                       (case_id,raw,hashlib.sha256(raw.encode()).hexdigest()))
        return hashlib.sha256(raw.encode()).hexdigest()

    def usage(self, case_id, model, usage, request_sha, price=None, status='complete'):
        identifier = uuid.uuid4().hex
        input_tokens = getattr(usage, 'input_tokens', None)
        output_tokens = getattr(usage, 'output_tokens', None)
        cache_tokens = getattr(usage, 'cache_input_tokens', None)
        estimate = None
        if price and price.get('currency') and input_tokens is not None and output_tokens is not None:
            # Cache prices must be explicit; unknown cache pricing is not a zero charge.
            cached = cache_tokens or 0
            if not cached or price.get('cached_input_per_million') is not None:
                estimate = str((Decimal(input_tokens-cached)*Decimal(str(price['input_per_million']))
                    + Decimal(cached)*Decimal(str(price.get('cached_input_per_million',0)))
                    + Decimal(output_tokens)*Decimal(str(price['output_per_million'])))/Decimal(1000000))
        with self.connect() as db:
            db.execute('INSERT INTO model_call(id,case_id,model,input_tokens,output_tokens,cache_tokens,estimated_cost,price_version,currency,request_sha,state) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,case_id,model,input_tokens,output_tokens,cache_tokens,estimate,
                 price.get('version') if price else None,price.get('currency') if price else None,request_sha,status))
        return identifier

    def settle(self, call_id, amount, currency, reference):
        amount = Decimal(str(amount))
        if not amount.is_finite() or amount < 0 or not reference or not currency:
            raise ValueError('Invalid supplier settlement')
        with self.connect() as db:
            db.execute('INSERT INTO supplier_settlement(id,call_id,amount,currency,reference) VALUES(?,?,?,?,?)',
                       (uuid.uuid4().hex,call_id,str(amount),currency,reference))

    def summary(self):
        with self.connect() as db:
            calls = [dict(row) for row in db.execute('SELECT * FROM model_call')]
            settled = [dict(row) for row in db.execute('SELECT * FROM supplier_settlement')]
        currencies = {r['currency'] for r in calls if r['estimated_cost'] is not None}
        return {'calls':len(calls),'input_tokens':sum(r['input_tokens'] or 0 for r in calls),
                'output_tokens':sum(r['output_tokens'] or 0 for r in calls),
                'usage_unknown_calls':sum(r['input_tokens'] is None or r['output_tokens'] is None for r in calls),
                'estimated_cost':str(sum((Decimal(r['estimated_cost']) for r in calls if r['estimated_cost'] is not None),Decimal(0))) if len(currencies)==1 else None,
                'estimated_by_currency':{currency:str(sum(Decimal(r['estimated_cost']) for r in calls if r['currency']==currency and r['estimated_cost'] is not None)) for currency in currencies},
                'price_unknown_calls':sum(r['estimated_cost'] is None for r in calls),
                'supplier_settled':{c:str(sum(Decimal(r['amount']) for r in settled if r['currency']==c)) for c in {r['currency'] for r in settled}},
                'unsettled_calls':sum(r['id'] not in {s['call_id'] for s in settled} for r in calls)}
