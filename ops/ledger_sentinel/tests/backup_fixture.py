"""Real four-role SQLite/archive restore proofs over explicitly synthetic data."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import sqlite3


def create_verified_backup(root,version):
    """Call inside the backup step with a NEW directory, never a production path."""
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    home=root/'source';(home/'commission').mkdir(parents=True)
    with sqlite3.connect(home/'workspace.db') as db:
        db.executescript('''
        CREATE TABLE period(store_id TEXT,period TEXT,run_id INTEGER,state TEXT);
        CREATE TABLE run(id INTEGER PRIMARY KEY,result TEXT);
        CREATE TABLE manual_finance(run_id INTEGER PRIMARY KEY,result_json TEXT);
        CREATE TABLE run_labor(run_id INTEGER PRIMARY KEY,amount TEXT);
        INSERT INTO period VALUES('synthetic-store','2030-01',1,'closed');
        INSERT INTO run_labor VALUES(1,'2.00');
        ''')
        payload=json.dumps({'fixture_kind':'synthetic_deployment_backup',
            'statement':[{'id':'n_receipt','value':100,'available':True}]})
        db.execute('INSERT INTO run VALUES(1,?)',(payload,))
        db.execute('INSERT INTO manual_finance VALUES(1,?)',(payload,))
    with sqlite3.connect(home/'commission/registry.db') as db:
        db.executescript('''
        CREATE TABLE payout_confirmation(id TEXT PRIMARY KEY,confirmed_total TEXT);
        CREATE TABLE settlement(id TEXT PRIMARY KEY,total TEXT);
        CREATE TABLE profit_exclusion(id TEXT PRIMARY KEY,included_profit TEXT);
        INSERT INTO payout_confirmation VALUES('synthetic-confirmation','3.00');
        INSERT INTO settlement VALUES('synthetic-settlement','3.00');
        INSERT INTO profit_exclusion VALUES('synthetic-exclusion','20.00');
        ''')
    app=root/'application';(app/'ledger/ledger').mkdir(parents=True)
    (app/'ledger/ledger/api.py').write_text('# synthetic application backup fixture\n')
    (app/'VERSION').write_text(version+'\n')
    model=root/'model';model.mkdir();(model/'stores.yaml').write_text('stores: []\n')
    repo=Path(__file__).resolve().parents[3]
    spec=importlib.util.spec_from_file_location('sentinel_real_backup_fixture',repo/'tools/ledger-backup.py')
    tool=importlib.util.module_from_spec(spec);spec.loader.exec_module(tool)
    target=root/'backup';result=tool.create(home,app,model,target,version)
    if result.get('passed') is not True:raise AssertionError('Synthetic restore proof did not verify')
    return target/'manifest.json'
