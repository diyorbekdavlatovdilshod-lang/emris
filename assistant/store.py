"""Persistent, file-backed state for the assistant.

Everything lives under ``data/`` (a Railway Volume in production) so history,
pause/limit state and de-duplication all survive restarts and redeploys.

Writes are atomic (temp file + :func:`os.replace`) and files are created with
owner-only permissions on POSIX. On Windows the permission bits are a no-op,
which is fine: the volume is private there anyway.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional


def _chmod(path: str, mode: int) -> None:
    """Best-effort chmod; silently ignored where the OS does not support it."""
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) as a private (0700) directory."""
    path.mkdir(parents=True, exist_ok=True)
    _chmod(str(path), 0o700)
    return path


class JsonFile:
    """A single JSON document on disk with atomic writes and a safe default."""

    def __init__(self, path: Path, default: Any) -> None:
        self.path = Path(path)
        self._default = default

    def read(self) -> Any:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            # Missing or corrupt file -> start from the default.
            return _copy_default(self._default)

    def write(self, data: Any) -> None:
        ensure_private_dir(self.path.parent)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".tmp-", suffix=".json"
        )
        try:
            _chmod(tmp_name, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _copy_default(default: Any) -> Any:
    if isinstance(default, dict):
        return dict(default)
    if isinstance(default, list):
        return list(default)
    return default


class HistoryStore:
    """Per-(business connection, chat) rolling message history."""

    def __init__(self, path: Path, limit: int) -> None:
        self._file = JsonFile(path, default={})
        self._limit = max(1, int(limit))
        self._data: Dict[str, List[Dict[str, Any]]] = self._file.read()
        if not isinstance(self._data, dict):
            self._data = {}

    @staticmethod
    def key(connection_id: str, chat_id: int) -> str:
        return f"{connection_id}:{chat_id}"

    def get(self, connection_id: str, chat_id: int) -> List[Dict[str, Any]]:
        return list(self._data.get(self.key(connection_id, chat_id), []))

    def add(
        self,
        connection_id: str,
        chat_id: int,
        *,
        role: str,
        name: str,
        text: str,
        lang: Optional[str],
        ts: float,
    ) -> None:
        key = self.key(connection_id, chat_id)
        items = self._data.setdefault(key, [])
        items.append(
            {"role": role, "name": name, "text": text, "lang": lang, "ts": ts}
        )
        if len(items) > self._limit:
            del items[: len(items) - self._limit]
        self._file.write(self._data)

    def last_language(self, connection_id: str, chat_id: int) -> Optional[str]:
        items = self._data.get(self.key(connection_id, chat_id), [])
        for item in reversed(items):
            lang = item.get("lang")
            if lang:
                return str(lang)
        return None

    def chat_count(self) -> int:
        return sum(1 for items in self._data.values() if items)


class SeenMessages:
    """Bounded, persisted set of processed message keys for de-duplication."""

    def __init__(self, path: Path, capacity: int = 5000) -> None:
        self._file = JsonFile(path, default=[])
        self._capacity = max(1, int(capacity))
        raw = self._file.read()
        self._seen: "OrderedDict[str, bool]" = OrderedDict()
        if isinstance(raw, list):
            for key in raw[-self._capacity :]:
                self._seen[str(key)] = True

    def first_time(self, key: str) -> bool:
        """True the first time ``key`` is seen; False on every later call."""
        if key in self._seen:
            return False
        self._seen[key] = True
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        self._file.write(list(self._seen.keys()))
        return True


class RuntimeState:
    """Owner-controlled runtime flags that must survive restarts."""

    def __init__(self, path: Path) -> None:
        self._file = JsonFile(path, default={"paused": False, "limit_until": 0.0})
        raw = self._file.read()
        if not isinstance(raw, dict):
            raw = {}
        self._paused = bool(raw.get("paused", False))
        try:
            self._limit_until = float(raw.get("limit_until", 0.0) or 0.0)
        except (TypeError, ValueError):
            self._limit_until = 0.0

    def _save(self) -> None:
        self._file.write({"paused": self._paused, "limit_until": self._limit_until})

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = bool(value)
        self._save()

    @property
    def limit_until(self) -> float:
        return self._limit_until

    @limit_until.setter
    def limit_until(self, value: float) -> None:
        self._limit_until = float(value or 0.0)
        self._save()
