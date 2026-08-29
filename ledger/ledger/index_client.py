"""Small loopback client for LedgerIndexer.

It deliberately uses the standard library: the production venv does not need another HTTP stack
just to call a service on 127.0.0.1.
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = os.environ.get("LEDGER_INDEXER_URL", "http://127.0.0.1:8765").rstrip("/")


class IndexerUnavailable(RuntimeError):
    pass


def get(path: str, params: dict | None = None, *, timeout: float = 3.0) -> dict:
    query = urlencode({key: value for key, value in (params or {}).items() if value not in (None, "")})
    url = BASE_URL + path + (f"?{query}" if query else "")
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise IndexerUnavailable(f"索引服务不可用：{exc}") from exc
    if isinstance(payload, dict) and payload.get("error"):
        raise IndexerUnavailable(str(payload["error"]))
    if not isinstance(payload, dict):
        raise IndexerUnavailable("索引服务返回了无效数据")
    return payload


def search(query: str, *, limit: int, store_id: str = "", platform: str = "", source: str = "") -> dict:
    return get("/search", {
        "q": query, "limit": limit, "store_id": store_id,
        "platform": platform, "source": source,
    }, timeout=10.0)
