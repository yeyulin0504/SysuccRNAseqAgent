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
