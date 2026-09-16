from __future__ import annotations

from copy import deepcopy
from typing import Any

from .defaults import (
    DEFAULT_CMS,
    DEFAULT_CONTAINER,
    DEFAULT_DIFFEXP,
    DEFAULT_PIPELINE,
    DEFAULT_REFERENCE,
)


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(config)

    reference = {**DEFAULT_REFERENCE, **normalized.get("reference", {})}
    normalized["reference"] = reference

    server = normalized.setdefault("server", {})
    server.setdefault("port", 22)
    server.setdefault("shell", "bash")
    server.setdefault("init_commands", [])

    normalized.setdefault(
        "polling",
        {
            "interval_seconds": 300,
            "timeout_hours": 168,
        },
    )

    pipeline = normalized.setdefault("pipeline", {})
    for step, defaults in DEFAULT_PIPELINE.items():
        pipeline.setdefault(step, defaults.copy())
        pipeline[step].setdefault("enabled", defaults["enabled"])
        pipeline[step].setdefault("version", defaults["version"])

    container = {**DEFAULT_CONTAINER, **normalized.get("container", {})}
    container.setdefault("bind_paths", [])
    normalized["container"] = container

    diffexp = {**DEFAULT_DIFFEXP, **normalized.get("diffexp", {})}
    diffexp.setdefault("formula", DEFAULT_DIFFEXP["formula"])
    diffexp.setdefault("min_replicates_per_group", DEFAULT_DIFFEXP["min_replicates_per_group"])
    normalized["diffexp"] = diffexp

    cms = {**DEFAULT_CMS, **normalized.get("cms", {})}
    cms.setdefault("n_perm", DEFAULT_CMS["n_perm"])
    cms.setdefault("fdr", DEFAULT_CMS["fdr"])
    cms.setdefault("seed", DEFAULT_CMS["seed"])
    cms.setdefault("do_plot", DEFAULT_CMS["do_plot"])
    cms.setdefault("min_samples", DEFAULT_CMS["min_samples"])
    cms.setdefault("run_mode", DEFAULT_CMS["run_mode"])
    cms.setdefault("reference_condition", DEFAULT_CMS["reference_condition"])
    normalized["cms"] = cms

    normalized.setdefault("notification", {"email_enabled": False})
    normalized.setdefault("sequencing", {"layout": "paired", "strandedness": "auto"})
    normalized.setdefault("samples", {"items": []})

    execution = normalized.setdefault("execution", {})
    execution.setdefault("mode", "free")
    execution.setdefault("skill_id", "")
    execution.setdefault("contract_file", "analysis_contract.json")
    return normalized


def is_counts_entry_config(config: dict[str, Any]) -> bool:
    """Whether the project runs DE/CMS from an uploaded count matrix.

    Truth sources (any one suffices):

    - ``samples.source == "counts_upload"``  —— web counts 直入表单写的来源;
    - ``cms.run_mode == "counts"``           —— CMS 直入模式;
    - ``study.input.counts_matrix``          —— 早期 counts 直入配置。

    Counts 直入项目没有 FASTQ 主流程：sample 表不需要 fastq_1/fastq_2，
    校验与适用性门禁不应再要求 STAR/featureCounts 参考路径与 paired 布局。
    """
    if str(config.get("samples", {}).get("source", "")).strip() == "counts_upload":
        return True
    if str(config.get("cms", {}).get("run_mode", "")).strip().lower() == "counts":
        return True
    return bool(config.get("study", {}).get("input", {}).get("counts_matrix"))


def is_remote_prestaged_config(config: dict[str, Any]) -> bool:
    """Whether reads already live on the server (no local upload needed).

    Truth sources (any one suffices):

    - ``samples.remote_prestaged``      —— web ``remote_path`` 表单写的标记;
    - ``samples.source == "remote_path"`` —— 同一表单写的来源字段。

    ``remote_path`` 项目里 ``samples.items[].fastq_1`` 只是**服务器上的文件名**，
    真正的目录在 ``samples.remote_data_dir``。本地校验若拿 ``local_data_dir``
    去拼这些文件名，必然把每一个样本都报成「缺少输入文件」——用户明明把
    服务器路径给对了，却收到一屏虚假失败（2026-09-16 实测，UI 按钮和对话
    写盘两条路径都中招）。所以校验必须先问这个函数，再决定要不要碰本地文件。

    与 ``run_agent._fastqs_prestaged`` 是同一个语义，收敛在这里做单一真源。
    """
    samples = config.get("samples", {})
    if samples.get("remote_prestaged"):
        return True
    return str(samples.get("source", "")).strip() == "remote_path"
