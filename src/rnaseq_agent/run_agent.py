from __future__ import annotations

import time
import platform
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
from .shell import shell_quote
from .storage import append_jsonl, load_json, save_json
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
    config = normalize_config(load_json(config_path))
    save_json(config_path, config)
    project_dir = config_path.parent
    run_id = _new_run_id()
    attempt_dir = project_dir / "attempts" / run_id
    logs_dir = attempt_dir / "agent_logs"
    downloads_dir = attempt_dir / "downloads"
    logs_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)

    run_config = _config_for_new_attempt(config, run_id)
    snapshot_path = attempt_dir / "project.snapshot.json"
    save_json(snapshot_path, run_config)
    _update_status(
        config_path,
        "preparing",
        "Preparing an isolated RNA-seq run attempt.",
        run_id=run_id,
        attempt_dir=str(attempt_dir),
        remote_run_workdir=run_config["server"]["remote_workdir"],
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

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
        submit_result = _submit_remote_job(run_config, logs_dir, transport)
        job_id = submit_result.stdout.strip() or "submitted"

        _update_status(
            config_path,
            "submitted",
            "Remote analysis job submitted.",
            job_id=job_id,
            submitted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            manifest_file=str(manifest_path),
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
                _update_status(config_path, "download_failed", message)
                _notify(run_config, "download_failed", message)
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
                _notify(run_config, "result_validation_failed", message)
                return RunOutcome("result_validation_failed", message, project_dir)
            message = "RNA-seq analysis completed and results downloaded."
            final_state = "completed"
        elif final_state == "timeout":
            message = "RNA-seq analysis did not finish before the polling timeout."
        else:
            message = "RNA-seq analysis failed on the server."

        _update_status(config_path, final_state, message)
        _notify(run_config, final_state, message)
        return RunOutcome(final_state, message, project_dir)
    except Exception as exc:
        message = str(exc)
        current_state = normalize_config(load_json(config_path)).get("status", {}).get("state")
        if current_state not in {"validation_failed", "policy_failed"}:
            _update_status(config_path, "run_failed", message)
        _log_event(logs_dir, "run_failed", {"run_id": run_id, "message": message})
        try:
            _notify(run_config, "run_failed", message)
        except Exception as notify_exc:
            _log_event(logs_dir, "notify_failed", {"message": str(notify_exc)})
        raise


# -- staged execution (说明书 §5 分阶段；UI 设计：检查点绑定同一 attempt) ----


def run_stage_project(
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
    from .pipeline import ALL_STAGES, STAGE_QC

    if stage not in ALL_STAGES:
        raise ValueError(f"Unsupported stage: {stage!r}. Allowed: {ALL_STAGES}")

    config = normalize_config(load_json(config_path))
    save_json(config_path, config)
    project_dir = config_path.parent

    # -- logical run id (created at qc stage; reused by later stages) -----
    status = config.get("status", {})
    run_id = str(status.get("run_id") or "").strip()
    stage_records = dict(status.get("stages") or {})
    previous = stage_records.get(stage) or {}

    if run_id and previous.get("status") in {"completed", "stage_completed", "remote_completed"}:
        return RunOutcome(
            str(previous["status"]),
            f"Stage {stage} 已完成（{previous.get('job_id', '')}），跳过重复提交。",
            project_dir,
        )
    if run_id and previous.get("status") in {"running", "queued", "submitted", "preparing"}:
        return RunOutcome(
            "submitted",
            f"Stage {stage} 已在运行（{previous.get('job_id', '')}），请先轮询。",
            project_dir,
        )

    if run_id:
        attempt_dir = project_dir / "attempts" / run_id
    else:
        run_id = _new_run_id()
        attempt_dir = project_dir / "attempts" / run_id
        stage_records = {}

    logs_dir = attempt_dir / "agent_logs"
    downloads_dir = attempt_dir / "downloads"
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
    save_json(snapshot_path, run_config)
    _update_status(
        config_path,
        "preparing",
        f"Preparing isolated run attempt for stage {stage}.",
        run_id=run_id,
        attempt_dir=str(attempt_dir),
        remote_run_workdir=run_config["server"]["remote_workdir"],
        stage=stage,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
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
        support_files = [*remote_scripts, snapshot_path]
        manifest_path = attempt_dir / "run_manifest.json"
        if manifest_path.is_file():
            support_files.append(manifest_path)
        _upload_support_files(run_config, support_files, logs_dir, transport)

        submit_result = _submit_stage_job(run_config, stage, logs_dir, transport)
        job_id = submit_result.stdout.strip() or "submitted"
        stage_records[stage] = {
            "status": "submitted",
            "job_id": job_id,
            "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _update_status(
            config_path,
            "submitted",
            f"Stage {stage} submitted.",
            run_id=run_id,
            job_id=job_id,
            submitted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            stages=stage_records,
            stage=stage,
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
                _update_status(config_path, "download_failed", message, stage=stage)
                _notify(run_config, "download_failed", message)
                return RunOutcome("download_failed", message, project_dir)
            if not summary.ok:
                message = f"Stage {stage} results are incomplete or invalid: " + "; ".join(summary.errors)
                _update_status(config_path, "result_validation_failed", message, stage=stage)
                _notify(run_config, "result_validation_failed", message)
                return RunOutcome("result_validation_failed", message, project_dir)
            stage_records[stage]["status"] = "completed"
            _update_status(
                config_path,
                "stage_completed",
                f"Stage {stage} completed and results verified.",
                run_id=run_id,
                stages=stage_records,
                stage=stage,
            )
            return RunOutcome("stage_completed", f"Stage {stage} completed.", project_dir)
        if final_state == "timeout":
            message = f"Stage {stage} did not finish before the polling timeout."
        else:
            message = f"Stage {stage} failed on the server."
        stage_records[stage]["status"] = final_state
        _update_status(config_path, final_state, message, stage=stage, stages=stage_records)
        _notify(run_config, final_state, message)
        return RunOutcome(final_state, message, project_dir)
    except Exception as exc:
        message = str(exc)
        current_state = normalize_config(load_json(config_path)).get("status", {}).get("state")
        if current_state not in {"validation_failed", "policy_failed"}:
            _update_status(config_path, "run_failed", message, stage=stage)
        _log_event(logs_dir, "stage_run_failed", {"run_id": run_id, "stage": stage, "message": message})
        try:
            _notify(run_config, "run_failed", message)
        except Exception as notify_exc:
            _log_event(logs_dir, "notify_failed", {"message": str(notify_exc)})
        raise


def refresh_status(config_path: Path) -> dict[str, Any]:
    config = normalize_config(load_json(config_path))
    active_config = _config_for_active_attempt(config)
    state = _read_remote_state(active_config, create_remote_transport(active_config))
    if state:
        message = f"Remote state: {state}"
        _update_status(config_path, state, message)
    return normalize_config(load_json(config_path)).get("status", {})


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


def _update_status(
    config_path: Path,
    state: str,
    message: str,
    **fields: Any,
) -> None:
    config = normalize_config(load_json(config_path))
    status = config.get("status", {})
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
    manifest = {
        "schema_version": 1,
        "manifest_id": f"sha256:{canonical_sha256(body)}",
        "body": body,
    }
    path = attempt_dir / "run_manifest.json"
    save_json(path, manifest)
    return path


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
