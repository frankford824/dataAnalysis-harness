"""Editable monthly labor overhead, using the existing company allocation."""
from __future__ import annotations
import csv
import io
from datetime import datetime
from decimal import Decimal
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from .model.transaction import model_lock, model_revision
from .model.loader import load_model
from .model.config import _atomic_text
from .overhead import allocate

class LaborChange(BaseModel):
    period: str
    name: str = Field(min_length=1, max_length=80)
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    revision: str

def check_period(period):
    try:
        if datetime.strptime(period, '%Y-%m').strftime('%Y-%m') != period: raise ValueError()
    except ValueError: raise HTTPException(400, '请选择有效月份')


def frozen_shares(root):
    path=root/'labor-closed-shares.csv'
    if not path.exists():return {}
    with path.open(encoding='utf-8-sig',newline='') as f:
        return {(r['period'],r['store_id'],r['run_id']):r for r in csv.DictReader(f)}


def save(root, body, closed_rows=()):
    check_period(body.period)
    with model_lock(root):
        if model_revision(root) != body.revision: raise HTTPException(409, '数据已更新，请刷新后再保存')
        path=root/'overheads.csv'; old=path.read_bytes() if path.exists() else None
        frozen_path=root/'labor-closed-shares.csv'; frozen_old=frozen_path.read_bytes() if frozen_path.exists() else None
        frozen=frozen_shares(root)
        for row in closed_rows:
            key=(body.period,row['store_id'],str(row['run_id']))
            frozen.setdefault(key,{'period':body.period,'store_id':row['store_id'],'run_id':str(row['run_id']),'amount':str(row['amount'] or 0)})
        model=load_model(root)
        rows=[{'period':x.period,'amount':str(x.amount),'name':x.name,'note':x.note} for x in model.overheads if x.period!=body.period]
        rows.append({'period':body.period,'amount':str(body.amount),'name':body.name.strip(),'note':''})
        if not body.name.strip(): raise HTTPException(400, '请填写科目名称')
        out=io.StringIO();writer=csv.DictWriter(out,fieldnames=['period','amount','name','note']);writer.writeheader();writer.writerows(sorted(rows,key=lambda r:r['period']))
        try:
            _atomic_text(path,out.getvalue())
            if closed_rows:
                saved=io.StringIO();cw=csv.DictWriter(saved,fieldnames=['period','store_id','run_id','amount']);cw.writeheader();cw.writerows(frozen.values());_atomic_text(frozen_path,saved.getvalue())
            load_model(root)
        except BaseException:
            if old is None:path.unlink(missing_ok=True)
            else:_atomic_text(path,old.decode('utf-8-sig'))
            if frozen_old is None:frozen_path.unlink(missing_ok=True)
            else:_atomic_text(frozen_path,frozen_old.decode('utf-8-sig'))
            raise


def install(app, workspace, model, model_root, invalidate, actor):
    @app.get('/api/commission-v2/labor')
    def read(period: str):
        check_period(period);m=model();ws=workspace()
        config=next((x for x in m.overheads if x.period==period),None)
        states=[x for x in ws.overview() if x.period==period]
        revenue=next((n.id for n in m.statement if n.headline=='revenue'),'')
        def basis(st):
            row=next((n for n in (st.result or {}).get('statement',[]) if n.get('id')==revenue),None)
            return row.get('value') if row and row.get('available',True) else None
        known=[(st,basis(st)) for st in states]
        spread=allocate(period,config.amount if config else None,[(st.store_id,v or 0) for st,v in known])
        basis_total=float(sum((Decimal(str(max(v or 0,0))) for _,v in known),Decimal(0)))
        names={s.id:s.name for s in m.stores};cuts={s.store_id:s.amount for s in spread.shares}
        locked=any(st.state=='closed' for st in states)
        return {'period':period,'name':config.name if config else '兼职人工费用','amount':config.amount if config else None,
                'revision':model_revision(model_root),'locked':False,'closed_stores':sum(st.state=='closed' for st in states),'basis_total':basis_total,
                'settled':spread.settled,'incomplete':any(v is None for _,v in known),
                'rows':[{'store_id':st.store_id,'closed':st.state=='closed','run_id':getattr(st,'run_id',None),'store':names.get(st.store_id,st.store_id),'sales':v,
                         'share':(max(v or 0,0)/basis_total if basis_total and v is not None else None),
                         'amount':cuts.get(st.store_id,0) if spread.settled and v is not None else None} for st,v in known]}

    @app.post('/api/commission-v2/labor')
    def update(body: LaborChange, request: Request):
        who=actor(request);ws=workspace()
        before=read(body.period)
        save(model_root,body,[row for row in before['rows'] if row['closed']]);invalidate()
        ws.log_config('labor-overhead','修改兼职人工费用',by=who.get('name','本机操作'),
                      before={'period':body.period,'name':before['name'],'amount':before['amount']},
                      after={'period':body.period,'name':body.name,'amount':str(body.amount)})
        return read(body.period)
