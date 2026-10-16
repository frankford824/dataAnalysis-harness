"""Read projections only. This router has no accounting mutation endpoints."""
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from . import freight_review


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

    app.include_router(router)
