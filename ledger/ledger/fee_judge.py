"""未归类费项的预判：判断模型只选一个口径项，并给出选它的把握。

预判不是配置。它只帮人少点几下：把握高的直接预选好，把握低的标「需要财务说明」，
最后仍由人在页面上保存或在助手的确认卡上点确认才落库。

做法：已归好的科目（字典 + 规则链）整段当例子，问「这一条归哪一项」，要求只答一个
字母，读每个字母的概率。温度 0、只出一个字，同一版例子下同一条问两次答案一致；
例子一变（字典或规则改了），缓存键跟着变，自动重判。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import string
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import assist
from .fees import known_fees, major_label, major_options, pretty_unmatched_label
from .model.schema import Model

JUDGE_MODEL = "qwen3.8-flash"
#: 选中项概率到这个数才预选。字典留一测过：≥0.97 时 89 条错 3 条，≥0.9 时 122 条错 7 条。
SURE = 0.97
EXCLUDE = "__exclude__"
UNSURE = "__unsure__"
_lock = threading.Lock()


def _settings(root: Path | None) -> tuple[assist.Config, str]:
    cfg = assist.load_config(root)
    model = JUDGE_MODEL
    try:
        raw = json.loads(assist.config_path(root).read_text(encoding="utf-8"))
        model = str(raw.get("judge_model") or model)
    except (OSError, ValueError, AttributeError):
        pass
    return cfg, model


def _options(model: Model) -> list[tuple[str, str]]:
    majors = [(m["id"], m.get("name") or m["id"]) for m in major_options(model)]
    return [*majors, (EXCLUDE, "排除：不是经营流水（提现、充值、账户互转、保证金缴存），不进损益"),
            (UNSURE, "看不出来：只凭这几个字判断不了，需要财务说明")]


def _examples(model: Model) -> str:
    rows = []
    for f in known_fees(model):
        target = "排除" if f.excluded else major_label(model, f.major)
        rows.append(f"{f.platform or '*'}｜{f.field}｜{f.key} → {target}")
    return "\n".join(sorted(set(rows)))


def _key(judge_model: str, examples: str, item: dict) -> str:
    raw = json.dumps([judge_model, hashlib.sha256(examples.encode()).hexdigest(),
                      sorted(item.get("platforms") or []), item.get("field"), item.get("how"),
                      item.get("value") or item.get("label")], ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _cache_path(root: Path) -> Path:
    return root / "fee_guess.json"


def _read(root: Path) -> dict[str, Any]:
    try:
        return json.loads(_cache_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(root: Path, updates: dict[str, Any]) -> None:
    with _lock:
        data = _read(root)
        data.update(updates)
        path = _cache_path(root)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


def _ask(cfg: assist.Config, judge_model: str, examples: str, options, item: dict) -> dict[str, Any]:
    letters = string.ascii_uppercase
    menu = "\n".join(f"{letters[i]}. {name}" for i, (_, name) in enumerate(options))
    platforms = "、".join(item.get("platforms") or []) or "不限"
    what = pretty_unmatched_label(item.get("label") or item.get("value") or "")
    question = (f"平台 {platforms} 的流水里，{item.get('caption') or what}"
                f"（{item.get('count', 0)} 笔，合计 {item.get('amount', 0):.2f} 元），应归到哪一项？")
    body = json.dumps({
        "model": judge_model, "temperature": 0, "max_tokens": 1, "enable_thinking": False,
        "logprobs": True, "top_logprobs": 5,
        "messages": [
            {"role": "system", "content": "你是电商财务核算的判断器。只回答一个选项字母，不要解释。"},
            {"role": "user", "content": f"【已归好的科目（平台｜看的列｜科目或关键词 → 归到）】\n{examples}\n\n"
                                        f"【问题】{question}\n{menu}\n\n只回答一个字母。"},
        ],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(cfg.base_url + "/chat/completions", data=body, headers={
        "Authorization": "Bearer " + cfg.api_key, "Content-Type": "application/json"})
    started = time.monotonic()
    out = json.loads(urllib.request.urlopen(req, timeout=60).read())
    tops = ((out["choices"][0].get("logprobs") or {}).get("content") or [{}])[0].get("top_logprobs") or []
    probs: dict[str, float] = {}
    for t in tops:
        letter = t["token"].strip()
        if len(letter) == 1 and letter in letters[:len(options)]:
            pick = options[letters.index(letter)][0]
            probs[pick] = probs.get(pick, 0.0) + math.exp(t["logprob"])
    total = sum(probs.values()) or 1.0
    ranked = sorted(((k, v / total) for k, v in probs.items()), key=lambda kv: -kv[1])
    return {"ranked": [[k, round(v, 4)] for k, v in ranked[:3]], "model": judge_model,
            "elapsed_ms": round((time.monotonic() - started) * 1000), "at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def _view(model: Model, raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw or not raw.get("ranked"):
        return None
    pick, p = raw["ranked"][0]
    names = dict(_options(model))
    sure = p >= SURE and pick != UNSURE
    return {
        "major": "" if pick in (EXCLUDE, UNSURE) else pick,
        "exclude": pick == EXCLUDE,
        "unsure": not sure,
        "p": p,
        "name": names.get(pick, pick),
        "others": [{"id": k, "name": names.get(k, k), "p": v} for k, v in raw["ranked"][1:]],
        "model": raw.get("model"),
    }


def cached(root: Path, model: Model, items: list[dict]) -> list[dict | None]:
    cfg, judge_model = _settings(root)
    examples = _examples(model)
    data = _read(root)
    return [_view(model, data.get(_key(judge_model, examples, it))) for it in items]


def judge(root: Path, model: Model, items: list[dict]) -> list[dict | None]:
    """对每条未归类给出预判，已判过的直接读缓存。没配模型时全部返回 None。"""
    cfg, judge_model = _settings(root)
    if not cfg.ready:
        return [None] * len(items)
    examples = _examples(model)
    options = _options(model)
    data = _read(root)
    keys = [_key(judge_model, examples, it) for it in items]
    missing = [(k, it) for k, it in zip(keys, items) if k not in data]

    def one(pair):
        k, it = pair
        try:
            return k, _ask(cfg, judge_model, examples, options, it)
        except Exception:
            return k, None

    if missing:
        with ThreadPoolExecutor(8) as pool:
            fresh = {k: v for k, v in pool.map(one, missing) if v}
        if fresh:
            _write(root, fresh)
            data.update(fresh)
    return [_view(model, data.get(k)) for k in keys]


def as_rules(items: list[dict], guesses: list[dict | None], *, by: str = "") -> list[dict]:
    """把把握够的预判转成可保存的费项规则（未归类条目自带匹配列、方式和词）。"""
    rules = []
    for it, g in zip(items, guesses):
        if not g or g["unsure"]:
            continue
        platforms = it.get("platforms") or []
        rules.append({
            "platform": platforms[0] if len(platforms) == 1 else "*",
            "field": it.get("field") or "subject", "how": it.get("how") or "exact",
            "value": it.get("value") or it.get("label") or "",
            "major": g["major"], "exclude": g["exclude"], "minor": "",
            "stage": "after", "count_without_order": False,
            "note": f"按预判归类（{g['name']}，把握 {g['p']:.0%}）", "by": by,
        })
    return rules
