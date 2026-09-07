"""CMScaller CMS molecular-subtype classification stage (conditional open).

Framework section 15.3 gold route A: after counts/TPM and fusions, a
*registered classification model* — the Consensus Molecular Subtypes (CMS)
of colorectal cancer — can run as a conditional open stage.

Frozen contract (confirmed 2026-09-07 with the project owner):

- Input: ``featurecounts/gene_counts.txt`` (featureCounts raw counts, first
  column ``Geneid`` = Ensembl gene id with version suffix).
- Frozen call::

      CMScaller(emat, rowNames="ensg", RNAseq=TRUE, nPerm=1000,
                seed=<fixed>, FDR=0.05, doPlot=FALSE)

  ``RNAseq=TRUE`` makes CMScaller log2(x + .25)-transform and quantile
  normalize internally; ``rowNames="ensg"`` routes rownames through
  ``replaceGeneId`` to Entrez before prediction.
- Applicability gate: cancer type must be colorectal (CRC), at least 30
  samples (CMScaller warns below 30), and featureCounts must be enabled.

Hard boundaries, mirroring :mod:`rnaseq_agent.differential`:

- CMS is colorectal-cancer specific — any non-CRC declared cancer type is
  ``NOT_EVALUABLE`` (a pan-cancer cohort can never be CMS-classified).
- Below ``CMS_MIN_SAMPLES`` the prediction variance is high, so the gate
  refuses rather than returning unreliable labels.

This module implements the gate, the frozen descriptor, and the deterministic
R-script renderer. It never executes anything itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .capability import GateResult, NOT_EVALUABLE, PASS

# CMScaller warns "few samples - high prediction variance" below 30 samples;
# freeze that as the applicability threshold.
CMS_MIN_SAMPLES = 30
# CMScaller frozen call contract (NTP, CMS templates, raw counts).
CMS_DEFAULT_N_PERM = 1000
CMS_DEFAULT_FDR = 0.05

# Colorectal cancer aliases accepted by the gate. Any other declared cancer
# type (including "pan_cancer") is refused because CMS is CRC-specific.
CRC_CANCER_TYPES = {
    "crc",
    "coad",
    "read",
    "coadread",
    "colon",
    "rectal",
    "colorectal",
    "colorectal_adenocarcinoma",
    "colon_adenocarcinoma",
    "rectal_adenocarcinoma",
    "crc_coad_read",
}

CMS_DIR = "cms"
CMS_RESULT_FILES = (
    "cms/cms_result.csv",       # prediction + template distances + p.value + FDR
    "cms/cms_summary.json",     # CMS1-4 frequency + frozen parameters
)

# cms 的两种输入来源（UI 2026-09-08）：
#   PIPELINE_ENTRY  —— 随 FASTQ 主流程跑完后接 featurecounts/gene_counts.txt；
#   COUNTS_ENTRY    —— 直接上传 counts 矩阵，仅跑 CMScaller（下游一级入口）。
PIPELINE_ENTRY = "pipeline"
COUNTS_ENTRY = "counts"
COUNTS_MATRIX_NAME = "counts_matrix.tsv"


def cms_run_mode(config: dict[str, Any]) -> str:
    """Resolve the CMS input mode: ``pipeline`` (featureCounts) or ``counts``.

    ``config.cms.run_mode`` may be set by the web UI's counts entry; anything
    that is not the literal ``"counts"`` keeps the frozen pipeline contract
    (featurecounts/gene_counts.txt), so existing configs are unchanged.
    """
    mode = str(config.get("cms", {}).get("run_mode", "")).strip().lower()
    return COUNTS_ENTRY if mode == COUNTS_ENTRY else PIPELINE_ENTRY


def cms_is_counts_entry(config: dict[str, Any]) -> bool:
    """Whether CMS should run from an uploaded count matrix, not FASTQ results."""
    return bool(config.get("pipeline", {}).get("cms", {}).get("enabled")) and cms_run_mode(
        config
    ) == COUNTS_ENTRY


def cms_is_requested(config: dict[str, Any]) -> bool:
    """Whether the user asked for the conditional CMS classification stage."""
    return bool(config.get("pipeline", {}).get("cms", {}).get("enabled"))


def cms_design_checks(config: dict[str, Any]) -> list[str]:
    """Return ``NOT_EVALUABLE``-style human reasons for the CMS gate.

    Empty list means the request satisfies the frozen template. Separated
    from the boolean gate so the session layer can attach the same reasons
    to a refusal or a plan note.
    """
    reasons: list[str] = []

    cancer = str(config.get("study", {}).get("cancer_type", "")).strip().lower()
    if not cancer:
        reasons.append(
            "CMS 分子分型仅适用于结直肠癌（CRC），当前未声明 study.cancer_type。"
        )
    elif cancer not in CRC_CANCER_TYPES:
        reasons.append(
            f"CMS 分子分型仅适用于结直肠癌（CRC），当前癌种为 {cancer!r}。"
        )

    samples = config.get("samples", {}).get("items", [])
    if len(samples) < CMS_MIN_SAMPLES:
        reasons.append(
            f"CMS 分型需要至少 {CMS_MIN_SAMPLES} 个样本以保证预测稳定性，"
            f"当前为 {len(samples)} 个。"
        )

    # 依赖说明：pipeline 模式读 featureCounts 产物；counts 直入读上传矩阵，
    # 不再要求 featurecounts/STAR 作为前置。
    if not cms_is_counts_entry(config) and not config.get("pipeline", {}).get(
        "featurecounts", {}
    ).get("enabled"):
        reasons.append(
            "CMS 分型依赖 featureCounts 原始 counts 输入，"
            "当前 pipeline.featurecounts.enabled 为 False。"
        )

    return reasons


def cms_gate(config: dict[str, Any]) -> GateResult:
    """CMS applicability gate: returns PASS or NOT_EVALUABLE with reasons."""
    reasons = cms_design_checks(config)
    if reasons:
        return GateResult(verdict=NOT_EVALUABLE, reasons=reasons)
    return GateResult(verdict=PASS)


def cms_design_of(config: dict[str, Any]) -> dict[str, Any]:
    """Frozen CMS descriptor (framework: registered classification model)."""
    cms = config.get("cms", {})
    return {
        "template": "cmscaller_ntp_crc",
        "tool": "CMScaller",
        "input": "featurecounts/gene_counts.txt (raw counts)",
        "rna_seq": True,
        "row_names": "ensg",
        "n_perm": int(cms.get("n_perm", CMS_DEFAULT_N_PERM)),
        "fdr": float(cms.get("fdr", CMS_DEFAULT_FDR)),
        "seed": int(cms.get("seed", 20260907)),
        "do_plot": bool(cms.get("do_plot", False)),
        "min_samples": CMS_MIN_SAMPLES,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def render_cms_script(config: dict[str, Any]) -> str:
    """Render the single frozen CMScaller R script.

    Reads ``featurecounts/gene_counts.txt``, strips the Ensembl version
    suffix from the ``Geneid`` column, keeps only the count columns (dropping
    the featureCounts ``Chr/Start/End/Strand/Length`` annotation columns and
    the ``Geneid`` column itself), renames samples in registration order, and
    runs the frozen ``CMScaller`` call. Outputs land in ``<workdir>/cms/``.
    """
    counts_source = "featurecounts/gene_counts.txt"
    is_counts_entry = cms_is_counts_entry(config)
    if is_counts_entry:
        # counts 直入：config.samples 携带样本列，counts 由执行层上传到工作区根目录。
        counts_source = "counts_matrix.tsv"
    return _render_cms_r_script(
        config,
        counts_source=counts_source,
        input_desc=counts_source,
        read_comment=(
            "# counts 直入模式：读取上传的 counts_matrix.tsv（首列 Geneid/rowname，"
            "其后每列为一个样本的原始 counts）"
            if is_counts_entry
            else "# 读取 featureCounts gene_counts.txt（首列 Geneid = Ensembl 带版本号）"
        ),
        drop_annotation_cols=not is_counts_entry,
        strip_version=True,
    )


def render_cms_counts_script(config: dict[str, Any]) -> str:
    """Render the CMScaller script for the *counts 直入* entry.

    The uploaded count matrix is placed at ``<workdir>/counts_matrix.tsv``;
    ``config.samples.items`` declare per-column sample metadata. The gene id
    column may be the literal ``Geneid`` (featureCounts export) or the first
    column with no header, matching the CMScaller row-name contract
    (Ensembl ids with a version suffix are stripped).
    """
    return _render_cms_r_script(
        config,
        counts_source="counts_matrix.tsv",
        input_desc="counts_matrix.tsv (uploaded)",
        read_comment=(
            "# counts 直入：读取用户上传的 counts_matrix.tsv（首列基因 id，"
            "其后每列一个样本的原始 counts）"
        ),
        # counts 直入矩阵没有 featureCounts 注释列：首列固定为基因 id，
        # 其余列全部是样本，不做 Chr/Start/Length 之类的删除。
        drop_annotation_cols=False,
        strip_version=True,
    )


def _render_cms_r_script(
    config: dict[str, Any],
    *,
    counts_source: str,
    input_desc: str,
    read_comment: str,
    drop_annotation_cols: bool,
    strip_version: bool,
) -> str:
    design = cms_design_of(config)
    n_perm = design["n_perm"]
    fdr = design["fdr"]
    seed = design["seed"]
    do_plot = "TRUE" if design["do_plot"] else "FALSE"

    sample_ids = [
        str(sample.get("sample_id", "")).strip()
        for sample in config.get("samples", {}).get("items", [])
        if str(sample.get("sample_id", "")).strip()
    ]
    sample_ids_r = "c(" + ", ".join(_r_quote(s) for s in sample_ids) + ")"
    annot_drop = (
        """
