import hashlib
import json

import httpx

from ledger_agent import qa
from ledger_agent.qa_evaluate import CASES, _context, grade
from ledger_agent.runtime import Config, ModelSpec
from ledger_agent.state import State

PRICE = {'version': 'unit', 'currency': 'CNY', 'input_per_million': '2', 'output_per_million': '8'}
BASE = 'https://gateway.example/v1'


def evaluated(tmp_path, **change):
    report = tmp_path/'evaluation.json'
    body = {'passed': True, 'models': ['m'], 'skills': ['qa'], 'case_count': 20, 'rounds': 5, 'trials': 100, 'correct': 100,
            'false_completions': 0, 'qa_sha': qa.qa_identity(), 'inputs_unchanged': True,
            'provider_sha': hashlib.sha256(BASE.encode()).hexdigest(), **change}
    report.write_text(json.dumps(body))
    return ModelSpec('m', skills=('qa',), price=PRICE, evaluation_path=str(report), evaluation_sha=hashlib.sha256(report.read_bytes()).hexdigest())


def answerer(tmp_path, reply, spec=None, seen=None):
    def handle(request):
        if seen is not None:seen.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': reply}}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    config = Config(BASE, 'key', (spec or evaluated(tmp_path),))
    return qa.Answerer(config, State(tmp_path/'state'), client=httpx.Client(transport=httpx.MockTransport(handle)))


def claimed(question='净利润怎么算的？'):
    return {'question_id': 'q1', 'question': question, 'context': _context()}


def test_guard_matches_ledger_rule():
    context = _context()
    assert qa.guard('净利润为{{node:net_profit}}，由毛利减去兼职分摊。', context) is None
    assert qa.guard('示例旗舰店在2026-05的销售收入为{{node:n_receipt}}', context) is None
    assert qa.guard('大约 120 元', context) == 'unsupported_number'
    assert qa.guard('共有三笔', context) == 'unsupported_number'
    assert qa.guard('{{node:invented}}', context) == 'unknown_placeholder'


def test_only_a_current_passing_qa_evaluation_qualifies(tmp_path):
    config = Config(BASE, 'key', ())
    assert qa.qualified(config, evaluated(tmp_path))
    assert not qa.qualified(config, evaluated(tmp_path, qa_sha='0'*64))
    assert not qa.qualified(config, evaluated(tmp_path, correct=99))
    assert not qa.qualified(Config('https://other.example/v1', 'key', ()), evaluated(tmp_path))
    spec = evaluated(tmp_path)
    assert not qa.qualified(config, ModelSpec('m', skills=('evidence',), price=PRICE, evaluation_path=spec.evaluation_path, evaluation_sha=spec.evaluation_sha))


def test_answer_sends_no_amounts_and_reserves_budget(tmp_path):
    seen = []
    result = answerer(tmp_path, '净利润为{{node:net_profit}}，等于毛利加上兼职分摊。', seen=seen).answer(claimed())
    assert result == {'text': '净利润为{{node:net_profit}}，等于毛利加上兼职分摊。'}
    assert 'value' not in json.dumps(seen[0]['messages'][1], ensure_ascii=False)
    summary = State(tmp_path/'state').summary()
    assert summary['calls'] == 1 and summary['input_tokens'] == 100 and summary['estimated_cost'] == '0.00036'
    assert float(summary['budget']['committed']) > 0


def test_unqualified_or_unconfigured_model_is_never_called(tmp_path):
    calls = []
    item = answerer(tmp_path, 'x', spec=evaluated(tmp_path, qa_sha='0'*64), seen=calls)
    assert item.answer(claimed()) == {'failure': 'model_unavailable', 'retryable': False} and not calls
    blank = qa.Answerer(Config('', '', ()), State(tmp_path/'s2'))
    assert blank.answer(claimed())['failure'] == 'model_unavailable'


def test_identifiers_in_the_question_are_not_sent(tmp_path):
    calls = []
    result = answerer(tmp_path, 'x', seen=calls).answer(claimed('订单 330000000000000123 的成本是多少'))
    assert result == {'failure': 'privacy_blocked', 'retryable': False} and not calls


def test_invented_number_is_retained_locally_and_reported(tmp_path):
    assert answerer(tmp_path, '净利润大约是 300 元').answer(claimed()) == {'failure': 'unsupported_number', 'retryable': False}


def test_serve_one_posts_answer_with_lease(tmp_path):
    posted = []

    class Control:
        worker_id = 'worker-1'
        def _headers(self):return {'Authorization': 'Bearer t'}
        def _raise(self, response):response.raise_for_status()
        class client:
            @staticmethod
            def post(path, headers, json):
                posted.append((path, json))
                if path.endswith('/claim'):
                    return httpx.Response(200, json={**claimed(), 'lease_token': 3}, request=httpx.Request('POST', 'http://x'+path))
                return httpx.Response(200, json={'status': 'answered'}, request=httpx.Request('POST', 'http://x'+path))

    result = qa.serve_one(Control(), answerer(tmp_path, '毛利为{{node:gross_profit}}'))
    assert result['state'] == 'question_answered'
    assert posted[1] == ('/api/v1/agent-questions/q1/answer', {'lease_token': 3, 'text': '毛利为{{node:gross_profit}}'})


def test_evaluation_cases_are_gradeable():
    assert len(CASES) >= 20
    name, question, context, expected = CASES[0]
    assert grade('净利润为{{node:net_profit}}', context, expected) == (True, None)
    assert grade('毛利较高', context, expected) == (True, None)
    assert grade('这个月还行', context, expected) == (False, 'missing_relevant_item')
    assert qa.guard('2026年5月和5月的每一笔都按引擎计算', context) is None
    assert qa.guard('2026年5月大约有5笔', context) == 'unsupported_number'
