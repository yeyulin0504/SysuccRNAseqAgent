"""Thread / message persistence for the workbench (UI design section 3).

Data model::

    Project
    ├── Thread A ── Message...
    ├── Thread B ── Message...

Rules:

- ``thread_id`` belongs to one and only one ``project_id``;
- messages persist user text, agent reply text, and *references* to project
  operations (plan / contract / run / artifact / checkpoint decisions). They
  never hold copies of scientific result files;
- a new thread can see the project's VALID artifacts and active run summary
  but does NOT read other threads' messages;
- archiving/renaming a thread never touches project inputs, runs or artifacts;
- v1 offers archive only (no cascade delete of project data).

Storage layout (per project directory)::

    <project_dir>/threads/<thread_id>.json   # {meta, messages:[...]}
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import load_json, save_json

THREADS_SUBDIR = "threads"
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class ThreadError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _thread_id_safe(thread_id: str) -> str:
    thread_id = str(thread_id).strip()
    if not THREAD_ID_RE.match(thread_id):
        raise ThreadError("thread_id 只能包含字母/数字/下划线/连字符，长度 1-80。")
    return thread_id


def _threads_dir(project_dir: Path) -> Path:
    return Path(project_dir) / THREADS_SUBDIR


def thread_path(project_dir: Path, thread_id: str) -> Path:
    return _threads_dir(project_dir) / f"{_thread_id_safe(thread_id)}.json"


def _default_thread_meta(thread_id: str) -> dict[str, Any]:
    now = _utc_now()
    return {
        "thread_id": thread_id,
        "title": thread_id,
        "created_at": now,
        "updated_at": now,
        "archived": False,
        "message_count": 0,
    }


def list_threads(project_dir: Path, *, include_archived: bool = False) -> list[dict[str, Any]]:
    """Thread list for a project, most recent activity first."""
    directory = _threads_dir(project_dir)
    rows: list[dict[str, Any]] = []
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("*.json")):
        try:
            payload = load_json(path)
        except (OSError, ValueError):
            continue
        if not include_archived and payload.get("archived"):
            continue
        rows.append(
            {
                "thread_id": payload.get("thread_id", path.stem),
                "title": payload.get("title", path.stem),
                "created_at": payload.get("created_at", ""),
                "updated_at": payload.get("updated_at", ""),
                "archived": bool(payload.get("archived")),
                "message_count": payload.get("message_count", len(payload.get("messages", []))),
            }
        )
    rows.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
    return rows


def create_thread(project_dir: Path, thread_id: str, *, title: str = "") -> dict[str, Any]:
    """Create a fresh thread for a project."""
    thread_id = _thread_id_safe(thread_id)
    path = thread_path(project_dir, thread_id)
    if path.is_file():
        raise ThreadError(f"对话 {thread_id!r} 已存在。")
    meta = _default_thread_meta(thread_id)
    if title:
        meta["title"] = str(title).strip()[:120] or thread_id
    payload = {**meta, "messages": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, payload)
    return payload


def rename_thread(project_dir: Path, thread_id: str, title: str) -> dict[str, Any]:
    path = thread_path(project_dir, thread_id)
    if not path.is_file():
        raise ThreadError(f"对话 {thread_id!r} 不存在。")
    payload = load_json(path)
    payload["title"] = str(title).strip()[:120]
    payload["updated_at"] = _utc_now()
    save_json(path, payload)
    return payload


def archive_thread(project_dir: Path, thread_id: str) -> dict[str, Any]:
    path = thread_path(project_dir, thread_id)
    if not path.is_file():
        raise ThreadError(f"对话 {thread_id!r} 不存在。")
    payload = load_json(path)
    payload["archived"] = True
    payload["updated_at"] = _utc_now()
    save_json(path, payload)
    return payload


def append_message(
    project_dir: Path,
    thread_id: str,
    *,
    role: str,
    content: str,
    references: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one message to a thread and return the saved message."""
    path = thread_path(project_dir, thread_id)
    if not path.is_file():
        raise ThreadError(f"对话 {thread_id!r} 不存在。")
    payload = load_json(path)
    message = {
        "message_id": f"m{len(payload['messages']) + 1:04d}",
        "role": role,  # user | agent
        "content": str(content),
        "references": dict(references or {}),  # IDs only, never result copies
        "created_at": _utc_now(),
    }
    payload["messages"].append(message)
    payload["message_count"] = len(payload["messages"])
    payload["updated_at"] = _utc_now()
    save_json(path, payload)
    return message


def get_thread(project_dir: Path, thread_id: str) -> dict[str, Any]:
    path = thread_path(project_dir, thread_id)
    if not path.is_file():
        raise ThreadError(f"对话 {thread_id!r} 不存在。")
    return load_json(path)


def messages(project_dir: Path, thread_id: str) -> list[dict[str, Any]]:
    """Only this thread's messages (no cross-thread reads)."""
    return list(get_thread(project_dir, thread_id).get("messages", []))


def thread_summaries(project_dir: Path) -> list[dict[str, Any]]:
    """Metadata only (UI doc rule: new threads never read other messages)."""
    return list_threads(project_dir)
