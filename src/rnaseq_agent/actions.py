from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .configuration import normalize_config
from .estimate import estimate_runtime
from .remote import local_project_dir, project_remote_workdir
from .report import generate_report
from .run_agent import RunOutcome, refresh_status, run_project, upload_project_fastqs
from .safety import identifier_error
from .storage import load_json, save_json
from .validation import ValidationResult, validate_local_fastqs


def load_project_config(config_path: Path) -> dict[str, Any]:
    return normalize_config(load_json(config_path))


def save_project_config(output_dir: Path, config: dict[str, Any]) -> tuple[Path, dict[str, Any], str]:
    normalized = prepare_project_config(config)
    config_path = project_config_path(output_dir, normalized["project"]["id"])
    save_json(config_path, normalized)
    return config_path, normalized, normalized["runtime_estimate"]["summary"]


def prepare_project_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_config(config)
    project = normalized["project"]
    project_id_error = identifier_error(project.get("id"), "project.id")
    if project_id_error:
        raise ValueError(project_id_error)
    server = normalized["server"]
    remote_workdir = project_remote_workdir(server["remote_base_dir"], project["id"])
    normalized["server"] = {**server, "remote_workdir": remote_workdir}

    samples = dict(normalized.get("samples", {}))
    if samples.get("remote_data_dir") == "AUTO":
        samples["remote_data_dir"] = f"{remote_workdir}/raw"
    normalized["samples"] = samples

    normalized.setdefault("schema_version", 1)
    normalized.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    normalized["runtime_estimate"] = runtime_payload(normalized)
    normalized.setdefault(
        "status",
        {
            "state": "configured",
            "message": "Project config created by chat mode.",
        },
    )
    return normalized


def project_config_path(output_dir: Path, project_id: str) -> Path:
    return local_project_dir(output_dir, project_id) / "project.json"


def validate_project(config_path: Path) -> tuple[dict[str, Any], ValidationResult]:
    config = load_project_config(config_path)
    return config, validate_local_fastqs(config)


def validation_summary(result: ValidationResult) -> list[str]:
    if result.ok:
        return ["本地 FASTQ 初步校验通过。"]

    lines = ["当前配置还没有通过本地 FASTQ 校验。"]
    lines.extend(f"- 配置错误：{error}" for error in result.errors)
    lines.extend(f"- 缺失文件：{path}" for path in result.missing_files[:8])
    if len(result.missing_files) > 8:
        lines.append(f"- 还有 {len(result.missing_files) - 8} 个缺失文件未显示")
    return lines


def run_project_action(config_path: Path, *, wait: bool = True) -> RunOutcome:
    return run_project(config_path, wait=wait)


def upload_project_fastqs_action(config_path: Path) -> RunOutcome:
    return upload_project_fastqs(config_path)


def refresh_project_status_action(config_path: Path) -> dict[str, Any]:
    return refresh_status(config_path)


def status_summary(status: dict[str, Any]) -> list[str]:
    lines = [f"状态：{status.get('state', 'unknown')}"]
    message = status.get("message")
    if message:
        lines.append(f"说明：{message}")
    if status.get("job_id"):
        lines.append(f"Job ID：{status['job_id']}")
    return lines


def load_status_summary(config_path: Path) -> list[str]:
    config = load_project_config(config_path)
    return status_summary(config.get("status", {}))


def generate_project_report(config_path: Path, output_path: Path | None = None) -> Path:
    return generate_report(config_path, output_path=output_path)


def summarize_project(config: dict[str, Any]) -> list[str]:
    project = config["project"]
    server = config["server"]
    samples = config.get("samples", {}).get("items", [])
    enabled_steps = [name for name, step in config.get("pipeline", {}).items() if step.get("enabled")]
    runtime = config.get("runtime_estimate") or runtime_payload(config)
    return [
        f"项目 ID：{project['id']}",
        f"项目名称：{project['title']}",
        f"远程目录：{server['remote_workdir']}",
        f"本地 FASTQ 目录：{config['samples'].get('local_data_dir', '')}",
        f"样本数量：{len(samples)}",
        f"调度器：{server.get('scheduler', 'unknown')}，线程：{server.get('threads', 'unknown')}，内存：{server.get('memory_gb', 'unknown')}G",
        f"启用步骤：{', '.join(enabled_steps) if enabled_steps else '无'}",
        runtime["summary"],
    ]


def explain_project(config: dict[str, Any]) -> list[str]:
    status = config.get("status", {})
    pipeline = config.get("pipeline", {})
    lines = [
        "结果解释建议：",
        "- 先看 status，确认项目目前是 configured / submitted / completed / failed 哪一种状态。",
    ]
    if status.get("state") != "completed":
        lines.append(
            "- 当前项目还没有显示 completed，应先关注配置是否通过、"
            "任务是否提交成功、服务器状态是否正常。"
        )
    else:
        lines.append(
            "- 当前项目已完成，可以结合 report.md 和 downloads/extracted/ "
            "目录查看主要产物。"
        )
    lines.extend(
        [
            "- fastp：查看质控和剪切结果，判断原始数据质量是否可接受。",
            "- STAR：查看比对日志，判断比对率是否合理。",
        ]
    )
    if pipeline.get("featurecounts", {}).get("enabled"):
        lines.append("- featureCounts：gene count 矩阵是后续差异表达分析的常用输入。")
    if pipeline.get("rsem", {}).get("enabled"):
        lines.append("- RSEM：查看基因和转录本表达定量结果。")
    if pipeline.get("arriba", {}).get("enabled"):
        lines.append("- Arriba：查看融合基因候选，但必须人工复核，不能直接作为结论。")
    lines.extend(
        [
            "- 推荐解释顺序：数据质量 → 比对情况 → 表达矩阵/定量结果 → 融合候选。",
            "- 本说明用于辅助阅读流程产物，不替代生物学或临床结论。",
        ]
    )
    return lines


def runtime_payload(config: dict[str, Any]) -> dict[str, Any]:
    runtime = estimate_runtime(config)
    return {"hours": runtime.hours, "summary": runtime.summary}
