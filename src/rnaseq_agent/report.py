from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .configuration import normalize_config
from .estimate import estimate_runtime
from .storage import load_json


def generate_report(config_path: Path, output_path: Path | None = None) -> Path:
    config = normalize_config(load_json(config_path))
    project_dir = config_path.parent
    if output_path is None:
        output_path = project_dir / "report.md"

    run_id = str(config.get("status", {}).get("run_id", "")).strip()
    attempt_dir = project_dir / "attempts" / run_id if run_id else project_dir
    events_path = attempt_dir / "agent_logs" / "events.jsonl"
    commands_path = attempt_dir / "agent_logs" / "commands.jsonl"
    extracted_dir = attempt_dir / "downloads" / "extracted"

    events, bad_event_lines = _load_jsonl(events_path)
    commands, bad_command_lines = _load_jsonl(commands_path)
    files = _collect_files(extracted_dir)
    downstream_summary = _load_downstream_summary(extracted_dir / "downstream" / "summary.json")

    report = _render_report(
        config=config,
        config_path=config_path,
        project_dir=project_dir,
        attempt_dir=attempt_dir,
        events=events,
        commands=commands,
        files=files,
        downstream_summary=downstream_summary,
        bad_event_lines=bad_event_lines,
        bad_command_lines=bad_command_lines,
        has_extracted=extracted_dir.exists(),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    return output_path


def _load_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0

    items: list[dict[str, Any]] = []
    bad_lines = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if isinstance(payload, dict):
                items.append(payload)
            else:
                bad_lines += 1
    return items, bad_lines


def _collect_files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*")
        if path.is_file()
    )


