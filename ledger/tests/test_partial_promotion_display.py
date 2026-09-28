"""Known postings remain visible without certifying incomplete expenses."""
from copy import deepcopy
import polars as pl
import pytest
from ledger import view
from ledger.engine.runtime import run
from ledger.engine.types import FileRef
from ledger.model.loader import load_model
from conftest import MODELS
from test_promotion_dates import calculate, ingested
from test_api import client


def partial_result(*, old=False, amount=10):
    result=calculate([['P1',amount,'2026-07-01'],['P2',20,'2026-07-01'],['UNKNOWN',5,'2026-07-01']])
    if old:
        template=load_model(MODELS/'cn-ecommerce').template('promotion_douyin_v1')
        item=ingested(template,['商品ID','整体消耗'],[['P1',7]])
        item.ref=FileRef('old','old-undated.xlsx','sheet')
        from ledger.engine.normalize import ANCHOR_FILE,ANCHOR_SHA
        item.frame=item.frame.with_columns(pl.lit('shop').alias('__hint_store__'),
            pl.lit('old-undated.xlsx').alias(ANCHOR_FILE),pl.lit('old').alias(ANCHOR_SHA))
        result.ingestion.items.append(item)
        result=run(result.ingestion,'douyin')
    return result


@pytest.mark.parametrize('old',[False,True])
def test_partial_posting_visible_but_not_certified(old):
    result=partial_result(old=old);sl=result.slice('shop','2026-07')
    row=view._statement(sl,result.model)[0]
    assert not row['available'] and row['value'] is None
    assert row['verified_partial']==-10
    assert '待核对' in row['unavailable_reason']
    assert any(f.check_id=='promotion_integrity' and not f.passed for f in sl.audit.findings)
    if old:assert 'old-undated.xlsx' in row['unavailable_reason']
    reasons=sl.facts.filter(~pl.col('counted'))['source_note'].drop_nulls().to_list()
    assert any('商品编号已匹配' in x for x in reasons)
    assert any('未匹配商品订单' in x for x in reasons)


def test_no_posting_is_unknown_not_zero():
    result=calculate([['P2',20,'2026-07-01']]);sl=result.slice('shop','2026-07')
    assert view._statement(sl,result.model)[0]['verified_partial'] is None


def test_evidenced_net_zero_is_not_unknown():
    result=calculate([['P1',10,'2026-07-01'],['P1',-10,'2026-07-02'],['P2',20,'2026-07-01']])
    row=view._statement(result.slice('shop','2026-07'),result.model)[0]
    assert row['verified_partial']==0 and not row['available']


def test_complete_expense_is_unchanged():
    result=calculate([['P1',10,'2026-07-01']]);row=view._statement(result.slice('shop','2026-07'),result.model)[0]
    assert row['available'] and row['value']==-10 and row['verified_partial'] is None


def test_control_included_rows_do_not_block_or_count_twice():
    result=calculate([['P1',10,'2026-07-01'],['P2',20,'2026-07-01'],['-',40,'总计']],platform='pdd')
    sl=result.slice('shop','2026-07');row=view._statement(sl,result.model)[0]
    assert row['available'] and row['value']==-40
    assert not any(f.check_id=='promotion_integrity' for f in sl.audit.findings)


def test_archived_drill_keeps_reason_metadata_on_every_filter(client,monkeypatch):
    import ledger.api as api
    result=partial_result(old=True);sl=result.slice('shop','2026-07')
    monkeypatch.setattr(api,'_model',lambda:result.model)
    payload=view.slice_dict(sl,result.model.stores[0],result.model)
    before=deepcopy(payload)
    ws=api.workspace();rid=ws.record('s','2026-07',payload,[])
    sl.facts.write_parquet(ws.facts_path(rid))
    for mode in ['counted','uncounted','allocated','all']:
        response=client.get(f'/api/runs/{rid}/drill/ad?only={mode}').json()
        assert response['statement_available'] is False
        assert response['verified_partial']==-10
        assert 'old-undated.xlsx' in response['unavailable_reason']
        assert response['uncounted']['rows']==2
        assert sum(x['rows'] for x in response['unposted_reasons'])==2
        assert all(x['source_note'] for x in response['unposted_reasons'])
    assert ws.state_by_run(rid).result==before
