"""Identified writes for legacy and automation routes; no anonymous fallback."""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .version import assert_code_integrity,CodeIntegrityError

_identity = contextvars.ContextVar('ledger_write_identity', default=None)
SERVICE_ROUTES = (
    ('POST', r'/api/upload', 'intake'),
    ('POST', r'/api/stores/[^/]+/recompute', 'recompute'),
    ('POST', r'/api/freight-review/experiments', 'freight_experiment'),
    ('POST', r'/api/automation/months/\d{4}-\d{2}/advance', 'advance'),
    ('POST', r'/api/automation/service-actions', 'approved_action'),
    ('POST', r'/api/automation/service-facts', 'approved_action'),
    ('POST', r'/api/automation/service-physical-facts', 'approved_action'),
    ('POST', r'/api/automation/source-recovery/requests', 'agent_control'),
    ('POST', r'/api/automation/source-recovery/requests/source-[0-9a-f]{40}/finish', 'agent_control'),
    ('POST', r'/api/v1/agents/heartbeat', 'agent_control'),
    ('POST', r'/api/v1/agent-jobs/claim', 'agent_control'),
    ('POST', r'/api/v1/agent-jobs/[^/]+/(progress|complete|fail)', 'agent_control'),
    ('POST', r'/api/v1/agent-questions/claim', 'agent_control'),
    ('POST', r'/api/v1/agent-questions/[0-9a-f]{32}/(answer|fail)', 'agent_control'),
)


def service_identity(request):
    header = request.headers.get('authorization', '')
    if not header.startswith('Bearer '):
        return None
    location = os.environ.get('LEDGER_SERVICE_IDENTITIES', '')
    if not location:
        return None
    try:
        entries = json.loads(Path(location).read_text(encoding='utf-8'))['identities']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    token_sha = hashlib.sha256(header[7:].encode()).hexdigest()
    for entry in entries:
        if (entry.get('enabled') is True and isinstance(entry.get('expires_at'), (int, float))
                and entry['expires_at'] > time.time() and entry.get('id')
                and hmac.compare_digest(str(entry.get('token_sha256', '')), token_sha)):
            return {'id': entry['id'], 'name': entry.get('name', entry['id']), 'kind': 'service',
                    'authenticated': True, 'capabilities': entry.get('capabilities', []),
                    'scopes': entry.get('scopes', [])}
    return None


LAN_ACTOR = {'id': 'lan:operator', 'name': '局域网操作', 'kind': 'human', 'authenticated': True, 'admin': True}


def lan_open():
    return os.environ.get('LEDGER_LAN_OPEN') == '1'


def strict_actor(request, workspace):
    saved = getattr(request.state, 'ledger_identity', None)
    if saved:
        return saved
    from .commission_registry import Registry
    root = Path(workspace) if isinstance(workspace, (str, os.PathLike)) else workspace.root
    actor = Registry(root).actor(request.cookies.get('commission_session', ''))
    if actor:
        return {**actor, 'kind': 'human', 'authenticated': True}
    service = service_identity(request)
    if service:
        return service
    if lan_open():
        return dict(LAN_ACTOR)
    raise HTTPException(401, '请先登录台账操作账号')


def current_actor_name():
    actor = _identity.get()
    if not actor:
        raise HTTPException(401, '写入缺少已认证操作人')
    return actor.get('name') or actor['id']


def install(app, workspace):
    app.state.strict_writes = True

    @app.middleware('http')
    async def identified_writes(request, call_next):
        if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'} or not request.url.path.startswith('/api/'):
            return await call_next(request)
        try:
            assert_code_integrity()
        except CodeIntegrityError as exc:
            return JSONResponse({'detail':str(exc),'code':'code_source_drift','kind':'maintenance'},status_code=503)
        marker = Path(os.environ.get('LEDGER_RELEASE_MARKER',str(Path(__file__).resolve().parents[2]/'.release-in-progress.json')))
        if marker.exists():
            return JSONResponse({'detail':'发布验证进行中，请稍后重试；本次操作尚未执行'},status_code=503)
        origin = request.headers.get('origin')
        if origin and urlparse(origin).netloc != request.headers.get('host'):
            return JSONResponse({'detail': '跨站写入已拒绝'}, status_code=403)
        if request.url.path == '/api/commission-v2/session' and request.method in {'POST', 'DELETE'}:
            return await call_next(request)
        try:
            actor = strict_actor(request, workspace())
            if actor['kind'] == 'service':
                capability = next((c for method, path, c in SERVICE_ROUTES
                                   if request.method == method and re.fullmatch(path, request.url.path)), None)
                if capability is None or capability not in actor['capabilities']:
                    raise HTTPException(403, '服务身份未获准执行此操作，不能代替人工财务确认')
            request.state.ledger_identity = actor
            token = _identity.set(actor)
            try:
                response = await call_next(request)
                if response.status_code < 400:
                    from .automation_actions import note_write
                    note_write()
                return response
            finally:
                _identity.reset(token)
        except HTTPException as exc:
            return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
