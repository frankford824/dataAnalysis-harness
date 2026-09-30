import csv
import hashlib
import io
import sqlite3
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi.testclient import TestClient

from ledger import api, order_issues
from ledger.engine.cost_policy import missing_supplier_costs
from ledger.model.loader import load_model
from ledger.storage_integrity import seal
from ledger.view import finding_action
from ledger.workspace import Workspace, WorkspaceError


def facts(order='3310000000000000001', sku='DF01', **extra):
    return {'metric_id': 'goods_cost', 'store': '天猫皇莉诗旗舰店', 'period': '2026-07',
            'order_id': order, 'internal_order_id': '15881234', 'sku': sku,
            'link_key': order, 'source_note': '代发商品：聚水潭成本计 0',
            'amount': 0., 'contribution': 0., 'counted': True,
            'file_sha': 'a' * 64, 'file_name': '订单台日期时点成本', 'sheet': '订单台',
            'row_no': 2, **extra}


def save(ws, rows):
    counts = order_issues.counts(rows)
    rid = ws.record('taobao_msy387nx', '2026-07', {
        'can_close': False, 'order_issue_counts': counts,
        'findings': [{'id': key, 'name': order_issues.TITLES[key], 'passed': False,
                      'message': f"{value['orders']} 笔待核对订单"} for key, value in counts.items()],
    }, [])
    rows.write_parquet(order_issues.path_for(ws, rid))
    seal(order_issues.path_for(ws, rid))
    return rid


def test_complete_lists_preserve_product_lines_and_exact_order_ids(tmp_path):
    # More than the previous head(100), with two products in the same order.
    frame = pl.DataFrame([facts(order=str(3310000000000000001 + i), row_no=i + 2)
                          for i in range(125)] + [facts(sku='DF02', row_no=127)])
    rows = order_issues.build(frame, frame.head(0), missing_supplier_costs(frame))
    ws = Workspace(tmp_path)
    rid = save(ws, rows)
    result = order_issues.page(ws, rid, 'dropship_cost_evidence', offset=100, limit=50)
    assert result['order_count'] == 125 and result['total'] == 126
    assert len(result['items']) == 26 and len(result['order_ids']) == 125
    result = order_issues.page(ws, rid, 'dropship_cost_evidence', q='DF02')
    assert result['total'] == 1 and result['order_ids'] == ['3310000000000000001']
    exported = list(csv.DictReader(io.StringIO(order_issues.export_csv(ws, rid, 'dropship_cost_evidence'))))
    assert len(exported) == 126
    assert exported[0]['平台订单号（原记录）'] == '="3310000000000000001"'
    assert exported[0]['聚水潭订单号'] == '15881234'


def test_saved_scope_matches_check_and_keeps_historical_remark(tmp_path):
    from ledger.engine.types import ANCHOR_SHA, ANCHOR_FILE, ANCHOR_SHEET, ANCHOR_ROW
    frame = pl.DataFrame([facts(source_note='代发范围待确认：备注未明确对应商品')])
    source = SimpleNamespace(frame=pl.DataFrame({ANCHOR_SHA: ['a' * 64],
        ANCHOR_FILE: ['订单台日期时点成本'],
        ANCHOR_SHEET: ['订单台'], ANCHOR_ROW: [2], 'order_remark': ['DF01 代发待确认'], 'quantity': [3]}))
    rows = order_issues.build(frame, frame, pl.DataFrame(), [source])
    assert rows['order_remark'].item() == 'DF01 代发待确认'
    assert rows['quantity'].item() == '3'
    ws = Workspace(tmp_path)
    rid = save(ws, rows)
    source.frame = source.frame.with_columns(pl.lit('今天改了备注').alias('order_remark'))
    assert order_issues.page(ws, rid, 'dropship_scope_evidence')['items'][0]['order_remark'] == 'DF01 代发待确认'
    # The monetary source is not mutated by evidence capture.
    assert frame['contribution'].item() == 0.


def test_order_context_cannot_come_from_another_synthetic_feed_table():
    from ledger.engine.types import ANCHOR_SHA, ANCHOR_FILE, ANCHOR_SHEET, ANCHOR_ROW
    frame = pl.DataFrame([facts(source_note='代发范围待确认：备注未明确对应商品')])
    unrelated = SimpleNamespace(frame=pl.DataFrame({ANCHOR_SHA: ['a' * 64],
        ANCHOR_FILE: ['订单台订单明细'], ANCHOR_SHEET: ['订单台'], ANCHOR_ROW: [2],
        'order_remark': ['另一张表的备注'], 'quantity': [10]}))
    rows = order_issues.build(frame, frame, pl.DataFrame(), [unrelated])
    assert rows['order_remark'].item() is None and rows['quantity'].item() is None


def test_service_seals_the_complete_order_issue_archive(tmp_path):
    from ledger.service import _keep_facts
    frame = pl.DataFrame([facts()])
    rows = order_issues.build(frame, frame.head(0), missing_supplier_costs(frame))
    ws = Workspace(tmp_path)
    rid = ws.record('taobao_msy387nx', '2026-07', {'can_close': False,
        'order_issue_counts': order_issues.counts(rows)}, [])
    _keep_facts(ws, rid, SimpleNamespace(facts=frame, order_issue_rows=rows))
    assert order_issues.page(ws, rid, 'dropship_cost_evidence')['order_count'] == 1


