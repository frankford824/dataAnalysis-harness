"""Explicit financial context for unit snapshots and preexisting database fixtures.

These helpers never waive a production check. Legacy compatibility tests first
assert that the current close rejects their incomplete input, then load the
already-closed database state that the read-side compatibility contract covers.
"""
import hashlib
import json
from pathlib import Path

from ledger.model.schema import Overhead
from ledger.storage_integrity import verified
from ledger.workspace import _now


def monthly_model(model,store_id,period,amount=0):
    return company_model(model,(store_id,),period,amount)


def company_model(model,store_ids,period,amount=0):
    return model.model_copy(update={'stores':tuple(model.store(sid) for sid in store_ids),
        'commission':tuple(rule for rule in model.commission if rule.store in store_ids),
        'statement':tuple(node.model_copy(update={'headline':'revenue'}) if node.id=='n_receipt' else node
                          for node in model.statement),
        'overheads':tuple(o for o in model.overheads if o.period!=period)+(Overhead(period=period,amount=amount),)})


def preexisting_closed(ws,run_id,*,note,ignored=(),labor_cut=None):
    row=ws.conn.execute('SELECT * FROM run WHERE id=?',(run_id,)).fetchone()
    if row is None:raise ValueError('Preexisting fixture requires a concrete stored run')
    payload=json.loads(row['result'])
    receipt={'kind':'preexisting_database_unit_fixture','run_id':run_id,'store_id':row['store_id'],
        'period':row['period'],'result_sha256':hashlib.sha256(row['result'].encode()).hexdigest(),
        'note':note,'old_ignored_blockers':list(ignored),'old_labor_cut':labor_cut,
        'not_a_current_close_authorization':True}
    raw=json.dumps(receipt,ensure_ascii=False,sort_keys=True).encode();sha=hashlib.sha256(raw).hexdigest()
    source=ws.root/'preexisting-test-receipts'/f'{sha}.json';source.parent.mkdir(parents=True,exist_ok=True)
    source.write_bytes(raw)
    if ws.facts_path(run_id).is_file() and not verified(ws.facts_path(run_id)):
        raise ValueError('Preexisting unit fixtures cannot bless unverified or corrupt evidence')
    with ws.transaction() as db:
        db.execute("UPDATE period SET state='closed',run_id=?,changed_at=?,by=?,note=? WHERE store_id=? AND period=?",
            (run_id,_now(),'fixture:legacy-operator',note,row['store_id'],row['period']))
        if labor_cut is not None:
            db.execute('INSERT INTO run_labor VALUES(?,?,?,?,?,?)',
                (run_id,row['store_id'],row['period'],str(labor_cut),_now(),'fixture:legacy-operator'))
        db.execute('INSERT INTO config_log(at,by,kind,summary,before_json,after_json) VALUES(?,?,?,?,?,?)',
            (_now(),'fixture:legacy-operator','preexisting-database-fixture',f"{row['store_id']} {row['period']}",'{}',
             json.dumps({'receipt_path':str(source),'receipt_sha256':sha},ensure_ascii=False)))
        if ignored:
            db.execute('INSERT INTO config_log(at,by,kind,summary,before_json,after_json) VALUES(?,?,?,?,?,?)',
                (_now(),'fixture:legacy-operator','manual-close',f"人工结账 {row['store_id']} {row['period']}",
                 json.dumps({'run_id':run_id,'blockers':payload.get('findings') or []},ensure_ascii=False),
                 json.dumps({'ignored':list(ignored),'reason':note,'source_receipt_sha256':sha},ensure_ascii=False)))
    return receipt
