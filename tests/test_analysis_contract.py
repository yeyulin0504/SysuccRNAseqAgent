from __future__ import annotations

import hashlib

import gzip
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

from rnaseq_agent.analysis_contract import (
    ContractError,
    build_analysis_contract,
    create_project_contract,
    verify_analysis_contract,
    verify_project_contract,
)
from rnaseq_agent.pipeline import render_remote_downstream_script
from rnaseq_agent.run_agent import run_project, run_standalone_downstream_project
from rnaseq_agent.storage import load_json, save_json
from rnaseq_agent.validation import validate_local_fastqs
from rnaseq_agent.workflow_profiles import apply_workflow_profile, workflow_profile_errors


def _write_fastq(path: Path, sequence: str = "ACGT") -> None:
    with gzip.open(path, "wt", encoding="ascii") as handle:
        handle.write(f"@read1\n{sequence}\n+\n{'I' * len(sequence)}\n")


def _config(root: Path) -> dict:
    data_dir = root / "fastq"
    return {
        "project": {"id": "contract_test", "title": "Contract test"},
        "server": {
            "host": "hpc.example.edu",
            "user": "researcher",
            "remote_base_dir": "/remote/projects",
            "remote_workdir": "/remote/projects/contract_test",
            "scheduler": "local",
            "threads": 4,
            "memory_gb": 8,
            "init_commands": [],
        },
        "samples": {
            "local_data_dir": str(data_dir),
            "remote_data_dir": "/remote/projects/contract_test/raw",
            "items": [
                {
                    "sample_id": "sample_1",
                    "condition": "control",
                    "fastq_1": "sample_R1.fastq.gz",
                    "fastq_2": "sample_R2.fastq.gz",
                }
            ],
        },
        "sequencing": {"layout": "paired", "strandedness": "unstranded"},
        "pipeline": {
            "fastp": {"enabled": True, "version": "0.24.1"},
            "star": {"enabled": False, "version": "2.7.11b"},
            "arriba": {"enabled": False, "version": "2.5.0"},
            "featurecounts": {"enabled": False, "version": "Subread 2.1.1"},
            "rsem": {"enabled": False, "version": "1.2.28"},
        },
        "reference": {},
        "execution": {"mode": "free", "skill_id": "", "contract_file": "analysis_contract.json"},
    }


def _prepare(root: Path) -> dict:
    data_dir = root / "fastq"
    data_dir.mkdir()
    _write_fastq(data_dir / "sample_R1.fastq.gz")
    _write_fastq(data_dir / "sample_R2.fastq.gz")
    return _config(root)


