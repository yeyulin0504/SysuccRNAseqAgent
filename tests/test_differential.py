"""Tests for the conditional DESeq2 differential expression stage.

Framework section 15.3 gold route A: DE is a *conditional open* stage after
counts/TPM and fusions, frozen to an independent two-group template
``~ condition`` with sample-level replicate thresholds and a confounding
refusal (single sample / insufficient replicates / confounding -> NOT_EVALUABLE).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rnaseq_agent.differential import (
    DEG_DESIGN_FORMULA,
    DEG_MIN_REPLICATES_PER_GROUP,
    deg_gate,
    diffexp_design_of,
    diffexp_is_requested,
    render_colData,
    render_diffexp_script,
)
from rnaseq_agent.capability import NOT_EVALUABLE, PASS


def _config(
    *,
    groups: tuple[str, int, str, int] | None = None,
    conditions: list[str] | None = None,
    batch: dict[str, list[str]] | None = None,
    design: str = "independent_two_group",
    samples_per_group: int = 3,
) -> dict:
    """Build a sample table. ``groups`` overrides: (condA,nA,condB,nB)."""
    items: list[dict] = []
    if conditions is not None:
        for index, condition in enumerate(conditions):
            items.append(
                {
                    "sample_id": f"{condition}_{index}",
                    "condition": condition,
                    "fastq_1": f"{condition}_{index}_R1.fastq.gz",
                    "fastq_2": f"{condition}_{index}_R2.fastq.gz",
                }
            )
    elif groups is not None:
        cond_a, n_a, cond_b, n_b = groups
        for condition, count in ((cond_a, n_a), (cond_b, n_b)):
            for index in range(count):
                sample = {
                    "sample_id": f"{condition}_{index}",
                    "condition": condition,
                    "fastq_1": f"{condition}_{index}_R1.fastq.gz",
                    "fastq_2": f"{condition}_{index}_R2.fastq.gz",
                }
                items.append(sample)
    else:
        for condition, count in (("control", samples_per_group), ("treatment", samples_per_group)):
            for index in range(count):
                sample = {
                    "sample_id": f"{condition}_{index}",
                    "condition": condition,
                    "fastq_1": f"{condition}_{index}_R1.fastq.gz",
                    "fastq_2": f"{condition}_{index}_R2.fastq.gz",
                }
                items.append(sample)

    if batch is not None:
        for sample in items:
            mapping = batch.get(sample["condition"])
            if mapping:
                # 轮流分配 batch，模拟有重复的批次设计。
                sample["batch"] = mapping[hash(sample["sample_id"]) % len(mapping)]

    return {
        "study": {"design": design},
        "samples": {"items": items},
        # M1.6：reference_condition 必须显式声明，helper 默认填 control。
        "diffexp": {"reference_condition": "control"},
    }


class TestDegGate:
    def test_two_group_valid_design_passes(self) -> None:
        gate = deg_gate(_config())
        assert gate.ok
        assert gate.verdict == PASS

    def test_single_sample_refused(self) -> None:
        gate = deg_gate(_config(conditions=["control"]))
        assert not gate.ok
        assert gate.verdict == NOT_EVALUABLE
        assert any("单样本不能做差异表达" in reason for reason in gate.reasons)

    def test_two_samples_one_per_group_refused(self) -> None:
        gate = deg_gate(_config(groups=("control", 1, "treatment", 1)))
        assert not gate.ok
        assert any("只有 1 个生物学重复" in reason for reason in gate.reasons)

    def test_insufficient_replicates_refused(self) -> None:
        gate = deg_gate(_config(groups=("control", 2, "treatment", 2)))
        assert not gate.ok
        assert any("低于冻结阈值" in reason for reason in gate.reasons)

    def test_three_conditions_refused(self) -> None:
        gate = deg_gate(_config(conditions=["a", "b", "c"]))
        assert not gate.ok
        assert any("恰好两个 condition" in reason for reason in gate.reasons)

    def test_paired_or_other_design_refused(self) -> None:
        gate = deg_gate(_config(design="paired"))
        assert not gate.ok
        assert any("独立非配对两组" in reason for reason in gate.reasons)

    def test_unsupported_formula_refused(self) -> None:
        config = _config()
        config["diffexp"] = {"formula": "~ condition + batch"}
        gate = deg_gate(config)
        assert not gate.ok
        assert any("冻结公式" in reason for reason in gate.reasons)

    def test_missing_reference_condition_refused(self) -> None:
        # M1.6：reference_condition 必须显式声明，不能留空取字典序最小。
        config = _config()
        config["diffexp"] = {"reference_condition": ""}
        gate = deg_gate(config)
        assert not gate.ok
        assert gate.verdict == NOT_EVALUABLE
        assert any("显式声明" in reason for reason in gate.reasons)

    def test_fully_confounded_batches_refused(self) -> None:
        # control 全部 batch=B1, treatment 全部 batch=B2 => 完全混杂。
        config = _config(groups=("control", 3, "treatment", 3))
        for sample in config["samples"]["items"]:
            sample["batch"] = "B1" if sample["condition"] == "control" else "B2"
        gate = deg_gate(config)
        assert not gate.ok
        assert any("完全混杂" in reason for reason in gate.reasons)

    def test_confounded_single_condition_batch_refused(self) -> None:
        # control 全 B1；treatment 有 B1/B2 => control 无法区分批次与组效应。
        config = _config(groups=("control", 3, "treatment", 3))
        for sample in config["samples"]["items"]:
            if sample["condition"] == "control":
                sample["batch"] = "B1"
            else:
                sample["batch"] = "B1" if sample["sample_id"].endswith(("0", "2")) else "B2"
        gate = deg_gate(config)
        assert not gate.ok
        assert any("condition 'control'" in reason and "完全混杂" in reason for reason in gate.reasons)


class TestDiffExpMetadata:
    def test_is_requested(self) -> None:
        assert not diffexp_is_requested({"pipeline": {"diffexp": {"enabled": False}}})
        assert diffexp_is_requested({"pipeline": {"diffexp": {"enabled": True}}})

    def test_design_of_resolves_contrast(self) -> None:
        config = _config()
        design = diffexp_design_of(config)
        assert design["template"] == "deseq2_independent_two_group"
        assert design["formula"] == DEG_DESIGN_FORMULA
        # 字典序最小 = control 为 reference。
        assert design["reference_condition"] == "control"
        assert design["contrast"] == "treatment_vs_control"
        assert design["min_replicates_per_group"] == DEG_MIN_REPLICATES_PER_GROUP

    def test_design_of_respects_explicit_reference(self) -> None:
        config = _config()
        config["diffexp"] = {"reference_condition": "treatment"}
        design = diffexp_design_of(config)
        assert design["contrast"] == "control_vs_treatment"
        assert design["reference_condition"] == "treatment"


class TestRenderers:
    def test_col_data_includes_batch(self) -> None:
        config = _config()
        for index, sample in enumerate(config["samples"]["items"]):
            sample["batch"] = f"B{index % 2}"
        text = render_colData(config)
        assert text.splitlines()[0] == "sample_id\tcondition\tbatch"
        assert text.count("B0") >= 3 or text.count("B1") >= 3
        # 每行都包含 batch 值。
        for line in text.splitlines()[1:]:
            assert "\tB0" in line or "\tB1" in line

    def test_col_data_na_for_missing_batch(self) -> None:
        text = render_colData(_config())
        for line in text.splitlines()[1:]:
            assert line.endswith("\tNA")

    def test_render_script_uses_frozen_design(self) -> None:
        script = render_diffexp_script(_config())
        assert "DESeqDataSetFromMatrix" in script
        assert "design = ~ condition" in script
        # R 字符串必须带引号（不能用 shell_quote 裸词）。
        assert 'levels = c(\'control\', \'treatment\')' in script
        assert 'contrast = c("condition", \'treatment\', \'control\')' in script
        assert "_results.tsv" in script
        assert "_summary.json" in script
        assert "significant_padj_0.05_lfc1 = n_sig_default" in script

    def test_render_script_uses_frozen_significance_cutoffs(self) -> None:
        config = _config()
        config["diffexp"].update({"padj_cutoff": 0.1, "lfc_cutoff": 0.75})

        script = render_diffexp_script(config)

        assert "padj_cutoff <- 0.1" in script
        assert "lfc_cutoff <- 0.75" in script
        assert "res_df$padj < padj_cutoff" in script
        assert "abs(res_df$log2FoldChange) >= lfc_cutoff" in script
        assert "padj_cutoff = padj_cutoff" in script
        assert "log2fc_cutoff = lfc_cutoff" in script
