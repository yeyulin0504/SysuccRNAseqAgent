from __future__ import annotations

import time
import platform
import re
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from shutil import copyfile
from typing import Any
from uuid import uuid4

from . import __version__
from .archive import safe_extract_tar_gz
from .analysis_contract import (
    ContractError,
    build_input_artifacts,
    build_workflow_snapshot,
    canonical_sha256,
    enforce_execution_policy,
    project_contract_path,
    sha256_file,
)
from .configuration import is_remote_prestaged_config, normalize_config
from .emailer import send_completion_email
from .execution import CommandResult
from .pipeline import (
    render_env_setup_script,
    render_remote_pipeline_script,
    render_stage_script,
    render_submit_script,
)
from .remote import collect_local_fastq_paths
from .remote_transport import RemoteTransport, create_remote_transport
from .result_manifest import ResultManifestSummary, create_result_manifest
from .run_audit import (
    default_environment,
    finalize_run_manifest,
    write_artifact_index,
    write_io_lineage,
    write_run_manifest as write_audit_run_manifest,
)
from .shell import shell_quote
from .storage import append_jsonl, load_json, project_state_lock, save_json
from .validation import validate_local_fastqs


@dataclass(frozen=True)
class RunOutcome:
    state: str
    message: str
    project_dir: Path


def upload_project_fastqs(config_path: Path) -> RunOutcome:
    """Validate and upload FASTQ files without submitting an analysis job."""
    config = normalize_config(load_json(config_path))
    save_json(config_path, config)
    project_dir = config_path.parent
    logs_dir = project_dir / "agent_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    _log_event(logs_dir, "upload_started", {"config": str(config_path)})
    try:
        validation = validate_local_fastqs(config, check_pipeline=False)
        if not validation.ok:
            parts = []
            if validation.missing_files:
                parts.append(f"{len(validation.missing_files)} 个文件缺失")
            if validation.errors:
                parts.append("; ".join(validation.errors))
            message = "本地 FASTQ 校验失败：" + "，".join(parts)
            _update_status(config_path, "validation_failed", message)
            _log_event(
                logs_dir,
                "upload_validation_failed",
                {
                    "missing_files": [str(path) for path in validation.missing_files],
                    "errors": validation.errors,
                },
            )
            raise RuntimeError(message)

        transport = create_remote_transport(config)
        _upload_fastqs(config, logs_dir, transport)
        remote_data_dir = config["samples"]["remote_data_dir"]
        message = f"FASTQ 已上传到服务器：{remote_data_dir}"
        _update_status(config_path, "uploaded", message)
        _log_event(
            logs_dir,
            "upload_completed",
            {
                "remote_data_dir": remote_data_dir,
                "files": [path.name for path in collect_local_fastq_paths(config)],
            },
        )
        return RunOutcome("uploaded", message, project_dir)
    except Exception as exc:
        message = str(exc)
        current = normalize_config(load_json(config_path)).get("status", {})
        if current.get("state") != "validation_failed":
            _update_status(config_path, "upload_failed", message)
        _log_event(logs_dir, "upload_failed", {"message": message})
        raise


def run_project(config_path: Path, *, wait: bool = True) -> RunOutcome:
    return _run_project_claimed(config_path, wait=wait)


def _run_project_claimed(config_path: Path, *, wait: bool = True) -> RunOutcome:
    project_dir = config_path.parent
    claim_start = _begin_execution_claim(config_path, stage="full")
    conflict = claim_start.get("conflict")
    if conflict:
        return RunOutcome("conflict", _claim_conflict_message(conflict), project_dir)

    config = claim_start["config"]
    run_id = claim_start["run_id"]
    claim_id = claim_start["claim_id"]
    attempt_dir = project_dir / "attempts" / run_id
    logs_dir = attempt_dir / "agent_logs"
    downloads_dir = attempt_dir / "downloads"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        run_config = _config_for_new_attempt(config, run_id)
        snapshot_path = attempt_dir / "project.snapshot.json"
        save_json(snapshot_path, run_config)
        _log_event(
            logs_dir,
            "run_started",
            {
                "run_id": run_id,
                "config": str(config_path),
                "snapshot": str(snapshot_path),
                "remote_run_workdir": run_config["server"]["remote_workdir"],
            },
        )
    except BaseException as exc:
        message = str(exc)
        _record_execution_exception(config_path, claim_id, message)
        _best_effort_write_audit_manifest(
            attempt_dir,
            config=config if "config" in locals() else {},
            run_id=run_id,
            attempt_id=run_id,
            final_status="failed",
            error=message,
        )
        _best_effort_log_event(logs_dir, "run_failed", {"run_id": run_id, "message": message})
        raise
    try:
        validation = validate_local_fastqs(run_config)
        if not validation.ok:
            parts = []
            if validation.missing_files:
                parts.append(f"{len(validation.missing_files)} files missing")
            if validation.errors:
                parts.append("; ".join(validation.errors))
            message = "Local FASTQ validation failed: " + ", ".join(parts)
            _update_status(config_path, "validation_failed", message)
            _log_event(
                logs_dir,
                "validation_failed",
                {
                    "missing_files": [str(path) for path in validation.missing_files],
                    "errors": validation.errors,
                },
            )
            raise RuntimeError(message)

        _update_status(config_path, "validated", "Local FASTQ validation passed.")
        try:
            policy = enforce_execution_policy(config_path, config)
        except ContractError as exc:
            _update_status(config_path, "policy_failed", str(exc))
            _log_event(logs_dir, "execution_policy_failed", {"run_id": run_id, "message": str(exc)})
            raise
        _record_policy_verification(config_path, policy)
        run_config["execution"].update(
            {key: value for key, value in policy.items() if value}
        )
        save_json(snapshot_path, run_config)
        _log_event(logs_dir, "execution_policy_verified", {"run_id": run_id, **policy})
        transport = create_remote_transport(run_config)
        _upload_fastqs(run_config, logs_dir, transport)
        _update_status(config_path, "uploaded", "Local FASTQ files uploaded to the server.")

        remote_scripts = _prepare_remote_scripts(run_config, config_path, attempt_dir, logs_dir)
        manifest_path = _write_run_manifest(
            config,
            run_config,
            config_path,
            snapshot_path,
            remote_scripts,
            policy,
            attempt_dir,
        )
        support_files = [*remote_scripts, snapshot_path, manifest_path]
        _upload_support_files(run_config, support_files, logs_dir, transport)
        _transition_execution_claim(
            config_path,
            claim_id,
            expected={"started"},
            claim_status="submitting",
            project_state="submitting",
            project_message="Remote analysis job submission started; retry is blocked pending reconciliation.",
        )
        submit_result = _submit_remote_job(run_config, logs_dir, transport)
        job_id = _parse_scheduler_job_id(run_config, submit_result.stdout)
        if not job_id:
            message = (
                "Scheduler submission returned no valid job id. The claim remains submitting; "
                "inspect the scheduler before any retry."
            )
            _transition_execution_claim(
                config_path,
                claim_id,
                expected={"submitting"},
                claim_status="submitting",
                project_state="reconcile_required",
                project_message=message,
            )
            _best_effort_log_event(
                logs_dir,
                "submission_reconcile_required",
                {"run_id": run_id, "reason": "missing_or_invalid_job_id"},
            )
            _finalize_attempt_audit(attempt_dir, "reconcile_required", message)
            return RunOutcome("reconcile_required", message, project_dir)

        _transition_execution_claim(
            config_path,
            claim_id,
            expected={"submitting"},
            claim_status="submitted",
            project_state="submitted",
            project_message="Remote analysis job submitted.",
            project_fields={
                "job_id": job_id,
                "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "manifest_file": str(manifest_path),
            },
        )
        _log_event(
            logs_dir,
            "submitted",
            {"run_id": run_id, "job_id": job_id, "stdout": submit_result.stdout.strip()},
        )

        if not wait:
            return RunOutcome("submitted", f"Job submitted: {job_id}", project_dir)

        final_state = _poll_until_finished(config_path, run_config, logs_dir, transport)
        if final_state == "completed":
            _update_status(config_path, "remote_completed", "Remote RNA-seq analysis completed.")
            try:
                result_summary = _download_results(
                    run_config,
                    downloads_dir,
                    logs_dir,
                    transport,
                )
            except Exception as download_exc:
                message = f"Remote analysis completed, but downloading results failed: {download_exc}"
                _transition_execution_claim(
                    config_path,
                    claim_id,
                    expected={"submitted"},
                    claim_status="failed",
                    project_state="download_failed",
                    project_message=message,
                )
                _notify(run_config, "download_failed", message)
                _finalize_attempt_audit(attempt_dir, "download_failed", message)
                return RunOutcome("download_failed", message, project_dir)
            _update_status(
                config_path,
                "results_verified" if result_summary.ok else "result_validation_failed",
                "Downloaded result manifest verified."
                if result_summary.ok
                else "Downloaded results are incomplete or invalid.",
                result_manifest_file=str(result_summary.path),
                result_files_sha256=result_summary.files_sha256,
                result_validation_errors=result_summary.errors,
            )
            if not result_summary.ok:
                message = "Remote analysis reported completion, but result validation failed: " + "; ".join(
                    result_summary.errors
                )
                _transition_execution_claim(
                    config_path,
                    claim_id,
                    expected={"submitted"},
                    claim_status="failed",
                    project_state="result_validation_failed",
                    project_message=message,
                )
                _notify(run_config, "result_validation_failed", message)
                _finalize_attempt_audit(attempt_dir, "result_validation_failed", message)
                return RunOutcome("result_validation_failed", message, project_dir)
            message = "RNA-seq analysis completed and results downloaded."
            final_state = "completed"
        elif final_state == "timeout":
            message = "RNA-seq analysis did not finish before the polling timeout."
        else:
            message = "RNA-seq analysis failed on the server."

        if final_state == "timeout":
            _transition_execution_claim(
                config_path,
                claim_id,
                expected={"submitted"},
                claim_status="submitted",
                project_state=final_state,
                project_message=message,
            )
        else:
            _transition_execution_claim(
                config_path,
                claim_id,
                expected={"submitted"},
                claim_status="completed" if final_state == "completed" else "failed",
                project_state=final_state,
                project_message=message,
            )
        _notify(run_config, final_state, message)
        _finalize_attempt_audit(attempt_dir, final_state, message)
        return RunOutcome(final_state, message, project_dir)
    except BaseException as exc:
        message = str(exc)
        _record_execution_exception(config_path, claim_id, message)
        _best_effort_write_audit_manifest(
            attempt_dir,
            config=run_config if "run_config" in locals() else config,
            run_id=run_id,
            attempt_id=run_id,
            final_status="failed",
            error=message,
        )
        _best_effort_log_event(logs_dir, "run_failed", {"run_id": run_id, "message": message})
        try:
            _notify(run_config, "run_failed", message)
        except BaseException as notify_exc:
            _best_effort_log_event(logs_dir, "notify_failed", {"message": str(notify_exc)})
        raise


