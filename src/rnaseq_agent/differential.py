"""Conditional DESeq2 differential expression stage for the bulk gold route.

Framework section 15.3 (gold route A) freezes DESeq2 two-group DE as a
*conditional open* stage after counts/TPM and fusions:

    counts/TPM与融合表
    -> 条件开放：DESeq2两组差异表达与fgsea（登记的MSigDB快照）
    -> 适用性门禁
    -> 已注册分型或疗效模型
    -> 版本化报告

Hard boundaries from the design document (sections 8.2 / 15.3):

- 单样本不能做差异表达 (a single sample can never run DE);
- 首期只接受独立非配对两组设计（冻结公式 ``~ condition``），
  不接受配对、时间序列、多因素或交互设计；
- 完全混杂时返回 ``NOT_EVALUABLE``；仅当 ``batch`` 未声明或每个 condition
  内 batch 均有分布且无一一映射时才放行；
- 每组的生物学重复数必须达到冻结阈值（默认每组至少 3），否则返回
  ``NOT_EVALUABLE``（框架要求的样本级重复门槛）。

This module implements the gate, the frozen design/contrast descriptor, and
the deterministic R-script renderer for that one template. It never executes
anything itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .capability import GateResult, NOT_EVALUABLE, PASS

# Frozen first-release default: 生物学重复阈值。
DEG_MIN_REPLICATES_PER_GROUP = 3
# 框架 8.2 / 15.3：冻结公式。
DEG_DESIGN_FORMULA = "~ condition"


def _r_quote(value: str) -> str:
    """Quote a string for R source (single-quoted, escaped)."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
# 首期拒绝任意公式、配对、多因素与交互。
UNSUPPORTED_FORMULA_HINTS = ("+ batch", "* ", ":", "subject", "paired")

DIFFEXP_DIR = "diffexp"
DIFFEXP_RESULT_FILES = (
    "diffexp/deseq2_results.tsv",          # 全部基因：baseMean/log2FoldChange/lfcSE/stat/pvalue/padj
    "diffexp/deseq2_summary.json",         # 设计、对比、过滤门槛与显著基因计数
)


def diffexp_design_checks(config: dict[str, Any]) -> list[str]:
    """Return ``NOT_EVALUABLE``-style human reasons for the DEG gate.

    Empty list means the design satisfies the frozen template. The checks are
    intentionally separated from the boolean gate so the session layer can
    attach the same reasons to a refusal or to a plan note.
    """
    reasons: list[str] = []
    samples = config.get("samples", {}).get("items", [])
    design = str(config.get("study", {}).get("design", "")).strip()
    contrast = _contrast_of(config)

    if design and design not in {"independent_two_group"}:
        reasons.append(
            f"首期只支持独立非配对两组设计（~ condition），当前 study.design={design!r}。"
        )

    # 框架 15.3：单样本不能做差异表达。
    if len(samples) < 2:
        reasons.append("单样本不能做差异表达：至少需要 2 个样本（且每组达到重复阈值）。")
        return reasons

    conditions = sorted(
        {str(sample.get("condition", "")).strip() for sample in samples}
        - {""}
    )
    if len(conditions) != 2:
        reasons.append(
            f"DESeq2 两组对比需要恰好两个 condition，当前检测到 {len(conditions)} 个："
            f"{conditions or '无'}。"
        )
        return reasons

    # 冻结公式：拒绝以任意方式引入第二项（配对/协变量/交互）。
    formula = str(config.get("diffexp", {}).get("formula") or DEG_DESIGN_FORMULA).strip()
    lowered_formula = formula.lower().replace(" ", "")
    if lowered_formula != "~condition":
        reasons.append(f"首期仅冻结公式 {DEG_DESIGN_FORMULA!r}，当前 formula={formula!r}。")
        return reasons

    # 对比方向：reference/对照组在 design 中以 reference_condition 指定；
    # 未指定时取字典序较小者，并记录在对比描述中。
    reference = _reference_condition(config)
    if reference not in conditions:
        reasons.append(f"reference_condition={reference!r} 不在样本 condition 中：{conditions}。")
        return reasons

    # 每组生物学重复必须达到阈值（框架：样本级重复门槛）。
    counts: dict[str, int] = {}
    for sample in samples:
        condition = str(sample.get("condition", "")).strip()
        counts[condition] = counts.get(condition, 0) + 1
    for condition, count in sorted(counts.items()):
        if count < DEG_MIN_REPLICATES_PER_GROUP:
            reasons.append(
                f"condition {condition!r} 只有 {count} 个生物学重复，"
                f"低于冻结阈值 {DEG_MIN_REPLICATES_PER_GROUP}。"
            )

    # 批次与 condition 完全混杂 -> 拒绝（框架 8.2/15.3 / 6.3 混杂拒绝）。
    # 仅当样本表声明了 batch 列时才做混杂检查；逐个 condition 检查其 batch 是否
    # 单一，单一即证明该 condition 与 batch 一一映射，无法区分组间效应与批次效应。
    declared_batches = {str(sample.get("batch", "")).strip() for sample in samples if sample.get("batch")}
    if declared_batches:
        for condition, batches in sorted(_batch_by_condition(config, samples).items()):
            if len(batches) < 2:
                reasons.append(
                    f"condition {condition!r} 的样本批次均相同（{sorted(batches)}），"
                    f"batch 与 condition 完全混杂，不能做组间比较。"
                )

    # contrast 记录会由调用方统一补全，这里只校验显式声明的基本合法性。
    return reasons


