"""Build optional read artifacts before a guarded release accepts traffic."""
import argparse
import asyncio
import time
import json
from pathlib import Path

from fastapi import FastAPI
from .commission_api import install
from .commission_reports import _visible_run_sql
from .model.loader import load_model
from .workspace import Workspace


async def request(app, path, body=None):
    data=json.dumps(body).encode() if body is not None else b''
    messages=[]
    async def receive():return {'type':'http.request','body':data,'more_body':False}
    async def send(message):messages.append(message)
    await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1',
               'method':'POST' if body is not None else 'GET','path':path,'raw_path':path.encode(),
               'root_path':'','scheme':'http','query_string':b'',
               'headers':[(b'content-type',b'application/json')],
               'client':('127.0.0.1',0),'server':('127.0.0.1',8000)},receive,send)
    status=next(m['status'] for m in messages if m['type']=='http.response.start')
    if status>=400:raise RuntimeError(f'Read warm failed: {path} HTTP {status}')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--model',required=True)
    args=parser.parse_args()
    ws=Workspace(Path(args.root));model=load_model(Path(args.model))
    app=FastAPI();install(app,lambda:ws,lambda:model,Path(args.model))
    started=time.perf_counter()
    ws.overview_summaries()
    print(f'WARM overview {time.perf_counter()-started:.2f}s',flush=True)
    started=time.perf_counter()
    asyncio.run(request(app,'/api/commission-v2/people/summary'))
    print(f'WARM people {time.perf_counter()-started:.2f}s',flush=True)
    source,_,_,_=_visible_run_sql(None)
    months=[r[0] for r in ws.conn.execute('SELECT r.period '+source+" WHERE r.period GLOB '????-??' GROUP BY r.period ORDER BY count(*) DESC,r.period DESC LIMIT 2")]
    for month in months:
        started=time.perf_counter()
        asyncio.run(request(app,'/api/commission-v2/reports/query',{'start':month,'end':month,'view':'people','limit':50}))
        print(f'WARM reports {month} {time.perf_counter()-started:.2f}s',flush=True)
        started=time.perf_counter()
        asyncio.run(request(app,'/api/commission-v2/reports/query',{'start':month,'end':month,'view':'managed','limit':50}))
        print(f'WARM managed {month} {time.perf_counter()-started:.2f}s',flush=True)
    ws.close()


if __name__=='__main__':main()