# -- staged execution (说明书 §5 分阶段；UI 设计：检查点绑定同一 attempt) ----


def run_stage_project(
    config_path: Path,
    stage: str,
    *,
    wait: bool = True,
) -> RunOutcome:
    from .pipeline import ALL_STAGES

    if stage not in ALL_STAGES:
        raise ValueError(f"Unsupported stage: {stage!r}. Allowed: {ALL_STAGES}")
    return _run_stage_project_claimed(config_path, stage, wait=wait)


def _run_stage_project_claimed(
    config_path: Path,
    stage: str,
    *,
    wait: bool = True,
) -> RunOutcome:
    """Execute a single scientific stage for a frozen project.

    Stages (see :mod:`rnaseq_agent.pipeline`): ``qc`` (fastp), ``quant``
    (STAR/quant/fusion), ``de`` (conditional DESeq2). The first stage for a
    logical run creates the isolated attempt and uploads raw FASTQs; later
    stages reuse the same attempt's remote workdir, so stage-1 ``fastp/``
    outputs are available to stage-2 (说明书 §5.1/§5.2 科学衔接).

    Idempotency (说明书 §6): the stage key is recorded in ``status.stages``.
    A stage already marked ``completed`` for this spec revision is not
    re-submitted. A ``running``/``queued`` stage returns the current state
    without submitting twice.
    """
    from .pipeline import STAGE_QC

    project_dir = config_path.parent
    claim_start = _begin_execution_claim(config_path, stage=stage)
    existing_outcome = claim_start.get("outcome")
    if existing_outcome:
        return existing_outcome
    conflict = claim_start.get("conflict")
    if conflict:
        return RunOutcome("conflict", _claim_conflict_message(conflict), project_dir)

    config = claim_start["config"]
    run_id = claim_start["run_id"]
    claim_id = claim_start["claim_id"]
    attempt_dir = project_dir / "attempts" / run_id

    logs_dir = attempt_dir / "agent_logs"
    downloads_dir = attempt_dir / "downloads"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        run_config = _config_for_new_attempt(config, run_id)
        # 保留既有 attempt：共享同一远程工作目录（stage1 的 fastp/ 供 stage2 使用）。
        if (attempt_dir / "project.snapshot.json").is_file():
            snapshot_payload = load_json(attempt_dir / "project.snapshot.json")
            run_config["server"]["remote_workdir"] = snapshot_payload["server"]["remote_workdir"]
            run_config["server"]["remote_base_dir"] = snapshot_payload["server"].get("remote_base_dir", "")
            run_config["samples"]["remote_data_dir"] = snapshot_payload["samples"]["remote_data_dir"]
            run_config["run"] = snapshot_payload.get("run", run_config["run"])

        snapshot_path = attempt_dir / "project.snapshot.json"
        if not snapshot_path.is_file():
            save_json(snapshot_path, run_config)
        _log_event(
            logs_dir,
            "stage_run_started",
            {
                "stage": stage,
                "run_id": run_id,
                "config": str(config_path),
                "remote_run_workdir": run_config["server"]["remote_workdir"],
            },
        )
    except BaseException as exc:
        message = str(exc)
        _record_execution_exception(config_path, claim_id, message, stage=stage)
        _best_effort_write_audit_manifest(
            attempt_dir,
            config=run_config if "run_config" in locals() else config,
            run_id=run_id,
            attempt_id=run_id,
            final_status="failed",
            error=message,
        )
        _best_effort_log_event(
            logs_dir,
            "stage_run_failed",
            {"run_id": run_id, "stage": stage, "message": message},
        )
        raise

    try:
        # 上传本地 FASTQ 只发生在第一个 stage（qc）；后续 stage 复用远程 raw。
        if stage == STAGE_QC:
            if not _fastqs_prestaged(run_config):
                validation = validate_local_fastqs(run_config)
                if not validation.ok:
                    parts = []
                    if validation.missing_files:
                        parts.append(f"{len(validation.missing_files)} files missing")
                    if validation.errors:
                        parts.append("; ".join(validation.errors))
                    message = "Local FASTQ validation failed: " + ", ".join(parts)
                    _update_status(config_path, "validation_failed", message)
                    _log_event(logs_dir, "validation_failed", {"stage": stage, "message": message})
                    raise RuntimeError(message)
            _update_status(config_path, "validated", "Local FASTQ validation passed.", stage=stage)
            transport = create_remote_transport(run_config)
            if not _fastqs_prestaged(run_config):
                _upload_fastqs(run_config, logs_dir, transport)
                _update_status(config_path, "uploaded", "Local FASTQ files uploaded.", stage=stage)
            else:
                _update_status(
                    config_path,
                    "uploaded",
                    "FASTQ 已存在于服务器（remote_path 数据源），跳过上传。",
                    stage=stage,
                )
        elif stage == "counts":
            # counts 直入（2026-09-08）：把上传的 counts_matrix.tsv 上传到
            # 远程工作区根目录，diffexp/cms R 脚本直接读它。
            transport = create_remote_transport(run_config)
            _upload_counts_matrix(run_config, config_path, logs_dir, transport)
        else:
            transport = create_remote_transport(run_config)

        try:
            policy = enforce_execution_policy(config_path, config)
        except ContractError as exc:
            _update_status(config_path, "policy_failed", str(exc), stage=stage)
            _log_event(logs_dir, "execution_policy_failed", {"run_id": run_id, "stage": stage, "message": str(exc)})
            raise
        _record_policy_verification(config_path, policy)
        remote_scripts = _prepare_stage_scripts(run_config, config_path, attempt_dir, logs_dir, stage)
        manifest_path = attempt_dir / "run_manifest.json"
        if not manifest_path.is_file():
            run_config["execution"].update(
                {key: value for key, value in policy.items() if value}
            )
            save_json(snapshot_path, run_config)
            manifest_path = _write_run_manifest(
                config,
                run_config,
                config_path,
                snapshot_path,
                remote_scripts,
                policy,
                attempt_dir,
            )
        support_files = [*remote_scripts, snapshot_path, manifest_path]
        _upload_support_files(run_config, support_files, logs_dir, transport)

        _transition_execution_claim(
            config_path,
            claim_id,
            expected={"started"},
            claim_status="submitting",
            project_state="submitting",
            project_message=f"Stage {stage} submission started; retry is blocked pending reconciliation.",
            stage_status="submitting",
        )
        submit_result = _submit_stage_job(run_config, stage, logs_dir, transport)
        job_id = _parse_scheduler_job_id(run_config, submit_result.stdout)
        if not job_id:
            message = (
                f"Stage {stage} submission returned no valid job id. The claim remains "
                "submitting; inspect the scheduler before any retry."
            )
            _transition_execution_claim(
                config_path,
                claim_id,
                expected={"submitting"},
                claim_status="submitting",
                project_state="reconcile_required",
                project_message=message,
                stage_status="submitting",
            )
            _best_effort_log_event(
                logs_dir,
                "stage_submission_reconcile_required",
                {"run_id": run_id, "stage": stage, "reason": "missing_or_invalid_job_id"},
            )
            _finalize_attempt_audit(attempt_dir, "reconcile_required", message)
            return RunOutcome("reconcile_required", message, project_dir)
        submitted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _transition_execution_claim(
            config_path,
            claim_id,
            expected={"submitting"},
            claim_status="submitted",
            project_state="submitted",
            project_message=f"Stage {stage} submitted.",
            project_fields={"job_id": job_id, "submitted_at": submitted_at},
            stage_status="submitted",
            stage_fields={"job_id": job_id, "submitted_at": submitted_at},
        )
        _log_event(logs_dir, "stage_submitted", {"stage": stage, "run_id": run_id, "job_id": job_id})

        if not wait:
            return RunOutcome("submitted", f"Stage {stage} submitted: {job_id}", project_dir)

        final_state = _poll_until_finished(config_path, run_config, logs_dir, transport)
        if final_state == "completed":
            _update_status(config_path, "remote_completed", f"Stage {stage} completed.", stage=stage)
            try:
                summary = _download_results(
                    run_config,
                    downloads_dir,
                    logs_dir,
                    transport,
                    stage=stage,
                )
            except Exception as download_exc:
                message = f"Stage {stage} completed, but downloading results failed: {download_exc}"
                _transition_execution_claim(
                    config_path,
                    claim_id,
                    expected={"submitted"},
                    claim_status="failed",
                    project_state="download_failed",
                    project_message=message,
                    stage_status="failed",
                )
                _notify(run_config, "download_failed", message)
                _finalize_attempt_audit(attempt_dir, "download_failed", message)
                return RunOutcome("download_failed", message, project_dir)
            if not summary.ok:
                message = f"Stage {stage} results are incomplete or invalid: " + "; ".join(summary.errors)
                _transition_execution_claim(
                    config_path,
                    claim_id,
                    expected={"submitted"},
                    claim_status="failed",
                    project_state="result_validation_failed",
                    project_message=message,
                    stage_status="failed",
                )
                _notify(run_config, "result_validation_failed", message)
                _finalize_attempt_audit(attempt_dir, "result_validation_failed", message)
                return RunOutcome("result_validation_failed", message, project_dir)
            _transition_execution_claim(
                config_path,
                claim_id,
                expected={"submitted"},
                claim_status="completed",
                project_state="stage_completed",
                project_message=f"Stage {stage} completed and results verified.",
                stage_status="completed",
            )
            _finalize_attempt_audit(attempt_dir, "stage_completed", f"Stage {stage} completed.")
            return RunOutcome("stage_completed", f"Stage {stage} completed.", project_dir)
        if final_state == "timeout":
            message = f"Stage {stage} did not finish before the polling timeout."
        else:
            message = f"Stage {stage} failed on the server."
        _transition_execution_claim(
            config_path,
            claim_id,
            expected={"submitted"},
            claim_status="submitted" if final_state == "timeout" else "failed",
            project_state=final_state,
            project_message=message,
            stage_status=final_state,
        )
        _notify(run_config, final_state, message)
        _finalize_attempt_audit(attempt_dir, final_state, message)
        return RunOutcome(final_state, message, project_dir)
    except BaseException as exc:
        message = str(exc)
        _record_execution_exception(config_path, claim_id, message, stage=stage)
        _best_effort_write_audit_manifest(
            attempt_dir,
            config=run_config if "run_config" in locals() else config,
            run_id=run_id,
            attempt_id=run_id,
            final_status="failed",
            error=message,
        )
        _best_effort_log_event(
            logs_dir,
            "stage_run_failed",
            {"run_id": run_id, "stage": stage, "message": message},
        )
        try:
            _notify(run_config, "run_failed", message)
        except BaseException as notify_exc:
            _best_effort_log_event(logs_dir, "notify_failed", {"message": str(notify_exc)})
        raise


