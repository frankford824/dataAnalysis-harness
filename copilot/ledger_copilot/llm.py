"""OpenAI 兼容网关的流式调用，带工具调用和月度费用上限。"""
import json
import urllib.error
import urllib.request

from . import config, store


class BudgetExhausted(RuntimeError):
    pass


def _price(model_id):
    for spec in config.models().values():
        if spec['id'] == model_id:
            return spec['in'], spec['out']
    return 8.0, 32.0


def _record(model_id, usage):
    tokens_in = int(usage.get('prompt_tokens') or 0)
    tokens_out = int(usage.get('completion_tokens') or 0)
    price_in, price_out = _price(model_id)
    cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    store.add_usage(model_id, tokens_in, tokens_out, cost)
    return tokens_in + tokens_out, cost


def _open(model_id, messages, tools, max_tokens, stream, timeout):
    if store.month_cost() >= config.MONTH_BUDGET_CNY:
        raise BudgetExhausted('本月模型费用已到上限')
    base, key = config.gateway()
    body = {'model': model_id, 'messages': messages, 'max_tokens': max_tokens, 'stream': stream}
    if stream:
        body['stream_options'] = {'include_usage': True}
    if tools:
        body['tools'] = tools
    request = urllib.request.Request(base + '/chat/completions', json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    return urllib.request.urlopen(request, timeout=timeout)


def stream(model_id, messages, tools=None, *, max_tokens=4096, timeout=180, stop=None):
    """Yield ('text', str), then finally ('end', {'tool_calls': [...], 'tokens': n, 'cost': x})."""
    calls = {}
    usage = None
    with _open(model_id, messages, tools, max_tokens, True, timeout) as response:
        for raw in response:
            if stop is not None and stop.is_set():
                break
            line = raw.decode('utf-8', 'replace').strip()
            if not line.startswith('data:'):
                continue
            data = line[5:].strip()
            if data == '[DONE]':
                break
            chunk = json.loads(data)
            if chunk.get('usage'):
                usage = chunk['usage']
            for choice in chunk.get('choices') or []:
                delta = choice.get('delta') or {}
                if delta.get('content'):
                    yield 'text', delta['content']
                for call in delta.get('tool_calls') or []:
                    slot = calls.setdefault(call.get('index', 0), {'id': '', 'name': '', 'arguments': ''})
                    slot['id'] = call.get('id') or slot['id']
                    function = call.get('function') or {}
                    slot['name'] += function.get('name') or ''
                    slot['arguments'] += function.get('arguments') or ''
    tokens, cost = _record(model_id, usage or {})
    yield 'end', {'tool_calls': [calls[i] for i in sorted(calls)], 'tokens': tokens, 'cost': cost}


def complete(model_id, messages, *, max_tokens=2048, timeout=120):
    with _open(model_id, messages, None, max_tokens, False, timeout) as response:
        data = json.loads(response.read())
    tokens, cost = _record(model_id, data.get('usage') or {})
    return data['choices'][0]['message'].get('content') or '', tokens, cost


def describe(error):
    if isinstance(error, urllib.error.HTTPError):
        try:
            return f'{error.code} {error.read().decode("utf-8", "replace")[:300]}'
        except Exception:
            return str(error.code)
    return f'{type(error).__name__}: {error}'
