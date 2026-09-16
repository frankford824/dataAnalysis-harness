"""Per-order human amounts remain auditable across recomputes and freezes."""
import hashlib
import json
import csv
import io
from pathlib import Path

import polars as pl
import pytest

from ledger import cost_lines, manual_cost
from ledger.model.repository import ModelRepository
from ledger.storage_integrity import seal
from ledger.workspace import Workspace, WorkspaceError


def model():
    return ModelRepository(Path(__file__).resolve().parents[2] / 'models' / 'cn-ecommerce').get().model


def payload():
    return {
        'store_id':'taobao_mt9sjmls','store':'谷本文-luckyglow旗舰店',
        'period':'2026-06','can_close':False,
        'statement':[{'id':'n_receipt','value':1000,'available':True}],
        'missing_sources':[],
        'findings':[{'id':'chk_goods_coverage','name':'商品成本核对',
                     'passed':False,'blocking':True,'message':'覆盖待确认'}],
        'commission':{'people':[],'total':None},
        'calculation_inputs':{
            'metric_totals':{'trade_receipt':1000.0,'goods_cost':-600.0},
            'unavailable_metrics':['goods_cost'],'inapplicable_metrics':[],
        },
    }


def archive(ws, run, keys=('S2','S3'), *, undated=False):
    frame = pl.DataFrame({
        'coverage_key':list(keys),
        'context_sha':[hashlib.sha256(key.encode()).hexdigest() for key in keys],
        'order_id':['O'+key[1:] for key in keys],
        'sub_order_id':list(keys),
        'product_ids':['P'+key[1:] for key in keys],
        'order_date':['' if undated and key=='S2' else '2026-06-02' for key in keys],
        'quantities':['1.0' for _ in keys],
        'order_state':['已发货' for _ in keys],
        'order_count':[1 for _ in keys],
        'product_count':[1 for _ in keys],
        'editable':[not (undated and key=='S2') for key in keys],
    })
    path=ws.coverage_gaps_path(run);frame.write_parquet(path);seal(path)
    return frame


def edited_cost_export(ws, run, amounts):
    text = cost_lines.export_csv(ws, run, 'taobao_mt9sjmls', '2026-06')
    rows = list(csv.DictReader(io.StringIO(text)))
    for row in rows:
        row['人工补录总成本'] = amounts.get(row['清单键'], '')
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader(); writer.writerows(rows)
    return output.getvalue().encode('utf-8-sig')


def test_batch_cost_round_trip_is_atomic_and_preserves_source_run(tmp_path):
    ws = Workspace(tmp_path); raw = payload()
    run = ws.record(raw['store_id'], raw['period'], raw, [])
    archive(ws, run)
    content = edited_cost_export(ws, run, {'S2': '12.50', 'S3': '20.00'})
    preview = cost_lines.batch_preview(ws, raw['store_id'], raw['period'],
                                       run, content, '财务从原成本单批量核对')
    assert preview['valid'] == 2 and preview['amount_total'] == 32.5
    assert preview['issue_count'] == 0
    saved = cost_lines.batch_apply(ws, raw['store_id'], raw['period'], run,
        content, expected_file_sha=preview['file_sha'],
        expected_line_revision=preview['line_revision'],
        default_reason='财务从原成本单批量核对')
    assert saved['saved'] == 2 and saved['amount_total'] == 32.5
    assert cost_lines.current(ws, run, raw['store_id'], raw['period'])['supplement_total'] == 32.5
    assert ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0] == 2
    assert json.loads(ws.conn.execute('SELECT result FROM run WHERE id=?', (run,)).fetchone()[0]) == raw
    with pytest.raises(WorkspaceError, match='发生变化|不能提交'):
        cost_lines.batch_apply(ws, raw['store_id'], raw['period'], run,
            content, expected_file_sha=preview['file_sha'],
            expected_line_revision=preview['line_revision'],
            default_reason='财务从原成本单批量核对')
    ws.close()


def test_batch_cost_rejects_stale_row_without_partial_write(tmp_path):
    ws = Workspace(tmp_path); raw = payload()
    run = ws.record(raw['store_id'], raw['period'], raw, [])
    archive(ws, run)
    content = edited_cost_export(ws, run, {'S2': '12.50', 'S3': '20.00'})
    rows = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
    rows[1]['核对版本'] = '0' * 64
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader(); writer.writerows(rows)
    stale = output.getvalue().encode('utf-8-sig')
    preview = cost_lines.batch_preview(ws, raw['store_id'], raw['period'],
                                       run, stale, '统一核对')
    assert preview['valid'] == 1 and preview['issue_count'] == 1
    with pytest.raises(WorkspaceError, match='不能提交'):
        cost_lines.batch_apply(ws, raw['store_id'], raw['period'], run,
            stale, expected_file_sha=preview['file_sha'],
            expected_line_revision=preview['line_revision'],
            default_reason='统一核对')
    assert ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0] == 0
    ws.close()