def _load_downstream_summary(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _render_report(
    *,
    config: dict[str, Any],
    config_path: Path,
    project_dir: Path,
    attempt_dir: Path,
    events: list[dict[str, Any]],
    commands: list[dict[str, Any]],
    files: list[str],
    downstream_summary: dict[str, Any] | None,
    bad_event_lines: int,
    bad_command_lines: int,
    has_extracted: bool,
) -> str:
    project = config.get("project", {})
    status = config.get("status", {})
    samples = config.get("samples", {}).get("items", [])
    sequencing = config.get("sequencing", {})
    reference = config.get("reference", {})
    server = config.get("server", {})
    polling = config.get("polling", {})
    notification = config.get("notification", {})
    pipeline = config.get("pipeline", {})
    execution = config.get("execution", {})
    runtime = config.get("runtime_estimate") or {
        "summary": estimate_runtime(config).summary,
    }

    title = project.get("title") or project.get("id") or "RNA-seq Project"
    lines: list[str] = []
    lines.append(f"# {title} RNA-seq Run Report")
    lines.append("")
    lines.append("## Project summary")
    lines.append("")
    lines.append(f"- Project ID: {project.get('id', 'unknown')}")
    lines.append(f"- Project title: {project.get('title', 'unknown')}")
    lines.append(f"- Owner: {project.get('owner', 'unknown')}")
    lines.append(f"- Status: {status.get('state', 'unknown')}")
    lines.append(f"- Message: {status.get('message', 'No status message available.')}")
    if status.get("job_id"):
        lines.append(f"- Job ID: {status['job_id']}")
    if status.get("submitted_at"):
        lines.append(f"- Submitted at: {status['submitted_at']}")
    if status.get("updated_at"):
        lines.append(f"- Updated at: {status['updated_at']}")
    if status.get("result_manifest_file"):
        lines.append(f"- Result manifest: {status['result_manifest_file']}")
    if status.get("result_files_sha256"):
        lines.append(f"- Result file-list fingerprint: sha256:{status['result_files_sha256']}")
    lines.append(f"- Config path: {config_path}")
    lines.append(f"- Project directory: {project_dir}")
    lines.append(f"- Run ID: {status.get('run_id', 'Not recorded (legacy layout)')}")
    lines.append(f"- Local attempt directory: {attempt_dir}")
    lines.append(
        f"- Remote run workdir: {status.get('remote_run_workdir') or server.get('remote_workdir', 'unknown')}"
    )
    lines.append(f"- Runtime estimate: {runtime.get('summary', 'Not available')}")
    lines.append(f"- Execution mode: {execution.get('mode', 'free')}")
    if execution.get("skill_id"):
        lines.append(f"- Workflow skill: {execution['skill_id']}")
    if execution.get("mode") == "contract":
        lines.append(f"- Analysis contract: {execution.get('contract_file', 'analysis_contract.json')}")
        if execution.get("verified_contract_id"):
            lines.append(f"- Verified contract ID: {execution['verified_contract_id']}")
    lines.append("")

    lines.append("## Input data")
    lines.append("")
    lines.append(f"- Source: {config.get('samples', {}).get('source', 'unknown')}")
    lines.append(f"- Local FASTQ directory: {config.get('samples', {}).get('local_data_dir', 'unknown')}")
    lines.append(f"- Remote FASTQ directory: {config.get('samples', {}).get('remote_data_dir', 'unknown')}")
    lines.append(f"- Sample count: {len(samples)}")
    lines.append(f"- Layout: {sequencing.get('layout', 'unknown')}")
    lines.append(f"- Strandedness: {sequencing.get('strandedness', 'unknown')}")
    lines.append("")
    lines.append("| Sample ID | Condition | FASTQ R1 | FASTQ R2 |")
    lines.append("| --- | --- | --- | --- |")
    for sample in samples:
        lines.append(
            f"| {sample.get('sample_id', '')} | {sample.get('condition', '')} | {sample.get('fastq_1', '')} | {sample.get('fastq_2', '')} |"
        )
    if not samples:
        lines.append("| No samples found |  |  |  |")
    lines.append("")

    lines.append("## Reference and run settings")
    lines.append("")
    lines.append(f"- Reference name: {reference.get('name', 'unknown')}")
    lines.append(f"- Species: {reference.get('species', 'unknown')}")
    lines.append(f"- Release: {reference.get('release', 'unknown')}")
    lines.append(f"- Assembly: {reference.get('assembly', 'unknown')}")
    lines.append(f"- Regions: {reference.get('regions', 'unknown')}")
    lines.append(f"- Scheduler: {server.get('scheduler', 'unknown')}")
    lines.append(f"- Threads: {server.get('threads', 'unknown')}")
    lines.append(f"- Memory GB: {server.get('memory_gb', 'unknown')}")
    lines.append(f"- Remote workdir: {server.get('remote_workdir', 'unknown')}")
    lines.append(f"- Poll interval seconds: {polling.get('interval_seconds', 'unknown')}")
    lines.append(f"- Poll timeout hours: {polling.get('timeout_hours', 'unknown')}")
    lines.append(f"- Email notification enabled: {bool(notification.get('email_enabled'))}")
    lines.append("")

    lines.append("## Enabled pipeline steps")
    lines.append("")
    for step in ["fastp", "star", "arriba", "featurecounts", "rsem"]:
        step_cfg = pipeline.get(step, {})
        lines.append(
            f"- {step}: enabled={bool(step_cfg.get('enabled'))}, version={step_cfg.get('version', 'unknown')}"
        )
    lines.append("")

    lines.append("## Downstream analysis")
    lines.append("")
    downstream = config.get("downstream", {})
    if not downstream.get("enabled", False):
        lines.append("- Not enabled for this run.")
    else:
        lines.append(f"- Profile: {downstream.get('profile_id', 'unknown')}")
        lines.append(f"- Design: {downstream.get('design', {}).get('formula', 'unknown')}")
        lines.append(f"- Contrasts: {', '.join(str(item.get('id', '')) for item in downstream.get('contrasts', [])) or 'none'}")
        lines.append(f"- Filter: count ≥ {downstream.get('filtering', {}).get('min_count', 'unknown')} in at least {downstream.get('filtering', {}).get('min_samples', 'unknown')} samples.")
        lines.append(f"- DE threshold: adjusted P ≤ {downstream.get('differential_expression', {}).get('padj_threshold', 'unknown')}; |log2FC| ≥ {downstream.get('differential_expression', {}).get('abs_log2_fold_change', 'unknown')}.")
        lines.append(f"- Immutable runtime image SHA-256: {downstream.get('runtime', {}).get('image_sha256', 'unknown')}")
        if downstream_summary:
            lines.append(f"- Completed design: {downstream_summary.get('design', 'unknown')}")
            for contrast_id, result in downstream_summary.get("contrasts", {}).items():
                lines.append(f"- {contrast_id}: {result.get('significant_gene_count', 'unknown')} genes met the configured DE threshold; results at {result.get('result_table', 'unknown')}.")
        else:
            lines.append("- No downloaded downstream summary was available.")
    lines.append("")

    lines.append("## Run timeline")
    lines.append("")
    if events:
        recent_events = events[-20:]
        for event in recent_events:
            summary = _summarize_event(event)
            lines.append(f"- {summary}")
        if bad_event_lines:
            lines.append(f"- Note: skipped {bad_event_lines} malformed event log lines.")
    else:
        lines.append("- No event log found.")
    lines.append("")

    lines.append("## Command summary")
    lines.append("")
    if commands:
        lines.append(f"- Commands logged: {len(commands)}")
        failures = [command for command in commands if int(command.get('returncode', 0)) != 0]
        for command in commands:
            lines.append(
                f"- {command.get('timestamp', 'unknown time')} | {command.get('description', 'command')} | returncode={command.get('returncode', 'unknown')}"
            )
        if failures:
            lines.append("")
            lines.append("### Command failures")
            lines.append("")
            for command in failures:
                stderr = str(command.get("stderr", "")).strip() or "No stderr captured."
                lines.append(f"- {command.get('description', 'command')}: {stderr}")
        if bad_command_lines:
            lines.append(f"- Note: skipped {bad_command_lines} malformed command log lines.")
    else:
        lines.append("- No command log found.")
    lines.append("")

    lines.append("## Result files")
    lines.append("")
    if has_extracted and files:
        for file_path in files:
            lines.append(f"- {file_path}")
    elif has_extracted:
        lines.append("- downloads/extracted exists, but no files were found.")
    else:
        lines.append("- No downloaded results found locally.")
    lines.append("")

    lines.append("## How to read these outputs")
    lines.append("")
    lines.append("- fastp: quality control and trimming reports.")
    lines.append("- star: alignment logs and mapping-related outputs.")
    lines.append("- featurecounts: gene-level count matrix for downstream differential expression.")
    lines.append("- downstream: VST quality control, differential-expression tables, and configured enrichment outputs.")
    lines.append("- rsem: gene and transcript expression quantification results.")
    lines.append("- arriba: candidate fusion calls when this step is enabled.")
    lines.append("- status/logs: execution state and audit trail for this run.")
    lines.append("")

    lines.append("## Warnings and gaps")
    lines.append("")
    lines.append("- This report summarizes run configuration, logs, and output artifacts.")
    lines.append("- It does not provide biological conclusions or replace manual expert review.")
    if downstream.get("enabled", False):
        lines.append("- Downstream statistics and enrichment require biological and statistical interpretation; they are not automated biological conclusions.")
    if not events:
        lines.append("- Event timeline is incomplete because no event log was found.")
    if not commands:
        lines.append("- Command audit is incomplete because no command log was found.")
    if not has_extracted:
        lines.append("- Result inventory is incomplete because no local extracted download directory was found.")
    if execution.get("mode", "free") == "free":
        lines.append("- The run is not locked to an approved analysis contract.")
    result_validation_errors = status.get("result_validation_errors") or []
    for error in result_validation_errors:
        lines.append(f"- Result validation: {error}")
    return "\n".join(lines) + "\n"


def _summarize_event(event: dict[str, Any]) -> str:
    timestamp = event.get("timestamp", "unknown time")
    name = event.get("event", "unknown_event")
    extras: list[str] = []
    for key in ["job_id", "message", "config"]:
        value = event.get(key)
        if value:
            extras.append(f"{key}={value}")
    if extras:
        return f"{timestamp} | {name} | " + "; ".join(extras)
    return f"{timestamp} | {name}"
