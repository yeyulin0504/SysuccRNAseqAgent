from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from rnaseq_agent.configuration import normalize_config
from rnaseq_agent.downstream import downstream_errors
from rnaseq_agent.downstream_artifacts import (
    DOWNSTREAM_CONFIG_FILENAME,
    DOWNSTREAM_SCRIPT_FILENAME,
    downstream_script_artifacts,
    render_downstream_command,
)
from rnaseq_agent.pipeline import render_remote_pipeline_script


def _config() -> dict:
    return {
        "reference": {
            "species": "human",
            "catalog_id": "GENCODE_R47_GRCh38p14_ALL",
            "index_state": "existing_confirmed",
        },
        "samples": {
            "items": [
                {"sample_id": "control_1"},
                {"sample_id": "control_2"},
                {"sample_id": "treated_1"},
                {"sample_id": "treated_2"},
            ]
        },
        "pipeline": {"star": {"enabled": True}, "featurecounts": {"enabled": True}},
        "downstream": {
            "enabled": True,
            "profile_id": "bulk_rnaseq_deseq2_v1",
            "input": {"kind": "featurecounts_raw_counts", "path": "featurecounts/gene_counts.txt"},
            "metadata": {
                "samples": [
                    {"sample_id": "control_1", "condition": "control"},
                    {"sample_id": "control_2", "condition": "control"},
                    {"sample_id": "treated_1", "condition": "treated"},
                    {"sample_id": "treated_2", "condition": "treated"},
                ]
            },
            "design": {"condition_column": "condition", "batch_column": "", "formula": "~ condition"},
            "contrasts": [
                {
                    "id": "treated_vs_control",
                    "factor": "condition",
                    "numerator": "treated",
                    "denominator": "control",
                }
            ],
            "filtering": {"min_count": 10, "min_samples": 2},
            "differential_expression": {"padj_threshold": 0.05, "abs_log2_fold_change": 1.0},
            "enrichment": {"enabled": True, "organism": "human", "go_ora": True, "kegg_ora": True, "gsea": True, "id_type": "ENSEMBL", "gmt": {"enabled": False, "source_filename": "", "source_path": "", "sha256": ""}},
            "runtime": {
                "environment_kind": "apptainer",
                "image_path": "/containers/rnaseq-agent-deseq2.sif",
                "image_sha256": "a" * 64,
                "rscript_path": "Rscript",
            },
        },
    }


