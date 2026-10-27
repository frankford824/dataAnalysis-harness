"""Pinned execution transport: never receives expected values or writes authority."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import sys

sys.path.insert(0, '/candidate/ledger')
from ledger.replay_service import Session


def export(session, target):
    if session.ws.conn.in_transaction:
        raise ValueError('Service left an unfinished financial transaction')
    target.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect(target/'workspace.db')) as db:
        session.ws.conn.backup(db)
    with session.registry.connect() as source, closing(sqlite3.connect(target/'registry.db')) as db:
        if source.in_transaction:raise ValueError('Service left an unfinished registry transaction')
        source.backup(db)
    for name in ('runs', 'commission/calculations'):
        folder = session.root/name
        if folder.exists():
            if folder.is_symlink() or any(p.is_symlink() for p in folder.rglob('*')):
                raise ValueError('Nonlocal financial artifact')
            shutil.copytree(folder, target/name)
    # This is a diagnostic RPC, cross-checked against SQLite by the host. Neither
    # this JSON nor a child exit code grants acceptance.
    (target/'snapshot.json').write_text(json.dumps(session.snapshot(), ensure_ascii=False))


def main():
    request = json.loads(Path('/request.json').read_text())
    for index, case in enumerate(request['cases']):
        with Session('/inputs/'+str(index), case['registry']) as session:
            if case.get('workspace_decisions'):
                session.restore_workspace_decisions(case['workspace_decisions'], fixture=case['kind']=='service_fixture')
            for step_index, step in enumerate(case['steps']):
                operation = step['operation']
                if operation not in {'recompute','supplement','confirm','replace_scheme','drain_pending','close_approved'}:
                    raise ValueError('Unsupported service operation')
                getattr(session, operation)(**{k:v for k,v in step.items() if k!='operation'})
                export(session, Path('/results')/str(index)/str(step_index))
    Path('/results/transport-completed.json').write_text(json.dumps({'cases':len(request['cases'])}))


if __name__ == '__main__':
    main()
