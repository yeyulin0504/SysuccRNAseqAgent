from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from rnaseq_agent.defaults import DEFAULT_REFERENCE
from rnaseq_agent.analysis_contract import ContractError, create_project_contract
from rnaseq_agent.llm_submission import (
    LLMSubmissionError,
    inspect_llm_submission,
    submit_llm_contract,
)
from rnaseq_agent.storage import load_json, save_json


def _write_fastq(path: Path) -> None:
    with gzip.open(path, "wt", encoding="ascii") as handle:
        handle.write("@read1\nACGT\n+\nIIII\n")


def _config(root: Path) -> dict:
    data_dir = root / "fastq"
    return {
        "project": {"id": "llm_contract", "title": "LLM contract"},
        "server": {
            "host": "hpc.example.edu",
            "user": "researcher",
            "remote_base_dir": "/remote/projects",
            "remote_workdir": "/remote/projects/llm_contract",
            "scheduler": "local",
            "threads": 4,
            "memory_gb": 8,
            "init_commands": [],
        },
        "samples": {
            "local_data_dir": str(data_dir),
            "remote_data_dir": "/remote/projects/llm_contract/raw",
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
        "pipeline": {"fastp": {"enabled": True, "version": "0.24.1"}},
        "reference": DEFAULT_REFERENCE.copy(),
        "execution": {"mode": "free", "skill_id": "", "contract_file": "analysis_contract.json"},
    }


def _contract_project(root: Path) -> Path:
    data_dir = root / "fastq"
    data_dir.mkdir()
    _write_fastq(data_dir / "sample_R1.fastq.gz")
    _write_fastq(data_dir / "sample_R2.fastq.gz")
    config_path = root / "project" / "project.json"
    save_json(config_path, _config(root))
    create_project_contract(config_path)
    return config_path


class LLMSubmissionTests(unittest.TestCase):
    def test_inspect_rejects_project_not_in_contract_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "project.json"
            save_json(config_path, _config(root))

            with self.assertRaisesRegex(LLMSubmissionError, "contract"):
                inspect_llm_submission(config_path)

    def test_submit_uses_nonblocking_executor_once_for_verified_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config_path = _contract_project(Path(temp_name))
            executor = MagicMock(return_value="submitted")

            outcome = submit_llm_contract(config_path, executor=executor)

            self.assertEqual(outcome, "submitted")
            executor.assert_called_once_with(config_path, wait=False)

    def test_submit_blocks_second_request_for_same_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config_path = _contract_project(Path(temp_name))
            executor = MagicMock(return_value="submitted")

            submit_llm_contract(config_path, executor=executor)
            with self.assertRaisesRegex(LLMSubmissionError, "already"):
                submit_llm_contract(config_path, executor=executor)

            executor.assert_called_once()

    def test_failed_executor_keeps_guard_and_records_redacted_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config_path = _contract_project(Path(temp_name))
            executor = MagicMock(side_effect=RuntimeError("network uncertain"))

            with self.assertRaisesRegex(RuntimeError, "network uncertain"):
                submit_llm_contract(config_path, executor=executor)
            with self.assertRaises(LLMSubmissionError):
                submit_llm_contract(config_path, executor=executor)

            audit = (config_path.parent / "llm_submission_audit.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "submission_failed"', audit)
            self.assertNotIn("sample_R1.fastq.gz", audit)
            self.assertNotIn("hpc.example.edu", audit)
            self.assertNotIn("/remote/projects", audit)

    def test_contract_drift_is_rejected_before_executor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config_path = _contract_project(Path(temp_name))
            config = load_json(config_path)
            config["server"]["threads"] = 16
            save_json(config_path, config)
            executor = MagicMock()

            with self.assertRaises(ContractError):
                submit_llm_contract(config_path, executor=executor)

            executor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
