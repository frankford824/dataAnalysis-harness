"""Source evidence must reproduce the existing booked six-decimal projection."""
from decimal import Decimal
import hashlib
import json

import polars as pl
import pytest

from ledger.engine.calculate import totals_by_metric
from ledger.engine.link import Spine
from ledger.engine.project import project
from ledger.engine.runtime import _mark_counted
from ledger.model.schema import Metric
from ledger.money import money_float


def metric(*, ratio=False):
    return Metric(id="m", name="test", source="s", value={"op": "sum", "of": ["amount"]},
                  link={"key": "order_id" if ratio else "sub_order_id",
                        "to": "order.order_id" if ratio else "order.sub_order_id", "grain": "order"},
                  allocate={"mode": "ratio", "by": "alloc_ratio"} if ratio else None)


def facts(rows):
    base = {"metric_id": "m", "source_id": "s", "store": "s", "period": "2026-09",
            "link_key": "C", "amount": 0., "major": None, "source_note": None,
            "file_sha": "a" * 64, "sheet": "raw", "row_no": 1}
    return pl.from_dicts([{**base, **row} for row in rows],
                         schema_overrides={"amount": pl.Float64, "source_note": pl.String})


def spine(*, ratio=False, keys=("C",)):
    return Spine(pl.from_dicts([{"store": "s", "period": "2026-09", "order_id": "P",
        "sub_order_id": key, "product_id": "product-" + key, "__spine_origin__": "order_detail_file",
        "alloc_ratio": 1. / len(keys)} for key in keys]))


def digest(frame):
    return hashlib.sha256(json.dumps(frame.to_dicts(), sort_keys=True, default=str).encode()).hexdigest()


@pytest.mark.parametrize("direction", [-1, 1])
def test_float_product_half_cent_evidence_matches_actual_booked_cost_and_refund(direction):
    # Exact original inputs are 3 and .305; Float64 multiplication falls just
    # below .915. The existing project() already rounds this to six decimals.
    raw = direction * 3 * .305
    source = facts([{"amount": raw}])
    m = metric();booked = project(source, m, spine()).facts
    before = digest(booked);statement = totals_by_metric(booked)
    marked = _mark_counted(source, booked, [m])
    expected = money_float(Decimal(direction) * Decimal("3") * Decimal("0.305"))
    assert money_float(marked["contribution"][0]) == expected
    assert statement["m"] == expected
    assert marked["amount"][0] == raw
    assert digest(booked) == before and totals_by_metric(booked) == statement


def test_one_hundred_subcent_freight_rows_keep_their_value_and_three_target_remainder():
    source = facts([{"link_key": "P", "amount": -.004, "row_no": row} for row in range(1, 101)])
    m = metric(ratio=True);booked = project(source, m, spine(ratio=True, keys=("C1", "C2", "C3"))).facts
    before = digest(booked);marked = _mark_counted(source, booked, [m])
    assert marked.height == 100 and marked["contribution"].to_list() == [-.004] * 100
    assert sum(Decimal(str(x)) for x in marked["contribution"]) == Decimal("-.400")
    assert sum(Decimal(str(x)) for x in booked["amount"]) == Decimal("-.400000")
    assert marked["amount"].to_list() == [-.004] * 100 and digest(booked) == before


def test_opposite_charges_and_refunds_do_not_disappear_into_a_net_zero():
    raw = 3 * .305
    source = facts([{"amount": -raw, "row_no": 1}, {"amount": raw, "row_no": 2}])
    m = metric();booked = project(source, m, spine()).facts
    marked = _mark_counted(source, booked, [m]).sort("row_no")
    assert marked["contribution"].to_list() == [-.915, .915]
    assert marked["amount"].to_list() == [-raw, raw]
    assert totals_by_metric(booked)["m"] == 0


