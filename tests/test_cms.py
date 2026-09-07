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
    render_cms_script,
)
from rnaseq_agent.capability import NOT_EVALUABLE, PASS


def _config(
    *,
    cancer_type: str = "coad",
    n_samples: int = CMS_MIN_SAMPLES,
    featurecounts: bool = True,
    cms_enabled: bool = True,
) -> dict:
    items: list[dict] = []
    for index in range(n_samples):
        items.append(
            {
                "sample_id": f"sample_{index}",
                "condition": "tumor",
                "fastq_1": f"sample_{index}_R1.fastq.gz",
                "fastq_2": f"sample_{index}_R2.fastq.gz",
            }
        )
    return {
        "study": {"cancer_type": cancer_type, "design": "single_group"},
        "samples": {"items": items},
        "pipeline": {
            "featurecounts": {"enabled": featurecounts},
            "cms": {"enabled": cms_enabled},
        },
        "cms": {"n_perm": 1000, "fdr": 0.05, "seed": 20260907, "do_plot": False},
    }


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