class AnalysisContractTests(unittest.TestCase):
    def test_contract_id_is_stable_across_creation_times(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config = _prepare(Path(temp_name))

            first = build_analysis_contract(config, created_at="2026-01-01T00:00:00")
            second = build_analysis_contract(config, created_at="2026-01-02T00:00:00")

            self.assertEqual(first["contract_id"], second["contract_id"])
            self.assertNotEqual(first["created_at"], second["created_at"])

    def test_input_change_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = _prepare(root)
            contract = build_analysis_contract(config)
            _write_fastq(root / "fastq" / "sample_R1.fastq.gz", sequence="TGCA")

            verification = verify_analysis_contract(config, contract)

            self.assertFalse(verification.ok)
            self.assertTrue(any("Input artifacts" in error for error in verification.errors))

    def test_workflow_change_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config = _prepare(Path(temp_name))
            contract = build_analysis_contract(config)
            changed = deepcopy(config)
            changed["server"]["threads"] = 16
            changed["server"]["scheduler"] = "slurm"

            verification = verify_analysis_contract(changed, contract)

            self.assertFalse(verification.ok)
            self.assertTrue(any("Workflow configuration" in error for error in verification.errors))
            self.assertTrue(any("Rendered scripts" in error for error in verification.errors))

    def test_contract_body_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config = _prepare(Path(temp_name))
            contract = build_analysis_contract(config)
            contract["body"]["workflow"]["server"]["threads"] = 99

            verification = verify_analysis_contract(config, contract)

            self.assertFalse(verification.ok)
            self.assertTrue(any("Stored contract ID" in error for error in verification.errors))

    def test_project_contract_creation_activates_and_verifies_contract_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = _prepare(root)
            config_path = root / "project" / "project.json"
            save_json(config_path, config)

            contract_path, contract = create_project_contract(config_path)
            verification = verify_project_contract(config_path)

            self.assertTrue(contract_path.is_file())
            self.assertEqual(load_json(config_path)["execution"]["mode"], "contract")
            self.assertEqual(verification.contract_id, contract["contract_id"])
            self.assertTrue(verification.ok)

            _, recreated = create_project_contract(config_path)
            self.assertEqual(recreated["contract_id"], contract["contract_id"])

    def test_downstream_configuration_and_rendered_artifacts_are_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config = _prepare(Path(temp_name))
            config["downstream"] = {
                "enabled": True,
                "profile_id": "bulk_rnaseq_deseq2_v1",
                "input": {"kind": "featurecounts_raw_counts", "path": "featurecounts/gene_counts.txt"},
                "metadata": {"samples": []},
                "design": {"condition_column": "condition", "batch_column": "", "formula": "~ condition"},
                "contrasts": [],
                "filtering": {"min_count": 10, "min_samples": 2},
                "differential_expression": {"padj_threshold": 0.05, "abs_log2_fold_change": 1.0},
                "enrichment": {"enabled": False, "organism": "", "go_ora": False, "kegg_ora": False, "gsea": False, "id_type": "ENSEMBL", "gmt": {"enabled": False}},
                "runtime": {"environment_kind": "apptainer", "image_path": "/containers/downstream.sif", "image_sha256": "a" * 64, "rscript_path": "Rscript"},
            }

            contract = build_analysis_contract(config)
            script_names = {item["name"] for item in contract["body"]["scripts"]}
            changed = deepcopy(config)
            changed["downstream"]["filtering"]["min_count"] = 20

            self.assertIn("downstream_config.json", script_names)
            self.assertIn("run_downstream.R", script_names)
            self.assertFalse(verify_analysis_contract(changed, contract).ok)

    def test_standalone_downstream_inputs_are_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = _prepare(root)
            input_root = root / "downstream_inputs"
            input_root.mkdir()
            counts = input_root / "counts.tsv"
            metadata = input_root / "metadata.tsv"
            counts.write_text(
                "gene_id\tcontrol_1\tcontrol_2\ttreated_1\ttreated_2\n"
                "ENSG000001\t10\t11\t20\t21\n"
                "ENSG000002\t1\t2\t3\t4\n",
                encoding="utf-8",
            )
            metadata.write_text(
                "sample_id\tcondition\n"
                "control_1\tcontrol\n"
                "control_2\tcontrol\n"
                "treated_1\ttreated\n"
                "treated_2\ttreated\n",
                encoding="utf-8",
            )
            config["downstream"] = {
                "enabled": True,
                "source_mode": "standalone_count_matrix",
                "profile_id": "bulk_rnaseq_deseq2_v1",
                "input_root": str(input_root),
                "input": {
                    "source_filename": counts.name,
                    "source_path": str(counts),
                },
                "metadata_input": {
                    "source_filename": metadata.name,
                    "source_path": str(metadata),
                },
                "design": {"condition_column": "condition", "batch_column": "", "formula": "~ condition"},
                "contrasts": [],
                "filtering": {"min_count": 10, "min_samples": 2},
                "differential_expression": {"padj_threshold": 0.05, "abs_log2_fold_change": 1.0},
                "enrichment": {"enabled": False, "organism": "", "go_ora": False, "kegg_ora": False, "gsea": False, "id_type": "ENSEMBL", "gmt": {"enabled": False}},
                "runtime": {"environment_kind": "apptainer", "image_path": "/containers/downstream.sif", "image_sha256": "a" * 64, "rscript_path": "Rscript"},
            }

            contract = build_analysis_contract(config)
            inputs = {item["role"]: item for item in contract["body"]["inputs"]}

            self.assertEqual(inputs["downstream_counts"]["logical_name"], "inputs/counts.tsv")
            self.assertEqual(inputs["downstream_counts"]["source_schema"], "standalone_count_matrix_tsv_v1")
            self.assertEqual(inputs["downstream_metadata"]["logical_name"], "inputs/metadata.tsv")
            scripts = {item["name"]: item for item in contract["body"]["scripts"]}
            self.assertIn("run_downstream.sh", scripts)
            self.assertNotIn("run_pipeline.sh", scripts)
            self.assertEqual(
                scripts["run_downstream.sh"]["sha256"],
                hashlib.sha256(render_remote_downstream_script(config).encode("utf-8")).hexdigest(),
            )
            metadata.write_text(metadata.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            verification = verify_analysis_contract(config, contract)
            self.assertFalse(verification.ok)
            self.assertTrue(any("Input artifacts" in error for error in verification.errors))

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = _prepare(root)
            gmt_path = root / "gene_sets.gmt"
            gmt_path.write_text("SET_A\tExample\t1\t2\n", encoding="utf-8")
            config["downstream"] = {
                "enabled": True,
                "profile_id": "bulk_rnaseq_deseq2_v1",
                "input": {"kind": "featurecounts_raw_counts", "path": "featurecounts/gene_counts.txt"},
                "metadata": {"samples": []},
                "design": {"condition_column": "condition", "batch_column": "", "formula": "~ condition"},
                "contrasts": [],
                "filtering": {"min_count": 10, "min_samples": 2},
                "differential_expression": {"padj_threshold": 0.05, "abs_log2_fold_change": 1.0},
                "enrichment": {
                    "enabled": True, "organism": "human", "go_ora": True, "kegg_ora": True,
                    "gsea": True, "id_type": "ENSEMBL",
                    "gmt": {
                        "enabled": True, "source_filename": gmt_path.name,
                        "source_path": str(gmt_path), "sha256": hashlib.sha256(gmt_path.read_bytes()).hexdigest(),
                    },
                },
                "runtime": {"environment_kind": "apptainer", "image_path": "/containers/downstream.sif", "image_sha256": "a" * 64, "rscript_path": "Rscript"},
            }

            contract = build_analysis_contract(config)
            gmt_inputs = [item for item in contract["body"]["inputs"] if item["role"] == "downstream_gmt"]
            self.assertEqual(gmt_inputs[0]["logical_name"], "scripts/gene_sets.gmt")

            gmt_path.write_text("SET_A\tChanged\t3\t4\n", encoding="utf-8")

            verification = verify_analysis_contract(config, contract)
            self.assertFalse(verification.ok)
            self.assertTrue(any("Input artifacts" in error for error in verification.errors))

    @patch("rnaseq_agent.run_agent.create_remote_transport")
    def test_contract_drift_blocks_run_before_remote_connection(
        self,
        create_transport: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = _prepare(root)
            config_path = root / "project" / "project.json"
            save_json(config_path, config)
            create_project_contract(config_path)
            _write_fastq(root / "fastq" / "sample_R1.fastq.gz", sequence="TGCA")

            with self.assertRaises(ContractError):
                run_project(config_path, wait=False)

            create_transport.assert_not_called()

    def test_path_escape_is_rejected_by_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = _prepare(root)
            config["samples"]["items"][0]["fastq_1"] = "../outside.fastq.gz"

            result = validate_local_fastqs(config)

            self.assertFalse(result.ok)
            self.assertTrue(any("not a path" in error for error in result.errors))


class WorkflowProfileTests(unittest.TestCase):
    def test_applying_profile_locks_pipeline_and_detects_drift(self) -> None:
        config = {"pipeline": {}, "execution": {"mode": "free"}}

        locked = apply_workflow_profile(config, "bulk_rnaseq_expression_v1")

        self.assertEqual(locked["execution"]["mode"], "skill")
        self.assertFalse(locked["pipeline"]["arriba"]["enabled"])
        self.assertEqual(workflow_profile_errors(locked), [])

        locked["pipeline"]["star"]["version"] = "different"
        self.assertTrue(workflow_profile_errors(locked))


if __name__ == "__main__":
    unittest.main()