def deg_gate(config: dict[str, Any]) -> GateResult:
    """DEG applicability gate: returns PASS or NOT_EVALUABLE with reasons."""
    reasons = diffexp_design_checks(config)
    if reasons:
        return GateResult(verdict=NOT_EVALUABLE, reasons=reasons)
    return GateResult(verdict=PASS)


def diffexp_is_requested(config: dict[str, Any]) -> bool:
    """Whether the user asked for the conditional DE stage."""
    return bool(config.get("pipeline", {}).get("diffexp", {}).get("enabled"))


def diffexp_design_of(config: dict[str, Any]) -> dict[str, Any]:
    """Frozen design/contrast descriptor (framework: condition队列另含 design/contrast)."""
    samples = config.get("samples", {}).get("items", [])
    conditions = sorted(
        {str(sample.get("condition", "")).strip() for sample in samples} - {""}
    )
    reference = _reference_condition(config)
    treatment = next((c for c in conditions if c != reference), reference or "")
    contrast = f"{treatment}_vs_{reference}"
    return {
        "template": "deseq2_independent_two_group",
        "formula": DEG_DESIGN_FORMULA,
        "reference_condition": reference,
        "treatment_condition": treatment,
        "contrast": contrast,
        "min_replicates_per_group": DEG_MIN_REPLICATES_PER_GROUP,
        "batches_declared": bool(
            any(sample.get("batch") for sample in samples)
        ),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def render_diffexp_script(config: dict[str, Any]) -> str:
    """Render the single frozen DESeq2 R script.

    The design matrix is exactly ``~ condition``; the contrast is
    ``treatment vs reference`` as resolved by ``diffexp_design_of``. Outputs
    land in ``<workdir>/diffexp/``. No arbitrary formulas are reachable.
    """
    design = diffexp_design_of(config)
    contrast = design["contrast"]
    reference = design["reference_condition"]
    treatment = design["treatment_condition"]
    ref_r = _r_quote(reference)
    trt_r = _r_quote(treatment)
    contrast_r = _r_quote(contrast)

    return f"""#!/usr/bin/env Rscript
# 冻结模板 deseq2_independent_two_group（框架 15.3 条件开放）
# design = {DEG_DESIGN_FORMULA}，contrast = {contrast}
suppressMessages({{ library(DESeq2) }})
suppressMessages({{ library(jsonlite) }})

args <- commandArgs(trailingOnly = TRUE)
counts_file <- if (length(args) >= 1) args[[1]] else "featurecounts/gene_counts.txt"
coldata_file <- if (length(args) >= 2) args[[2]] else "diffexp/colData.tsv"
out_prefix <- if (length(args) >= 3) args[[3]] else "diffexp/deseq2"

dir.create("diffexp", showWarnings = FALSE, recursive = TRUE)
counts <- as.matrix(read.delim(counts_file, row.names = 1, check.names = FALSE))
coldata <- read.delim(coldata_file, row.names = 1, check.names = FALSE)

coldata$condition <- factor(coldata$condition, levels = c({ref_r}, {trt_r}))
stopifnot(all(rownames(coldata) %in% colnames(counts)))
counts <- counts[, rownames(coldata), drop = FALSE]

dds <- DESeqDataSetFromMatrix(
  countData = counts,
  colData = coldata,
  design = ~ condition
)
dds <- DESeq(dds, quiet = TRUE)
res <- results(dds, contrast = c("condition", {trt_r}, {ref_r}))
res <- res[order(res$padj), ]
res_df <- as.data.frame(res)
res_df$gene <- rownames(res_df)
res_df <- res_df[, c("gene", "baseMean", "log2FoldChange", "lfcSE", "stat", "pvalue", "padj")]
write.table(res_df, file = paste0(out_prefix, "_results.tsv"),
            sep = "\\t", row.names = FALSE, quote = FALSE)

n_sig <- sum(res_df$padj < 0.05 & abs(res_df$log2FoldChange) >= 1, na.rm = TRUE)
summary_json <- list(
  contrast = {contrast_r},
  formula = {_r_quote(DEG_DESIGN_FORMULA)},
  reference_condition = {ref_r},
  treatment_condition = {trt_r},
  tested_genes = nrow(res_df),
  significant_padj_0.05_lfc1 = n_sig
)
write(toJSON(summary_json, auto_unbox = TRUE), paste0(out_prefix, "_summary.json"))
cat("completed", n_sig, "significant genes\\n")
"""


def render_colData(config: dict[str, Any]) -> str:
    """Deterministic colData.tsv from the sample table (row = sample_id).

    Values are emitted raw; sample_id / condition / batch have already been
    validated as safe identifiers or short tags upstream (contract gates),
    so no shell quoting is applied here — this is a TSV, not a shell string.
    """
    samples = config.get("samples", {}).get("items", [])
    lines = ["sample_id\tcondition\tbatch"]
    for sample in samples:
        batch = str(sample.get("batch") or "").strip()
        lines.append(
            f"{sample['sample_id']}\t"
            f"{str(sample.get('condition', '')).strip()}\t"
            f"{batch if batch else 'NA'}"
        )
    return "\n".join(lines) + "\n"


# -- internal helpers -------------------------------------------------------


def _reference_condition(config: dict[str, Any]) -> str:
    diffexp = config.get("diffexp", {})
    explicit = str(diffexp.get("reference_condition", "")).strip()
    if explicit:
        return explicit
    samples = config.get("samples", {}).get("items", [])
    conditions = sorted(
        {str(sample.get("condition", "")).strip() for sample in samples} - {""}
    )
    return conditions[0] if conditions else ""


def _contrast_of(config: dict[str, Any]) -> str:
    design = diffexp_design_of(config)
    return str(design.get("contrast", ""))


def _batch_by_condition(
    config: dict[str, Any],
    samples: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """Map condition -> declared batch values (only samples with a batch)."""
    mapping: dict[str, set[str]] = {}
    for sample in samples:
        batch = str(sample.get("batch", "")).strip()
        condition = str(sample.get("condition", "")).strip()
        if batch and condition:
            mapping.setdefault(condition, set()).add(batch)
    return mapping
