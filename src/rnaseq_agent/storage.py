from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


_PROJECT_STATE_LOCKS_GUARD = threading.Lock()
_PROJECT_STATE_LOCKS: dict[str, Any] = {}


class _ProjectStateLock:
    """Re-entrant thread lock backed by an OS lock for cross-process CAS."""

    def __init__(self, config_path: Path) -> None:
        self._thread_lock = threading.RLock()
        self._local = threading.local()
        self._lock_path = config_path.parent / ".project-state.lock"

    def __enter__(self):
        self._thread_lock.acquire()
        depth = int(getattr(self._local, "depth", 0))
        handle = None
        try:
            if depth == 0:
                self._lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = self._lock_path.open("a+b")
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                self._acquire_file_lock(handle)
                self._local.handle = handle
            self._local.depth = depth + 1
            return self
        except BaseException:
            if handle is not None:
                handle.close()
            self._thread_lock.release()
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        depth = int(getattr(self._local, "depth", 1)) - 1
        self._local.depth = depth
        try:
            if depth == 0:
                handle = self._local.handle
                try:
                    self._release_file_lock(handle)
                finally:
                    handle.close()
                    del self._local.handle
        finally:
            self._thread_lock.release()

    @staticmethod
    def _acquire_file_lock(handle) -> None:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    @staticmethod
    def _release_file_lock(handle) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def project_state_lock(config_path: Path):
    """Return the re-entrant process and filesystem lock for live config.

    The lock is re-entrant because callers such as ``record_qc_decision``
    perform a compare-and-write transaction and then reuse ``_update_status``.
    The filesystem lock makes read/compare/write sections exclusive across
    worker processes and is released by the OS if a process exits abruptly.
    """
    key = str(Path(config_path).resolve())
    with _PROJECT_STATE_LOCKS_GUARD:
        lock = _PROJECT_STATE_LOCKS.get(key)
        if lock is None:
            lock = _ProjectStateLock(Path(config_path).resolve())
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
