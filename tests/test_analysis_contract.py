from __future__ import annotations

import gzip
import json
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


def test_paired_contract_changes_when_canonical_pair_mapping_changes(tmp_path):
    counts_path = tmp_path / "counts.tsv"
    counts_path.write_text("gene\tp1_u\tp1_t\np1\t1\t2\n", encoding="utf-8")
    config = {
        "project": {"id": "paired-contract", "title": "paired"},
        "study": {"design": "paired_two_group"},
        "server": {"scheduler": "local", "threads": 1, "memory_gb": 1, "shell": "bash"},
        "sequencing": {"layout": "paired"},
        "samples": {"source": "counts_upload", "counts_path": str(counts_path), "items": [
            {"sample_id": "p1_u", "condition": "untrt", "pair_id": "p1", "fastq_1": ""},
            {"sample_id": "p1_t", "condition": "trt", "pair_id": "p1", "fastq_1": ""},
            {"sample_id": "p2_u", "condition": "untrt", "pair_id": "p2", "fastq_1": ""},
            {"sample_id": "p2_t", "condition": "trt", "pair_id": "p2", "fastq_1": ""},
            {"sample_id": "p3_u", "condition": "untrt", "pair_id": "p3", "fastq_1": ""},
            {"sample_id": "p3_t", "condition": "trt", "pair_id": "p3", "fastq_1": ""},
        ]},
        "pipeline": {"diffexp": {"enabled": True}},
        "diffexp": {"formula": "~ pair_id + condition", "reference_condition": "untrt", "contrast_condition": "trt", "min_count_prefilter": 0},
        "execution": {"mode": "free"},
    }
    first = build_analysis_contract(config)
    changed = json.loads(json.dumps(config))
    changed["samples"]["items"][0]["pair_id"] = "p9"
    changed["samples"]["items"][1]["pair_id"] = "p9"
    second = build_analysis_contract(changed)
    assert first["contract_id"] != second["contract_id"]
from rnaseq_agent.run_agent import run_project
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

        locked = apply_workflow_profile(config, "workflow.bulk_rna.grch38_pe_expression_fusion")

        self.assertEqual(locked["execution"]["mode"], "skill")
        # 框架 15.2：expression + fusion 主线，Arriba 融合检测默认启用。
        self.assertTrue(locked["pipeline"]["arriba"]["enabled"])
        self.assertEqual(workflow_profile_errors(locked), [])

        locked["pipeline"]["star"]["version"] = "different"
        self.assertTrue(workflow_profile_errors(locked))


if __name__ == "__main__":
    unittest.main()
