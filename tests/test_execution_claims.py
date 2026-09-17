from __future__ import annotations

import gzip
import json
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rnaseq_agent.execution import CommandResult
from rnaseq_agent.run_agent import refresh_status, run_project, run_stage_project
from rnaseq_agent.storage import load_json, save_json


def _write_fastq(path: Path, read_id: str) -> None:
    with gzip.open(path, "wt", encoding="ascii") as handle:
        handle.write(f"@{read_id}\nACGT\n+\nIIII\n")


def _fastq_project(tmp_path: Path) -> Path:
    data_dir = tmp_path / "fastq"
    data_dir.mkdir()
    _write_fastq(data_dir / "sample_R1.fastq.gz", "read1/1")
    _write_fastq(data_dir / "sample_R2.fastq.gz", "read1/2")
    config = {
        "project": {"id": "claim_test", "title": "Execution claim test"},
        "server": {
            "host": "hpc.example.edu",
            "user": "researcher",
            "remote_workdir": "/remote/projects/claim_test",
            "scheduler": "local",
            "threads": 2,
            "memory_gb": 4,
            "init_commands": [],
        },
        "samples": {
            "local_data_dir": str(data_dir),
            "remote_data_dir": "/remote/projects/claim_test/raw",
            "items": [
                {
                    "sample_id": "sample_1",
                    "condition": "control",
                    "fastq_1": "sample_R1.fastq.gz",
                    "fastq_2": "sample_R2.fastq.gz",
                }
            ],
        },
        "sequencing": {"layout": "paired", "strandedness": "unstranded"},
        "pipeline": {
            "fastp": {"enabled": True, "version": "0.24.1"},
            "star": {"enabled": False, "version": "2.7.11b"},
            "arriba": {"enabled": False, "version": "2.5.0"},
            "featurecounts": {"enabled": False, "version": "Subread 2.1.1"},
            "rsem": {"enabled": False, "version": "1.2.28"},
        },
        "reference": {},
        "execution": {"mode": "free", "skill_id": "", "contract_file": "analysis_contract.json"},
        "notification": {"email_enabled": False},
    }
    config_path = tmp_path / "project" / "project.json"
    save_json(config_path, config)
    return config_path


def _counts_project(tmp_path: Path) -> Path:
    config_path = _fastq_project(tmp_path)
    counts_path = tmp_path / "counts.tsv"
    counts_path.write_text(
        "gene\tc1\tc2\tc3\tt1\tt2\tt3\nG1\t1\t2\t3\t4\t5\t6\n",
        encoding="utf-8",
    )
    config = load_json(config_path)
    config["samples"] = {
        "counts_path": str(counts_path),
        "local_data_dir": str(tmp_path),
        "remote_data_dir": "AUTO",
        "items": [
            {"sample_id": f"c{i}", "condition": "control"} for i in range(1, 4)
        ]
        + [{"sample_id": f"t{i}", "condition": "treated"} for i in range(1, 4)],
    }
    for step in config["pipeline"].values():
        step["enabled"] = False
    config["pipeline"]["diffexp"] = {"enabled": True, "version": "DESeq2"}
    config["pipeline"]["cms"] = {"enabled": False, "version": "CMScaller"}
    config["diffexp"] = {"formula": "~ condition", "reference_condition": "control"}
    config["cms"] = {"run_mode": "counts"}
    config["container"] = {"enabled": False}
    save_json(config_path, config)
    return config_path


class CountingTransport:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.submission_count = 0

    def execute(self, remote_command: str) -> CommandResult:
        stdout = ""
        if "nohup bash scripts/submit" in remote_command:
            with self._lock:
                self.submission_count += 1
                stdout = f"{1000 + self.submission_count}\n"
        return CommandResult(["fake-ssh", remote_command], 0, stdout, "")

    def upload(self, local_paths, remote_dir: str) -> CommandResult:
        return CommandResult(["fake-upload", remote_dir], 0, "uploaded", "")

    def download(self, remote_file: str, local_path: Path) -> CommandResult:
        raise AssertionError("wait=False must not download remote results")


