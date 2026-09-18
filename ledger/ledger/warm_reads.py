"""Build optional read artifacts before a guarded release accepts traffic."""
import argparse
import asyncio
import time
from pathlib import Path

from fastapi import FastAPI
from .commission_api import install, ReportSelection
from .commission_reports import _visible_run_sql
from .model.loader import load_model
from .workspace import Workspace


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--model',required=True)
    args=parser.parse_args()
    ws=Workspace(Path(args.root));model=load_model(Path(args.model))
    app=FastAPI();install(app,lambda:ws,lambda:model,Path(args.model))
    endpoints={route.path:route.endpoint for route in app.routes if hasattr(route,'endpoint')}
    started=time.perf_counter()
    endpoints['/api/commission-v2/people/summary']()
    print(f'WARM people {time.perf_counter()-started:.2f}s',flush=True)
    source,_,_,_=_visible_run_sql(None)
    months=[r[0] for r in ws.conn.execute('SELECT r.period '+source+" WHERE r.period GLOB '????-??' GROUP BY r.period ORDER BY count(*) DESC,r.period DESC LIMIT 2")]
    for month in months:
        started=time.perf_counter()
        asyncio.run(endpoints['/api/commission-v2/reports/query'](ReportSelection(start=month,end=month,view='people',limit=50)))
        print(f'WARM reports {month} {time.perf_counter()-started:.2f}s',flush=True)
    ws.close()


if __name__=='__main__':main()
