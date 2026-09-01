from __future__ import annotations

from copy import deepcopy
from typing import Any

from .defaults import DEFAULT_PIPELINE


# 框架 15.2 冻结的 bulk RNA 黄金路线能力槽（对齐架构图）。
DEFAULT_WORKFLOW_PROFILE = "workflow.bulk_rna.grch38_pe_expression_fusion"


WORKFLOW_PROFILES: dict[str, dict[str, Any]] = {
    # 框架 15.2：workflow.bulk_rna.grch38_pe_expression_fusion 1.0.0
    # 固定 paired-end bulk RNA 表达/融合主线。
    "workflow.bulk_rna.grch38_pe_expression_fusion": {
        "title": "Bulk RNA-seq expression + fusion (GRCh38 paired-end) v1",
        "description": (
            "Versioned fastp -> MultiQC -> STAR -> featureCounts + RSEM -> "
            "Arriba workflow for GRCh38 Illumina paired-end FASTQ, matching "
            "framework gold route A."
        ),
        "cancer_types": ["pan_cancer"],
        "sample_mode": "cohort_or_single",
        "pipeline": deepcopy(DEFAULT_PIPELINE),
    },
}


def list_workflow_profiles() -> list[tuple[str, dict[str, Any]]]:
    return [(profile_id, deepcopy(WORKFLOW_PROFILES[profile_id])) for profile_id in sorted(WORKFLOW_PROFILES)]


def apply_workflow_profile(config: dict[str, Any], profile_id: str) -> dict[str, Any]:
    if profile_id not in WORKFLOW_PROFILES:
        available = ", ".join(sorted(WORKFLOW_PROFILES))
        raise ValueError(f"Unknown workflow profile: {profile_id}. Available profiles: {available}")

    updated = deepcopy(config)
    updated["pipeline"] = deepcopy(WORKFLOW_PROFILES[profile_id]["pipeline"])
    execution = updated.setdefault("execution", {})
    execution.update(
        {
            "mode": "skill",
            "skill_id": profile_id,
            "contract_file": execution.get("contract_file", "analysis_contract.json"),
        }
    )
    execution.pop("source_mode", None)
    execution.pop("verified_contract_id", None)
    execution.pop("verified_at", None)
    return updated


def workflow_profile_errors(config: dict[str, Any]) -> list[str]:
    execution = config.get("execution", {})
    if execution.get("mode") != "skill":
        return []

    profile_id = str(execution.get("skill_id", "")).strip()
    if not profile_id:
        return ["Skill execution mode requires execution.skill_id."]
    profile = WORKFLOW_PROFILES.get(profile_id)
    if profile is None:
        return [f"Unknown workflow profile: {profile_id}"]

    errors: list[str] = []
    actual_pipeline = config.get("pipeline", {})
    expected_pipeline = profile["pipeline"]
    for step, expected in expected_pipeline.items():
        actual = actual_pipeline.get(step, {})
        if bool(actual.get("enabled")) != bool(expected["enabled"]):
            errors.append(
                f"Workflow profile {profile_id} requires pipeline.{step}.enabled="
                f"{str(bool(expected['enabled'])).lower()}."
            )
        if str(actual.get("version", "")) != str(expected["version"]):
            errors.append(
                f"Workflow profile {profile_id} requires pipeline.{step}.version="
                f"{expected['version']}."
            )
    return errors
