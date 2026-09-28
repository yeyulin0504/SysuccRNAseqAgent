"""Scientific Adapter: inspect/plan (read-only) and materialize.

Framework section 5.2 call chain:

    Registry -> Gate-A -> Adapter.inspect/plan -> user confirm
    -> Analysis Contract -> Adapter.materialize -> Execution Gateway

- ``adapter.inspect()``   : read-only deep pre-check of the data semantics
                            (matrix shape, file presence, layout, design,
                            reference compatibility). Never writes anything.
- ``adapter.plan()``      : build the concrete ExecutionPlan the user will
                            confirm, from the capability and the config.
- ``adapter.materialize()``: after the contract is frozen, generate the
                            actual run inputs (rendered scripts) into the
                            project directory.

The LLM never reaches this layer with shell strings: the only inputs are a
``capability_id`` and a structured config, and the only outputs are a plan
or rendered, deterministic scripts.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .capability import Capability, ExecutionPlan, GateResult, build_execution_plan
from .configuration import is_counts_entry_config
from .pipeline import (
    render_env_setup_script,
    render_remote_pipeline_script,
    render_submit_script,
)

class AdapterError(RuntimeError):
    pass


def discover_adapter(capability_id: str, version: str | None = None) -> dict[str, Any]:
    """Return registry metadata used to select an existing adapter implementation."""
    from .capability_registry import load_capability_registry
    from .capability import GateResult, NOT_EVALUABLE

    registry = load_capability_registry()
    try:
        return registry.get(capability_id, version=version)
    except KeyError:
        code = "INCOMPATIBLE_VERSION" if version is not None else "UNKNOWN_CAPABILITY"
        reason = (
            f"Adapter capability {capability_id} is not compatible with version {version}."
            if version is not None
            else f"Unknown adapter capability: {capability_id}"
        )
        return GateResult(verdict=NOT_EVALUABLE, reasons=[reason], code=code)


class InspectResult:
    """Read-only pre-check result, mirroring the framework's Adapter.inspect."""

    def __init__(
        self,
        *,
        ok: bool,
        checks: list[str],
        findings: list[str],
    ) -> None:
        self.ok = ok
        self.checks = checks  # e.g. ["layout=paired PASS", "reference compatible PASS"]
        self.findings = findings  # human-readable notes for the user

    @property
    def verdict(self) -> str:
        from .capability import NOT_EVALUABLE, PASS

        return PASS if self.ok else NOT_EVALUABLE


def adapter_inspect(
    capability: Capability,
    config: dict[str, Any],
    *,
    validation_errors: list[str] | None = None,
    missing_files: list[str] | None = None,
) -> InspectResult:
    """Read-only deep pre-check. No side effects.

    Extends Gate-A with the adapter-level semantic checks the framework puts
    in ``Adapter.inspect/plan``: sample table completeness, paired-end R2
    presence, reference key coverage, and layout consistency.
    """
    checks: list[str] = []
    findings: list[str] = []
    failures: list[str] = []

    layout = str(config.get("sequencing", {}).get("layout", "paired")).strip()
    items = config.get("samples", {}).get("items", [])
    counts_entry = is_counts_entry_config(config)

    # counts 直入没有 FASTQ：跳过 paired/R2、样本 fastq 字段与参考路径检查，
    # 只验证样本表元数据 + 上传矩阵存在。
    if counts_entry:
        for index, sample in enumerate(items, start=1):
            missing = [f for f in ("sample_id", "condition") if not sample.get(f)]
            if missing:
                failures.append(f"样本 {index} 缺少 {', '.join(missing)}")
        counts_path = str(config.get("samples", {}).get("counts_path") or "").strip()
        if counts_path:
            if not Path(counts_path).is_file():
                failures.append(f"counts 直入矩阵文件不存在：{counts_path}")
            else:
                checks.append(f"counts 矩阵存在：{Path(counts_path).name}")
        else:
            failures.append("counts 直入缺少 samples.counts_path。")
        if not failures:
            checks.append(f"{len(items)} samples, counts 直入元数据完整")
        for error in validation_errors or []:
            failures.append(error)
        for missing in missing_files or []:
            failures.append(f"缺少输入文件：{missing}")
        if failures:
            findings = failures
            return InspectResult(ok=False, checks=checks, findings=findings)
        findings.append("预检通过：counts 直入数据语义满足 Adapter 输入契约。")
        return InspectResult(ok=True, checks=checks, findings=findings)

    # Layout consistency: single layout does not need R2.
    if layout == "paired":
        missing_r2 = [
            str(sample.get("sample_id", "?"))
            for sample in items
            if not sample.get("fastq_2")
        ]
        if missing_r2:
            failures.append(f"paired 布局下缺少 R2：{', '.join(missing_r2)}")
        else:
            checks.append("layout=paired, all samples have R2")
    else:
        checks.append(f"layout={layout}")

    # Sample table completeness.
    required_fields = capability.input_contract.get("required_sample_fields", [])
    for index, sample in enumerate(items, start=1):
        missing = [f for f in required_fields if not sample.get(f)]
        if missing:
            failures.append(f"样本 {index}（{sample.get('sample_id') or 'unnamed'}）缺少 {', '.join(missing)}")
    if not failures:
        checks.append(f"{len(items)} samples, required fields present")

    # Reference key coverage.
    reference = config.get("reference", {})
    missing_ref = [k for k in capability.requires_reference_keys if not reference.get(k)]
    if missing_ref:
        failures.append(f"缺少参考设置：{', '.join(missing_ref)}")
    else:
        checks.append("reference keys present")

    # External validation errors (e.g. validate_project).
    for error in validation_errors or []:
        failures.append(error)
    for missing in missing_files or []:
        failures.append(f"缺少输入文件：{missing}")

    if failures:
        findings = failures
        return InspectResult(ok=False, checks=checks, findings=findings)
    findings.append("预检通过：数据语义与参考设置满足 Adapter 输入契约。")
    return InspectResult(ok=True, checks=checks, findings=findings)


