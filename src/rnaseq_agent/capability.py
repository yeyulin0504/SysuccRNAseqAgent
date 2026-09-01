"""Capability registry, Gate-A checks, and execution plan generation.

This module implements the MVP subset of the architecture described in
docs/superpowers/specs/2026-08-20-local-first-multiomics-agent-design.md:

    Capability Resolver -> Gate-A -> Adapter.inspect/plan -> Analysis Contract
        -> Adapter.materialize -> Execution Gateway

Responsibilities are kept deliberately narrow:

- ``Capability``  : an immutable, versioned description of an analysis we are
                    allowed to run (what it takes as input, what it produces).
- ``Gate-A``      : metadata-level suitability checks before any heavy work.
                    Failures are returned as ``NOT_EVALUABLE`` instead of
                    raising, mirroring the design document's refusal semantics.
- ``ExecutionPlan``: the concrete materialized plan that is later frozen into
                    an Analysis Contract.

Nothing in this module executes analysis; it only inspects and plans.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .defaults import DEFAULT_PIPELINE
from .workflow_profiles import DEFAULT_WORKFLOW_PROFILE, WORKFLOW_PROFILES

CAPABILITY_SCHEMA_VERSION = 1

# Refusal semantics from the design document (framework section 6.3).
NOT_EVALUABLE = "NOT_EVALUABLE"
ABSTAIN = "ABSTAIN"
PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
FAIL_OUTPUT_CONTRACT = "FAIL_OUTPUT_CONTRACT"
NOT_APPLICABLE = "NOT_APPLICABLE"
WAITING_USER = "WAITING_USER"
WAITING_HPC = "WAITING_HPC"
STALE = "STALE"


@dataclass(frozen=True)
class Capability:
    capability_id: str
    title: str
    description: str
    version: str
    input_contract: dict[str, Any]
    output_artifacts: dict[str, str]
    requires_reference_keys: list[str]
    pipeline: dict[str, Any]
    source: str = "workflow_profile"


@dataclass(frozen=True)
class GateResult:
    verdict: str
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict == PASS

    def formatted(self) -> list[str]:
        if self.ok:
            return ["通过：满足 Gate-A 元数据级适用性检查。"]
        prefix = "不适用" if self.verdict == NOT_EVALUABLE else "警告"
        return [f"{prefix}：{reason}" for reason in self.reasons]


@dataclass(frozen=True)
class ExecutionPlan:
    capability_id: str
    config: dict[str, Any]
    steps: list[str]
    summary: str


def register_builtin_capabilities() -> dict[str, Capability]:
    """Return the versioned set of workflow capabilities available in this MVP.

    Capability IDs follow the frozen slots in framework section 15.2.
    """

    bulk_profile = WORKFLOW_PROFILES[DEFAULT_WORKFLOW_PROFILE]

    return {
        # 框架 15.2：workflow.bulk_rna.grch38_pe_expression_fusion 1.0.0
        DEFAULT_WORKFLOW_PROFILE: Capability(
            capability_id=DEFAULT_WORKFLOW_PROFILE,
            title=bulk_profile["title"],
            description=bulk_profile["description"],
            version="1.0.0",
            input_contract={
                "data_type": "bulk_rna_seq_fastq",
                "layout": {"paired"},  # 框架 15.3：Illumina paired-end
                "min_samples": 1,
                "designs": {"independent_two_group", "single_group"},
                "required_sample_fields": ["sample_id", "condition", "fastq_1"],
                "cancer_types": bulk_profile.get("cancer_types", ["pan_cancer"]),
                "sample_mode": bulk_profile.get("sample_mode", "cohort_or_single"),
            },
            output_artifacts={
                "featurecounts/gene_counts.txt": "gene-level count matrix",
                "rsem/*.genes.results": "per-sample RSEM gene-level quantification",
                "arriba/*.fusions.tsv": "fusion candidate calls (manual review required)",
                "fastp/*.json": "per-sample fastp QC JSON",
                "star/*.Log.final.out": "per-sample STAR alignment summary",
            },
            requires_reference_keys=[
                "star_index_dir",
                "remote_gtf_path",
                "remote_genome_fasta_path",
                "rsem_index_prefix",
            ],
            pipeline=bulk_profile["pipeline"],
        ),
    }


def resolve_capability(capability_id: str) -> Capability:
    """Resolve a capability by id, raising ValueError when unknown."""
    capabilities = register_builtin_capabilities()
    if capability_id not in capabilities:
        available = ", ".join(sorted(capabilities))
        raise ValueError(f"Unknown capability: {capability_id}. Available: {available}")
    return capabilities[capability_id]


def list_capabilities() -> list[Capability]:
    return sorted(register_builtin_capabilities().values(), key=lambda c: c.capability_id)


def gate_a_check(
    capability: Capability,
    config: dict[str, Any],
    *,
    validation_errors: list[str] | None = None,
    missing_files: list[str] | None = None,
) -> GateResult:
    """Run metadata-level suitability checks for a capability.

    Only metadata is inspected (layout, design, sample fields, reference
    settings, file presence). Failures return ``NOT_EVALUABLE`` rather than
    raising, so the session can explain why the request cannot proceed.
    """
    reasons: list[str] = []

    layout = str(config.get("sequencing", {}).get("layout", "paired")).strip()
    if layout not in capability.input_contract["layout"]:
        reasons.append(f"测序类型 {layout!r} 不在能力 {capability.capability_id} 的支持范围内。")

    # 框架 5.2：Gate-A 检查癌种与单样本/队列模式。
    cancer_types = capability.input_contract.get("cancer_types", [])
    declared_cancer = str(config.get("study", {}).get("cancer_type", "")).strip()
    if cancer_types and declared_cancer and declared_cancer not in cancer_types:
        reasons.append(
            f"能力 {capability.capability_id} 的适用癌种为 {cancer_types}，"
            f"当前声明为 {declared_cancer!r}。"
        )

    sample_mode = capability.input_contract.get("sample_mode", "cohort_or_single")
    items = config.get("samples", {}).get("items", [])
    if len(items) < capability.input_contract["min_samples"]:
        reasons.append(
            f"至少需要 {capability.input_contract['min_samples']} 个样本，当前为 {len(items)} 个。"
        )
    design = str(config.get("study", {}).get("design", "")).strip()
    if sample_mode == "cohort_or_single":
        conditions = {str(sample.get("condition", "")).strip() for sample in items}
        conditions.discard("")
        if design and design not in capability.input_contract["designs"]:
            reasons.append(
                f"能力 {capability.capability_id} 支持的设计为 "
                f"{sorted(capability.input_contract['designs'])}，当前声明为 {design!r}。"
            )
        if len(conditions) > 2:
            reasons.append(
                f"首期只接受非配对两组或单组队列设计，检测到 {len(conditions)} 个分组。"
            )

    required_fields = capability.input_contract["required_sample_fields"]
    for index, sample in enumerate(items, start=1):
        missing_fields = [field for field in required_fields if not sample.get(field)]
        if missing_fields:
            reasons.append(
                f"样本 {index}（{sample.get('sample_id') or 'unnamed'}）缺少字段："
                + ", ".join(missing_fields)
            )

    reference = config.get("reference", {})
    for key in capability.requires_reference_keys:
        if not reference.get(key):
            reasons.append(f"能力 {capability.capability_id} 需要参考设置 {key}，当前未配置。")

    for error in validation_errors or []:
        reasons.append(error)
    for missing in missing_files or []:
        reasons.append(f"缺少输入文件：{missing}")

    if reasons:
        return GateResult(verdict=NOT_EVALUABLE, reasons=reasons)
    return GateResult(verdict=PASS)


def build_execution_plan(capability: Capability, config: dict[str, Any]) -> ExecutionPlan:
    """Materialize a concrete plan for a capability against a project config.

    The plan is what gets frozen into the Analysis Contract, so it must be
    deterministic given ``(capability_id, config)``.
    """
    pipeline = config.get("pipeline", {})
    enabled_steps = [name for name, step in pipeline.items() if step.get("enabled")]
    samples = config.get("samples", {}).get("items", [])
    steps = [
        f"{capability.capability_id} @ {capability.version}",
        f"样本：{len(samples)} 个（{'，'.join(s['sample_id'] for s in samples) or '无'}）",
        f"流程步骤：{' -> '.join(enabled_steps) or '无'}",
        f"调度器：{config.get('server', {}).get('scheduler', 'unknown')}，"
        f"线程 {config.get('server', {}).get('threads', 'unknown')}，"
        f"内存 {config.get('server', {}).get('memory_gb', 'unknown')}G",
    ]
    summary = (
        f"将对 {len(samples)} 个样本执行 {capability.capability_id} "
        f"（{' -> '.join(enabled_steps) or '无步骤'}）。"
    )
    return ExecutionPlan(
        capability_id=capability.capability_id,
        config=deepcopy(config),
        steps=steps,
        summary=summary,
    )
