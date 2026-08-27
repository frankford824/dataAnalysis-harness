"""进程内的不可变模型快照。"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .loader import load_model
from .schema import Model
from .transaction import model_revision


@dataclass(frozen=True, slots=True)
class ModelSnapshot:
    model: Model
    revision: str
    fingerprint: tuple[tuple[str, int, int], ...]


class ModelRepository:
    """只在模型文件实际变化时重新读取和校验。"""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self._lock = threading.RLock()
        self._snapshot: ModelSnapshot | None = None

    def _fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        out = []
        for path in sorted(self.root.iterdir()):
            if not path.is_file() or path.suffix.lower() not in {".yaml", ".csv"}:
                continue
            stat = path.stat()
            out.append((path.name, stat.st_size, stat.st_mtime_ns))
        return tuple(out)

    def get(self) -> ModelSnapshot:
        fingerprint = self._fingerprint()
        cached = self._snapshot
        if cached is not None and cached.fingerprint == fingerprint:
            return cached
        with self._lock:
            fingerprint = self._fingerprint()
            cached = self._snapshot
            if cached is not None and cached.fingerprint == fingerprint:
                return cached
            snapshot = ModelSnapshot(
                model=load_model(self.root),
                revision=model_revision(self.root),
                fingerprint=fingerprint,
            )
            self._snapshot = snapshot
            return snapshot

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None