def refresh_status(config_path: Path) -> dict[str, Any]:
    config = normalize_config(load_json(config_path))
    observed_status = config.get("status", {})
    observed_run_id = str(observed_status.get("run_id") or "")
    observed_claim_id = str(observed_status.get("execution_claim_id") or "")
    observed_claims = observed_status.get("execution_claims")
    observed_claim = (
        observed_claims.get(observed_claim_id)
        if isinstance(observed_claims, dict) and observed_claim_id
        else None
    )
    observed_claim_status = str((observed_claim or {}).get("status") or "")
    active_config = _config_for_active_attempt(config)
    state = _read_remote_state(active_config, create_remote_transport(active_config))
    if state:
        _apply_refresh_observation(
            config_path,
            observed_run_id=observed_run_id,
            observed_claim_id=observed_claim_id,
            observed_claim_status=observed_claim_status,
            remote_state=state,
        )
    return normalize_config(load_json(config_path)).get("status", {})


def _apply_refresh_observation(
    config_path: Path,
    *,
    observed_run_id: str,
    observed_claim_id: str,
    observed_claim_status: str,
    remote_state: str,
) -> bool:
    """Apply a remote observation only to the run and claim that were probed."""

    with project_state_lock(config_path):
        config = normalize_config(load_json(config_path))
        status = config.get("status", {})
        if str(status.get("run_id") or "") != observed_run_id:
            return False
        if str(status.get("execution_claim_id") or "") != observed_claim_id:
            return False

        message = f"Remote state: {remote_state}"
        if observed_claim_id:
            claims = status.get("execution_claims")
            if not isinstance(claims, dict):
                return False
            claim = claims.get(observed_claim_id)
            if not isinstance(claim, dict):
                return False
            if str(claim.get("run_id") or "") != observed_run_id:
                return False
            live_claim_status = str(claim.get("status") or "")
            if (
                live_claim_status != observed_claim_status
                or live_claim_status not in _ACTIVE_EXECUTION_CLAIM_STATUSES
            ):
                return False
            claim_stage = str(claim.get("stage") or "")
            _transition_execution_claim(
                config_path,
                observed_claim_id,
                expected={live_claim_status},
                claim_status=(
                    "completed"
                    if remote_state == "completed"
                    else "failed"
                    if remote_state == "failed"
                    else live_claim_status
                ),
                project_state=remote_state,
                project_message=message,
                stage_status=(
                    "completed"
                    if remote_state == "completed"
                    else "failed"
                    if remote_state == "failed"
                    else remote_state
                )
                if claim_stage != "full"
                else None,
            )
            return True

        _update_status(config_path, remote_state, message)
        return True