class DownstreamAnalysisTests(unittest.TestCase):
    def test_normalization_disables_downstream_for_legacy_project(self) -> None:
        normalized = normalize_config({})

        self.assertFalse(normalized["downstream"]["enabled"])
        self.assertEqual(normalized["downstream"]["profile_id"], "bulk_rnaseq_deseq2_v1")

    def test_accepts_explicit_replicated_condition_design(self) -> None:
        self.assertEqual(downstream_errors(_config()), [])

    def test_requires_featurecounts_and_star(self) -> None:
        config = _config()
        config["pipeline"]["featurecounts"]["enabled"] = False
        config["pipeline"]["star"]["enabled"] = False

        errors = downstream_errors(config)

        self.assertIn("Downstream analysis requires featurecounts to be enabled.", errors)
        self.assertIn("Downstream analysis requires star to be enabled.", errors)

    def test_rejects_metadata_that_does_not_match_samples(self) -> None:
        config = _config()
        config["downstream"]["metadata"]["samples"][3]["sample_id"] = "other"

        self.assertIn(
            "Downstream metadata sample IDs must exactly match configured samples.",
            downstream_errors(config),
        )

    def test_rejects_duplicate_metadata_and_missing_condition(self) -> None:
        config = _config()
        config["downstream"]["metadata"]["samples"][1]["sample_id"] = "control_1"
        config["downstream"]["metadata"]["samples"][2]["condition"] = ""

        errors = downstream_errors(config)

        self.assertIn("Duplicate downstream metadata sample_id: control_1", errors)
        self.assertIn("Downstream metadata sample treated_1 is missing condition.", errors)

    def test_requires_two_replicates_for_each_contrast_level(self) -> None:
        config = _config()
        config["downstream"]["metadata"]["samples"][1]["condition"] = "treated"

        self.assertIn(
            "Contrast treated_vs_control requires at least 2 samples in condition level control.",
            downstream_errors(config),
        )

    def test_rejects_invalid_contrast_and_unconfirmed_reference(self) -> None:
        config = _config()
        config["downstream"]["contrasts"][0]["denominator"] = "treated"
        config["reference"]["index_state"] = "unconfigured"

        errors = downstream_errors(config)

        self.assertIn("Contrast treated_vs_control must use different numerator and denominator.", errors)
        self.assertIn("Downstream enrichment requires a confirmed human or mouse reference identity.", errors)

    def test_rejects_perfectly_confounded_batch(self) -> None:
        config = _config()
        metadata = config["downstream"]["metadata"]["samples"]
        for index, row in enumerate(metadata):
            row["batch"] = "batch_1" if index < 2 else "batch_2"
        config["downstream"]["design"] = {"condition_column": "condition", "batch_column": "batch", "formula": "~ batch + condition"}

        self.assertIn(
            "Downstream batch is perfectly confounded with condition.",
            downstream_errors(config),
        )

    def test_requires_fingerprinted_immutable_runtime_and_gmt(self) -> None:
        config = _config()
        runtime = config["downstream"]["runtime"]
        runtime["image_sha256"] = ""
        config["downstream"]["enrichment"]["gmt"] = {"enabled": True, "source_filename": "set.gmt", "source_path": "", "sha256": ""}

        errors = downstream_errors(config)

        self.assertIn("Downstream runtime.image_sha256 must be a SHA-256 digest.", errors)
        self.assertIn("Enabled downstream GMT requires a SHA-256 digest.", errors)
        self.assertIn("Enabled downstream GMT must use a readable local source_path.", errors)

    def test_accepts_local_gmt_with_matching_digest(self) -> None:
        config = _config()
        with tempfile.TemporaryDirectory() as temp_name:
            gmt_path = Path(temp_name) / "sets.gmt"
            gmt_path.write_text("SET_A\tExample set\t1\t2\n", encoding="utf-8")
            config["downstream"]["enrichment"]["gmt"] = {
                "enabled": True,
                "source_filename": gmt_path.name,
                "source_path": str(gmt_path),
                "sha256": hashlib.sha256(gmt_path.read_bytes()).hexdigest(),
            }

            self.assertEqual(downstream_errors(config), [])
    def test_renders_stable_local_artifacts_without_runtime_install_or_download(self) -> None:
        config = _config()

        artifacts = downstream_script_artifacts(config)

        self.assertEqual(set(artifacts), {DOWNSTREAM_CONFIG_FILENAME, DOWNSTREAM_SCRIPT_FILENAME})
        self.assertIn('"featurecounts/gene_counts.txt"', artifacts[DOWNSTREAM_CONFIG_FILENAME])
        self.assertNotIn("install.packages", artifacts[DOWNSTREAM_SCRIPT_FILENAME])
        self.assertNotIn("BiocManager::install", artifacts[DOWNSTREAM_SCRIPT_FILENAME])
        self.assertNotIn("download.file", artifacts[DOWNSTREAM_SCRIPT_FILENAME])
        self.assertIn("clusterProfiler::enrichGO", artifacts[DOWNSTREAM_SCRIPT_FILENAME])
        self.assertIn("clusterProfiler::GSEA", artifacts[DOWNSTREAM_SCRIPT_FILENAME])
        self.assertIn("clusterProfiler::read.gmt", artifacts[DOWNSTREAM_SCRIPT_FILENAME])

    def test_renders_downstream_after_featurecounts_in_same_job(self) -> None:
        config = normalize_config(_config())
        config["server"] = {"threads": 4, "init_commands": []}
        config["sequencing"] = {"layout": "paired", "strandedness": "unstranded"}
        config["reference"].update(
            {
                "remote_gtf_path": "/ref/genes.gtf",
                "remote_genome_fasta_path": "/ref/genome.fa",
                "star_index_dir": "/ref/star",
            }
        )
        config["pipeline"]["arriba"]["enabled"] = False
        config["pipeline"]["rsem"]["enabled"] = False
        config["samples"]["items"] = [
            {
                "sample_id": "control_1",
                "fastq_1": "control_1_R1.fastq.gz",
                "fastq_2": "control_1_R2.fastq.gz",
            },
            {
                "sample_id": "control_2",
                "fastq_1": "control_2_R1.fastq.gz",
                "fastq_2": "control_2_R2.fastq.gz",
            },
            {
                "sample_id": "treated_1",
                "fastq_1": "treated_1_R1.fastq.gz",
                "fastq_2": "treated_1_R2.fastq.gz",
            },
            {
                "sample_id": "treated_2",
                "fastq_1": "treated_2_R1.fastq.gz",
                "fastq_2": "treated_2_R2.fastq.gz",
            },
        ]

        script = render_remote_pipeline_script(config)

        self.assertLess(script.index("featureCounts"), script.index("run_downstream.R"))
        self.assertLess(script.index("run_downstream.R"), script.index('echo "completed"'))
        self.assertIn("apptainer exec /containers/rnaseq-agent-deseq2.sif Rscript", script)
        self.assertIn("scripts/downstream_config.json", script)

    def test_rejects_unsafe_downstream_runtime_path(self) -> None:
        config = _config()
        config["downstream"]["runtime"]["image_path"] = "/containers/downstream image.sif"

        self.assertIn(
            "Downstream runtime.image_path must be a safe absolute path.",
            downstream_errors(config),
        )


if __name__ == "__main__":
    unittest.main()
