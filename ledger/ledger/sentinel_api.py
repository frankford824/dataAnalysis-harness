"""Read-only operational events and site notifications; never starts recovery."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from datetime import datetime

from fastapi import APIRouter, Query


def install(app,home,max_age_seconds=180):
    router=APIRouter(prefix='/api/operations/sentinel')
    def read(table,limit):
        root=Path(home() if callable(home) else home)
        path=root/'sentinel.db'
        if not path.is_file():return {'status':'unknown','reason':'sentinel_not_observed','items':[]}
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
                db.row_factory=sqlite3.Row
                rows=[dict(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY at DESC LIMIT ?',(limit,))]
            for row in rows:row['body']=json.loads(row['body'])
            return {'status':'available','items':rows}
        except (sqlite3.Error,ValueError):
            return {'status':'unknown','reason':'sentinel_evidence_unavailable','items':[]}
    @router.get('/events')
    def events(limit:int=Query(50,ge=1,le=200)):return read('event',limit)
    @router.get('/notifications')
    def notifications(limit:int=Query(50,ge=1,le=200)):return read('notification',limit)
    @router.get('/status')
    def status():
        value=read('observation',200)
        value['health']='ok' if value['items'] and all(r['status']=='ok' for r in value['items']) else 'unknown' if not value['items'] else 'attention'
        for row in value['items']:
            try:
                stamp=datetime.fromisoformat(row['at'].replace('Z','+00:00'))
                age=time.time()-stamp.timestamp()
                stale=stamp.tzinfo is None or age< -30 or age>max_age_seconds
            except (ValueError,TypeError):stale=True
            if stale:value.update(health='unknown',reason='sentinel_observation_stale')
        return value
    app.include_router(router)