def _upload_fastqs(
    config: dict[str, Any],
    logs_dir: Path,
    transport: RemoteTransport,
) -> None:
    remote_data_dir = config["samples"]["remote_data_dir"]
    private_attempt = bool(config.get("run", {}).get("id"))
    mkdir_command = f"mkdir -p {shell_quote(remote_data_dir)}"
    if private_attempt:
        mkdir_command = f"umask 077 && {mkdir_command} && chmod 700 {shell_quote(remote_data_dir)}"
    result = transport.execute(mkdir_command)
    _log_command(logs_dir, "Create remote FASTQ directory", result)
    local_paths = collect_local_fastq_paths(config)
    if local_paths:
        result = transport.upload(local_paths, remote_data_dir)
        _log_command(logs_dir, "Upload local FASTQ files to server", result)
        if private_attempt:
            uploaded = " ".join(
                shell_quote(f"{remote_data_dir.rstrip('/')}/{path.name}") for path in local_paths
            )
            result = transport.execute(f"chmod 600 {uploaded}")
            _log_command(logs_dir, "Restrict uploaded FASTQ permissions", result)


def _upload_counts_matrix(
    config: dict[str, Any],
    config_path: Path,
    logs_dir: Path,
    transport: RemoteTransport,
) -> None:
    """Upload the counts 直入 matrix to the remote workdir root.

    The web entry stores the uploaded matrix inside the project directory and
    records its absolute path under ``samples.counts_path``; this helper ships
    it to ``<remote_workdir>/counts_matrix.tsv`` (the fixed name the diffexp /
    cms R scripts read).
    """
    samples = config.get("samples", {})
    local_counts = Path(str(samples.get("counts_path") or "")).expanduser()
    remote_workdir = config["server"]["remote_workdir"]
    if not local_counts.is_file():
        raise RuntimeError(
            f"counts 直入需要本地上传矩阵，但 samples.counts_path={local_counts!s} 不是文件。"
        )
    mkdir_command = f"umask 077 && mkdir -p {shell_quote(remote_workdir)}"
    result = transport.execute(mkdir_command)
    _log_command(logs_dir, "Create remote workdir", result)
    remote_target = f"{remote_workdir.rstrip('/')}/counts_matrix.tsv"
    result = transport.upload([local_counts], remote_workdir)
    _log_command(logs_dir, "Upload counts matrix", result)
    result = transport.execute(f"chmod 600 {shell_quote(remote_target)}")
    _log_command(logs_dir, "Restrict counts matrix permissions", result)


def _prepare_remote_scripts(
    config: dict[str, Any],
    config_path: Path,
    project_dir: Path,
    logs_dir: Path,
) -> list[Path]:
    scripts_dir = project_dir / "generated_scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    env_setup_script = scripts_dir / "env_setup.sh"
    env_setup_script.write_text(render_env_setup_script(config), encoding="utf-8", newline="\n")

    run_script = scripts_dir / "run_pipeline.sh"
    run_script.write_text(render_remote_pipeline_script(config), encoding="utf-8", newline="\n")

    submit_name = "submit.sh"
    scheduler = config["server"]["scheduler"]
    if scheduler == "slurm":
        submit_name = "submit.sbatch"
    elif scheduler == "pbs":
        submit_name = "submit.pbs"
    submit_script = scripts_dir / submit_name
    submit_script.write_text(render_submit_script(config), encoding="utf-8", newline="\n")

    rendered_paths = [env_setup_script, run_script, submit_script]

    # 框架 15.3 条件开放：diffexp 启用时把冻结 DESeq2 R 脚本与 colData
    # 一并渲染上传（run_pipeline.sh 在 counts 落盘后调用它）。
    if config.get("pipeline", {}).get("diffexp", {}).get("enabled"):
        from .differential import render_colData, render_diffexp_script

        diffexp_script = scripts_dir / "diffexp_deseq2.R"
        diffexp_script.write_text(render_diffexp_script(config), encoding="utf-8", newline="\n")
        col_data = scripts_dir / "colData.tsv"
        col_data.write_text(render_colData(config), encoding="utf-8", newline="\n")
        rendered_paths.extend([diffexp_script, col_data])

    if config.get("execution", {}).get("mode") == "contract":
        contract_source = project_contract_path(config_path, config)
        contract_copy = scripts_dir / "analysis_contract.json"
        copyfile(contract_source, contract_copy)
        rendered_paths.append(contract_copy)
    _log_event(logs_dir, "scripts_rendered", {"paths": [str(path) for path in rendered_paths]})
    return rendered_paths


def _upload_support_files(
    config: dict[str, Any],
    paths: list[Path],
    logs_dir: Path,
    transport: RemoteTransport,
) -> None:
    remote_workdir = config["server"]["remote_workdir"]

    result = transport.execute(
        f"umask 077 && mkdir -p {shell_quote(remote_workdir + '/scripts')} "
        f"{shell_quote(remote_workdir + '/logs')} "
        f"{shell_quote(remote_workdir + '/status')} && "
        f"chmod 700 {shell_quote(remote_workdir)} "
        f"{shell_quote(remote_workdir + '/scripts')} "
        f"{shell_quote(remote_workdir + '/logs')} "
        f"{shell_quote(remote_workdir + '/status')}"
    )
    _log_command(logs_dir, "Create remote work directories", result)

    result = transport.upload(paths, f"{remote_workdir.rstrip('/')}/scripts")
    _log_command(logs_dir, "Upload support scripts", result)
    uploaded = " ".join(
        shell_quote(f"{remote_workdir.rstrip('/')}/scripts/{path.name}") for path in paths
    )
    result = transport.execute(f"chmod 600 {uploaded}")
    _log_command(logs_dir, "Restrict support file permissions", result)


