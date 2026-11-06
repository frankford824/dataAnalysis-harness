"""OpenAI 兼容网关的流式调用，带工具调用和月度费用上限。"""
import json
import urllib.error
import urllib.request

from . import config, pricing, store


class BudgetExhausted(RuntimeError):
    pass


def _record(model_id, usage):
    tokens_in = int(usage.get('prompt_tokens') or 0)
    tokens_out = int(usage.get('completion_tokens') or 0)
    details = usage.get('prompt_tokens_details') or {}
    cached = int(details.get('cached_tokens') or 0)
    written = int(details.get('cache_creation_input_tokens') or 0)
    explicit = details.get('cache_type') == 'ephemeral'
    cost = pricing.cost(model_id, tokens_in, tokens_out, cached, written=written, explicit=explicit)
    store.add_usage(model_id, tokens_in, tokens_out, cost, cached, written, explicit)
    searches = int(((usage.get('x_tools') or {}).get('search') or (usage.get('plugins') or {}).get('search') or {})
                   .get('count') or 0)
    if searches:
        search_cost = pricing.cost(pricing.WEB_SEARCH, searches, 0)
        store.add_usage(pricing.WEB_SEARCH, searches, 0, search_cost)
        cost += search_cost
    return tokens_in + tokens_out, cost


def _open(model_id, messages, tools, max_tokens, stream, timeout, extra=None):
    from . import costs
    if costs.month_total() >= config.MONTH_BUDGET_CNY:
        raise BudgetExhausted('本月模型费用已到上限')
    base, key = config.gateway()
    body = {'model': model_id, 'messages': messages, 'max_tokens': max_tokens, 'stream': stream}
    if stream:
        body['stream_options'] = {'include_usage': True}
    if tools:
        body['tools'] = tools
    if model_id.startswith('qwen'):
        # 百炼默认开深度思考：每步多等 5-20 秒，还按输出计费。
        body['enable_thinking'] = False
    body.update(extra or {})
    request = urllib.request.Request(base + '/chat/completions', json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    return urllib.request.urlopen(request, timeout=timeout)


# 思考等级：关 / 低 / 中 / 高，对应百炼的深度思考长度上限（token）。
THINKING = {'off': 0, 'low': 2048, 'medium': 8192, 'high': 24576}
_THINKS = ('qwen', 'deepseek', 'glm')


def thinking_extra(model_id, level):
    budget = THINKING.get(level or 'off', 0)
    if not budget or not model_id.startswith(_THINKS):
        return None
    if model_id.startswith('qwen'):
        return {'enable_thinking': True, 'thinking_budget': budget}
    return {'enable_thinking': True}


def stream(model_id, messages, tools=None, *, max_tokens=4096, timeout=180, stop=None, extra=None):
    """Yield ('text', str), then finally ('end', {'tool_calls': [...], 'tokens': n, 'cost': x})."""
    calls = {}
    usage = None
    with _open(model_id, messages, tools, max_tokens, True, timeout, extra) as response:
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


def relay_open(body, thinking='off'):
    """Pi 的模型请求转给网关：关深度思考、要用量统计、超预算拒绝；主模型报错时换备用模型再试一次。"""
    from . import costs
    if costs.month_total() >= config.MONTH_BUDGET_CNY:
        raise BudgetExhausted('本月模型费用已到上限')
    for tool in body.get('tools') or []:
        tool.pop('cache_control', None)
        (tool.get('function') or {}).pop('cache_control', None)
    if body.get('stream'):
        body['stream_options'] = {'include_usage': True}
    base, key = config.gateway()
    model_id = body.get('model', '')
    try:
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import roster
        fallback = roster.fallback_for(model_id)
    except Exception:
        fallback = None
    if not fallback:
        fallback = next((config.FALLBACK[role] for role, spec in config.models().items()
                         if spec.get('id') == model_id and role in config.FALLBACK), None)
    for candidate in [model_id, fallback] if fallback else [model_id]:
        body['model'] = candidate
        body.pop('thinking_budget', None)
        if thinking_extra(candidate, thinking):
            body.update(thinking_extra(candidate, thinking))
        elif candidate.startswith('qwen'):
            body['enable_thinking'] = False
        else:
            body.pop('enable_thinking', None)
        request = urllib.request.Request(base + '/chat/completions', json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                         headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        try:
            return urllib.request.urlopen(request, timeout=180), candidate
        except urllib.error.HTTPError as exc:
            if candidate == fallback or not fallback or exc.code == 429:
                raise


def relay_stream(response, model_id, on_usage):
    usage = None
    try:
        for raw in response:
            if raw.startswith(b'data:') and b'"usage"' in raw:
                try:
                    usage = json.loads(raw[5:].strip()).get('usage') or usage
                except ValueError:
                    pass
            yield raw
    finally:
        response.close()
        on_usage(model_id, *_record(model_id, usage or {}))


def relay_complete(response, model_id, on_usage):
    with response:
        data = response.read()
    on_usage(model_id, *_record(model_id, json.loads(data).get('usage') or {}))
    return data


def complete(model_id, messages, *, max_tokens=2048, timeout=120, extra=None):
    with _open(model_id, messages, None, max_tokens, False, timeout, extra) as response:
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