def test_edit_and_remove_append_history_and_respect_latest_order_context(tmp_path):
    ws=Workspace(tmp_path);raw=payload()
    run=ws.record(raw['store_id'],raw['period'],raw,[])
    frame=archive(ws,run)
    first=frame.row(0,named=True)
    result=cost_lines.save(ws,raw['store_id'],raw['period'],run,
                           coverage_key='S2',context_sha=first['context_sha'],
                           amount='12.50',reason='运营核对原单',expected_line_revision=0)
    assert result['total']==2 and result['reviewed_count']==1 and result['supplement_total']==12.5
    assert json.loads(ws.conn.execute('SELECT result FROM run WHERE id=?',(run,)).fetchone()[0])==raw
    with pytest.raises(WorkspaceError,match='已被修改'):
        cost_lines.save(ws,raw['store_id'],raw['period'],run,
                        coverage_key='S2',context_sha=first['context_sha'],
                        amount='13.00',reason='旧页面',expected_line_revision=0)
    current_revision=result['items'][0]['line_revision']
    updated=cost_lines.save(ws,raw['store_id'],raw['period'],run,
                            coverage_key='S2',context_sha=first['context_sha'],
                            amount='20.00',reason='第二次核对',expected_line_revision=current_revision)
    assert updated['supplement_total']==20
    assert ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0]==2
    newer=ws.record(raw['store_id'],raw['period'],raw,[])
    archive(ws,newer,('S3',))  # Order S2 now has verified system cost.
    assert cost_lines.current(ws,newer,raw['store_id'],raw['period'])['supplement_total']==0
    assert cost_lines.current(ws,run,raw['store_id'],raw['period'])['supplement_total']==20
    ws.close()


def test_unique_undated_order_can_be_confirmed_without_fabricating_a_day(tmp_path):
    ws=Workspace(tmp_path);raw=payload()
    run=ws.record(raw['store_id'],raw['period'],raw,[])
    first=archive(ws,run,undated=True).row(0,named=True)
    assert not first['editable'] and first['order_date']==''  # An older archived gate.
    page=cost_lines.page(ws,run,raw['store_id'],raw['period'])
    assert page['items'][0]['editable'] and page['items'][0]['order_date']==''
    cost_lines.save(ws,raw['store_id'],raw['period'],run,
                    coverage_key='S2',context_sha=first['context_sha'],
                    amount='12.00',reason='确认本店本月该唯一订单成本',
                    expected_line_revision=0)
    assert cost_lines.current(ws,run,raw['store_id'],raw['period'])['supplement_total']==12
    ws.close()


def test_close_freezes_source_line_amount_and_profit_without_double_count(tmp_path):
    m=model();ws=Workspace(tmp_path);raw=payload()
    run=ws.record(raw['store_id'],raw['period'],raw,[])
    first=archive(ws,run).row(0,named=True)
    cost_lines.save(ws,raw['store_id'],raw['period'],run,
                    coverage_key='S2',context_sha=first['context_sha'],
                    amount='12.00',reason='运营人工补录',expected_line_revision=0)
    lines=cost_lines.current(ws,run,raw['store_id'],raw['period'])
    assert lines['line_revision']==1 and lines['supplement_total']==12
    trial=manual_cost.preview(m,raw,{'goods':'612.00','dropship':'0.00','reshipment':'0.00'})
    assert trial['gross']==388.0 and trial['profit']==388.0
    decided,audit=manual_cost.certified(m,raw,run,
        {'goods':'612.00','dropship':'0.00','reshipment':'0.00'},[],True,'已确认行金额')
    audit.update(line_revision=lines['line_revision'],line_reviews=lines['reviewed'])
    decided['manual_cost']['line_supplement']=12
    closed=ws.close_period(raw['store_id'],raw['period'],note='已确认行金额',
                            expected_run_id=run,manual_result=decided,manual_decision=audit)
    assert closed.closed and closed.result['manual_cost']['confirmed']['goods']==612
    assert cost_lines.current(ws,run,raw['store_id'],raw['period'])['supplement_total']==12
    ws.reopen_period(raw['store_id'],raw['period'],note='重新核查')
    newer=ws.record(raw['store_id'],raw['period'],raw,[])
    archive(ws,newer)
    cost_lines.save(ws,raw['store_id'],raw['period'],newer,
                    coverage_key='S2',context_sha=first['context_sha'],
                    amount='22.00',reason='新的核查',expected_line_revision=1)
    assert cost_lines.current(ws,newer,raw['store_id'],raw['period'])['supplement_total']==22
    assert cost_lines.current(ws,run,raw['store_id'],raw['period'])['supplement_total']==12
    ws.close()


