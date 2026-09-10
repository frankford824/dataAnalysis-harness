from datetime import datetime, timezone
from types import SimpleNamespace
import json
import polars as pl
from ledger.order_date_context import collect, publish
from ledger.engine.types import ANCHOR_SHA, ANCHOR_ROW
from ledger.model.schema import Store


def source(rows, template='uploaded_orders'):
    frame=pl.DataFrame(rows).with_columns(pl.lit('a'*64).alias(ANCHOR_SHA),pl.int_range(2,len(rows)+2,dtype=pl.Int64).alias(ANCHOR_ROW))
    return SimpleNamespace(frame=frame,template=SimpleNamespace(id=template))


def test_original_dates_are_scoped_evidenced_and_conflicts_are_not_guessed(tmp_path):
    store=Store(id='test_shop',name='本店',platform='taobao')
    rows=[{'order_id':'3306862164152025489','sub_order_id':'3306862164152025490','store_name':'本店','order_time':datetime(2026,6,5)},
          {'order_id':'3306862164152025489','sub_order_id':'3306862164152025491','store_name':'本店','order_time':datetime(2026,6,6)},
          {'order_id':'9999999999999999999','sub_order_id':'9999999999999999999','store_name':'其它店','order_time':datetime(2026,6,7)}]
    data=[source(rows),source(rows,'order_console_live')]
    ing=SimpleNamespace(frames_of=lambda name:data)
    result={r['order_id']:r for r in collect(ing,store)}
    assert set(result)=={'3306862164152025489','3306862164152025490','3306862164152025491'}
    assert result['3306862164152025489']['order_date'] is None
    assert result['3306862164152025489']['status']=='conflict'
    assert result['3306862164152025490']['order_date']=='2026-06-05'
    assert result['3306862164152025490']['evidence'][0]['document_row']==2
    publish(ing,store,tmp_path)
    path=tmp_path/'order-date-context/test_shop.json';stamp=path.stat().st_mtime_ns
    publish(ing,store,tmp_path)
    assert path.stat().st_mtime_ns==stamp
    assert json.loads(path.read_text())['orders']==collect(ing,store)


def test_context_uses_the_business_day_and_does_not_invent_dates():
    store=Store(id='test',name='本店',platform='douyin')
    data=source([{'order_id':'6927041886243815238','sub_order_id':'6927041886243815238','order_time':datetime(2026,6,5,17,tzinfo=timezone.utc)}])
    rows=collect(SimpleNamespace(frames_of=lambda name:[data]),store)
    assert rows[0]['order_date']=='2026-06-06'
    data.frame=data.frame.with_columns(pl.lit(None).alias('order_time'))
    assert collect(SimpleNamespace(frames_of=lambda name:[data]),store)==[]
