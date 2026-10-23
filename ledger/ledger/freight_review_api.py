"""Read projections only. This router has no accounting mutation endpoints."""
from urllib.parse import quote
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import freight_review, freight_trial, store_classification


class ShipmentRequest(BaseModel):
    period: str


def install(app, workspace, model, display_names):
    router = APIRouter(prefix='/api/freight-review')

    def result(period, store_id, platform):
        try:
            return freight_review.build(workspace(), model(), period, store_id=store_id,
                                        platform=platform, store_names=display_names())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get('')
    def review(period: str, store_id: str = '', platform: str = ''):
        return result(period, store_id, platform)

    @router.get('/export.csv')
    def export(period: str, store_id: str = '', platform: str = '', group: str = '', fingerprint: str = ''):
        data = result(period, store_id, platform)
        if not fingerprint or data['fingerprint'] != fingerprint:
            raise HTTPException(409, '核算依据有更新，请更新结果后再导出')
        try:
            content = freight_review.export_csv(data, group)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        filename = f'运费复核-{period}.csv'
        return PlainTextResponse(content, media_type='text/csv; charset=utf-8', headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename),
            'Cache-Control': 'private,no-cache'})

    def jobs():return freight_trial.manager(workspace().root)

    def guarded(operation):
        try:return operation()
        except freight_trial.InputError as exc:raise HTTPException(400,str(exc)) from exc

    @router.get('/store-classification')
    def classification_catalog():
        value=store_classification.resolve([{'id':s.id,'name':s.name,'platform':s.platform} for s in model().stores])
        return {k:v for k,v in value.items() if k!='mapping'}

    @router.get('/shipments')
    def shipment_latest(period: str):
        return guarded(lambda:jobs().latest(period))

    @router.post('/shipments',status_code=202)
    def shipment_start(body: ShipmentRequest):
        # This only writes separate proposal artifacts. It never invokes the
        # financial recompute queue, close/reopen, or commission operations.
        return guarded(lambda:jobs().start(body.period,model(),
            os.environ.get('LEDGER_ORDER_FEED_ROOT',r'D:\order\exchange\ledger-feed'),display_names()))

    @router.get('/shipments/{job_id}')
    def shipment_result(job_id: str, store_id: str='', platform: str='', reason: str='',q: str='',offset: int=0,limit: int=30,exclude_store_ids: str='',only_category: str='',exclude_categories: str='',classification_view: str=''):
        return guarded(lambda:freight_trial.query(jobs(),job_id,store_id=store_id,platform=platform,reason=reason,q=q,offset=offset,limit=limit,exclude_store_ids=exclude_store_ids,only_category=only_category,exclude_categories=exclude_categories,classification_view=classification_view))

    @router.get('/shipments/{job_id}/charges/{charge_id}')
    def shipment_charge(job_id: str,charge_id: str,store_id: str='',platform: str='',exclude_store_ids: str='',only_category: str='',exclude_categories: str='',classification_view: str=''):
        return guarded(lambda:freight_trial.detail(jobs(),job_id,charge_id,store_id=store_id,platform=platform,exclude_store_ids=exclude_store_ids,only_category=only_category,exclude_categories=exclude_categories,classification_view=classification_view))

    @router.get('/shipments/{job_id}/export.csv')
    def shipment_export(job_id: str,store_id: str='',platform: str='',reason: str='',q: str='',scope_fingerprint: str='',exclude_store_ids: str='',only_category: str='',exclude_categories: str='',classification_view: str='',classification_version: str=''):
        content=guarded(lambda:freight_trial.export(jobs(),job_id,store_id=store_id,platform=platform,reason=reason,q=q,scope_fingerprint=scope_fingerprint,classification_version=classification_version,exclude_store_ids=exclude_store_ids,only_category=only_category,exclude_categories=exclude_categories,classification_view=classification_view))
        return PlainTextResponse(content,media_type='text/csv; charset=utf-8',headers={
            'Content-Disposition':"attachment; filename*=UTF-8''"+quote('包裹运费试算.csv'),
            'Cache-Control':'private,no-cache'})

    app.include_router(router)
