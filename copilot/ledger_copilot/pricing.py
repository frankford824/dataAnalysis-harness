"""百炼按量计费（华北 2 北京，人民币/百万 token，原价，不含活动优惠）。

来源：https://help.aliyun.com/zh/model-studio/model-pricing （2026-11-04 抓取）
- 阶梯计费按单次请求的输入 token 总量定档，整次请求都按该档单价。
- deepseek 系列分忙时（北京时间 08:00-22:00）和闲时单价。
- 隐式缓存命中的输入 token 通常按输入单价的 20% 计费；显式缓存（cache_control）写入按 125%、
  命中按 10% 计费，有效期 5 分钟，命中后续期（/zh/model-studio/context-cache）。
- 模型内置联网搜索按次计费（turbo 策略 3 元/千次，见 /zh/model-studio/web-search）；
  用量表里这一行的 tokens_in 记的是搜索次数。
可在 ROOT/prices.json 按同样结构覆盖或补充。
"""
import json
from datetime import datetime

from . import config

CACHE_HIT = 0.2
EXPLICIT_HIT = 0.1
EXPLICIT_WRITE = 1.25
SOURCE = 'https://help.aliyun.com/zh/model-studio/model-pricing'
VERSION = 'bailian-cn-beijing-2026-11-04'

WEB_SEARCH = 'web-search'
PER_CALL = {WEB_SEARCH: 0.003}

# tiers: [(输入上限 token, 输入单价, 输出单价)]；peak: 闲时单价 (输入, 输出)
PRICES = {
    'qwen3.8-max': {'tiers': [(1_000_000, 12, 36)]},
    'qwen3.8-flash': {'tiers': [(1_000_000, 0.8, 2.7)]},
    'qwen3.7-plus': {'tiers': [(256_000, 2, 8)]},
    'qwen3-vl-plus': {'tiers': [(32_000, 1, 10), (128_000, 1.5, 15), (256_000, 3, 30)]},
    'kimi-k2.7-code': {'tiers': [(None, 6.5, 27)]},
    'kimi-k3': {'tiers': [(None, 20, 100)]},
    'glm-5.3': {'tiers': [(None, 8, 28)]},
    'deepseek-v4-pro': {'tiers': [(None, 12, 24)]},
    'deepseek-v4.1-flash': {'tiers': [(None, 2, 8)], 'offpeak': (1, 4)},
    'deepseek-v4.1-flash-1017': {'tiers': [(None, 2, 8)], 'offpeak': (1, 4)},
    'deepseek-v4-flash-0731': {'tiers': [(None, 3, 9)], 'offpeak': (1.5, 4.5)},
}


def table():
    path = config.ROOT / 'prices.json'
    if path.exists():
        return {**PRICES, **json.loads(path.read_text(encoding='utf-8'))}
    return PRICES


def rates(model, tokens_in, at=None):
    spec = table().get(model)
    if spec is None:
        return None
    tier = next((t for t in spec['tiers'] if t[0] is None or tokens_in <= t[0]), spec['tiers'][-1])
    price_in, price_out = tier[1], tier[2]
    when = at or datetime.now()
    if spec.get('offpeak') and not 8 <= when.hour < 22:
        price_in, price_out = spec['offpeak']
    return price_in, price_out


def _per_call(model, calls):
    return (calls or 0) * PER_CALL[model]


def cost(model, tokens_in, tokens_out, cached=0, at=None, written=0, explicit=False):
    """Unknown models are charged at the most expensive listed rate, never zero."""
    if model in PER_CALL:
        return _per_call(model, tokens_in)
    found = rates(model, tokens_in or 0, at)
    price_in, price_out = found or (20, 100)
    tokens_in, tokens_out = tokens_in or 0, tokens_out or 0
    cached = min(cached or 0, tokens_in)
    written = min(written or 0, tokens_in - cached)
    plain = tokens_in - cached - written
    hit = EXPLICIT_HIT if explicit else CACHE_HIT
    return (plain * price_in + cached * price_in * hit + written * price_in * EXPLICIT_WRITE
            + tokens_out * price_out) / 1_000_000


def saved(model, tokens_in, cached, explicit=False, at=None):
    found = rates(model, tokens_in or 0, at) if model not in PER_CALL else None
    if not found:
        return 0.0
    return (cached or 0) * found[0] * (1 - (EXPLICIT_HIT if explicit else CACHE_HIT)) / 1_000_000


def worker_price(model):
    price_in, price_out = rates(model, 0)
    return {'version': VERSION, 'currency': 'CNY', 'input_per_million': str(price_in),
            'output_per_million': str(price_out), 'cached_input_per_million': str(round(price_in * CACHE_HIT, 4))}