def _parse_scheduler_job_id(config: dict[str, Any], stdout: str) -> str:
    lines = [line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    if len(lines) != 1:
        return ""
    candidate = lines[0]
    scheduler = str(config.get("server", {}).get("scheduler") or "").lower()
    patterns = {
        "local": r"[0-9]+",
        "slurm": r"[0-9]+(?:;[A-Za-z0-9_.-]+)?",
        "pbs": r"[0-9]+(?:\.[A-Za-z0-9_.-]+)?(?:\[\])?",
    }
    pattern = patterns.get(scheduler)
    if not pattern or re.fullmatch(pattern, candidate) is None:
        return ""
    return candidate


def _submit_remote_job(
    config: dict[str, Any],
    logs_dir: Path,
    transport: RemoteTransport,
) -> CommandResult:
    server = config["server"]
    remote_workdir = server["remote_workdir"]
    scheduler = server["scheduler"]

    if scheduler == "slurm":
        remote_cmd = f"cd {shell_quote(remote_workdir)} && sbatch --parsable scripts/submit.sbatch"
    elif scheduler == "pbs":
        remote_cmd = f"cd {shell_quote(remote_workdir)} && qsub scripts/submit.pbs"
    elif scheduler == "local":
        remote_cmd = (
            f"cd {shell_quote(remote_workdir)} && "
            "nohup bash scripts/submit.sh > logs/local-run.out 2>&1 < /dev/null & echo $!"
        )
    else:
        raise ValueError(f"Unsupported scheduler: {scheduler}")

    result = transport.execute(remote_cmd)
    _log_command(logs_dir, "Submit remote job", result)
    return result


def _prepare_stage_scripts(
    config: dict[str, Any],
    config_path: Path,
    project_dir: Path,
    logs_dir: Path,
    stage: str,
) -> list[Path]:
    """Render stage-specific env/stage/submit scripts into the attempt dir.

    Script names follow the stage so a single logical run can hold multiple
    submissions without overwriting each other:
    ``run_stage_<stage>.sh`` and ``submit_stage_<stage>.{sh,sbatch,pbs}``.
    """
    scripts_dir = project_dir / "generated_scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    env_setup_script = scripts_dir / "env_setup.sh"
    env_setup_script.write_text(render_env_setup_script(config), encoding="utf-8", newline="\n")

    run_script = scripts_dir / f"run_stage_{stage}.sh"
    run_script.write_text(render_stage_script(config, stage), encoding="utf-8", newline="\n")

    submit_name = f"submit_stage_{stage}.sh"
    scheduler = config["server"]["scheduler"]
    if scheduler == "slurm":
        submit_name = f"submit_stage_{stage}.sbatch"
    elif scheduler == "pbs":
        submit_name = f"submit_stage_{stage}.pbs"
    submit_script = scripts_dir / submit_name
    submit_script.write_text(
        render_submit_script(config, stage=stage),
        encoding="utf-8",
        newline="\n",
    )

    rendered_paths = [env_setup_script, run_script, submit_script]
    if stage == "de":
        from .differential import (
            render_colData,
            render_diffexp_counts_script,
            render_diffexp_script,
        )

        if config.get("pipeline", {}).get("diffexp", {}).get("enabled"):
            diffexp_script = scripts_dir / "diffexp_deseq2.R"
            col_data = scripts_dir / "colData.tsv"
            # counts 直入：脚本读上传矩阵；否则读 featurecounts 产物。
            if _counts_entry(config):
                diffexp_script.write_text(
                    render_diffexp_counts_script(config), encoding="utf-8", newline="\n"
                )
            else:
                diffexp_script.write_text(
                    render_diffexp_script(config), encoding="utf-8", newline="\n"
                )
            col_data.write_text(render_colData(config), encoding="utf-8", newline="\n")
            rendered_paths.extend([diffexp_script, col_data])
    elif stage == "cms":
        from .cms import render_cms_counts_script, render_cms_script

        cms_script = scripts_dir / "cms_cmscaller.R"
        # counts 直入：读上传矩阵；否则读 featurecounts 产物。
        if _counts_entry(config):
            cms_script.write_text(
                render_cms_counts_script(config), encoding="utf-8", newline="\n"
            )
        else:
            cms_script.write_text(render_cms_script(config), encoding="utf-8", newline="\n")
        rendered_paths.append(cms_script)
    elif stage == "counts":
        # counts 直入（2026-09-08）：DESeq2 / CMScaller 共用同一上传矩阵，
        # 各自的 R 脚本与 colData 一并渲染，由 run_stage_counts.sh 调用。
        from .cms import render_cms_counts_script
        from .differential import render_colData, render_diffexp_counts_script

        if config.get("pipeline", {}).get("diffexp", {}).get("enabled"):
            diffexp_script = scripts_dir / "diffexp_counts_deseq2.R"
            diffexp_script.write_text(
                render_diffexp_counts_script(config), encoding="utf-8", newline="\n"
            )
            rendered_paths.append(diffexp_script)
        if config.get("pipeline", {}).get("cms", {}).get("enabled"):
            cms_script = scripts_dir / "cms_counts_cmscaller.R"
            cms_script.write_text(
                render_cms_counts_script(config), encoding="utf-8", newline="\n"
            )
            rendered_paths.append(cms_script)
        col_data = scripts_dir / "colData.tsv"
        col_data.write_text(render_colData(config), encoding="utf-8", newline="\n")
        rendered_paths.append(col_data)

    _log_event(logs_dir, "stage_scripts_rendered", {"stage": stage, "paths": [str(path) for path in rendered_paths]})
    return rendered_paths


def _submit_stage_job(
    config: dict[str, Any],
    stage: str,
    logs_dir: Path,
    transport: RemoteTransport,
) -> CommandResult:
    server = config["server"]
    remote_workdir = server["remote_workdir"]
    scheduler = server["scheduler"]

    submit_name = f"submit_stage_{stage}.sh"
    if scheduler == "slurm":
        submit_name = f"submit_stage_{stage}.sbatch"
    elif scheduler == "pbs":
        submit_name = f"submit_stage_{stage}.pbs"

    if scheduler == "slurm":
        remote_cmd = f"cd {shell_quote(remote_workdir)} && sbatch --parsable scripts/{submit_name}"
    elif scheduler == "pbs":
        remote_cmd = f"cd {shell_quote(remote_workdir)} && qsub scripts/{submit_name}"
    elif scheduler == "local":
        remote_cmd = (
            f"cd {shell_quote(remote_workdir)} && "
            f"nohup bash scripts/{submit_name} > logs/local-run-{stage}.out 2>&1 < /dev/null & echo $!"
        )
    else:
        raise ValueError(f"Unsupported scheduler: {scheduler}")

    result = transport.execute(remote_cmd)
    _log_command(logs_dir, f"Submit stage {stage} job", result)
    return result


def _poll_until_finished(
    config_path: Path,
    config: dict[str, Any],
    logs_dir: Path,
    transport: RemoteTransport,
) -> str:
    polling = config.get("polling", {})
    interval_seconds = int(polling.get("interval_seconds", 300))
    timeout_hours = float(polling.get("timeout_hours", 168))
    deadline = time.time() + timeout_hours * 3600

    while time.time() < deadline:
        state = _read_remote_state(config, transport)
        if state:
            _update_status(config_path, state, f"Remote state: {state}")
            _log_event(logs_dir, "poll", {"state": state})
            if state in {"completed", "failed"}:
                return state
        time.sleep(interval_seconds)

    _log_event(logs_dir, "poll_timeout", {"timeout_hours": timeout_hours})
    return "timeout"


def _read_remote_state(
    config: dict[str, Any],
    transport: RemoteTransport,
) -> str:
    server = config["server"]
    remote_workdir = server["remote_workdir"]
    command = (
        f"if [ -f {shell_quote(remote_workdir + '/status/completed.flag')} ]; then echo completed; "
        f"elif [ -f {shell_quote(remote_workdir + '/status/failed.flag')} ]; then echo failed; "
        f"elif [ -f {shell_quote(remote_workdir + '/status/state.txt')} ]; then cat {shell_quote(remote_workdir + '/status/state.txt')}; "
        "else echo queued; fi"
    )
    result = transport.execute(command)
    return result.stdout.strip()


def _download_results(
    config: dict[str, Any],
    downloads_dir: Path,
    logs_dir: Path,
    transport: RemoteTransport,
    *,
    stage: str | None = None,
) -> ResultManifestSummary:
    server = config["server"]
    remote_workdir = server["remote_workdir"]
    downloads_dir.mkdir(parents=True, exist_ok=True)
    remote_tar = f"{remote_workdir.rstrip('/')}/downloads_bundle.tar.gz"
    pack_cmd = (
        f"umask 077 && cd {shell_quote(remote_workdir)} && "
        "paths=(); "
        "for d in scripts logs fastp star arriba featurecounts rsem diffexp cms counts_matrix.tsv status; do "
        'if [ -e "$d" ]; then paths+=("$d"); fi; '
        "done; "
        f"tar -czf {shell_quote(remote_tar)} \"${{paths[@]}}\""
    )
    result = transport.execute(pack_cmd)
    _log_command(logs_dir, "Package results on server", result)

    local_tar = downloads_dir / "downloads_bundle.tar.gz"
    result = transport.download(remote_tar, local_tar)
    _log_command(logs_dir, "Download result bundle", result)
    extract_dir = downloads_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    extracted = safe_extract_tar_gz(local_tar, extract_dir)
    result = CommandResult(
        command=["safe_extract_tar_gz", str(local_tar), str(extract_dir)],
        returncode=0,
        stdout=f"Extracted {len(extracted)} archive members.",
        stderr="",
    )
    _log_command(logs_dir, "Extract result bundle locally", result)
    result_manifest_path = downloads_dir.parent / "result_manifest.json"
    summary = create_result_manifest(config, extract_dir, result_manifest_path, stage=stage)
    _log_event(
        logs_dir,
        "results_validated",
        {
            "run_id": config.get("run", {}).get("id", ""),
            "ok": summary.ok,
            "errors": summary.errors,
            "manifest": str(summary.path),
            "files_sha256": summary.files_sha256,
        },
    )
    return summary


def _notify(config: dict[str, Any], state: str, message: str) -> None:
    notification = config.get("notification", {})
    if not notification.get("email_enabled"):
        return
    subject = f"[RNA-seq Agent] {config['project']['id']} {state}"
    body = (
        f"Project: {config['project']['id']}\n"
        f"State: {state}\n"
        f"Message: {message}\n"
        f"Remote workdir: {config['server']['remote_workdir']}\n"
    )
    send_completion_email(notification, subject, body)


_ACTIVE_EXECUTION_CLAIM_STATUSES = {"started", "submitting", "submitted"}
_TERMINAL_EXECUTION_CLAIM_STATUSES = {"completed", "failed"}
_KNOWN_EXECUTION_CLAIM_STATUSES = (
    _ACTIVE_EXECUTION_CLAIM_STATUSES | _TERMINAL_EXECUTION_CLAIM_STATUSES
)
_LEGACY_ACTIVE_PROJECT_STATES = {
    "preparing",
    "validated",
    "uploaded",
    "submitting",
    "submitted",
    "running",
    "queued",
    "timeout",
    "reconcile_required",
}
_LEGACY_ALWAYS_CONFLICT_PROJECT_STATES = {
    "submitting",
    "submitted",
    "running",
    "queued",
    "timeout",
    "reconcile_required",
}
_LEGACY_ACTIVE_STAGE_STATES = _LEGACY_ACTIVE_PROJECT_STATES | {"remote_completed"}


def _best_effort_log_event(logs_dir: Path, event: str, payload: dict[str, Any]) -> None:
    try:
        _log_event(logs_dir, event, payload)
    except BaseException:
        pass


def _execution_contract_id(config_path: Path, config: dict[str, Any]) -> str:
    verified = str(config.get("execution", {}).get("verified_contract_id") or "").strip()
    if verified:
        return verified
    try:
        path = project_contract_path(config_path, config)
        if path.is_file():
            return str(load_json(path).get("contract_id") or "").strip()
    except (OSError, ValueError, TypeError):
        pass
    return ""


def _execution_claim_key(
    *,
    project_id: str,
    contract_id: str,
    run_id: str,
    stage: str,
) -> str:
    body = {
        "project_id": project_id,
        "contract_id": contract_id,
        "scope": "full" if stage == "full" else "stage",
        "run_id": "" if stage == "full" else run_id,
        "stage": stage,
    }
    return f"sha256:{canonical_sha256(body)}"


def _reconciliation_payload(*, required: bool) -> dict[str, Any]:
    if not required:
        return {"required": False, "action": "none"}
    return {
        "required": True,
        "action": (
            "Inspect the scheduler and attempt artifacts for this run before manually "
            "marking the claim completed or failed; never retry while the claim is active."
        ),
    }


def _claim_conflict_message(claim: dict[str, Any]) -> str:
    reason = str(claim.get("reason") or "").strip()
    reason_text = f"Reason: {reason}. " if reason else ""
    return (
        "Execution conflict: durable claim "
        f"{claim.get('claim_id', '<unknown>')} is {claim.get('status', 'active')} "
        f"for run {claim.get('run_id', '<unknown>')} stage {claim.get('stage', '<unknown>')}. "
        f"{reason_text}"
        "Retry is fail-closed. Reconcile the scheduler and attempt artifacts manually, then "
        "mark the claim terminal before starting another execution."
    )


def _reconciliation_conflict(status: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "claim_id": str(status.get("execution_claim_id") or "reconciliation-required"),
        "run_id": str(status.get("run_id") or "legacy-or-unknown"),
        "stage": str(status.get("stage") or "project"),
        "status": "reconcile_required",
        "reason": reason,
    }


def _valid_execution_claim(claim_id: str, claim: Any) -> bool:
    if not isinstance(claim, dict):
        return False
    required_strings = ("claim_id", "project_id", "run_id", "stage", "created_at", "updated_at")
    if any(
        not isinstance(claim.get(field), str) or not claim[field].strip()
        for field in required_strings
    ):
        return False
    if claim.get("claim_id") != claim_id:
        return False
    if not isinstance(claim.get("contract_id"), str):
        return False
    status = claim.get("status")
    if status not in _KNOWN_EXECUTION_CLAIM_STATUSES:
        return False
    key = claim.get("idempotency_key")
    if not isinstance(key, str) or not key.startswith("sha256:"):
        return False
    reconciliation = claim.get("reconciliation")
    return isinstance(reconciliation, dict) and isinstance(reconciliation.get("required"), bool)


def _durable_execution_conflict(status: dict[str, Any]) -> dict[str, Any] | None:
    pointer = str(status.get("execution_claim_id") or "").strip()
    if "execution_claims" in status:
        raw_claims = status.get("execution_claims")
        if not isinstance(raw_claims, dict):
            return _reconciliation_conflict(status, "execution_claims ledger has the wrong type")
        for claim_id, claim in raw_claims.items():
            if not isinstance(claim_id, str) or not _valid_execution_claim(claim_id, claim):
                return _reconciliation_conflict(status, "execution_claims ledger contains a malformed entry")
        if pointer and pointer not in raw_claims:
            return _reconciliation_conflict(status, "execution_claim_id points to a missing claim")
        for claim in raw_claims.values():
            if claim.get("status") in _ACTIVE_EXECUTION_CLAIM_STATUSES:
                return claim
        if str(status.get("state") or "") in _LEGACY_ACTIVE_PROJECT_STATES:
            return _reconciliation_conflict(
                status,
                "project state is active but the durable ledger has no active claim",
            )
    elif pointer:
        return _reconciliation_conflict(status, "execution_claim_id exists without a claim ledger")

    raw_stages = status.get("stages")
    if raw_stages is not None:
        if not isinstance(raw_stages, dict):
            return _reconciliation_conflict(status, "legacy stages ledger has the wrong type")
        for stage_name, record in raw_stages.items():
            if not isinstance(record, dict):
                return _reconciliation_conflict(status, f"legacy stage {stage_name!s} is malformed")
            stage_state = str(record.get("status") or "").strip()
            if stage_state in _LEGACY_ACTIVE_STAGE_STATES:
                conflict = _reconciliation_conflict(
                    status,
                    f"legacy stage {stage_name!s} remains {stage_state}",
                )
                conflict["stage"] = str(stage_name)
                return conflict

    project_state = str(status.get("state") or "").strip()
    has_execution_evidence = bool(
        status.get("run_id")
        or status.get("job_id")
        or status.get("attempt_dir")
        or status.get("remote_run_workdir")
        or raw_stages
    )
    if project_state in _LEGACY_ALWAYS_CONFLICT_PROJECT_STATES or (
        project_state in _LEGACY_ACTIVE_PROJECT_STATES and has_execution_evidence
    ):
        return _reconciliation_conflict(
            status,
            f"legacy project execution remains {project_state}",
        )
    return None


def _begin_execution_claim(config_path: Path, *, stage: str) -> dict[str, Any]:
    """Atomically bind one execution before any remote submission side effect."""

    project_dir = config_path.parent
    with project_state_lock(config_path):
        config = normalize_config(load_json(config_path))
        status = config.setdefault("status", {})
        conflict = _durable_execution_conflict(status)
        if conflict:
            return {"conflict": conflict}
        raw_claims = status.get("execution_claims")
        claims: dict[str, dict[str, Any]] = raw_claims if isinstance(raw_claims, dict) else {}
        project_id = str(config.get("project", {}).get("id") or project_dir.name)
        contract_id = _execution_contract_id(config_path, config)

        run_id = str(status.get("run_id") or "").strip() if stage != "full" else ""
        stage_records = dict(status.get("stages") or {})
        previous = stage_records.get(stage) or {}
        if stage != "full" and run_id and previous.get("status") in {
            "completed",
            "stage_completed",
            "remote_completed",
        }:
            return {
                "outcome": RunOutcome(
                    str(previous["status"]),
                    f"Stage {stage} 已完成（{previous.get('job_id', '')}），跳过重复提交。",
                    project_dir,
                )
            }

        if not run_id:
            run_id = _new_run_id()
        idempotency_key = _execution_claim_key(
            project_id=project_id,
            contract_id=contract_id,
            run_id=run_id,
            stage=stage,
        )

        if stage != "full" and previous.get("status") in {
            "running",
            "queued",
            "submitted",
            "preparing",
            "submitting",
            "timeout",
        }:
            return {
                "conflict": {
                    "claim_id": str(previous.get("claim_id") or "legacy-stage-record"),
                    "run_id": run_id,
                    "stage": stage,
                    "status": str(previous.get("status")),
                }
            }

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        claim_id = f"{run_id}:{stage}:{uuid4().hex[:8]}"
        claim = {
            "claim_version": 1,
            "claim_id": claim_id,
            "project_id": project_id,
            "contract_id": contract_id,
            "run_id": run_id,
            "stage": stage,
            "created_at": now,
            "updated_at": now,
            "status": "started",
            "idempotency_key": idempotency_key,
            "reconciliation": _reconciliation_payload(required=True),
        }
        claims[claim_id] = claim

        attempt_dir = project_dir / "attempts" / run_id
        project_workdir = str(config.get("server", {}).get("remote_workdir") or "").rstrip("/")
        remote_run_workdir = f"{project_workdir}/attempts/{run_id}" if project_workdir else ""
        for stale_field in (
            "job_id",
            "submitted_at",
            "manifest_file",
            "result_manifest_file",
            "result_files_sha256",
            "result_validation_errors",
        ):
            status.pop(stale_field, None)
        status.update(
            {
                "state": "preparing",
                "message": (
                    "Preparing an isolated RNA-seq run attempt."
                    if stage == "full"
                    else f"Preparing isolated run attempt for stage {stage}."
                ),
                "updated_at": now,
                "run_id": run_id,
                "attempt_dir": str(attempt_dir),
                "remote_run_workdir": remote_run_workdir,
                "started_at": now,
                "execution_claim_id": claim_id,
                "execution_claims": claims,
            }
        )
        if stage != "full":
            status["stage"] = stage
            stage_records[stage] = {
                "status": "preparing",
                "claim_id": claim_id,
                "started_at": now,
            }
            status["stages"] = stage_records
        config["status"] = status
        save_json(config_path, config)
        return {"config": config, "run_id": run_id, "claim_id": claim_id}


def _transition_execution_claim(
    config_path: Path,
    claim_id: str,
    *,
    expected: set[str],
    claim_status: str,
    project_state: str,
    project_message: str,
    project_fields: dict[str, Any] | None = None,
    stage_status: str | None = None,
    stage_fields: dict[str, Any] | None = None,
) -> None:
    """CAS a claim and live project status in one project-state transaction."""

    with project_state_lock(config_path):
        config = normalize_config(load_json(config_path))
        status = config.setdefault("status", {})
        claims = status.get("execution_claims")
        if not isinstance(claims, dict) or claim_id not in claims:
            raise RuntimeError(f"Execution claim disappeared: {claim_id}")
        claim = claims[claim_id]
        current = str(claim.get("status") or "")
        if current not in expected:
            raise RuntimeError(
                f"Execution claim CAS failed for {claim_id}: expected {sorted(expected)}, got {current!r}."
            )

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        claim["status"] = claim_status
        claim["updated_at"] = now
        claim["reconciliation"] = _reconciliation_payload(
            required=claim_status in _ACTIVE_EXECUTION_CLAIM_STATUSES
        )
        fields = dict(project_fields or {})
        claims[claim_id] = claim

        status.update(
            {
                "state": project_state,
                "message": project_message,
                "updated_at": now,
                "execution_claim_id": claim_id,
                "execution_claims": claims,
            }
        )
        status.update(fields)
        stage = str(claim.get("stage") or "")
        if stage != "full" and stage_status is not None:
            stage_records = dict(status.get("stages") or {})
            record = dict(stage_records.get(stage) or {})
            record.update({"status": stage_status, "claim_id": claim_id, "updated_at": now})
            record.update(stage_fields or {})
            stage_records[stage] = record
            status["stages"] = stage_records
            status["stage"] = stage
        config["status"] = status
        save_json(config_path, config)


def _record_execution_exception(
    config_path: Path,
    claim_id: str,
    message: str,
    *,
    stage: str | None = None,
) -> None:
    """Close pre-submit failures and preserve ambiguous submit claims."""

    with project_state_lock(config_path):
        config = normalize_config(load_json(config_path))
        status = config.setdefault("status", {})
        claims = status.get("execution_claims")
        if not isinstance(claims, dict) or claim_id not in claims:
            return
        claim = claims[claim_id]
        current = str(claim.get("status") or "")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if current == "started":
            claim["status"] = "failed"
            claim["updated_at"] = now
            claim["reconciliation"] = _reconciliation_payload(required=False)
            if status.get("state") not in {"validation_failed", "policy_failed"}:
                status.update({"state": "run_failed", "message": message, "updated_at": now})
            if stage:
                stage_records = dict(status.get("stages") or {})
                record = dict(stage_records.get(stage) or {})
                record.update({"status": "failed", "claim_id": claim_id, "updated_at": now})
                stage_records[stage] = record
                status["stages"] = stage_records
        elif current in {"submitting", "submitted"}:
            status.update(
                {
                    "state": "reconcile_required",
                    "message": (
                        "Execution stopped after remote submission may have started. "
                        f"Claim {claim_id} remains {current}; inspect the scheduler before any retry."
                    ),
                    "updated_at": now,
                }
            )
        claims[claim_id] = claim
        status["execution_claims"] = claims
        config["status"] = status
        save_json(config_path, config)


def _update_status(
    config_path: Path,
    state: str,
    message: str,
    **fields: Any,
) -> None:
    with project_state_lock(config_path):
        config = normalize_config(load_json(config_path))
        status = config.get("status", {})
        previous_run_id = str(status.get("run_id") or "")
        next_run_id = str(fields.get("run_id") or previous_run_id)
        if "run_id" in fields and next_run_id != previous_run_id:
            status.pop("qc", None)
        status.update(
            {
                "state": state,
                "message": message,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        status.update(fields)
        config["status"] = status
        save_json(config_path, config)


def _record_policy_verification(config_path: Path, policy: dict[str, str]) -> None:
    with project_state_lock(config_path):
        config = normalize_config(load_json(config_path))
        execution = config.setdefault("execution", {})
        execution["mode"] = policy["mode"]
        if policy.get("skill_id"):
            execution["skill_id"] = policy["skill_id"]
        if policy.get("contract_id"):
            execution["verified_contract_id"] = policy["contract_id"]
            execution["verified_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_json(config_path, config)


def _new_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid4().hex[:8]}"


def _config_for_new_attempt(config: dict[str, Any], run_id: str) -> dict[str, Any]:
    runtime = deepcopy(config)
    server = runtime.setdefault("server", {})
    project_workdir = str(server.get("remote_workdir", "")).rstrip("/")
    if not project_workdir:
        raise ValueError("Missing server.remote_workdir.")
    remote_run_workdir = f"{project_workdir}/attempts/{run_id}"
    server["project_workdir"] = project_workdir
    server["remote_workdir"] = remote_run_workdir
    samples_block = runtime.setdefault("samples", {})
    if _fastqs_prestaged(runtime):
        # remote_path：reads 在用户服务器已有目录，保持原样并在 run 里登记。
        remote_data_dir = str(samples_block.get("remote_data_dir") or "").strip()
        if not remote_data_dir or remote_data_dir == "AUTO":
            remote_data_dir = f"{remote_run_workdir}/raw"
    else:
        remote_data_dir = f"{remote_run_workdir}/raw"
    samples_block["remote_data_dir"] = remote_data_dir
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    runtime["run"] = {
        "id": run_id,
        "started_at": started_at,
        "remote_project_workdir": project_workdir,
        "remote_run_workdir": remote_run_workdir,
        "remote_run_data_dir": runtime["samples"]["remote_data_dir"],
    }
    runtime["status"] = {
        "state": "preparing",
        "message": "Immutable project snapshot for an isolated run attempt.",
    }
    return runtime


def _config_for_active_attempt(config: dict[str, Any]) -> dict[str, Any]:
    active = deepcopy(config)
    remote_run_workdir = str(config.get("status", {}).get("remote_run_workdir", "")).strip()
    if remote_run_workdir:
        active.setdefault("server", {})["remote_workdir"] = remote_run_workdir
    return active


def _counts_entry(config: dict[str, Any]) -> bool:
    """Whether the project runs DE/CMS from an uploaded count matrix.

    The web counts 直入 entry flips ``pipeline.cms.enabled`` plus
    ``cms.run_mode == "counts"`` (and / or the same for diffexp); configs
    created through the classic FASTQ wizard keep ``run_mode`` empty and stay
    on the featureCounts contract.
    """
    cms = config.get("cms", {})
    counts_mode = str(cms.get("run_mode", "")).strip().lower() == "counts"
    diffexp_wants_counts = bool(
        config.get("study", {}).get("input", {}).get("counts_matrix")
    )
    return counts_mode or diffexp_wants_counts


def _fastqs_prestaged(config: dict[str, Any]) -> bool:
    """Whether reads already live on the server (no local upload needed).

    The web form's ``remote_path`` data source records the remote FASTQ
    directory the user owns; this flag makes the QC/quant pipeline read
    ``$INPUTDIR`` on the server instead of uploading local files.

    判定已收敛到 ``configuration.is_remote_prestaged_config``：validation 也要
    问同一个问题（否则本地校验会把服务器 reads 报成「缺少输入文件」），
    两处各写一遍必然漂移，这里只做转发。
    """
    return is_remote_prestaged_config(config)


def _write_run_manifest(
    project_config: dict[str, Any],
    run_config: dict[str, Any],
    config_path: Path,
    snapshot_path: Path,
    script_paths: list[Path],
    policy: dict[str, str],
    attempt_dir: Path,
) -> Path:
    inputs: list[dict[str, Any]]
    if policy.get("contract_id"):
        contract = load_json(project_contract_path(config_path, project_config))
        inputs = deepcopy(contract.get("body", {}).get("inputs", []))
    else:
        inputs = build_input_artifacts(project_config)

    script_artifacts = []
    for path in sorted(script_paths, key=lambda item: item.name):
        script_artifacts.append(
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )

    body = {
        "run_id": run_config["run"]["id"],
        "created_at": run_config["run"]["started_at"],
        "project_id": run_config.get("project", {}).get("id", ""),
        "agent": {
            "name": "sysu-rnaseq-agent",
            "version": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "execution": {
            "mode": policy.get("mode", "free"),
            "skill_id": policy.get("skill_id", ""),
            "contract_id": policy.get("contract_id", ""),
        },
        "locations": {
            "local_attempt_dir": str(attempt_dir),
            "remote_target": {
                "host": run_config.get("server", {}).get("host", ""),
                "port": run_config.get("server", {}).get("port", 22),
                "user": run_config.get("server", {}).get("user", ""),
            },
            "remote_project_workdir": run_config["run"]["remote_project_workdir"],
            "remote_run_workdir": run_config["run"]["remote_run_workdir"],
        },
        "fingerprints": {
            "project_snapshot_sha256": sha256_file(snapshot_path),
            "workflow_sha256": canonical_sha256(build_workflow_snapshot(project_config)),
            "inputs_sha256": canonical_sha256(inputs),
            "scripts_sha256": canonical_sha256(script_artifacts),
        },
        "inputs": inputs,
        "scripts": script_artifacts,
    }
    body["audit"] = {
        "revision_id": project_config.get("revision_id") or project_config.get("revision", {}).get("id", ""),
        "attempt_id": run_config.get("attempt_id") or run_config.get("run", {}).get("id", ""),
        "parameter_sha256": canonical_sha256(run_config.get("parameters", run_config.get("pipeline", {}))),
        "config_sha256": canonical_sha256(project_config),
        "validation": {"status": "pass"},
        "preflight": run_config.get("preflight", {}),
        "final_status": "prepared",
    }
    manifest = {
        "schema_version": 1,
        "manifest_id": f"sha256:{canonical_sha256(body)}",
        "body": body,
    }
    path = attempt_dir / "run_manifest.json"
    save_json(path, manifest)
    lineage_scripts = [
        {
            "logical_name": item["name"],
            "path": str((attempt_dir / "generated_scripts" / item["name"]).relative_to(attempt_dir)),
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
        for item in script_artifacts
    ]
    write_io_lineage(attempt_dir, inputs, lineage_scripts)
    write_artifact_index(attempt_dir, script_artifacts)
    return path


def _best_effort_write_audit_manifest(
    attempt_dir: Path,
    *,
    config: dict[str, Any],
    run_id: str,
    attempt_id: str,
    final_status: str,
    error: str,
) -> None:
    """Persist failure evidence without replacing the original exception."""

    try:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        project = config.get("project", {})
        execution = config.get("execution", {})
        if (attempt_dir / "run_manifest.json").is_file():
            finalize_run_manifest(attempt_dir, final_status=final_status, error=error)
            return
        write_audit_run_manifest(
            attempt_dir,
            project_id=str(project.get("id", "")),
            revision_id=str(config.get("revision_id") or config.get("revision", {}).get("id", "")),
            run_id=run_id,
            attempt_id=attempt_id,
            contract_id=str(execution.get("verified_contract_id") or execution.get("contract_id") or ""),
            parameters=config.get("parameters", config.get("pipeline", {})),
            config=config,
            tool_versions={"agent": __version__},
            environment=default_environment(),
            validation={"status": "failed", "error": error},
            preflight=config.get("preflight", {}),
            final_status=final_status,
        )
    except BaseException:
        return


def _finalize_attempt_audit(attempt_dir: Path, final_status: str, message: str) -> None:
    try:
        finalize_run_manifest(attempt_dir, final_status=final_status, error=message)
    except BaseException:
        return


def _log_event(logs_dir: Path, event: str, payload: dict[str, Any]) -> None:
    append_jsonl(
        logs_dir / "events.jsonl",
        {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **payload,
        },
    )


def _log_command(logs_dir: Path, description: str, result: CommandResult) -> None:
    append_jsonl(
        logs_dir / "commands.jsonl",
        {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "description": description,
            "command": result.command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )
