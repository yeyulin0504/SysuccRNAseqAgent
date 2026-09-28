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

from .configuration import is_counts_entry_config
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

# 框架 15.3 条件开放阶段的输出产物（仅在 diffexp 启用后要求）。
DIFFEXP_OUTPUT_ARTIFACTS: dict[str, str] = {
    "diffexp/deseq2_results.tsv": "DESeq2 all-gene results (baseMean/lfc/padj)",
    "diffexp/deseq2_summary.json": "DESeq2 design/contrast summary",
}

# 框架 15.3 条件开放阶段：CMScaller CMS 分型（仅在 cms 启用后要求）。
CMS_OUTPUT_ARTIFACTS: dict[str, str] = {
    "cms/cms_result.csv": "CMScaller CMS prediction + distances + p.value/FDR",
    "cms/cms_summary.json": "CMScaller CMS subtype frequencies + frozen parameters",
}


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
    code: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.verdict == PASS

    def formatted(self) -> list[str]:
        if self.ok:
            return ["通过：满足 Gate-A 元数据级适用性检查。"]
        prefix = "不适用" if self.verdict == NOT_EVALUABLE else "警告"
        return [f"{prefix}：{reason}" for reason in self.reasons]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.verdict,
            "verdict": self.verdict,
            "code": self.code,
            "reasons": list(self.reasons),
            "details": deepcopy(self.details),
        }


@dataclass(frozen=True)
class ExecutionPlan:
    capability_id: str
    config: dict[str, Any]
    steps: list[str]
    summary: str


def register_legacy_capabilities() -> dict[str, dict[str, Any]]:
    """Return the versioned set of workflow capabilities available in this MVP.

    Capability IDs follow the frozen slots in framework section 15.2.
    """

    bulk_profile = WORKFLOW_PROFILES[DEFAULT_WORKFLOW_PROFILE]
    pipeline = bulk_profile["pipeline"]
    output_artifacts: dict[str, str] = {
        "featurecounts/gene_counts.txt": "gene-level count matrix",
        "rsem/*.genes.results": "per-sample RSEM gene-level quantification",
        "arriba/*.fusions.tsv": "fusion candidate calls (manual review required)",
        "fastp/*.json": "per-sample fastp QC JSON",
        "star/*.Log.final.out": "per-sample STAR alignment summary",
    }
    # 框架 15.3 条件开放：启用 diffexp 时才把 DE 产物列为必需输出。
    if pipeline.get("diffexp", {}).get("enabled"):
        output_artifacts.update(DIFFEXP_OUTPUT_ARTIFACTS)
    # 框架 15.3 条件开放：启用 cms 时才把 CMS 产物列为必需输出。
    if pipeline.get("cms", {}).get("enabled"):
        output_artifacts.update(CMS_OUTPUT_ARTIFACTS)

    return {
        # 框架 15.2：workflow.bulk_rna.grch38_pe_expression_fusion 1.0.0
        DEFAULT_WORKFLOW_PROFILE: {
            "id": DEFAULT_WORKFLOW_PROFILE,
            "version": "1.0.0",
            "title": bulk_profile["title"],
            "description": bulk_profile["description"],
            "entry_stage": "qc",
            "required_inputs": ["samples", "reference"],
            "required_tools": ["fastp", "STAR", "featureCounts", "RSEM", "Arriba"],
            "required_artifacts": list(output_artifacts),
            "supports": {"data_type": ["bulk_rna_seq_fastq"], "layout": ["paired"]},
            "gates": {},
            "input_contract": {
                "data_type": "bulk_rna_seq_fastq",
                "layout": {"paired"},  # 框架 15.3：Illumina paired-end
                "min_samples": 1,
                "designs": {"independent_two_group", "paired_two_group", "single_group"},
                "required_sample_fields": ["sample_id", "condition", "fastq_1"],
                "cancer_types": bulk_profile.get("cancer_types", ["pan_cancer"]),
                "sample_mode": bulk_profile.get("sample_mode", "cohort_or_single"),
            },
            "output_artifacts": output_artifacts,
            "requires_reference_keys": [
                "star_index_dir",
                "remote_gtf_path",
                "remote_genome_fasta_path",
                "rsem_index_prefix",
            ],
            "pipeline": pipeline,
        },
    }


def _capability_from_record(record: dict[str, Any]) -> Capability:
    input_contract = deepcopy(record.get("input_contract") or {})
    supports = record.get("supports") or {}
    gates = record.get("gates") or {}
    input_contract.setdefault("data_type", (supports.get("data_type") or ["bulk_rna_seq_fastq"])[0])
    input_contract.setdefault("layout", set(supports.get("layout") or ["paired"]))
    if isinstance(input_contract["layout"], list):
        input_contract["layout"] = set(input_contract["layout"])
    input_contract.setdefault("min_samples", gates.get("min_samples", 1))
    input_contract.setdefault("designs", set(supports.get("design") or ["independent_two_group", "paired_two_group", "single_group"]))
    if isinstance(input_contract["designs"], list):
        input_contract["designs"] = set(input_contract["designs"])
    input_contract.setdefault("required_sample_fields", gates.get("required_sample_fields", ["sample_id", "condition"]))
    input_contract.setdefault("cancer_types", supports.get("cancer_type") or ["pan_cancer"])
    input_contract.setdefault("sample_mode", "cohort_or_single")
    return Capability(
        capability_id=record["id"],
        title=record.get("title", record["id"]),
        description=record.get("description", ""),
        version=record["version"],
        input_contract=input_contract,
        output_artifacts=deepcopy(record.get("output_artifacts") or {item: item for item in record["required_artifacts"]}),
        requires_reference_keys=list(record.get("requires_reference_keys") or gates.get("required_reference_keys", [])),
        pipeline=deepcopy(record.get("pipeline") or DEFAULT_PIPELINE),
        source="json" if record.get("id") else "workflow_profile",
    )


