"""Cached local NAS and physical-link observations for health reporting."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path


_guard = threading.Lock()
_cached_at = 0.0
_cached: dict = {}


def ingest_mode() -> str:
    value = os.environ.get("LEDGER_INGEST_MODE", "api").strip().lower()
    return value if value in {"api", "nas"} else "api"


def nas_root() -> Path:
    return Path(os.environ.get("LEDGER_NAS_ROOT", r"X:\台账系统"))


def upload_path() -> str:
    return os.environ.get(
        "LEDGER_NAS_UPLOAD_PATH", r"\\192.168.0.125\dataAnalysis\台账系统\00_上传区",
    )


def _link_speed() -> tuple[str, int | None]:
    if os.name != "nt":
        return "unknown", None
    command = (
        "Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | "
        "Sort-Object LinkSpeed -Descending | Select-Object -First 1 -ExpandProperty LinkSpeed"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=3, creationflags=0x08000000, check=True,
        )
        label = completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown", None
    match = re.search(r"([\d.]+)\s*([GMK])bps", label, re.IGNORECASE)
    if not match:
        return label or "unknown", None
    value = float(match.group(1))
    unit = match.group(2).upper()
    mbps = int(value * {"G": 1000, "M": 1, "K": 0.001}[unit])
    return label, mbps


def read(force: bool = False) -> dict:
    global _cached_at, _cached
    now = time.monotonic()
    with _guard:
        if _cached and not force and now - _cached_at < 30:
            return dict(_cached)
        root = nas_root()
        speed_label, speed_mbps = _link_speed()
        _cached = {
            "ingest_mode": ingest_mode(),
            "nas_root": str(root),
            "nas_reachable": root.is_dir(),
            "upload_path": upload_path(),
            "link_speed": speed_label,
            "link_speed_mbps": speed_mbps,
            "link_degraded": speed_mbps is not None and speed_mbps < 1000,
        }
        _cached_at = now
        return dict(_cached)
