"""Income semantics use actual engine facts, never the sign of an aggregated net."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json

import polars as pl
import pytest

from conftest import MODELS, write_xlsx
from ledger import operations_income as income, service, view
from ledger.automation_period_proof import prepare, save
from ledger.engine.runtime import ingest, run
from ledger.model.loader import load_model
from ledger.model.schema import Overhead, Store
from ledger.storage_integrity import seal
from ledger.workspace import Workspace


def make_income_workspace(tmp_path, *, receipts=None, orders=None):
    """Reusable synthetic original XLSX -> real engine -> sealed Workspace case.

    Return a dict with ws/model/payload/run_id/result/ingestion/raw_files. The
    caller owns ws.close(). This fixture proves income evidence; it does not
    assert that the intentionally absent cost sources satisfy month closing.
    `receipts` entries are (order_id, subject, signed_income); default is A +100,
    A refund -20, B +100. `orders` entries are (order_id, product_id).
    """
    receipts = receipts if receipts is not None else [('A', '订单收入', 100), ('A', '订单售后退款', -20), ('B', '订单收入', 100)]
    orders = orders if orders is not None else [('A', 'P1'), ('B', 'P2')]
    model = load_model(MODELS / 'cn-ecommerce').model_copy(update={
        'stores': (Store(id='s1', name='1688合成店', platform='alibaba1688'),),
        'overheads': (Overhead(period='2026-07', amount=10),),
    })
    source = tmp_path / 'originals'; source.mkdir(parents=True, exist_ok=True)
    order_path = write_xlsx(source / '订单明细-1688合成店.xlsx', [
        ['说明'], ['订单号', '买家公司名称', '货品总价', '实付款（元）', '运单号', '订单创建时间', 'Offer_ID', '数量'],
        *[[oid, '合成买家', 100, 100, 'synthetic-' + str(i), '2026-07-01', pid, 1] for i, (oid, pid) in enumerate(orders)],
    ])
    settlement_path = write_xlsx(source / '对账-1688合成店.xlsx', [
        ['#1688收入账单明细查询'],
        ['账单编号', '账单创建时间', '应收金额(元)', '已收金额(元)', '账单状态', '账单类型', '场景类型', '场景明细', '关联订单号'],
        *[[f'synthetic-txn-{i}', '2026-07-02', amount, amount, '已结清', '收款', subject, subject, oid]
            for i, (oid, subject, amount) in enumerate(receipts)],
    ])
    store = model.store('s1')
    ingestion = ingest([order_path, settlement_path], model, [store.name], default_store=store.name)
    result = run(ingestion, store.platform)
    sl = result.slice(store.name, '2026-07')
    payload = view.slice_dict(sl, store, model)
    ws = Workspace(tmp_path / 'workspace')
    proof = prepare(ws, model, store, ingestion, sl, payload, None)
    run_id = ws.record(store.id, sl.period, payload, [item.ref.sha256 for item in ingestion.items],
        model_revision=hashlib.sha256(model.model_dump_json().encode()).hexdigest())
    service._keep_facts(ws, run_id, sl)
    save(ws, run_id, proof)
    return {'ws': ws, 'model': model, 'payload': payload, 'run_id': run_id,
        'result': result, 'ingestion': ingestion, 'raw_files': [order_path, settlement_path]}


@pytest.fixture
def actual_income(tmp_path):
    case = make_income_workspace(tmp_path)
    try:
        yield case
    finally:
        case['ws'].close()


def _frames(case):
    path = case['ws'].facts_path(case['run_id'])
    return pl.read_parquet(path), pl.read_parquet(path.with_suffix('.operations.parquet'))


def _analyze(case, facts=None, projected=None, **kwargs):
    raw, projection = _frames(case)
    return income.analyze(raw if facts is None else facts, projection if projected is None else projected,
        saved_net=180, store='1688合成店', period='2026-07',
        facts_verified=True, projection_verified=True, **kwargs)


def test_real_engine_1688_source_labels_survive_but_order_projection_is_net(actual_income):
    case = actual_income; facts, projected = _frames(case)
    booked = facts.filter((pl.col('metric_id') == income.METRIC) & pl.col('counted'))
    assert booked.select('subject', 'contribution').to_dicts() == [
        {'subject': '订单收入', 'contribution': 100.0},
        {'subject': '订单售后退款', 'contribution': -20.0},
        {'subject': '订单收入', 'contribution': 100.0},
    ]
    receipt_projection = projected.filter(pl.col('metric_id') == income.METRIC)
    assert sorted(receipt_projection['amount'].to_list()) == [80, 100]
    assert income.saved_net(case['payload'], 'alibaba1688') == 180
    result = income.load(case['ws'], case['run_id'], case['payload'], 'alibaba1688')
    assert (result['status'], result['receipts'], result['refunds'], result['net_income']) == ('split_verified', 200, 20, 180)
    assert result['by_spine'] == {0: {'receipts': Decimal(100), 'refunds': Decimal(20), 'net_income': Decimal(80)},
        1: {'receipts': Decimal(100), 'refunds': Decimal(0), 'net_income': Decimal(100)}}


def test_summary_and_detail_share_identical_money_proof_and_input_identity(actual_income):
    case = actual_income
    summary = income.load(case['ws'], case['run_id'], {}, 'alibaba1688', details=False)
    detail = income.load(case['ws'], case['run_id'], {}, 'alibaba1688', details=True)
    assert summary['by_spine'] == {} and detail['by_spine']
    assert {key: value for key, value in summary.items() if key not in ('by_spine', 'detail_proof')} == {
        key: value for key, value in detail.items() if key not in ('by_spine', 'detail_proof')}
    proof = summary['proof']['net_source']
    assert proof['kind'] == 'saved_1688_net_statement'
    assert proof['metric_id'] == income.METRIC and proof['metric_total'] == 180
    assert proof['refund_not_applicable'] == {'method': 'inapplicable_metrics', 'metric_ids': sorted(income.REFUND_METRICS)}
    identity = {key: proof[key] for key in ('store_id', 'period', 'run_id', 'platform', 'statement_receipt')}
    expected = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()
    assert proof['result_sha'] == expected


@pytest.mark.parametrize('field,value,reason', [
    ('subject', None, 'source_semantics_unknown'), ('subject', '交易关闭', 'source_semantics_unknown'),
    ('subject', '订单收入调整', 'source_semantics_unknown'), ('source_id', 'promotion', 'source_semantics_unknown'),
    ('template_id', 'unknown_1688_v2', 'source_semantics_unknown'), ('major', 'trade_refund', 'source_semantics_unknown'),
    ('contribution', -100.0, 'source_direction_conflict'), ('amount', -100.0, 'source_direction_conflict'),
    ('store', 'another-store', 'source_scope_or_anchor_invalid'), ('period', '2026-06', 'source_scope_or_anchor_invalid'),
])
def test_unknown_or_conflicting_source_semantics_keep_net_without_a_fabricated_zero(actual_income, field, value, reason):
    facts, projection = _frames(actual_income)
    rows = facts.to_dicts()
    next(row for row in rows if row['metric_id'] == income.METRIC and row['counted'])[field] = value
    result = _analyze(actual_income, pl.from_dicts(rows, schema=facts.schema))
    assert result['receipts'] is None and result['refunds'] is None and result['net_income'] == 180
    assert reason in result['proof']['reason_codes']
    assert result['by_spine'][0] == {'receipts': None, 'refunds': None, 'net_income': Decimal(80)}


def test_projection_only_and_source_without_subject_cannot_reconstruct_gross(actual_income):
    facts, projection = _frames(actual_income)
    projection_only = income.analyze(None, projection, saved_net=180, store='1688合成店', period='2026-07', projection_verified=True)
    unlabeled = _analyze(actual_income, facts.drop('subject'))
    for result in (projection_only, unlabeled):
        assert result['receipts'] is None and result['refunds'] is None and result['net_income'] == 180
        assert sorted(row['net_income'] for row in result['by_spine'].values()) == [80, 100]
        assert all(row['receipts'] is None and row['refunds'] is None for row in result['by_spine'].values())


def test_missing_source_file_keeps_only_net_from_captured_real_run(actual_income):
    case = actual_income
    case['ws'].facts_path(case['run_id']).unlink()
    result = income.load(case['ws'], case['run_id'], {'statement': [{'id': 'n_receipt', 'value': 999, 'available': True}]}, 'alibaba1688')
    assert result['receipts'] is None and result['refunds'] is None and result['net_income'] == 180
    assert result['proof']['net_source']['statement_receipt']['value'] == 180
    assert sorted(row['net_income'] for row in result['by_spine'].values()) == [80, 100]


def test_unsealed_or_resealed_changed_source_cannot_borrow_original_manifest(actual_income):
    case = actual_income; path = case['ws'].facts_path(case['run_id'])
    path.with_name(path.name + '.sha256').unlink()
    assert income.load(case['ws'], case['run_id'], {}, 'alibaba1688')['receipts'] is None
    facts = pl.read_parquet(path).with_columns(pl.lit(None, dtype=pl.String).alias('subject'))
    facts.write_parquet(path); seal(path)
    result = income.load(case['ws'], case['run_id'], {}, 'alibaba1688')
    assert result['net_income'] == 180 and result['receipts'] is None and result['refunds'] is None


def test_net_proof_uses_persisted_result_and_rejects_self_declared_unknown(actual_income):
    case = actual_income; ws, run_id = case['ws'], case['run_id']
    original = deepcopy(case['payload'])
    next(row for row in original['statement'] if row['id'] == 'n_receipt')['available'] = False
    from ledger.commission_slice import save as save_slice
    with ws.conn:
        ws.conn.execute('UPDATE run SET result=? WHERE id=?', (json.dumps(original, ensure_ascii=False), run_id))
        save_slice(ws.conn, run_id, original)
    result = income.load(ws, run_id, case['payload'], 'alibaba1688')
    assert result['status'] == 'unavailable' and result['net_income'] is None
    assert result['proof']['net_source'] is None


@pytest.mark.parametrize('mutation', ['metric_total_missing', 'metric_total_mismatch', 'refund_applicable', 'wrong_platform'])
def test_saved_net_requires_known_metric_and_non_applicable_refund_contract(actual_income, mutation):
    payload = deepcopy(actual_income['payload'])
    if mutation == 'metric_total_missing':payload['calculation_inputs']['metric_totals'].pop(income.METRIC)
    elif mutation == 'metric_total_mismatch':payload['calculation_inputs']['metric_totals'][income.METRIC] = 179
    elif mutation == 'refund_applicable':payload['calculation_inputs']['inapplicable_metrics'].remove('trade_refund')
    else:payload['platform'] = 'taobao'
    assert income.saved_net(payload, 'alibaba1688') is None


def test_source_closure_is_six_places_not_rounded_to_cents(actual_income):
    facts, projection = _frames(actual_income)
    rows = facts.to_dicts()
    next(row for row in rows if row['metric_id'] == income.METRIC and row['counted'])['contribution'] += .00002
    result = _analyze(actual_income, pl.from_dicts(rows, schema=facts.schema))
    assert result['receipts'] is None and result['refunds'] is None
    assert 'source_net_not_closed' in result['proof']['reason_codes']


def test_decimal_source_sums_preserve_subcent_evidence():
    rows = []
    for index, amount in enumerate(('0.0101', '0.0202')):
        rows.append({'metric_id': income.METRIC, 'source_id': income.SOURCE, 'template_id': income.TEMPLATE,
            'major': income.METRIC, 'subject': '订单收入', 'counted': True, 'linked': True,
            'amount': amount, 'contribution': amount, 'store': 'shop', 'period': '2026-07', 'link_key': 'A',
            'file_sha': 'a' * 64, 'sheet': 'sheet', 'row_no': index + 1})
    result = income.analyze(rows, saved_net='0.0303', store='shop', period='2026-07', facts_verified=True)
    assert result['receipts'] == Decimal('.0303') and result['net_income'] == Decimal('.0303')


def test_wrong_projection_factor_is_not_normalised_into_credible_order_income(actual_income):
    facts, projection = _frames(actual_income)
    bad = projection.with_columns(pl.when(pl.col('metric_id') == income.METRIC).then(pl.col('factor') * 2).otherwise(pl.col('factor')).alias('factor'))
    result = _analyze(actual_income, facts, bad)
    assert result['receipts'] == 200 and result['refunds'] == 20
    assert 'source_projection_factor_not_closed' in result['detail_proof']['reason_codes']
    assert all(row['receipts'] is None and row['refunds'] is None for row in result['by_spine'].values())


def test_no_reclassification_or_amount_writes_to_original_financial_record(actual_income):
    case = actual_income; ws, run_id = case['ws'], case['run_id']
    before = ws.conn.execute('SELECT result FROM run WHERE id=?', (run_id,)).fetchone()[0]
    for details in (False, True):income.load(ws, run_id, case['payload'], 'alibaba1688', details=details)
    assert ws.conn.execute('SELECT result FROM run WHERE id=?', (run_id,)).fetchone()[0] == before
    assert ws.conn.execute('SELECT count(*) FROM run').fetchone()[0] == 1


def test_summary_checks_projection_identity_without_decoding_order_parquet(actual_income, monkeypatch):
    case = actual_income; read = pl.read_parquet; observed = []
    def guarded(path, *args, **kwargs):
        observed.append(str(path))
        assert not str(path).endswith('.operations.parquet'), 'Store summary decoded the order projection'
        return read(path, *args, **kwargs)
    monkeypatch.setattr(pl, 'read_parquet', guarded)
    summary = income.load(case['ws'], case['run_id'], {}, 'alibaba1688', details=False)
    assert summary['status'] == 'split_verified' and summary['proof']['projection_sha']
    assert len(observed) == 1


def test_unverified_projection_does_not_change_independently_proven_store_income(actual_income):
    case = actual_income; path = case['ws'].facts_path(case['run_id']).with_suffix('.operations.parquet')
    bad = pl.read_parquet(path).with_columns((pl.col('amount') + 1).alias('amount'))
    bad.write_parquet(path); seal(path)  # Deliberately differs from the captured manifest.
    summary = income.load(case['ws'], case['run_id'], {}, 'alibaba1688', details=False)
    detail = income.load(case['ws'], case['run_id'], {}, 'alibaba1688', details=True)
    assert summary['receipts'] == detail['receipts'] == 200
    assert summary['proof'] == detail['proof']
    assert summary['input_sha'] == detail['input_sha'] and detail['by_spine'] == {}
    assert detail['proof']['projection_sha'] is None


def test_fully_offset_order_keeps_proven_gross_and_refund(tmp_path):
    case = make_income_workspace(tmp_path, receipts=[('A', '订单收入', 100), ('A', '订单售后退款', -100)], orders=[('A', 'P1')])
    try:
        result = income.load(case['ws'], case['run_id'], {}, 'alibaba1688')
        assert (result['receipts'], result['refunds'], result['net_income']) == (100, 100, 0)
        assert result['by_spine'][0] == {'receipts': Decimal(100), 'refunds': Decimal(100), 'net_income': Decimal(0)}
    finally:
        case['ws'].close()


def test_partial_effective_factor_must_match_actual_booked_source_contribution():
    fact = {'metric_id': income.METRIC, 'source_id': income.SOURCE, 'template_id': income.TEMPLATE,
        'major': income.METRIC, 'subject': '订单收入', 'counted': True, 'linked': True,
        'amount': '100', 'contribution': '50', 'store': 'shop', 'period': '2026-07', 'link_key': 'A',
        'file_sha': 'a' * 64, 'sheet': 'sheet', 'row_no': 1}
    projection = {'metric_id': income.METRIC, 'source_id': income.SOURCE, 'store': 'shop',
        'period': '2026-07', 'link_key': 'A', 'amount': '50', 'factor': '.5', 'spine_row': 0}
    result = income.analyze([fact], [projection], saved_net=50, store='shop', period='2026-07',
        facts_verified=True, projection_verified=True)
    assert result['by_spine'][0] == {'receipts': Decimal(50), 'refunds': Decimal(0), 'net_income': Decimal(50)}
    projection['factor'] = '1'
    bad = income.analyze([fact], [projection], saved_net=50, store='shop', period='2026-07',
        facts_verified=True, projection_verified=True)
    assert bad['by_spine'][0]['receipts'] is None
    assert 'source_projection_factor_not_closed' in bad['detail_proof']['reason_codes']


def test_selected_saved_run_is_not_replaced_by_a_newer_candidate(actual_income):
    case = actual_income; ws = case['ws']
    newer = deepcopy(case['payload'])
    next(row for row in newer['statement'] if row['id'] == 'n_receipt')['value'] = 999
    newer['calculation_inputs']['metric_totals'][income.METRIC] = 999
    latest = ws.record('s1', '2026-07', newer, [])
    assert latest != case['run_id']
    result = income.load(ws, case['run_id'], newer, 'alibaba1688')
    assert result['net_income'] == 180 and result['receipts'] == 200
    assert result['proof']['net_source']['run_id'] == case['run_id']


def test_read_layer_uses_closed_manual_overlay_instead_of_original_raw_or_caller(actual_income):
    # This is a declared read-layer fixture, not evidence that the absent costs
    # passed the actual month-close service. Seed an immutable overlay directly
    # to check the same persisted selection the snapshot reader uses.
    case = actual_income; ws, run_id = case['ws'], case['run_id']
    manual = deepcopy(case['payload'])
    next(row for row in manual['statement'] if row['id'] == 'n_receipt')['available'] = False
    from ledger.commission_slice import save as save_slice
    with ws.conn:
        ws.conn.execute('INSERT INTO manual_finance VALUES(?,?,?,?,?,?,?,?)',
            (run_id, 's1', '2026-07', json.dumps(manual, ensure_ascii=False), '{}', 'synthetic-read-layer', '2026-07-31', 'synthetic'))
        save_slice(ws.conn, run_id, manual, kind='manual')
        ws.conn.execute("UPDATE period SET state='closed',run_id=? WHERE store_id='s1' AND period='2026-07'", (run_id,))
    result = income.load(ws, run_id, case['payload'], 'alibaba1688')
    assert result['status'] == 'unavailable' and result['net_income'] is None
    assert result['proof']['net_source'] is None


def _corrupt_manifest(case, key, value):
    # Explicit physical-corruption fault injection in this isolated database.
    # Ordinary writes cannot mutate the first manifest; the production trigger
    # is restored even if this legacy/corruption read-layer setup fails.
    ws, run_id = case['ws'], case['run_id']
    raw = ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?', (run_id,)).fetchone()[0]
    manifest = json.loads(raw); manifest[key] = value
    with ws.conn:
        trigger=ws.conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='automation_run_proof_no_update'").fetchone()
        assert trigger is not None
        ws.conn.execute('DROP TRIGGER automation_run_proof_no_update')
        try:
            ws.conn.execute('UPDATE automation_run_proof SET manifest_json=? WHERE run_id=?',
                (json.dumps(manifest, ensure_ascii=False), run_id))
        finally:
            ws.conn.execute(trigger[0])


@pytest.mark.parametrize('artifacts', [[None], [1], None, 'file.parquet', {'path': 'file.parquet'}])
def test_wrong_manifest_artifact_shapes_are_unverified_not_server_errors(actual_income, artifacts):
    case = actual_income
    _corrupt_manifest(case, 'artifacts', artifacts)
    result = income.load(case['ws'], case['run_id'], {}, 'alibaba1688')
    assert result['receipts'] is None and result['refunds'] is None
    assert result['proof']['facts_sha'] is None and result['proof']['projection_sha'] is None


@pytest.mark.parametrize('templates', [f'prefix-{income.TEMPLATE}-suffix', None, {'id': income.TEMPLATE}, [None]])
def test_template_allowlist_requires_an_actual_list_of_exact_string_elements(actual_income, templates):
    case = actual_income
    _corrupt_manifest(case, 'template_ids', templates)
    path = case['ws'].facts_path(case['run_id'])
    path.unlink(); path.with_suffix('.operations.parquet').unlink()
    result = income.load(case['ws'], case['run_id'], case['payload'], 'alibaba1688')
    assert result['status'] == 'unavailable' and result['net_income'] is None
    assert result['proof']['net_source'] is None


@pytest.mark.parametrize('field,value', [('statement', [None]), ('statement', 'n_receipt'),
    ('calculation_inputs', []), ('calculation_inputs', {'metric_totals': []}),
    ('calculation_inputs', {'metric_totals': {income.METRIC: 180}, 'inapplicable_metrics': None})])
def test_saved_net_rejects_structurally_invalid_statement_contract(actual_income, field, value):
    payload = deepcopy(actual_income['payload']); payload[field] = value
    assert income.saved_net(payload, 'alibaba1688') is None