meta_cols <- intersect(c("Chr", "Start", "End", "Strand", "Length", "Geneid"), colnames(tab))
count_cols <- setdiff(colnames(tab), meta_cols)
"""
        if drop_annotation_cols
        else """
# counts 直入矩阵无 featureCounts 注释列，直接保留所有样本列。
count_cols <- setdiff(colnames(tab), colnames(tab)[1])
"""
    )
    version_strip = (
        """
gene_ids <- sub("\\\\..*$", "", gene_ids)
"""
        if strip_version
        else ""
    )

    return f"""#!/usr/bin/env Rscript
# 冻结模板 cmscaller_ntp_crc（框架 15.3 条件开放：已注册分型模型）
# CMScaller: RNAseq=TRUE（原始 counts），rowNames=ensg，doPlot={do_plot}
suppressMessages({{ library(CMScaller) }})
suppressMessages({{ library(jsonlite) }})

args <- commandArgs(trailingOnly = TRUE)
counts_file <- if (length(args) >= 1) args[[1]] else "{counts_source}"
out_prefix  <- if (length(args) >= 2) args[[2]] else "cms/cms"

dir.create("cms", showWarnings = FALSE, recursive = TRUE)

tab <- read.delim(counts_file, check.names = FALSE, stringsAsFactors = FALSE)

