from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

INTAKE_FILE = "intake.json"
HISTORY_FILE = "history.json"
VISIBLE_STATES = {
    "setup",
    "input_ready",
    "planned",
    "confirmed",
    "running",
    "waiting_user",
    "completed",
    "failed",
}
SESSION_STATE_MAP = {
    "idle": "setup",
    "drafting": "input_ready",
    "planned": "planned",
    "confirmed": "confirmed",
    "executing": "running",
    "waiting": "waiting_user",
    "waiting_user": "waiting_user",
    "completed": "completed",
    "failed": "failed",
    "error": "failed",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_intake(project_dir: Path) -> dict[str, Any]:
    path = project_dir / INTAKE_FILE
    if not path.is_file():
        return {"state": "setup", "route": None, "input_type": None, "updated_at": None}
    payload = json.loads(path.read_text(encoding="utf-8"))
    state = str(payload.get("state") or "setup")
    if state not in VISIBLE_STATES:
        payload["state"] = "setup"
    return payload


def save_intake(project_dir: Path, intake: Mapping[str, Any]) -> dict[str, Any]:
    project_dir.mkdir(parents=True, exist_ok=True)
    payload = {**load_intake(project_dir), **dict(intake)}
    state = str(payload.get("state") or "setup")
    if state not in VISIBLE_STATES:
        raise ValueError(f"invalid project intake state: {state}")
    payload["state"] = state
    payload["updated_at"] = _now()
    (project_dir / INTAKE_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def derive_visible_state(project_dir: Path, registry_state: str | None = None) -> str:
    session_path = project_dir / "session.json"
    if session_path.is_file():
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
            return SESSION_STATE_MAP.get(str(session.get("state") or "").lower(), "input_ready")
        except (OSError, json.JSONDecodeError):
            return "failed"
    intake_state = str(load_intake(project_dir).get("state") or "setup")
    if intake_state in VISIBLE_STATES:
        return intake_state
    if registry_state in {"completed", "failed", "planned", "confirmed"}:
        return registry_state
    return "setup"


def history_items(project_dir: Path) -> list[dict[str, Any]]:
    path = project_dir / HISTORY_FILE
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("items", []))


def append_history(project_dir: Path, item: Mapping[str, Any]) -> dict[str, Any]:
    project_dir.mkdir(parents=True, exist_ok=True)
    rows = history_items(project_dir)
    row = {
        "id": f"h{len(rows) + 1:04d}",
        "created_at": _now(),
        "type": str(item.get("type") or "event"),
        "name": str(item.get("name") or item.get("type") or "event"),
        "state": str(item.get("state") or "ready"),
        "details": item.get("details") or {},
    }
    rows.append(row)
    (project_dir / HISTORY_FILE).write_text(
        json.dumps({"items": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return row
