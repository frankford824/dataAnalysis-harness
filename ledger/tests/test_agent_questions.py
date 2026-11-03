import json
import re

import pytest

from ledger import agent_questions as qa
from test_agent_authority import HUMAN, ungranted

WORKER = {'id': 'worker-1', 'kind': 'service', 'authenticated': True, 'scopes': ['business'], 'capabilities': ['agent_control']}


def computed(tmp_path):
    ws, model, rid = ungranted(tmp_path)
    row = ws.latest_run('s', '2026-07');result = json.loads(row['result'])
    result['statement'] = [{**n, 'available': True, 'value': 128.2} if n['id'] == 'n_receipt' else n for n in result['statement']]
    ws.record('s', '2026-07', result, json.loads(row['file_shas']) if isinstance(row.get('file_shas'), str) else [], model_revision=row['model_revision'])
    return ws, model, rid


def asked(tmp_path, text='为什么本月销售收入是这个数？'):
    ws, model, rid = computed(tmp_path)
    question = qa.ask(ws, model, request_id='q1', store_id='s', period='2026-07', question=text, actor=HUMAN)
    return ws, model, question


def test_model_context_has_structure_and_signs_but_no_amounts(tmp_path):
    ws, model, question = asked(tmp_path)
    assert question['state'] == 'queued'
    claimed = qa.claim(ws, model, WORKER)
    raw = json.dumps(claimed, ensure_ascii=False)
    assert '128.2' not in raw and not re.search(r'(?<![0-9a-f])128(?![0-9a-f])', raw)
    node = next(n for n in claimed['context']['statement'] if n['id'] == 'n_receipt')
    assert node['sign'] == 'positive' and node['available'] is True and node['name'] == '销售收入'
    assert claimed['context']['store_name'] == 'shop' and claimed['question'] == question['question']


def test_engine_renders_cited_values_in_the_answer(tmp_path):
    ws, model, question = asked(tmp_path)
    claimed = qa.claim(ws, model, WORKER)
    assert qa.answer(ws, model, claimed['question_id'], WORKER, lease_token=claimed['lease_token'],
        text='本月销售收入为{{node:n_receipt}}，来自 shop 在2026-07的结算流水。')['status'] == 'answered'
    item = qa.listing(ws, 's', '2026-07')['items'][0]
    assert item['state'] == 'answered' and '128.20 元' in item['answer'] and item['numbers_from'] == 'engine_run'
    stored = ws.conn.execute('SELECT answer FROM agent_question').fetchone()[0]
    assert '{{node:n_receipt}}' in stored and '128' not in stored


def test_period_wording_is_not_an_invented_number(tmp_path):
    ws, model, question = asked(tmp_path)
    claimed = qa.claim(ws, model, WORKER)
    assert qa.answer(ws, model, claimed['question_id'], WORKER, lease_token=claimed['lease_token'],
        text='2026年7月（7月）的每一笔收入合计为{{node:n_receipt}}。')['status'] == 'answered'


@pytest.mark.parametrize('text', ['销售收入大约130元', '共有三笔订单', '利润率为12%', '2026年7月有5笔'])
def test_invented_numbers_are_rejected_not_displayed(tmp_path, text):
    ws, model, question = asked(tmp_path)
    claimed = qa.claim(ws, model, WORKER)
    assert qa.answer(ws, model, claimed['question_id'], WORKER, lease_token=claimed['lease_token'], text=text)['status'] == 'rejected'
    item = qa.listing(ws, 's', '2026-07')['items'][0]
    assert item['state'] == 'failed' and item['answer'] == '' and '无法由账本核实' in item['failure']


