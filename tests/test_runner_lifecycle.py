from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from rnaseq_agent.analysis_contract import ContractError
from rnaseq_agent.run_agent import run_project, run_stage_project
from rnaseq_agent.storage import load_json
from rnaseq_agent.validation import ValidationResult

from test_execution_modes_integration import (
    FakeTransport,
    _prepare_project,
)


def _events(attempt_dir: Path) -> list[dict[str, object]]:
    path = attempt_dir / "agent_logs" / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("runner", ["full", "stage"])
def test_dry_run_creates_audited_attempt_without_submit(tmp_path: Path, runner: str) -> None:
    config_path, _ = _prepare_project(tmp_path, f"dry_run_{runner}")
    transport = FakeTransport()

    with patch("rnaseq_agent.run_agent.create_remote_transport", return_value=transport) as create_transport:
        if runner == "full":
            outcome = run_project(config_path, wait=False, dry_run=True)
        else:
            outcome = run_stage_project(config_path, "qc", wait=False, dry_run=True)

    assert outcome.state == "dry_run"
    status = load_json(config_path)["status"]
    attempt_dir = Path(status["attempt_dir"])
    manifest = load_json(attempt_dir / "run_manifest.json")
    assert manifest["body"]["final_status"] == "dry_run"
    assert any(item.get("event") == "lifecycle" and item.get("phase") == "dry_run" for item in _events(attempt_dir))
    assert not any("submit" in command for command in transport.executed)
    create_transport.assert_not_called()


def test_full_dry_run_does_not_create_transport_or_upload_fastqs(tmp_path: Path) -> None:
    config_path, _ = _prepare_project(tmp_path, "dry_run_no_transport")

    with patch("rnaseq_agent.run_agent.create_remote_transport") as create_transport:
        outcome = run_project(config_path, wait=False, dry_run=True)

    assert outcome.state == "dry_run"
    create_transport.assert_not_called()


def test_ambiguous_submit_keeps_attempt_manifest_and_reconcile_claim(tmp_path: Path) -> None:
    config_path, _ = _prepare_project(tmp_path, "ambiguous_lifecycle")
    transport = FakeTransport()
    transport.execute = lambda remote_command: __import__("rnaseq_agent.execution", fromlist=["CommandResult"]).CommandResult(
        ["fake-ssh", remote_command], 0, "", ""
    )

    with patch("rnaseq_agent.run_agent.create_remote_transport", return_value=transport):
        outcome = run_project(config_path, wait=False)

    assert outcome.state == "reconcile_required"
    config = load_json(config_path)
    assert config["status"]["state"] == "reconcile_required"
    claim = next(iter(config["status"]["execution_claims"].values()))
    assert claim["status"] == "submitting"
    attempt_dir = Path(config["status"]["attempt_dir"])
    manifest = load_json(attempt_dir / "run_manifest.json")
    assert manifest["body"]["final_status"] == "reconcile_required"


def test_failed_attempt_is_not_overwritten_by_next_attempt(tmp_path: Path) -> None:
    config_path, _ = _prepare_project(tmp_path, "attempt_history")

    with patch(
        "rnaseq_agent.run_agent._prepare_remote_scripts",
        side_effect=RuntimeError("pre-submit failure"),
    ), patch("rnaseq_agent.run_agent.create_remote_transport", return_value=FakeTransport()):
        with pytest.raises(RuntimeError, match="pre-submit failure"):
            run_project(config_path, wait=False)

    first = load_json(config_path)["status"]
    first_dir = Path(first["attempt_dir"])
    first_manifest = load_json(first_dir / "run_manifest.json")
    assert first_manifest["body"]["final_status"] == "failed"

    transport = FakeTransport()
    with patch("rnaseq_agent.run_agent.create_remote_transport", return_value=transport):
        second_outcome = run_project(config_path, wait=False)

    assert second_outcome.state == "submitted"
    second_dir = Path(load_json(config_path)["status"]["attempt_dir"])
    assert second_dir != first_dir
    assert load_json(first_dir / "run_manifest.json")["body"]["final_status"] == "failed"
    assert (second_dir / "run_manifest.json").is_file()


@pytest.mark.parametrize("failure_kind", ["input", "policy"])
def test_preflight_failure_is_written_before_failure_manifest(
    tmp_path: Path, failure_kind: str
) -> None:
    config_path, _ = _prepare_project(tmp_path, f"preflight_failure_{failure_kind}")

    if failure_kind == "input":
        failure = ValidationResult(
            ok=False,
            checked_files=0,
            errors=["missing input"],
            missing_files=[],
        )
        patcher = patch("rnaseq_agent.run_agent.validate_local_fastqs", return_value=failure)
        expected = "missing input"
    else:
        patcher = patch(
            "rnaseq_agent.run_agent.enforce_execution_policy",
            side_effect=ContractError("policy denied"),
        )
        expected = "policy denied"

    with patcher:
        with pytest.raises((RuntimeError, ContractError), match=expected):
            run_project(config_path, wait=False)

    attempt_dir = Path(load_json(config_path)["status"]["attempt_dir"])
    preflight = load_json(attempt_dir / "preflight.json")
    manifest = load_json(attempt_dir / "run_manifest.json")
    assert preflight["schema_version"] == 1
    assert preflight["overall"] == "blocked"
    assert manifest["body"]["final_status"] == "failed"
    assert manifest["body"]["preflight"]["schema_version"] == 1
    assert expected in str(manifest["body"]["preflight"])

