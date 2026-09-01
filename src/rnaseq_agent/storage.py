from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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
