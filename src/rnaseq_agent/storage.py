from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


_PROJECT_STATE_LOCKS_GUARD = threading.Lock()
_PROJECT_STATE_LOCKS: dict[str, Any] = {}


def project_state_lock(config_path: Path):
    """Return the process-local lock protecting one project's live config.

    The lock is re-entrant because callers such as ``record_qc_decision``
    perform a compare-and-write transaction and then reuse ``_update_status``.
    It coordinates in-process attempt switches and QC decisions; the SQLite
    graph checkpointer remains the durable state store across restarts.
    """
    key = str(Path(config_path).resolve())
    with _PROJECT_STATE_LOCKS_GUARD:
        lock = _PROJECT_STATE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROJECT_STATE_LOCKS[key] = lock
        return lock


def user_state_dir() -> Path:
    return Path.home() / ".sysu_rnaseq_agent"


def defaults_path() -> Path:
    return user_state_dir() / "defaults.json"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.write("\n")


def load_defaults() -> dict[str, Any] | None:
    path = defaults_path()
    if not path.exists():
        return None
    return load_json(path)


def save_defaults(payload: dict[str, Any]) -> None:
    save_json(defaults_path(), payload)
