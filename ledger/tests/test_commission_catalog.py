import polars as pl

from ledger.commission_catalog import observe, refresh
from ledger.commission_registry import Registry
from test_commission import _model


def test_new_order_product_survives_older_daily_catalog(tmp_path):
    registry = Registry(tmp_path)
    model = _model()
    spine = pl.DataFrame({"store": ["测试店"], "product_id": ["123456789001"], "product_name": ["新订单商品"]})
    assert observe(registry, model.store("s1"), spine) == 1
    class Client:
        def get(self, path, params=None):
            if path == "stores":
                return {"stores": [{"order_store_id":"10","ledger_store_id":"s1","mapping_status":"confirmed"}]}
            if path == "users":
                return {"users": []}
            return {"refreshed_at":"2026-06-01T00:00:00", "has_more":False,
                    "listings":[{"order_store_id":"10","shop_item_id":"123456789002","listing_name":"目录商品"}]}
    result = refresh(registry, model, Client())
    assert result["recent_order_products"] == 1
    with registry.connect() as conn:
        assert conn.execute("SELECT count(*) FROM catalog").fetchone()[0] == 2
    assert registry.revision() == 0


def test_total_pages_follow_filter_and_ignore_cursor(tmp_path):
    from ledger.commission_catalog import settings
    registry=Registry(tmp_path);model=_model()
    observe(registry,model.store('s1'),pl.DataFrame({'store':['测试店']*5,'product_id':['12345678900'+str(i) for i in range(5)],'product_name':['商品']*5}))
    first=settings(registry,state='pending',limit=2)
    assert first['total']==5 and first['total_pages']==3
    second=settings(registry,state='pending',limit=2,after=first['next_after'])
    assert second['total']==5 and second['total_pages']==3 and len(second['rows'])==2
    empty=settings(registry,search='不存在',limit=2)
    assert empty['total']==0 and empty['total_pages']==0
