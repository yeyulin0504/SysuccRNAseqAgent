from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

from .actions import run_project_action
from .analysis_contract import ContractError, verify_project_contract
from .configuration import normalize_config
from .storage import append_jsonl, load_json


T = TypeVar("T")


@dataclass(frozen=True)
class LLMSubmissionPreview:
    contract_id: str


class LLMSubmissionError(RuntimeError):
    pass


def inspect_llm_submission(config_path: Path) -> LLMSubmissionPreview:
    config_path = config_path.resolve()
    config = normalize_config(load_json(config_path))
    if str(config.get("execution", {}).get("mode", "free")) != "contract":
        raise LLMSubmissionError("The current saved project is not in contract execution mode.")

    verification = verify_project_contract(config_path)
    if not verification.ok:
        raise ContractError(
            "Approved analysis contract verification failed: " + "; ".join(verification.errors)
        )
    return LLMSubmissionPreview(contract_id=verification.contract_id)


def submit_llm_contract(
    config_path: Path,
    *,
    executor: Callable[..., T] = run_project_action,
) -> T:
    config_path = config_path.resolve()
    preview = inspect_llm_submission(config_path)
    _reserve_guard(config_path.parent, preview.contract_id)
    _audit(config_path.parent, "submission_reserved", preview.contract_id)
    try:
        outcome = executor(config_path, wait=False)
    except Exception:
        _audit(config_path.parent, "submission_failed", preview.contract_id)
        raise
    _audit(config_path.parent, "submission_dispatched", preview.contract_id)
    return outcome


def _reserve_guard(project_dir: Path, contract_id: str) -> Path:
    guard_path = project_dir / "llm_submission_guards" / f"{_guard_name(contract_id)}.json"
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(guard_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise LLMSubmissionError(
            "This approved contract was already submitted through the model channel. "
            "Automatic retry is disabled."
        ) from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write('{"contract_id": "' + contract_id + '"}\n')
    return guard_path


def _guard_name(contract_id: str) -> str:
    return contract_id.removeprefix("sha256:")


def _audit(project_dir: Path, event: str, contract_id: str) -> None:
    append_jsonl(
        project_dir / "llm_submission_audit.jsonl",
        {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "contract_id": contract_id,
        },
    )