def test_http_edit_is_visible_in_all_missing_orders_and_store_preview(tmp_path,monkeypatch):
    from contextlib import nullcontext
    from fastapi.testclient import TestClient
    from ledger import api
    from ledger.model import transaction
    m=model();ws=Workspace(tmp_path);raw=payload()
    run=ws.record(raw['store_id'],raw['period'],raw,[],
                  model_revision=hashlib.sha256(m.model_dump_json().encode()).hexdigest())
    first=archive(ws,run).row(0,named=True)
    monkeypatch.setattr(api,'WORKSPACE_ROOT',tmp_path)
    monkeypatch.setattr(api,'_ws',ws)
    monkeypatch.setattr(api,'_model',lambda:m)
    monkeypatch.setattr(transaction,'model_lock',lambda _:nullcontext())
    client=TestClient(api.app)
    rows=client.get(f'/api/runs/{run}/coverage-gaps')
    assert rows.status_code==200 and rows.json()['total']==2
    saved=client.post(f"/api/stores/{raw['store_id']}/periods/{raw['period']}/cost-lines",json={
        'run_id':run,'coverage_key':'S2','context_sha':first['context_sha'],
        'amount':'12.00','reason':'直接在页面核对','expected_line_revision':0,
    })
    assert saved.status_code==200,saved.text
    assert saved.json()['supplement_total']==12
    endpoint=f"/api/stores/{raw['store_id']}/periods/{raw['period']}/manual-cost-preview"
    body={'run_id':run,'line_revision':saved.json()['line_revision'],
          'costs':{'goods':'600.00','dropship':'0.00','reshipment':'0.00'}}
    preview=client.post(endpoint,json=body)
    assert preview.status_code==200,preview.text
    assert preview.json()['confirmed']['goods']==612 and preview.json()['profit']==388
    assert client.post(endpoint,json={**body,'line_revision':0}).status_code==409
    export=client.get(f'/api/runs/{run}/coverage-gaps.csv')
    assert export.status_code==200 and export.content.startswith(b'\xef\xbb\xbf')
    assert 'O2' in export.content.decode('utf-8-sig')
    closed=client.post(f"/api/stores/{raw['store_id']}/periods/{raw['period']}/close",json={
        **body,'note':'财务逐行核实，无提成人员','no_payout':True,
    })
    assert closed.status_code==200,closed.text
    assert ws.state(raw['store_id'],raw['period']).result['manual_cost']['confirmed']['goods']==612
    assert cost_lines.current(ws,run,raw['store_id'],raw['period'])['supplement_total']==12
    assert client.post(f"/api/stores/{raw['store_id']}/periods/{raw['period']}/cost-lines",json={
        'run_id':run,'coverage_key':'S2','context_sha':first['context_sha'],
        'amount':'20.00','reason':'结账后试图修改','expected_line_revision':1,
    }).status_code==409
    ws.close()


def test_http_batch_preview_then_apply_preserves_audited_cost_rows(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from ledger import api
    m = model(); ws = Workspace(tmp_path); raw = payload()
    run = ws.record(raw['store_id'], raw['period'], raw, [],
                    model_revision=hashlib.sha256(m.model_dump_json().encode()).hexdigest())
    archive(ws, run)
    monkeypatch.setattr(api, 'WORKSPACE_ROOT', tmp_path)
    monkeypatch.setattr(api, '_ws', ws)
    monkeypatch.setattr(api, '_model', lambda: m)
    client = TestClient(api.app)
    content = edited_cost_export(ws, run, {'S2': '12.50', 'S3': '20.00'})
    base = f"/api/stores/{raw['store_id']}/periods/{raw['period']}/cost-lines"
    uploaded = {'file': ('missing-cost.csv', content, 'text/csv')}
    preview = client.post(base + '/batch-preview',
        params={'run_id': run, 'default_reason': '财务批量核对成本'}, files=uploaded)
    assert preview.status_code == 200, preview.text
    assert preview.json()['valid'] == 2 and preview.json()['issue_count'] == 0
    saved = client.post(base + '/batch-apply', params={
        'run_id': run, 'default_reason': '财务批量核对成本',
        'expected_file_sha': preview.json()['file_sha'],
        'expected_line_revision': preview.json()['line_revision']}, files=uploaded)
    assert saved.status_code == 200, saved.text
    assert saved.json()['saved'] == 2
    assert client.get(f'/api/runs/{run}/coverage-gaps').json()['reviewed_count'] == 2
    assert ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0] == 2
    ws.close()