{read_comment}
gene_ids <- tab[[1]]
{version_strip}
{annot_drop}
emat <- as.matrix(tab[, count_cols, drop = FALSE])
rownames(emat) <- gene_ids
mode(emat) <- "numeric"

# 样本列名按登记顺序对齐（featureCounts 按 bam 传入顺序输出 counts 列）
sample_ids <- {sample_ids_r}
colnames(emat) <- sample_ids[seq_len(ncol(emat))]

res <- CMScaller(
  emat = emat,
  rowNames = "ensg",
  RNAseq = TRUE,
  nPerm = {n_perm},
  seed = {seed},
  FDR = {fdr},
  doPlot = {do_plot},
  verbose = TRUE
)

res_df <- data.frame(sample = rownames(res), res, stringsAsFactors = FALSE)
write.csv(res_df, file = paste0(out_prefix, "_result.csv"), row.names = FALSE)

freq <- as.data.frame(table(res$prediction, useNA = "ifany"))
colnames(freq) <- c("subtype", "n")

summary_json <- list(
  tool = "CMScaller",
  template = "cmscaller_ntp_crc",
  input = "{input_desc} (raw counts)",
  rna_seq = TRUE,
  row_names = "ensg",
  n_perm = {n_perm},
  fdr = {fdr},
  seed = {seed},
  do_plot = {do_plot},
  n_samples = ncol(emat),
  n_genes = nrow(emat),
  counts = freq
)
write(toJSON(summary_json, auto_unbox = TRUE), paste0(out_prefix, "_summary.json"))
cat("completed", ncol(emat), "samples classified\\n")
"""


def _r_quote(value: str) -> str:
    """Quote a string for R source (single-quoted, escaped)."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"