class AmbiguousSubmitTransport(CountingTransport):
    def __init__(self, submit_stdout: str) -> None:
        super().__init__()
        self.submit_stdout = submit_stdout

    def execute(self, remote_command: str) -> CommandResult:
        if "nohup bash scripts/submit" in remote_command:
            with self._lock:
                self.submission_count += 1
            return CommandResult(["fake-ssh", remote_command], 0, self.submit_stdout, "")
        return CommandResult(["fake-ssh", remote_command], 0, "", "")


def _concurrent_calls(call):
    start = threading.Barrier(2)

    def invoke():
        start.wait(timeout=5)
        return call()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke) for _ in range(2)]
        return [future.result(timeout=10) for future in futures]


def _claims(config_path: Path) -> list[dict]:
    status = load_json(config_path)["status"]
    claims = status.get("execution_claims", {})
    return list(claims.values())


def _claim_process_worker(config_path: str, start, results) -> None:
    from rnaseq_agent.run_agent import _begin_execution_claim

    start.wait(timeout=10)
    result = _begin_execution_claim(Path(config_path), stage="full")
    results.put("conflict" if result.get("conflict") else "claimed")


def test_concurrent_full_runs_persist_one_claim_before_one_submit(tmp_path, monkeypatch) -> None:
    """Removing atomic full-run claim acquisition would allow two scheduler submissions."""
    config_path = _fastq_project(tmp_path)
    transport = CountingTransport()
    monkeypatch.setattr("rnaseq_agent.run_agent.create_remote_transport", lambda _config: transport)

    outcomes = _concurrent_calls(lambda: run_project(config_path, wait=False))

    assert sorted(outcome.state for outcome in outcomes) == ["conflict", "submitted"]
    assert transport.submission_count == 1
    claims = _claims(config_path)
    assert len(claims) == 1
    claim = claims[0]
    assert claim["project_id"] == "claim_test"
    assert claim["contract_id"] == ""
    assert claim["run_id"]
    assert claim["stage"] == "full"
    assert claim["created_at"]
    assert claim["status"] == "submitted"
    assert claim["idempotency_key"].startswith("sha256:")
    assert set(claim) == {
        "claim_version",
        "claim_id",
        "project_id",
        "contract_id",
        "run_id",
        "stage",
        "created_at",
        "updated_at",
        "status",
        "idempotency_key",
        "reconciliation",
    }
    serialized = json.dumps(claim, ensure_ascii=False)
    assert "hpc.example.edu" not in serialized
    assert "researcher" not in serialized
    assert "/remote/projects" not in serialized


def test_concurrent_same_stage_persists_one_claim_before_one_submit(tmp_path, monkeypatch) -> None:
    """Removing stage-key CAS would let two clicks submit the same stage twice."""
    config_path = _counts_project(tmp_path)
    transport = CountingTransport()
    monkeypatch.setattr("rnaseq_agent.run_agent.create_remote_transport", lambda _config: transport)

    outcomes = _concurrent_calls(lambda: run_stage_project(config_path, "counts", wait=False))

    assert sorted(outcome.state for outcome in outcomes) == ["conflict", "submitted"]
    assert transport.submission_count == 1
    claims = _claims(config_path)
    assert len(claims) == 1
    assert claims[0]["stage"] == "counts"
    assert claims[0]["status"] == "submitted"


def test_active_stage_claim_blocks_a_different_stage_in_same_project(tmp_path, monkeypatch) -> None:
    """Allowing stage overlap would let both writers overwrite the live project status."""
    config_path = _counts_project(tmp_path)
    transport = CountingTransport()
    monkeypatch.setattr("rnaseq_agent.run_agent.create_remote_transport", lambda _config: transport)

    counts = run_stage_project(config_path, "counts", wait=False)
    de = run_stage_project(config_path, "de", wait=False)

    assert counts.state == "submitted"
    assert de.state == "conflict"
    assert transport.submission_count == 1
    assert len(_claims(config_path)) == 1