def adapter_plan(
    capability: Capability,
    config: dict[str, Any],
    *,
    inspection: InspectResult | None = None,
) -> ExecutionPlan:
    """Materialize the ExecutionPlan the user confirms.

    If an ``inspection`` is provided, its findings are embedded so the plan
    is traceable back to the pre-check (framework: Adapter.plan is read-only
    and produces the plan that gets frozen).

    A failed inspection does NOT raise: it returns a plan whose steps carry
    the NOT_EVALUABLE reasons, so the caller can show the user exactly why
    the plan cannot proceed (framework 5.2 refusal semantics). The plan can
    never be confirmed until the inspection passes.
    """
    plan = build_execution_plan(capability, config)
    if inspection is not None:
        notes = list(plan.steps)
        if inspection.ok:
            notes.append(f"预检：{len(inspection.checks)} 项检查，全部通过")
            summary = plan.summary
        else:
            notes.append("预检未通过（NOT_EVALUABLE）：")
            notes.extend(f"  - {finding}" for finding in inspection.findings)
            summary = "Adapter 预检未通过，计划不可确认：" + "; ".join(inspection.findings)
        plan = ExecutionPlan(
            capability_id=plan.capability_id,
            config=plan.config,
            steps=notes,
            summary=summary,
        )
    return plan


def adapter_materialize(
    config_path: Path,
    config: dict[str, Any],
) -> dict[str, Path]:
    """Generate the actual run inputs (rendered scripts) into the project.

    Called only after the Analysis Contract is frozen. Writes the three
    deterministic scripts into ``<project_dir>/generated_scripts/`` and
    returns their paths. The Execution Gateway then uploads and runs them.

    When the frozen config enables the conditional diffexp stage, the
    DESeq2 R script and its colData table are materialized too (framework
    15.3: 条件开放 DE 阶段；单样本/混杂等设计由 DEG 门禁先行拒绝)。
    """
    project_dir = config_path.parent
    scripts_dir = project_dir / "generated_scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # counts 直入：没有 FASTQ 主流程，materialize 只生成环境准备脚本；
    # DESeq2/CMScaller 的 R 脚本由执行层按 counts 阶段渲染并上传。
    if is_counts_entry_config(config):
        rendered: dict[str, str] = {
            "env_setup.sh": render_env_setup_script(config),
        }
        paths: dict[str, Path] = {}
        for name, content in rendered.items():
            path = scripts_dir / name
            path.write_text(content, encoding="utf-8")
            paths[name] = path
        return paths

    rendered = {
        "env_setup.sh": render_env_setup_script(config),
        "run_pipeline.sh": render_remote_pipeline_script(config),
        "submit.sh": render_submit_script(config),
    }
    if config.get("pipeline", {}).get("diffexp", {}).get("enabled"):
        from .differential import render_colData, render_diffexp_script

        rendered["diffexp_deseq2.R"] = render_diffexp_script(config)
        rendered["colData.tsv"] = render_colData(config)

    paths = {}
    for name, content in rendered.items():
        path = scripts_dir / name
        path.write_text(content, encoding="utf-8")
        paths[name] = path
    return paths