def register_builtin_capabilities() -> dict[str, Capability]:
    """Return declarative capabilities, with the old Python registry as fallback."""
    from .capability_registry import load_capability_registry

    registry = load_capability_registry()
    return {record["id"]: _capability_from_record(record) for record in registry.list_available()}


def resolve_capability(capability_id: str) -> Capability:
    """Resolve a capability by id, raising ValueError when unknown."""
    capabilities = register_builtin_capabilities()
    if capability_id not in capabilities:
        available = ", ".join(sorted(capabilities))
        raise ValueError(f"Unknown capability: {capability_id}. Available: {available}")
    return capabilities[capability_id]


def discover_capability(capability_id: str, version: str | None = None) -> "CapabilityDiscoveryResult":
    """Discover a declarative capability record without selecting an adapter."""
    from .capability_registry import CapabilityDiscoveryResult, load_capability_registry

    return load_capability_registry().discover(capability_id, version=version)


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

    required_fields = list(capability.input_contract["required_sample_fields"])
    if design == "paired_two_group" and "pair_id" not in required_fields:
        required_fields.append("pair_id")
    if is_counts_entry_config(config):
        # counts 直入没有 FASTQ：样本表只需 sample_id + condition，参考路径
        # 与 STAR/featureCounts 前置检查交给下游 counts 阶段，不在此要求。
        required_fields = [
            field for field in required_fields if field not in {"fastq_1", "fastq_2"}
        ]
    for index, sample in enumerate(items, start=1):
        missing_fields = [field for field in required_fields if not sample.get(field)]
        if missing_fields:
            reasons.append(
                f"样本 {index}（{sample.get('sample_id') or 'unnamed'}）缺少字段："
                + ", ".join(missing_fields)
            )

    reference = config.get("reference", {})
    if is_counts_entry_config(config):
        requires_reference_keys: list[str] = []
    else:
        requires_reference_keys = capability.requires_reference_keys
    for key in requires_reference_keys:
        if not reference.get(key):
            reasons.append(f"能力 {capability.capability_id} 需要参考设置 {key}，当前未配置。")

    for error in validation_errors or []:
        reasons.append(error)
    for missing in missing_files or []:
        reasons.append(f"缺少输入文件：{missing}")

    # 框架 15.3：diffexp 是条件开放阶段 —— 请求启用时必须满足设计门禁，
    # 否则把整个计划置为 NOT_EVALUABLE（单样本/不足重复/混杂均在此拒绝）。
    if config.get("pipeline", {}).get("diffexp", {}).get("enabled"):
        from .differential import diffexp_design_checks

        reasons.extend(diffexp_design_checks(config))

    # 框架 15.3：cms 同为条件开放阶段 —— 请求启用时必须满足 CMS 门禁
    # （癌种=CRC / 样本≥30 / featureCounts 前置），否则 NOT_EVALUABLE。
    # counts 直入时 featureCounts 前置由 cms_design_checks 内部豁免。
    if config.get("pipeline", {}).get("cms", {}).get("enabled"):
        from .cms import cms_design_checks

        reasons.extend(cms_design_checks(config))

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

    # 框架 15.3 条件开放：启用 diffexp 时把冻结设计/对比写入计划。
    diffexp = config.get("pipeline", {}).get("diffexp", {}).get("enabled")
    if diffexp:
        from .differential import diffexp_design_of

        design = diffexp_design_of(config)
        steps.append(
            f"差异表达：DESeq2 两组对比 {design['contrast']}"
            f"（{design['formula']}，reference={design['reference_condition']}）"
        )
        summary += f" 条件开放：DESeq2 差异表达（{design['contrast']}）。"

    # 框架 15.3 条件开放：启用 cms 时把冻结分型模型写入计划。
    if config.get("pipeline", {}).get("cms", {}).get("enabled"):
        from .cms import cms_design_of

        design = cms_design_of(config)
        steps.append(
            f"CMS 分型：CMScaller（RNAseq=TRUE, rowNames=ensg, "
            f"nPerm={design['n_perm']}, FDR={design['fdr']}）"
        )
        summary += " 条件开放：CMScaller CMS 结直肠癌分子分型。"

    return ExecutionPlan(
        capability_id=capability.capability_id,
        config=deepcopy(config),
        steps=steps,
        summary=summary,
    )
