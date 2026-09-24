from copy import deepcopy
from datetime import datetime

import polars as pl
import pytest

from conftest import MODELS, write_xlsx
from ledger.engine.runtime import Ingested, Ingestion, _dedupe_across_files, ingest, run
from ledger.engine.types import ANCHOR_ROW, FileRef, Recognition
from ledger.model.loader import load_model
from ledger.model.schema import Template, ColumnBinding


def record(**changes):
    return dict(txn_id="1112397625990474", base_order_id="3310761902812009459",
                settle_time="2026-07-14 09:16:38", subject="交易收款", income=648., outgo=None,
                __hint_store__="shop", **changes)


def item(rows, name="first", template="taobao_settlement_wechat_v1", *, namespace=None):
    model = load_model(MODELS / "cn-ecommerce")
    t = model.template(template)
    if namespace:
        t = t.model_copy(update={"event_namespace": namespace})
    frame = pl.DataFrame(rows, infer_schema_length=None).with_row_index(ANCHOR_ROW, offset=2).with_columns(
        pl.lit(name).alias("__sha__"), pl.lit(name + ".xlsx").alias("__file__"), pl.lit("sheet").alias("__sheet__"))
    ref = FileRef(name, name + ".xlsx", "sheet")
    return Ingested(ref=ref, frame=frame, rows=len(rows), template=t,
                    recognition=Recognition(ref=ref, signature="s", header_count=6,
                                            source_id="settlement", template_id=t.id))


def dedup(*items):
    model = load_model(MODELS / "cn-ecommerce")
    ing = Ingestion(model=model, items=list(items))
    _dedupe_across_files(ing, model)
    return ing


def test_real_wechat_schema_null_zero_difference():
    first = record()
    second = {**first, "outgo": -0., "settle_time": "2026/07/14 09:16:38"}
    ing = dedup(item([first]), item([second], "second", "taobao_settlement_wechat_v2"))
    assert [i.frame.height for i in ing.items] == [1, 0]
    assert ing.items[0].frame["outgo"][0] is None
    assert ing.deduplication[0]["removed_rows"] == 1
    assert not ing.validation_errors


def test_blank_business_description_and_merchant_alias_are_normal():
    old = {**record(), "subject": None, "biz_type": "交易", "remark": "提现收入"}
    new = {**old, "outgo": -0., "store_name": "merchant historical display name"}
    ing = dedup(item([new]), item([old], "old", "taobao_settlement_wechat_v2"))
    assert sum(i.frame.height for i in ing.items) == 1
    assert not ing.validation_errors


@pytest.mark.parametrize("field,value", [("txn_id", "another"), ("__hint_store__", "other-shop")])
def test_distinct_events_and_shops_retained(field, value):
    ing = dedup(item([record()]), item([{**record(), field: value}], "other"))
    assert sum(i.frame.height for i in ing.items) == 2
    assert not ing.validation_errors


def test_wechat_alipay_are_not_the_same_channel():
    ing = dedup(item([record()]), item([record()], "alipay", "taobao_settlement_alipay_v1"))
    assert sum(i.frame.height for i in ing.items) == 2


def test_exact_atomic_duplicates_inside_file_collapse():
    ing = dedup(item([record(), record()]))
    assert ing.items[0].frame.height == 1


def test_transfer_debit_and_credit_are_distinct_legs():
    debit = {**record(), "income": None, "outgo": -648.}
    ing = dedup(item([record(), debit]), item([record(), debit], "copy"))
    assert sum(i.frame.height for i in ing.items) == 2
    assert not ing.validation_errors


def test_spreadsheet_id_display_wrappers_are_not_identity():
    row = {**record(), "txn_id": "'1112397625990474", "base_order_id": '`3310761902812009459'}
    ing = dedup(item([record()]), item([row], "copy"))
    assert sum(i.frame.height for i in ing.items) == 1


def test_lost_precision_is_blocked_even_without_a_collision():
    ing = dedup(item([{**record(), "txn_id": "2.02606041134381e+27"}]))
    assert "settlement" in ing.validation_errors
    assert ing.items[0].frame.height == 1


def test_conflict_survives_engine_and_disables_authoritative_totals():
    from ledger.model.schema import Model, SourceContract, StatementNode, Store
    from test_reship_period_and_cost_drill import item as order_item
    ing = dedup(item([record()]), item([{**record(), "income": 649.}], "conflict"))
    builtin = ing.model
    orders = [dict(order_id=record()["base_order_id"], product_id="p", store_name="shop",
                   order_time=datetime(2026, 7, 1), buyer_paid=648.)]
    ing.items.insert(0, order_item("order_detail", orders, orders[0].keys()))
    ing.model = Model(id="test", name="test", stores=(Store(id="s", name="shop", platform="taobao"),),
        sources=(builtin.source("order_detail"), builtin.source("settlement").model_copy(update={"provides": ("trade_receipt",)})),
        metrics=(builtin.metric("trade_receipt"),),
        dictionary=builtin.dictionary,
        statement=(StatementNode(id="income", name="income", formula={"op": "add", "of": ["trade_receipt"]}),))
    result = run(ing, "taobao")
    sl = result.slice("shop", "2026-07")
    assert "settlement" in sl.completeness.missing
    assert not sl.can_close
    assert sl.nodes["income"].value is None
    assert sl.deduplication[0]["status"] == "conflict"


