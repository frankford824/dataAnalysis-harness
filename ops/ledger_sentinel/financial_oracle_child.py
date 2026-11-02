"""Pinned execution transport: never receives expected values or writes authority."""
from contextlib import closing
import json
import hashlib
import re
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
    # Financial run proofs also bind immutable originals and admitted source
    # evidence. Export precisely those dependencies instead of dropping every
    # artifact outside runs/. The host independently rechecks every SHA.
    root=session.root.resolve()
    manifests=[json.loads(row[0]) for row in session.ws.conn.execute('SELECT manifest_json FROM automation_run_proof')]
    tables={r[0] for r in session.ws.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'period_cost_input' in tables:
        manifests.extend(json.loads(row[0]) for row in session.ws.conn.execute('SELECT evidence_json FROM period_cost_input'))
    for manifest in manifests:
        for artifact in manifest.get('artifacts',[]):
            relative=Path(str(artifact.get('path','')));expected=artifact.get('sha')
            if (relative.is_absolute() or '..' in relative.parts or not relative.parts
                or not re.fullmatch('[0-9a-f]{64}',str(expected or ''))):
                raise ValueError('Invalid financial artifact reference')
            source=session.root/relative;destination=target/relative
            if (source.is_symlink() or not source.resolve().is_relative_to(root) or not source.is_file()
                or hashlib.sha256(source.read_bytes()).hexdigest()!=expected):
                raise ValueError('Financial source artifact missing or changed')
            if destination.exists():
                if destination.is_symlink() or hashlib.sha256(destination.read_bytes()).hexdigest()!=expected:
                    raise ValueError('Financial export artifact collision')
            else:
                destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(source,destination)
    # This is a diagnostic RPC, cross-checked against SQLite by the host. Neither
    # this JSON nor a child exit code grants acceptance.
    (target/'snapshot.json').write_text(json.dumps(session.snapshot(), ensure_ascii=False, allow_nan=False))


def main():
    request = json.loads(Path('/request.json').read_text())
    for index, case in enumerate(request['cases']):
        scopes=case.get('publication_scopes')
        if (not isinstance(scopes,list) or not scopes or len(scopes)!=len(set(scopes))
            or any(not isinstance(key,str) or not re.fullmatch(r'[^/]+/\d{4}-(0[1-9]|1[0-2])',key) for key in scopes)):
            raise ValueError('Explicit approved publication scopes are required')
        with Session('/inputs/'+str(index), case['registry'],publication_scopes=scopes) as session:
            if case.get('workspace_decisions'):
                session.restore_workspace_decisions(case['workspace_decisions'], fixture=case['kind']=='service_fixture')
            for step_index, step in enumerate(case['steps']):
                operation = step['operation']
                if operation not in {'recompute','supplement','confirm','replace_scheme','drain_pending','close_approved','restore_approved_manual_finance'}:
                    raise ValueError('Unsupported service operation')
                arguments={k:v for k,v in step.items() if k!='operation'}
                if operation=='restore_approved_manual_finance':
                    if 'fixture' in arguments:raise ValueError('Case cannot override manual source kind')
                    arguments['fixture']=case['kind']=='service_fixture'
                getattr(session, operation)(**arguments)
                export(session, Path('/results')/str(index)/str(step_index))
    Path('/results/transport-completed.json').write_text(json.dumps({'cases':len(request['cases'])}))


if __name__ == '__main__':
    main()