def test_cross_process_claim_cas_allows_only_one_owner(tmp_path) -> None:
    """Removing the OS-backed lock would let separate workers both own the full-run claim."""
    config_path = _fastq_project(tmp_path)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [
        context.Process(target=_claim_process_worker, args=(str(config_path), start, results))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(timeout=15)
        assert worker.exitcode == 0

    outcomes = sorted(results.get(timeout=5) for _ in workers)
    assert outcomes == ["claimed", "conflict"]
    assert len(_claims(config_path)) == 1


def test_system_exit_after_full_submit_keeps_submitting_claim_and_retry_fails_closed(
    tmp_path, monkeypatch
) -> None:
    """Clearing a claim after an ambiguous submit crash would duplicate a remote job on retry."""
    config_path = _fastq_project(tmp_path)
    transport = CountingTransport()
    submit_calls = 0
    monkeypatch.setattr("rnaseq_agent.run_agent.create_remote_transport", lambda _config: transport)

    def crash_after_side_effect(*_args, **_kwargs):
        nonlocal submit_calls
        submit_calls += 1
        raise SystemExit("process died after scheduler accepted the job")

    monkeypatch.setattr("rnaseq_agent.run_agent._submit_remote_job", crash_after_side_effect)

    with pytest.raises(SystemExit, match="scheduler accepted"):
        run_project(config_path, wait=False)

    claim = _claims(config_path)[0]
    assert claim["status"] == "submitting"
    assert claim["reconciliation"]["required"] is True

    retry = run_project(config_path, wait=False)

    assert retry.state == "conflict"
    assert "reconcile" in retry.message.lower()
    assert submit_calls == 1


def test_keyboard_interrupt_after_stage_submit_keeps_claim_and_retry_fails_closed(
    tmp_path, monkeypatch
) -> None:
    """A stage submit interrupted after its side effect must remain non-replayable."""
    config_path = _counts_project(tmp_path)
    transport = CountingTransport()
    submit_calls = 0
    monkeypatch.setattr("rnaseq_agent.run_agent.create_remote_transport", lambda _config: transport)

    def interrupt_after_side_effect(*_args, **_kwargs):
        nonlocal submit_calls
        submit_calls += 1
        raise KeyboardInterrupt("interrupted after scheduler accepted the stage")

    monkeypatch.setattr("rnaseq_agent.run_agent._submit_stage_job", interrupt_after_side_effect)

    with pytest.raises(KeyboardInterrupt, match="scheduler accepted"):
        run_stage_project(config_path, "counts", wait=False)

    claim = _claims(config_path)[0]
    assert claim["status"] == "submitting"
    assert claim["stage"] == "counts"

    retry = run_stage_project(config_path, "counts", wait=False)

    assert retry.state == "conflict"
    assert submit_calls == 1


def test_terminal_failure_closes_claim_and_explicit_new_run_gets_new_claim(
    tmp_path, monkeypatch
) -> None:
    """Treating failed claims as active forever would prevent an intentional later run."""
    config_path = _fastq_project(tmp_path)
    transport = CountingTransport()
    monkeypatch.setattr("rnaseq_agent.run_agent.create_remote_transport", lambda _config: transport)
    monkeypatch.setattr("rnaseq_agent.run_agent._poll_until_finished", lambda *_args: "failed")

    first = run_project(config_path, wait=True)
    first_run_id = load_json(config_path)["status"]["run_id"]
    second = run_project(config_path, wait=False)
    second_run_id = load_json(config_path)["status"]["run_id"]

    assert first.state == "failed"
    assert second.state == "submitted"
    assert first_run_id != second_run_id
    assert transport.submission_count == 2
    claims = _claims(config_path)
    assert [claim["status"] for claim in claims] == ["failed", "submitted"]


def test_refreshing_remote_terminal_state_closes_wait_false_claim(tmp_path, monkeypatch) -> None:
    """Leaving a remotely terminal async claim active would block all later explicit runs."""
    config_path = _fastq_project(tmp_path)
    transport = CountingTransport()
    monkeypatch.setattr("rnaseq_agent.run_agent.create_remote_transport", lambda _config: transport)

    submitted = run_project(config_path, wait=False)
    monkeypatch.setattr("rnaseq_agent.run_agent._read_remote_state", lambda *_args: "failed")

    status = refresh_status(config_path)

    assert submitted.state == "submitted"
    assert status["state"] == "failed"
    assert _claims(config_path)[0]["status"] == "failed"
    rerun = run_project(config_path, wait=False)
    assert rerun.state == "submitted"
    assert transport.submission_count == 2


def test_claim_binds_frozen_contract_identifier_when_present(tmp_path, monkeypatch) -> None:
    """Dropping the contract binding would make two frozen specifications indistinguishable."""
    config_path = _fastq_project(tmp_path)
    save_json(config_path.parent / "analysis_contract.json", {"contract_id": "sha256:frozen-contract"})
    transport = CountingTransport()
    monkeypatch.setattr("rnaseq_agent.run_agent.create_remote_transport", lambda _config: transport)

    outcome = run_project(config_path, wait=False)

    assert outcome.state == "submitted"
    assert _claims(config_path)[0]["contract_id"] == "sha256:frozen-contract"


def test_full_setup_failure_after_claim_closes_started_claim(tmp_path) -> None:
    """Leaving setup failures started would permanently block a run that never reached submit."""
    config_path = _fastq_project(tmp_path)
    config = load_json(config_path)
    config["server"]["remote_workdir"] = ""
    save_json(config_path, config)

    with pytest.raises(ValueError, match="remote_workdir"):
        run_project(config_path, wait=False)

    assert _claims(config_path)[0]["status"] == "failed"


def test_keyboard_interrupt_before_submit_closes_started_claim(tmp_path, monkeypatch) -> None:
    """BaseException before submit is definite non-submission and must close the claim."""
    config_path = _fastq_project(tmp_path)

    def interrupt_validation(*_args, **_kwargs):
        raise KeyboardInterrupt("operator interrupted validation")

    monkeypatch.setattr("rnaseq_agent.run_agent.validate_local_fastqs", interrupt_validation)

    with pytest.raises(KeyboardInterrupt, match="validation"):
        run_project(config_path, wait=False)

    assert _claims(config_path)[0]["status"] == "failed"


def test_stage_log_failure_before_submit_closes_claim_without_masking_error(
    tmp_path, monkeypatch
) -> None:
    """Best-effort audit logging must not prevent closing a definite pre-submit failure."""
    config_path = _counts_project(tmp_path)

    def broken_log(*_args, **_kwargs):
        raise OSError("audit disk unavailable")

    monkeypatch.setattr("rnaseq_agent.run_agent._log_event", broken_log)

    with pytest.raises(OSError, match="audit disk unavailable"):
        run_stage_project(config_path, "counts", wait=False)

    assert _claims(config_path)[0]["status"] == "failed"


@pytest.mark.parametrize("legacy_kind", ["full", "other_stage"])
def test_legacy_active_execution_blocks_project_wide_submission(
    tmp_path, monkeypatch, legacy_kind
) -> None:
    """Ignoring pre-upgrade active state would submit a second remote job during migration."""
    config_path = _counts_project(tmp_path)
    config = load_json(config_path)
    if legacy_kind == "full":
        config["status"] = {"state": "submitted"}
    else:
        config["status"] = {
            "state": "confirmed",
            "run_id": "legacy-stage-run",
            "stages": {"counts": {"status": "submitted", "job_id": "654"}},
        }
    save_json(config_path, config)
    transport = CountingTransport()
    monkeypatch.setattr("rnaseq_agent.run_agent.create_remote_transport", lambda _config: transport)

    outcome = (
        run_project(config_path, wait=False)
        if legacy_kind == "full"
        else run_stage_project(config_path, "de", wait=False)
    )

    assert outcome.state == "conflict"
    assert "reconcile" in outcome.message.lower()
    assert transport.submission_count == 0


@pytest.mark.parametrize(
    ("ledger", "pointer"),
    [
        ([], "old:full:deadbeef"),
        ({}, "old:full:deadbeef"),
        ({"broken": {"claim_id": "broken", "status": "mystery"}}, "broken"),
    ],
)
def test_malformed_claim_ledger_fails_closed(tmp_path, monkeypatch, ledger, pointer) -> None:
    """Treating damaged durable state as empty would permit duplicate scheduler effects."""
    config_path = _fastq_project(tmp_path)
    config = load_json(config_path)
    config["status"] = {
        "state": "submitted",
        "run_id": "old-run",
        "execution_claim_id": pointer,
        "execution_claims": ledger,
    }
    save_json(config_path, config)
    transport = CountingTransport()
    monkeypatch.setattr("rnaseq_agent.run_agent.create_remote_transport", lambda _config: transport)

    outcome = run_project(config_path, wait=False)

    assert outcome.state == "conflict"
    assert "reconcile" in outcome.message.lower()
    assert transport.submission_count == 0


@pytest.mark.parametrize(
    ("stage", "submit_stdout"),
    [("full", ""), ("counts", "accepted but job id unavailable\nsecond line")],
)
def test_ambiguous_scheduler_stdout_stays_submitting_for_reconciliation(
    tmp_path, monkeypatch, stage, submit_stdout
) -> None:
    """Inventing a job id would falsely make an ambiguous scheduler side effect replayable."""
    config_path = _fastq_project(tmp_path) if stage == "full" else _counts_project(tmp_path)
    transport = AmbiguousSubmitTransport(submit_stdout)
    monkeypatch.setattr("rnaseq_agent.run_agent.create_remote_transport", lambda _config: transport)

    outcome = (
        run_project(config_path, wait=False)
        if stage == "full"
        else run_stage_project(config_path, stage, wait=False)
    )

    status = load_json(config_path)["status"]
    assert outcome.state == "reconcile_required"
    assert status["state"] == "reconcile_required"
    assert "job_id" not in status
    assert _claims(config_path)[0]["status"] == "submitting"
    assert transport.submission_count == 1


@pytest.mark.parametrize("observed_state", ["queued", "completed"])
def test_stale_refresh_observation_cannot_overwrite_new_claim(
    tmp_path, monkeypatch, observed_state
) -> None:
    """A delayed probe for claim A must be discarded after claim B becomes current."""
    from rnaseq_agent.run_agent import _transition_execution_claim

    config_path = _fastq_project(tmp_path)
    transport = CountingTransport()
    monkeypatch.setattr("rnaseq_agent.run_agent.create_remote_transport", lambda _config: transport)
    first = run_project(config_path, wait=False)
    assert first.state == "submitted"
    first_status = load_json(config_path)["status"]
    first_claim_id = first_status["execution_claim_id"]

    probe_started = threading.Event()
    release_probe = threading.Event()

    def delayed_probe(*_args):
        probe_started.set()
        release_probe.wait(timeout=5)
        return observed_state

    monkeypatch.setattr("rnaseq_agent.run_agent._read_remote_state", delayed_probe)
    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_refresh = executor.submit(refresh_status, config_path)
        assert probe_started.wait(timeout=5)
        _transition_execution_claim(
            config_path,
            first_claim_id,
            expected={"submitted"},
            claim_status="failed",
            project_state="failed",
            project_message="first attempt failed",
        )
        second = run_project(config_path, wait=False)
        second_status = load_json(config_path)["status"]
        second_claim_id = second_status["execution_claim_id"]
        release_probe.set()
        stale_refresh.result(timeout=5)

    live = load_json(config_path)["status"]
    assert second.state == "submitted"
    assert second_claim_id != first_claim_id
    assert live["execution_claim_id"] == second_claim_id
    assert live["state"] == "submitted"
    assert live["execution_claims"][second_claim_id]["status"] == "submitted"