@pytest.mark.parametrize("field,value", [("income", 649.), ("base_order_id", "other"),
                                        ("settle_time", "2026-08-14 09:16:38"), ("subject", "退款")])
def test_same_event_conflict_blocks_instead_of_choosing(field, value):
    ing = dedup(item([record()]), item([{**record(), field: value}], "conflict"))
    assert sum(i.frame.height for i in ing.items) == 2
    assert "settlement" in ing.validation_errors
    assert ing.deduplication[0]["status"] == "conflict"
    assert ing.deduplication[0]["events"] == 1


@pytest.mark.parametrize("counts", [(2, 3), (3, 2), (1, 1), (3, 3)])
def test_composite_multiset_retains_legitimate_multiplicity(counts):
    row = {**record(), "txn_id": ""}
    ing = dedup(*(item([row] * count, str(n), "pdd_settlement_v1") for n, count in enumerate(counts)))
    assert sum(i.frame.height for i in ing.items) == max(counts)
    assert not ing.validation_errors


def test_missing_identity_does_not_merge_unrelated_rows():
    row = {**record(), "txn_id": "", "settle_time": None}
    ing = dedup(item([row]), item([row], "other"))
    assert sum(i.frame.height for i in ing.items) == 2
    assert "settlement" in ing.validation_errors


def test_missing_both_amounts_not_zero():
    row = {**record(), "income": None, "outgo": None}
    ing = dedup(item([row]), item([row], "other"))
    assert "settlement" in ing.validation_errors
    assert sum(i.frame.height for i in ing.items) == 2


def test_empty_spacers_and_export_footers_are_not_payments():
    empty = {k: None for k in record()}
    empty['__hint_store__'] = 'shop'
    footer = {**empty, 'base_order_id': '#支出合计：3485笔-59276.09元'}
    ing = dedup(item([record(), empty, footer], template='pdd_settlement_v1'))
    assert not ing.validation_errors
    assert ing.items[0].frame.height == 3


def test_late_arrival_and_split_payments_do_not_collapse_by_order():
    rows = [{**record(), "txn_id": str(i), "settle_time": f"2026-0{month}-14 09:16:38"}
            for i, month in enumerate([7, 7, 8, 9])]
    ing = dedup(item(rows), item(rows, "copy"))
    assert sum(i.frame.height for i in ing.items) == 4
    assert not ing.validation_errors


def test_no_global_key_weakening_from_template_missing_txn_id():
    ing = dedup(item([record(), {**record(), "txn_id": "different"}]),
                item([{k: v for k, v in record().items() if k != "txn_id"}], "pdd", "pdd_settlement_v1"))
    assert sum(i.frame.height for i in ing.items) == 3


def test_idempotence():
    ing = dedup(item([record(), record()]), item([record()], "other"))
    before = deepcopy(ing.deduplication)
    _dedupe_across_files(ing, ing.model)
    assert ing.deduplication == before
    assert sum(i.frame.height for i in ing.items) == 1


def test_conflict_is_visible_before_payout_and_cannot_be_ignored_on_close(tmp_path):
    from test_commission_reports import fixture
    from ledger.workspace import WorkspaceError
    import json
    ws, registry, people, client = fixture(tmp_path)
    run_id = ws.record('s1', '2026-06', {
        'can_close': True, 'findings': [], 'missing_sources': [], 'statement': [],
        'deduplication': [{'status': 'conflict', 'message': '原流水金额不一致'}],
        'commission': {'engine': 'commission-v2', 'people': [{'person_id': people[0]['id'],
                       'person': '甲', 'amount': 10, 'base': 100}], 'total': 10, 'amount_complete': False},
    }, [])
    with pytest.raises(WorkspaceError, match='流水存在冲突'):
        ws.close_period('s1', '2026-06', by='财务', note='不能忽略', expected_run_id=run_id)
    context = client.get('/api/commission-v2/payout-confirmations/context', params={
        'store_id': 's1', 'period': '2026-06', 'run_id': run_id}).json()
    assert context['statement_risk'] == ['原流水金额不一致']
    response = client.post('/api/commission-v2/payout-confirmations', json={
        'store_id': 's1', 'period': '2026-06', 'run_id': run_id, 'source_sha': context['source_sha'],
        'reason': '尝试人工忽略', 'payouts': [{'person_id': people[0]['id'], 'amount': '12.00'}]})
    assert response.status_code == 400
    assert '流水存在冲突' in response.text
    with registry.connect() as conn:
        assert conn.execute('SELECT count(*) FROM payout_confirmation').fetchone()[0] == 0