def test_existing_six_decimal_residue_uses_stable_source_identity_after_permutation():
    source = facts([{"link_key": "P", "amount": value, "row_no": row} for row, value in
                    [(1, -.0000004), (2, -.0000004), (3, -.0000004), (4, -.0000008)]])
    m = metric(ratio=True);target = spine(ratio=True, keys=("C1", "C2", "C3"))
    a = project(source, m, target).facts
    b = project(source.reverse(), m, target).facts
    marked = _mark_counted(source, a, [m]).sort("row_no")
    reversed_marked = _mark_counted(source.reverse(), b, [m]).sort("row_no")
    assert marked["contribution"].to_list() == reversed_marked["contribution"].to_list()
    assert sum(Decimal(str(x)) for x in marked["contribution"]) == sum(Decimal(str(x)) for x in a["amount"])
    assert marked["amount"].to_list() == source["amount"].to_list()


@pytest.mark.parametrize("unknown", [None, float("nan"), float("inf")])
def test_unknown_and_nonfinite_source_amounts_are_not_replaced_by_a_zero(unknown):
    source = facts([{"amount": unknown}]);m = metric()
    # An inconsistent projection must not confer a known source amount.
    booked = pl.DataFrame({"metric_id": ["m"], "source_id": ["s"], "store": ["s"],
        "period": ["2026-09"], "link_key": ["C"], "amount": [0.], "factor": [1.], "spine_row": [0]})
    marked = _mark_counted(source, booked, [m])
    if unknown is None:
        assert marked["amount"][0] is None and marked["contribution"][0] is None
    elif unknown != unknown:
        assert marked["amount"][0] != marked["amount"][0]
        assert marked["contribution"][0] != marked["contribution"][0]
    else:
        assert marked["amount"][0] == unknown and marked["contribution"][0] == unknown


def test_a_real_projection_discrepancy_cannot_be_hidden_as_a_rounding_residue():
    source = facts([{"amount": -1.01}]);m = metric()
    booked = pl.DataFrame({"metric_id": ["m"], "source_id": ["s"], "store": ["s"],
        "period": ["2026-09"], "link_key": ["C"], "amount": [-2.], "factor": [1.], "spine_row": [0]})
    marked = _mark_counted(source, booked, [m])
    assert marked["amount"][0] == marked["contribution"][0] == -1.01
    assert booked["amount"][0] == -2.



def test_rounded_zero_destinations_do_not_erase_the_original_small_fee():
    # Existing project() places the six-decimal residue on one destination and
    # omits the other rounded-zero rows. Their absent factors are not a rebate.
    source = facts([{"link_key": "P", "amount": -.000003}])
    m = metric(ratio=True)
    booked = project(source, m, spine(ratio=True, keys=tuple("C" + str(i) for i in range(20)))).facts
    marked = _mark_counted(source, booked, [m])
    assert sum(Decimal(str(x)) for x in booked["amount"]) == Decimal("-.000003")
    assert marked["contribution"][0] == -.000003
    assert marked["amount"][0] == -.000003



@pytest.mark.parametrize("factor", [2., .5])
def test_a_full_source_sum_cannot_hide_a_material_wrong_factor(factor):
    source = facts([{"amount": 10.}]);m = metric()
    booked = pl.DataFrame({"metric_id": ["m"], "source_id": ["s"], "store": ["s"],
        "period": ["2026-09"], "link_key": ["C"], "amount": [10.], "factor": [factor], "spine_row": [0]})
    marked = _mark_counted(source, booked, [m])
    assert marked["contribution"][0] == 10. * factor
    assert marked["contribution"][0] != booked["amount"][0]


def test_a_fake_omitted_zero_control_cannot_hide_a_nonzero_missing_amount():
    source = facts([{"amount": 10.}]);m = metric()
    booked = pl.DataFrame({"metric_id": ["m"], "source_id": ["s"], "store": ["s"],
        "period": ["2026-09"], "link_key": ["C"], "amount": [10.], "factor": [.5], "spine_row": [0],
        "__projection_factor_total": [1.], "__projection_dropped_zero_factor": [.5],
        "__projection_dropped_zero_count": [1], "__projection_target_count": [2]})
    assert _mark_counted(source, booked, [m])["contribution"][0] == 5.
