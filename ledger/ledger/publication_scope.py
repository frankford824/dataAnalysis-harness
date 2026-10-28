"""Explicit, startup-bound store/month publication authority for automation.

This fences publication, not source collection or in-memory cross-month math.
Service requests cannot choose a scope file or turn an absent scope into all
months. Human/internal manual calls retain their existing financial checks.
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading

FILE_ENV = 'LEDGER_AUTO_PUBLICATION_SCOPE_FILE'
SHA_ENV = 'LEDGER_AUTO_PUBLICATION_SCOPE_SHA256'
SCHEMA = 'ledger_automatic_publication_scope.v1'
_active = contextvars.ContextVar('ledger_publication_scope', default=None)
_lock = threading.RLock()
_startup = {}


class PublicationScopeError(RuntimeError):
    pass


def _require(condition, reason):
    if not condition:
        raise PublicationScopeError(reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _code_identity():
    from .version import assert_code_integrity
    try:
        return assert_code_integrity()['source_code_sha']
    except Exception as exc:
        raise PublicationScopeError('automatic_publication_code_identity_unverified') from exc


def _pairs(rows):
    _require(isinstance(rows, list) and 0 < len(rows) <= 10000, 'automatic_publication_scope_empty_or_unbounded')
    output = set()
    for row in rows:
        _require(isinstance(row, dict) and set(row) == {'store_id', 'period'}, 'automatic_publication_scope_contract')
        sid, period = row['store_id'], row['period']
        _require(isinstance(sid, str) and bool(sid.strip()) and sid == sid.strip()
                 and len(sid) <= 200 and not any(c in sid for c in '*?\x00\r\n/'), 'automatic_publication_store_invalid')
        _require(isinstance(period, str) and re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period)
                 and period[:4] != '0000', 'automatic_publication_period_invalid')
        try:
            date.fromisoformat(period + '-01')
        except ValueError as exc:
            raise PublicationScopeError('automatic_publication_period_invalid') from exc
        _require((sid, period) not in output, 'automatic_publication_scope_duplicate')
        output.add((sid, period))
    return frozenset(output)


@dataclass(frozen=True)
class PublicationScope:
    root: Path
    scopes: frozenset
    source_sha256: str
    approval_reference: str
    startup_source_code_sha: str
    path: Path | None = None
    environment: tuple | None = None
    isolated: bool = False

    @property
    def identity(self):
        return {'schema': SCHEMA, 'source_sha256': self.source_sha256,
                'approval_reference': self.approval_reference,
                'startup_source_code_sha': self.startup_source_code_sha}

    def allows(self, store_id, period):
        return (store_id, period) in self.scopes

    def periods_for_store(self, store_id):
        return frozenset(period for sid, period in self.scopes if sid == store_id)

    def verify(self, workspace_root=None, model=None):
        root = Path(workspace_root).resolve() if workspace_root is not None else self.root
        _require(root == self.root, 'automatic_publication_workspace_changed')
        _require(_code_identity() == self.startup_source_code_sha, 'automatic_publication_code_changed')
        if self.path is not None:
            _require(self.path.is_file() and not self.path.is_symlink()
                     and self.path.stat().st_size <= 2 * 1024 * 1024, 'automatic_publication_scope_missing_or_invalid')
            _require(_sha(self.path.read_bytes()) == self.source_sha256, 'automatic_publication_scope_changed')
        if self.environment is not None:
            _require((os.environ.get(FILE_ENV, ''), os.environ.get(SHA_ENV, '')) == self.environment,
                     'automatic_publication_startup_configuration_changed')
            _require(not Path(self.environment[0]).is_symlink() and Path(self.environment[0]).resolve() == self.path,
                     'automatic_publication_startup_configuration_changed')
        if self.isolated:
            temporary = Path(tempfile.gettempdir()).resolve()
            _require(root != temporary and root.is_relative_to(temporary), 'isolated_publication_scope_outside_temporary_workspace')
        if model is not None:
            _require({sid for sid, _ in self.scopes} <= {s.id for s in model.stores}, 'automatic_publication_unknown_store')
        return self

    def require(self, store_id, period, *, workspace_root=None):
        self.verify(workspace_root)
        _require(self.allows(store_id, period), 'automatic_publication_outside_approved_scope')
        return self

    @classmethod
    def for_isolated(cls, workspace_root, scopes, *, approval_reference):
        """Explicit authority for a disposable replay/test, never production."""
        root = Path(workspace_root).resolve()
        temporary = Path(tempfile.gettempdir()).resolve()
        _require(root != temporary and root.is_relative_to(temporary), 'isolated_publication_scope_outside_temporary_workspace')
        pairs = _pairs([{'store_id': sid, 'period': period} for sid, period in scopes])
        _require(isinstance(approval_reference, str) and bool(approval_reference.strip()), 'automatic_publication_approval_missing')
        raw = json.dumps({'schema': SCHEMA, 'root': str(root), 'approval_reference': approval_reference,
                          'scopes': sorted(pairs), 'isolated': True}, sort_keys=True).encode()
        return cls(root, pairs, _sha(raw), approval_reference, _code_identity(), isolated=True)


def _read(root, location, expected, model):
    _require(isinstance(location, str) and bool(location) and re.fullmatch('[0-9a-f]{64}', expected or ''),
             'automatic_publication_scope_unconfigured')
    path = Path(location)
    _require(path.is_absolute() and not path.is_symlink() and path.is_file()
             and path.stat().st_size <= 2 * 1024 * 1024, 'automatic_publication_scope_missing_or_invalid')
    raw = path.read_bytes()
    _require(_sha(raw) == expected, 'automatic_publication_scope_sha_mismatch')
    def unique(values):
        obj = {}
        for key, value in values:
            _require(key not in obj, 'automatic_publication_scope_duplicate_key')
            obj[key] = value
        return obj
    try:
        body = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise PublicationScopeError('automatic_publication_scope_contract') from exc
    _require(isinstance(body, dict) and body.get('schema') == SCHEMA
             and body.get('kind') == 'automatic_publication'
             and isinstance(body.get('approval_reference'), str) and bool(body['approval_reference'].strip())
             and isinstance(body.get('workspace_root'), str) and Path(body['workspace_root']).is_absolute()
             and Path(body['workspace_root']).resolve() == root, 'automatic_publication_scope_contract')
    scope = PublicationScope(root, _pairs(body.get('scopes')), expected, body['approval_reference'],
                             _code_identity(), path.resolve(), (location, expected))
    return scope.verify(root, model)


def load_automatic(workspace_root, model=None):
    """Pin one explicit controller-owned scope per workspace/process startup."""
    root = Path(workspace_root).resolve()
    with _lock:
        if root not in _startup:
            _startup[root] = _read(root, os.environ.get(FILE_ENV, ''), os.environ.get(SHA_ENV, ''), model)
        return _startup[root].verify(root, model)


def install_isolated(scope, *, replace=False):
    """Explicit disposable-harness enrollment, also visible to its HTTP threads."""
    _require(isinstance(scope, PublicationScope) and scope.isolated,
             'isolated_publication_scope_required')
    scope.verify(scope.root)
    with _lock:
        old = _startup.get(scope.root)
        _require(old is None or old == scope or (replace and old.isolated),
                 'isolated_publication_scope_cannot_replace_controller_authority')
        _startup[scope.root] = scope
    return scope


def remove_isolated(scope):
    with _lock:
        if scope.isolated and _startup.get(scope.root) is scope:
            del _startup[scope.root]


def current():
    return _active.get()


def hold(scope, workspace_root, attempt_id, reason, *, store_id=None, period=None):
    """Idempotent technical evidence; never mutate an unscoped business row."""
    root = Path(workspace_root).resolve()
    record = {'schema': 'ledger_publication_hold.v1', 'attempt_id': str(attempt_id),
              'reason': reason, 'store_id': store_id, 'period': period,
              'publication_scope': scope.identity, 'financial_applied': False}
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    folder = root / 'publication-holds'
    _require(not folder.is_symlink(), 'automatic_publication_hold_path_invalid')
    folder.mkdir(exist_ok=True)
    path = folder / (_sha(raw) + '.json')
    _require(not path.is_symlink(), 'automatic_publication_hold_path_invalid')
    try:
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        _require(path.read_bytes() == raw, 'automatic_publication_hold_integrity')
    return record


def _service_actor():
    from . import write_auth
    actor = write_auth._identity.get()
    return bool(actor and actor.get('kind') in {'service', 'delegated_service'})


def resolve(workspace_root, model=None, explicit=None):
    scope = explicit if explicit is not None else current()
    if scope is None and _service_actor():
        scope = load_automatic(workspace_root, model)
    if scope is None:
        return None  # Existing trusted internal/human manual entry point.
    _require(isinstance(scope, PublicationScope), 'automatic_publication_scope_object_invalid')
    return scope.verify(workspace_root, model)


@contextmanager
def bind(scope, workspace_root):
    _require(isinstance(scope, PublicationScope), 'automatic_publication_scope_object_invalid')
    scope.verify(workspace_root)
    previous = current()
    _require(previous is None or previous == scope, 'automatic_publication_scope_context_changed')
    token = _active.set(scope)
    try:
        yield scope
    finally:
        _active.reset(token)