def test_supplier_list_uses_projected_target_not_whole_order(tmp_path):
    frame = pl.DataFrame([facts(order='MAIN,store:MAIN', link_key='child1'),
                          facts(order='MAIN,store:MAIN', sku='DF02', link_key='child2', row_no=3)])
    projected = pl.DataFrame([
        dict(metric_id='goods_cost', store='天猫皇莉诗旗舰店', period='2026-07', link_key='child1', spine_row=1, amount=0.),
        dict(metric_id='goods_cost', store='天猫皇莉诗旗舰店', period='2026-07', link_key='child2', spine_row=2, amount=0.),
        dict(metric_id='dropship_cost', store='天猫皇莉诗旗舰店', period='2026-07', link_key='MAIN', spine_row=1, amount=-12.),
    ])
    rows = order_issues.build(frame, frame.head(0), missing_supplier_costs(frame, projected))
    assert rows['sku'].to_list() == ['DF02']


def test_missing_or_mismatched_evidence_is_an_error_not_an_empty_list(tmp_path):
    frame = pl.DataFrame([facts()])
    rows = order_issues.build(frame, frame.head(0), missing_supplier_costs(frame))
    ws = Workspace(tmp_path)
    rid = save(ws, rows)
    path = order_issues.path_for(ws, rid)
    rows.head(0).write_parquet(path)
    with pytest.raises(WorkspaceError, match='校验'):
        order_issues.page(ws, rid, 'dropship_cost_evidence')
    seal(path)
    with pytest.raises(WorkspaceError, match='数量'):
        order_issues.page(ws, rid, 'dropship_cost_evidence')
    path.unlink()
    with pytest.raises(WorkspaceError, match='缺失'):
        order_issues.page(ws, rid, 'dropship_cost_evidence')


def test_legacy_scope_recovers_from_sealed_run_without_recomputing(tmp_path):
    ws = Workspace(tmp_path)
    rid = ws.record('taobao_msy387nx', '2026-07', {'can_close': False,
        'findings': [{'id': 'dropship_scope_evidence', 'passed': False, 'message': '1 笔订单备注有代发字样'}]}, [])
    frame = pl.DataFrame([facts(source_note='代发范围待确认：备注未明确对应商品'),
                          facts(order='OTHER', source_note='普通成本', row_no=3)])
    frame.write_parquet(ws.facts_path(rid)); seal(ws.facts_path(rid))
    before = ws.state_by_run(rid).result
    result = order_issues.page(ws, rid, 'dropship_scope_evidence')
    assert result['order_ids'] == ['3310000000000000001']
    assert result['items'][0]['order_remark'] is None
    assert ws.state_by_run(rid).result == before


def test_legacy_supplier_recovers_archived_child_to_parent_relationships(tmp_path):
    model = load_model(api.DEFAULT_MODEL)
    ws = Workspace(tmp_path)
    cid = 'archived'
    rid = ws.record('taobao_msy387nx', '2026-07', {'can_close': False, 'platform': 'taobao',
        'commission': {'calculation_id': cid}, 'findings': [{'id': 'dropship_cost_evidence',
            'passed': False, 'message': '1 笔代发订单未找到已入账的代发支出'}]}, [])
    frame = pl.DataFrame([facts(order='MAIN,store:MAIN', link_key='CHILD'),
        facts(order='OTHER', link_key='OTHERCHILD', row_no=3),
        facts(order='MAIN', metric_id='dropship_cost', amount=-12., contribution=-12., sku=None)])
    frame.write_parquet(ws.facts_path(rid)); seal(ws.facts_path(rid))
    folder = tmp_path / 'commission'; (folder / 'calculations').mkdir(parents=True)
    archive = folder / 'calculations' / 'archived.parquet'
    pl.DataFrame({'spine_row': [1, 2], 'order_id': ['MAIN', 'OTHER'],
                  'sub_order_id': ['CHILD', 'OTHERCHILD']}).write_parquet(archive)
    with sqlite3.connect(folder / 'registry.db') as conn:
        conn.execute('create table calculation(id,finance_run,store_id,period,path,sha,model_json)')
        conn.execute('insert into calculation values(?,?,?,?,?,?,?)',
            (cid, rid, 'taobao_msy387nx', '2026-07', archive.name,
             hashlib.sha256(archive.read_bytes()).hexdigest(), model.model_dump_json()))
    result = order_issues.page(ws, rid, 'dropship_cost_evidence')
    assert result['order_ids'] == ['OTHER']


def test_api_list_and_export_use_same_saved_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'WORKSPACE_ROOT', tmp_path / 'space')
    monkeypatch.setattr(api, '_ws', None)
    with TestClient(api.app) as client:
        frame = pl.DataFrame([facts()])
        rows = order_issues.build(frame, frame.head(0), missing_supplier_costs(frame))
        rid = save(api.workspace(), rows)
        url = f'/api/runs/{rid}/order-issues/dropship_cost_evidence'
        body = client.get(url).json()
        assert body['order_ids'] == ['3310000000000000001']
        assert client.get(url + '?q=absent').json()['total'] == 0
        response = client.get(url + '/export.csv')
        assert response.status_code == 200 and '3310000000000000001' in response.text
        assert client.get(f'/api/runs/{rid}/order-issues/not-supported').status_code == 409
        order_issues.path_for(api.workspace(), rid).unlink()
        assert client.get(url).status_code == 409
        assert client.get(url + '/export.csv').status_code == 409
    for issue in order_issues.TITLES:
        assert finding_action({'id': issue}, load_model(api.DEFAULT_MODEL))['orders'] == issue


def test_copyable_ids_remove_only_explicit_header_prefixes():
    assert order_issues.platform_ids('3311038992241005394,13954149:3311038992241005394') == ['3311038992241005394']
    assert order_issues.platform_ids('260731-672483264002045') == ['260731-672483264002045']
    assert order_issues.platform_ids('123456789,42:987654321') == ['123456789', '987654321']
    assert order_issues.platform_ids('备注:12345待确认') == []
