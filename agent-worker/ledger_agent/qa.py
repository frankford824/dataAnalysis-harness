"""Answer one human question from statement structure; numbers stay placeholders.

The ledger sends structure, availability, signs and reason codes, never
amounts. The model may cite ``{{node:<id>}}``; the ledger renders values from
its engine run. A model without a passing QA evaluation is not called.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace

import httpx

from .privacy import check_outbound
from .state import BudgetExhausted, budget, worst_case

SKILL = 'qa'
MAX_OUTPUT_TOKENS = 4000
PLACEHOLDER = re.compile(r'\{\{node:([A-Za-z0-9_]+)\}\}')
NUMERIC = re.compile(r'\d|[零一二三四五六七八九十百千万亿]+(?:元|块|笔|家|单|%)')
ROOT = Path(__file__).resolve().parent


def qa_identity():
    files = [ROOT/'qa.py', ROOT/'privacy.py', ROOT/'state.py', ROOT.parent/'skills'/SKILL/'SKILL.md']
    rows = [(str(p.relative_to(ROOT.parent)), hashlib.sha256(p.read_bytes()).hexdigest()) for p in files]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def prompt():
    return (ROOT.parent/'skills'/SKILL/'SKILL.md').read_text(encoding='utf-8')


def period_names(period):
    if not re.fullmatch(r'\d{4}-\d{2}', period or ''):return []
    year, month = period.split('-')
    return [period, f'{year}年{month}月', f'{year}年{int(month)}月', f'{year}年', f'{int(month)}月', f'{month}月', '每一笔', '每一单', '每一家']


def guard(text, context):
    """Same acceptance rule the ledger applies before showing an answer."""
    known = {n['id'] for n in context.get('statement', [])}
    if any(c not in known for c in PLACEHOLDER.findall(text)) or '{{' in PLACEHOLDER.sub('', text):return 'unknown_placeholder'
    remainder = PLACEHOLDER.sub('', text)
    names = [*period_names(context.get('period', '')), context.get('store_name', ''), context.get('platform', ''),
             *(n.get('name', '') for n in context.get('statement', [])), *(f.get('name', '') for f in context.get('findings', []))]
    for token in sorted({n for n in names if n}, key=len, reverse=True):remainder = remainder.replace(token, '')
    return 'unsupported_number' if NUMERIC.search(remainder) else None


def qualified(config, model):
    try:
        raw = Path(model.evaluation_path).read_bytes();result = json.loads(raw)
        return (SKILL in model.skills and hashlib.sha256(raw).hexdigest() == model.evaluation_sha
            and result.get('passed') is True and model.name in result.get('models', []) and SKILL in result.get('skills', [])
            and result.get('case_count', 0) >= 20 and result.get('rounds', 0) >= 5 and result.get('trials') == result.get('correct')
            and result.get('false_completions') == 0 and result.get('qa_sha') == qa_identity() and result.get('inputs_unchanged') is True
            and result.get('provider_sha') == hashlib.sha256(config.base_url.rstrip('/').encode()).hexdigest())
    except (OSError, ValueError, TypeError, AttributeError):
        return False


class Answerer:
    def __init__(self, config, state, *, client=None):
        self.config, self.state = config, state
        self.client = client or httpx.Client(timeout=60, follow_redirects=False, trust_env=False)

    def select(self):
        if not self.config.base_url or not self.config.api_key:return None
        candidates = [m for m in self.config.models if SKILL in m.skills and (self.config.evaluation or qualified(self.config, m))]
        return min(candidates, key=lambda m: (m.cost_per_correct is None, m.cost_per_correct or 0, m.name)) if candidates else None

    def answer(self, claimed):
        """Returns {'text': ...} or {'failure': reason, 'retryable': bool}."""
        spec = self.select()
        if spec is None or worst_case(spec.price, 0, MAX_OUTPUT_TOKENS) is None:
            return {'failure': 'model_unavailable', 'retryable': False}
        context = claimed['context']
        messages = [{'role': 'system', 'content': prompt()},
                    {'role': 'user', 'content': json.dumps({'question': claimed['question'], 'context': context}, ensure_ascii=False)}]
        try:check_outbound(messages)
        except ValueError:return {'failure': 'privacy_blocked', 'retryable': False}
        case_id = 'question:'+claimed['question_id']
        request_sha = self.state.outbound(case_id, messages)
        size = len(json.dumps(messages, ensure_ascii=False).encode())
        try:self.state.reserve(case_id, spec.name, spec.price, request_sha, size, MAX_OUTPUT_TOKENS, budget(self.config.monthly_budget))
        except BudgetExhausted:return {'failure': 'model_unavailable', 'retryable': False}
        try:
            response = self.client.post(self.config.base_url.rstrip('/')+'/chat/completions',
                headers={'Authorization': 'Bearer '+self.config.api_key},
                json={'model': spec.name, 'messages': messages, 'temperature': 0, 'max_tokens': MAX_OUTPUT_TOKENS})
            response.raise_for_status();wire = response.json()
        except (httpx.HTTPError, ValueError):
            self.state.usage(case_id, spec.name, None, request_sha, status='usage_unknown_failed')
            return {'failure': 'technical_failure', 'retryable': True}
        usage = wire.get('usage') or {}
        self.state.usage(case_id, spec.name, SimpleNamespace(input_tokens=usage.get('prompt_tokens'),
            output_tokens=usage.get('completion_tokens'),
            cache_input_tokens=(usage.get('prompt_tokens_details') or {}).get('cached_tokens')), request_sha, spec.price)
        text = (((wire.get('choices') or [{}])[0].get('message') or {}).get('content') or '').strip()
        if not text:return {'failure': 'technical_failure', 'retryable': True}
        problem = guard(text, context)
        if problem:
            self.state.evidence({'generated_text': text, 'guard': problem}, case_id)
            return {'failure': 'unsupported_number', 'retryable': False}
        return {'text': text}


def serve_one(control, answerer):
    """Claim and settle one question; None when the queue is empty."""
    response = control.client.post('/api/v1/agent-questions/claim', headers=control._headers(), json={'agent_id': control.worker_id})
    if response.status_code == 204:return None
    control._raise(response);claimed = response.json()
    outcome = answerer.answer(claimed)
    base = '/api/v1/agent-questions/'+claimed['question_id']
    if 'text' in outcome:
        response = control.client.post(base+'/answer', headers=control._headers(), json={'lease_token': claimed['lease_token'], 'text': outcome['text']})
    else:
        response = control.client.post(base+'/fail', headers=control._headers(),
            json={'lease_token': claimed['lease_token'], 'reason': outcome['failure'], 'retryable': outcome['retryable']})
    control._raise(response)
    return {'state': 'question_'+response.json().get('status', 'unknown'), 'question_id': claimed['question_id'], 'model_used': 'text' in outcome or outcome.get('failure') in {'unsupported_number', 'technical_failure'}}
