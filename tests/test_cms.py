"""Tests for the conditional CMScaller CMS classification stage.

Framework section 15.3 gold route A: CMS molecular subtyping is a
*registered classification model* opened conditionally after counts/TPM and
fusions, frozen to ``CMScaller(RNAseq=TRUE, rowNames="ensg")`` with an
applicability gate (colorectal cancer / >= 30 samples / featureCounts input).
"""

from __future__ import annotations

import pytest

from rnaseq_agent.cms import (
    CMS_MIN_SAMPLES,
    cms_design_of,
    cms_gate,
    cms_is_requested,
    render_cms_counts_script,
    render_cms_script,
)
from rnaseq_agent.capability import NOT_EVALUABLE, PASS


def _config(
    *,
    cancer_type: str = "coad",
    n_samples: int = CMS_MIN_SAMPLES,
    featurecounts: bool = True,
    cms_enabled: bool = True,
    counts_entry: bool = False,
) -> dict:
    items: list[dict] = []
    for index in range(n_samples):
        item: dict = {
            "sample_id": f"sample_{index}",
            "condition": "tumor",
            "fastq_1": f"sample_{index}_R1.fastq.gz",
            "fastq_2": f"sample_{index}_R2.fastq.gz",
        }
        if counts_entry:
            # counts 直入样本表没有 FASTQ 字段。
            item = {"sample_id": f"sample_{index}", "condition": "tumor"}
        items.append(item)
    config: dict = {
        "study": {"cancer_type": cancer_type, "design": "single_group"},
        "samples": {"items": items},
        "pipeline": {
            "featurecounts": {"enabled": featurecounts},
            "cms": {"enabled": cms_enabled},
        },
        "cms": {"n_perm": 1000, "fdr": 0.05, "seed": 20260907, "do_plot": False},
    }
    if counts_entry:
        config["samples"]["source"] = "counts_upload"
        config["cms"]["run_mode"] = "counts"
    return config


class TestCmsGate:
    def test_crc_cohort_passes(self) -> None:
        gate = cms_gate(_config())
        assert gate.ok
        assert gate.verdict == PASS

    def test_pan_cancer_refused(self) -> None:
        gate = cms_gate(_config(cancer_type="pan_cancer"))
        assert not gate.ok
        assert gate.verdict == NOT_EVALUABLE
        assert any("结直肠癌" in reason for reason in gate.reasons)

    def test_missing_cancer_type_refused(self) -> None:
        gate = cms_gate(_config(cancer_type=""))
        assert not gate.ok
        assert any("未声明" in reason for reason in gate.reasons)

    def test_too_few_samples_refused(self) -> None:
        gate = cms_gate(_config(n_samples=CMS_MIN_SAMPLES - 1))
        assert not gate.ok
        assert any("至少" in reason and "样本" in reason for reason in gate.reasons)

    def test_featurecounts_disabled_refused(self) -> None:
        gate = cms_gate(_config(featurecounts=False))
        assert not gate.ok
        assert any("featureCounts" in reason for reason in gate.reasons)

    def test_not_requested_does_not_block_other_checks(self) -> None:
        # 门禁本身独立于开关（与 diffexp_design_checks 对齐，由调用方决定何时调用）。
        assert cms_gate(_config(cms_enabled=False)).ok

    # -- counts 直入入口（并列一级入口，非快速模式）------------------------

    def test_counts_entry_passes_without_featurecounts(self) -> None:
        # counts 直入读取上传矩阵：featureCounts 前置被豁免。
        gate = cms_gate(_config(featurecounts=False, counts_entry=True))
        assert gate.ok
        assert gate.verdict == PASS

    def test_counts_entry_ignores_fastq_fields(self) -> None:
        # counts 直入样本表没有 fastq_1/fastq_2，不应触发 featureCounts 依赖检查。
        gate = cms_gate(_config(featurecounts=False, counts_entry=True))
        assert not any("featureCounts" in reason for reason in gate.reasons)

    def test_counts_entry_still_enforces_cancer_and_samples(self) -> None:
        # 豁免仅针对 featureCounts 前置；癌种与样本数门禁仍然生效。
        gate = cms_gate(_config(cancer_type="pan_cancer", counts_entry=True))
        assert not gate.ok
        assert any("结直肠癌" in reason for reason in gate.reasons)

        gate = cms_gate(_config(n_samples=CMS_MIN_SAMPLES - 1, counts_entry=True))
        assert not gate.ok
        assert any("至少" in reason and "样本" in reason for reason in gate.reasons)


class TestCmsMetadata:
    def test_is_requested(self) -> None:
        assert not cms_is_requested({"pipeline": {"cms": {"enabled": False}}})
        assert cms_is_requested({"pipeline": {"cms": {"enabled": True}}})

    def test_design_of_frozen_template(self) -> None:
        design = cms_design_of(_config())
        assert design["template"] == "cmscaller_ntp_crc"
        assert design["tool"] == "CMScaller"
        assert design["rna_seq"] is True
        assert design["row_names"] == "ensg"
        assert design["n_perm"] == 1000
        assert design["fdr"] == 0.05
        assert design["do_plot"] is False
        assert design["min_samples"] == CMS_MIN_SAMPLES


class TestRenderers:
    def test_render_script_has_frozen_call(self) -> None:
        script = render_cms_script(_config())
        assert "CMScaller(" in script
        assert 'rowNames = "ensg"' in script
        assert "RNAseq = TRUE" in script
        assert "nPerm = 1000" in script
        assert "_result.csv" in script
        assert "_summary.json" in script

    def test_render_script_strips_ensembl_version(self) -> None:
        script = render_cms_script(_config())
        # 去 Ensembl 版本号的正则（ENSG00000186092.4 -> ENSG00000186092）。
        assert "gene_ids" in script
        assert "sub(" in script

    def test_render_script_embeds_sample_ids(self) -> None:
        script = render_cms_script(_config(n_samples=30))
        assert "'sample_0'" in script
        assert "'sample_29'" in script

    def test_render_script_reads_featurecounts_counts(self) -> None:
        script = render_cms_script(_config())
        assert "featurecounts/gene_counts.txt" in script
        assert "Chr" in script and "Length" in script  # 排除 featureCounts 注释列

    def test_render_cms_script_counts_entry_switches_source(self) -> None:
        # render_cms_script 在 counts 直入配置下也指向上传矩阵并跳过注释列逻辑。
        script = render_cms_script(_config(counts_entry=True))
        assert "counts_matrix.tsv" in script
        assert "Chr" not in script
        assert "Length" not in script

    def test_render_cms_counts_script_keeps_first_column_as_gene_id(self) -> None:
        # counts 直入脚本：首列固定为基因 id，其余全部为样本列（无注释列删除）。
        script = render_cms_counts_script(_config(n_samples=30, counts_entry=True))
        assert "counts_matrix.tsv" in script
        # 不出现 featureCounts 注释列名。
        assert "Chr" not in script and "Length" not in script and "Geneid" not in script
        # 样本列按登记顺序。
        assert "'sample_0'" in script
        assert "'sample_29'" in script
