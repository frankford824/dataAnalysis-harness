"""Append-only, amount-free workload measurements; never a financial input."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Event(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: UUID
    kind: Literal['page_open','store_period_open','manual_amount_edit','manual_confirm','operation_complete']
    at: datetime
    view: Literal['board','period','deliver','commission','fees','labor','freight','ops']
    store_id: str = Field(default='', max_length=128, pattern=r'^[\w.-]*$')
    period: str = Field(default='', pattern=r'^(\d{4}-(0[1-9]|1[0-2]))?$')
    action: str = Field(default='', pattern=r'^[a-z_]{0,48}$')
    duration_ms: int = Field(default=0, ge=0, le=86_400_000)
    outcome: Literal['success','failure','observed'] = 'observed'

    @field_validator('at')
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError('event timestamp requires a timezone')
        return value


class Events(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_id: UUID
    events: list[Event] = Field(min_length=1, max_length=50)


def install(app, workspace, actor):
    router = APIRouter(prefix='/api/automation/telemetry')

    def prepare(ws):
        ws.conn.executescript('''CREATE TABLE IF NOT EXISTS workload_event (
          event_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, actor_sha TEXT NOT NULL,
          period TEXT NOT NULL, kind TEXT NOT NULL, duration_ms INTEGER NOT NULL, payload TEXT NOT NULL);
          CREATE TRIGGER IF NOT EXISTS workload_event_no_update BEFORE UPDATE ON workload_event
          BEGIN SELECT RAISE(ABORT,'workload history is append-only'); END;
          CREATE TRIGGER IF NOT EXISTS workload_event_no_delete BEFORE DELETE ON workload_event
          BEGIN SELECT RAISE(ABORT,'workload history is append-only'); END;''')

    @router.post('')
    async def append(body: Events, request: Request):
        identity = actor(request) or {}
        if identity.get('kind') != 'human' or not identity.get('authenticated'):
            raise HTTPException(403, '人工工作量只接受已登录人员的操作')
        if len(await request.body()) > 32768:
            raise HTTPException(413, 'Telemetry batch too large')
        ws = workspace()
        prepare(ws)
        with ws.conn:
            for event in body.events:
                ws.conn.execute('INSERT OR IGNORE INTO workload_event VALUES(?,?,?,?,?,?,?)',
                    (str(event.event_id), str(body.client_id), hashlib.sha256(identity['id'].encode()).hexdigest(),
                     event.period, event.kind, event.duration_ms, event.model_dump_json()))
        return {'accepted': len(body.events)}

    @router.get('/summary')
    def summary(period: str, request: Request, client_id: str = ''):
        identity = actor(request) or {}
        if identity.get('kind') != 'human': raise HTTPException(403, '仅维护人员读取工作量统计')
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period):
            raise HTTPException(400, 'Invalid period')
        if client_id:
            try: UUID(client_id)
            except ValueError as exc: raise HTTPException(400, 'Invalid client identity') from exc
        ws = workspace()
        if not ws.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workload_event'").fetchone():
            return {'period': period, 'events': [], 'events_present': False,
                    'baseline_measured': False, 'actual_human_time_baseline_measured': False,
                    'actual_reduction_percent': None}
        rows = [dict(row) for row in ws.conn.execute('SELECT kind,count(*) AS count,sum(duration_ms) AS duration_ms '
            'FROM workload_event WHERE period=? AND (?=\'\' OR client_id=?) GROUP BY kind', (period,client_id,client_id))]
        # Event presence is an observation, not paired-cohort human-time evidence.
        # No approved human-time baseline is recorded by this event collector.
        return {'period': period, 'events': rows, 'events_present': bool(rows),
                'baseline_measured': False, 'actual_human_time_baseline_measured': False,
                'actual_reduction_percent': None,
                'note': '服务耗时不等于人工实际处理时长；减少比例须固定同一批样本另行对照'}

    app.include_router(router)