def test_unknown_placeholder_and_foreign_or_stale_lease_are_refused(tmp_path):
    ws, model, question = asked(tmp_path)
    claimed = qa.claim(ws, model, WORKER)
    with pytest.raises(qa.QuestionError):
        qa.answer(ws, model, claimed['question_id'], WORKER, lease_token=claimed['lease_token'], text='利润{{node:invented}}')
    with pytest.raises(qa.QuestionError):
        qa.answer(ws, model, claimed['question_id'], {**WORKER, 'id': 'other'}, lease_token=claimed['lease_token'], text='好的')
    with pytest.raises(qa.QuestionError):
        qa.answer(ws, model, claimed['question_id'], WORKER, lease_token=claimed['lease_token']+1, text='好的')
    with pytest.raises(qa.QuestionError):
        qa.answer(ws, model, claimed['question_id'], WORKER, lease_token=claimed['lease_token'], text='好的', clock=lambda: 2**40)


def test_retryable_failure_requeues_until_attempts_are_exhausted(tmp_path):
    ws, model, question = asked(tmp_path)
    for attempt in range(qa.MAX_ATTEMPTS):
        claimed = qa.claim(ws, model, WORKER)
        result = qa.fail(ws, claimed['question_id'], WORKER, lease_token=claimed['lease_token'], reason='model_unavailable', retryable=True)
        assert result['status'] == ('failed' if attempt == qa.MAX_ATTEMPTS-1 else 'queued')
    assert qa.claim(ws, model, WORKER) is None
    assert '预算' in qa.listing(ws, 's', '2026-07')['items'][0]['failure']


def test_abandoned_claim_is_shown_as_failed_after_last_attempt(tmp_path):
    ws, model, question = asked(tmp_path)
    for _ in range(qa.MAX_ATTEMPTS):
        assert qa.claim(ws, model, WORKER, lease_seconds=0)
    assert qa.claim(ws, model, WORKER) is None
    assert qa.listing(ws, 's', '2026-07')['items'][0]['state'] == 'failed'


def test_only_authenticated_humans_ask_and_request_ids_are_idempotent(tmp_path):
    ws, model, question = asked(tmp_path)
    assert qa.ask(ws, model, request_id='q1', store_id='s', period='2026-07', question=question['question'], actor=HUMAN)['question_id'] == question['question_id']
    with pytest.raises(qa.QuestionError):
        qa.ask(ws, model, request_id='q1', store_id='s', period='2026-07', question='另一个问题', actor=HUMAN)
    with pytest.raises(PermissionError):
        qa.ask(ws, model, request_id='q2', store_id='s', period='2026-07', question='自问自答', actor=WORKER)
    with pytest.raises(qa.QuestionError):
        qa.ask(ws, model, request_id='q3', store_id='s', period='2026-09', question='没有核算的月份', actor=HUMAN)


def test_http_round_trip_between_human_and_worker(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ledger import automation_api, worker_control_api
    ws, model, rid = computed(tmp_path)
    who = {'value': HUMAN}
    app = FastAPI()
    automation_api.install(app, lambda: ws, lambda: model, actor=lambda request: who['value'])
    worker_control_api.install(app, lambda: ws, lambda: model, lambda request: who['value'])
    client = TestClient(app)
    asked = client.post('/api/automation/questions', json={'request_id': 'r', 'store_id': 's', 'period': '2026-07', 'question': '收入从哪来'})
    assert asked.status_code == 200, asked.text
    assert client.post('/api/v1/agent-questions/claim', json={}).status_code == 403
    who['value'] = WORKER
    assert client.post('/api/automation/questions', json={'request_id': 'r2', 'store_id': 's', 'period': '2026-07', 'question': '自问'}).status_code == 403
    claimed = client.post('/api/v1/agent-questions/claim', json={'agent_id': 'worker-1'}).json()
    done = client.post(f"/api/v1/agent-questions/{claimed['question_id']}/answer", json={'lease_token': claimed['lease_token'], 'text': '收入为{{node:n_receipt}}'})
    assert done.json()['status'] == 'answered'
    assert client.post('/api/v1/agent-questions/claim', json={'agent_id': 'worker-1'}).status_code == 204
    items = client.get('/api/automation/questions', params={'store_id': 's', 'period': '2026-07'}).json()['items']
    assert '128.20 元' in items[0]['answer']
