from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rnaseq_agent.analysis_contract import ContractError, create_project_contract
from rnaseq_agent.execution import CommandResult
from rnaseq_agent.run_agent import run_project
from rnaseq_agent.storage import load_json, save_json
from rnaseq_agent.workflow_profiles import apply_workflow_profile


SKILL_ID = "bulk_rnaseq_expression_v1"


def _write_fastq(path: Path, read_id: str, sequence: str = "ACGT") -> None:
    with gzip.open(path, "wt", encoding="ascii") as handle:
        handle.write(f"@{read_id}\n{sequence}\n+\n{'I' * len(sequence)}\n")


def _base_config(data_dir: Path, project_id: str) -> dict:
    remote_workdir = f"/remote/projects/{project_id}"
    return {
        "project": {"id": project_id, "title": "Execution mode integration test"},
        "server": {
            "host": "hpc.example.edu",
            "user": "researcher",
            "remote_workdir": remote_workdir,
            "scheduler": "local",
            "threads": 2,
            "memory_gb": 4,
            "init_commands": [],
        },
        "samples": {
            "local_data_dir": str(data_dir),
            "remote_data_dir": f"{remote_workdir}/raw",
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
        "reference": {
            "remote_gtf_path": "/reference/genes.gtf",
            "remote_genome_fasta_path": "/reference/genome.fa",
            "star_index_dir": "/reference/star",
            "rsem_index_prefix": "/reference/rsem/index",
        },
        "execution": {
            "mode": "free",
            "skill_id": "",
            "contract_file": "analysis_contract.json",
        },
        "notification": {"email_enabled": False},
    }


def _prepare_project(root: Path, project_id: str) -> tuple[Path, Path]:
    data_dir = root / "fastq"
    data_dir.mkdir(parents=True)
    _write_fastq(data_dir / "sample_R1.fastq.gz", "read1/1")
    _write_fastq(data_dir / "sample_R2.fastq.gz", "read1/2")
    config_path = root / "project" / "project.json"
    save_json(config_path, _base_config(data_dir, project_id))
    return config_path, data_dir


class FakeTransport:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.uploaded: list[tuple[list[Path], str]] = []

    def execute(self, remote_command: str) -> CommandResult:
        self.executed.append(remote_command)
        stdout = "fake-job-1\n" if "nohup bash scripts/submit.sh" in remote_command else ""
        return CommandResult(["fake-ssh", remote_command], 0, stdout, "")

    def upload(self, local_paths, remote_dir: str) -> CommandResult:
        paths = [Path(path) for path in local_paths]
        self.uploaded.append((paths, remote_dir))
        return CommandResult(
            ["fake-upload", *(str(path) for path in paths), remote_dir],
            0,
            "uploaded",
            "",
        )

    def download(self, remote_file: str, local_path: Path) -> CommandResult:
        raise AssertionError("run_project(wait=False) must not download results")


class ExecutionModesIntegrationTests(unittest.TestCase):
    @patch("rnaseq_agent.run_agent.create_remote_transport")
    def test_free_skill_and_contract_runs_record_mode_specific_manifests(
        self,
        create_transport,
    ) -> None:
        expected_execution = {
            "free": {"skill_id": "", "contract_id": ""},
            "skill": {"skill_id": SKILL_ID, "contract_id": ""},
            "contract": {"skill_id": "", "contract_id": None},
        }

        with tempfile.TemporaryDirectory() as temp_name:
            suite_root = Path(temp_name)
            for mode, expected in expected_execution.items():
                with self.subTest(mode=mode):
                    config_path, _ = _prepare_project(
                        suite_root / mode,
                        f"integration_{mode}",
                    )
                    contract = None
                    if mode == "skill":
                        config = apply_workflow_profile(load_json(config_path), SKILL_ID)
                        save_json(config_path, config)
                    elif mode == "contract":
                        _, contract = create_project_contract(config_path)

                    transport = FakeTransport()
                    create_transport.return_value = transport
                    outcome = run_project(config_path, wait=False)

                    self.assertEqual(outcome.state, "submitted")
                    status = load_json(config_path)["status"]
                    run_id = status.get("run_id", "")
                    self.assertTrue(run_id)
                    attempt_dir = config_path.parent / "attempts" / run_id
                    self.assertEqual(Path(status["attempt_dir"]), attempt_dir)
                    self.assertTrue(attempt_dir.is_dir())

                    manifest = load_json(attempt_dir / "run_manifest.json")
                    execution = manifest["body"]["execution"]
                    self.assertEqual(execution["mode"], mode)
                    self.assertEqual(execution["skill_id"], expected["skill_id"])
                    if mode == "contract":
                        assert contract is not None
                        self.assertEqual(execution["contract_id"], contract["contract_id"])
                        contract_copy = (
                            attempt_dir / "generated_scripts" / "analysis_contract.json"
                        )
                        self.assertTrue(contract_copy.is_file())
                        self.assertEqual(
                            load_json(contract_copy)["contract_id"],
                            contract["contract_id"],
                        )
                    else:
                        self.assertEqual(execution["contract_id"], expected["contract_id"])

                    self.assertTrue(transport.executed)
                    self.assertTrue(transport.uploaded)

    @patch("rnaseq_agent.run_agent.create_remote_transport")
    def test_contract_fastq_drift_is_blocked_before_transport_creation(
        self,
        create_transport,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path, data_dir = _prepare_project(root, "integration_contract_drift")
            create_project_contract(config_path)
            _write_fastq(data_dir / "sample_R1.fastq.gz", "read1/1", sequence="TGCA")

            with self.assertRaises(ContractError):
                run_project(config_path, wait=False)

            create_transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
