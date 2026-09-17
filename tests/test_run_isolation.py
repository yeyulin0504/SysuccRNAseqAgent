from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rnaseq_agent.analysis_contract import sha256_file
from rnaseq_agent.execution import CommandResult
from rnaseq_agent.pipeline import render_remote_pipeline_script
from rnaseq_agent.run_agent import refresh_status, run_project, run_stage_project
from rnaseq_agent.storage import load_json, save_json


def _write_fastq(path: Path, read_id: str) -> None:
    with gzip.open(path, "wt", encoding="ascii") as handle:
        handle.write(f"@{read_id}\nACGT\n+\nIIII\n")


def _project_config(data_dir: Path) -> dict:
    return {
        "project": {"id": "isolation_test", "title": "Run isolation test"},
        "server": {
            "host": "hpc.example.edu",
            "user": "researcher",
            "remote_workdir": "/remote/projects/isolation_test",
            "scheduler": "local",
            "threads": 2,
            "memory_gb": 4,
            "init_commands": [],
        },
        "samples": {
            "local_data_dir": str(data_dir),
            "remote_data_dir": "/remote/projects/isolation_test/raw",
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
        "execution": {
            "mode": "free",
            "skill_id": "",
            "contract_file": "analysis_contract.json",
        },
        "notification": {"email_enabled": False},
    }


class FakeTransport:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.uploaded: list[tuple[list[Path], str]] = []
        self.submission_count = 0

    def execute(self, remote_command: str) -> CommandResult:
        self.executed.append(remote_command)
        stdout = ""
        if "nohup bash scripts/submit" in remote_command:
            self.submission_count += 1
            stdout = f"{1000 + self.submission_count}\n"
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
        raise AssertionError("wait=False must not download remote results")


class RunIsolationTests(unittest.TestCase):
    @patch("rnaseq_agent.run_agent.create_remote_transport")
    def test_counts_stage_creates_and_uploads_run_manifest(
        self,
        create_transport,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            counts_path = root / "counts.tsv"
            counts_path.write_text(
                "gene\tc1\tc2\tc3\tt1\tt2\tt3\nG1\t1\t2\t3\t4\t5\t6\n",
                encoding="utf-8",
            )
            config = _project_config(root)
            config["samples"] = {
                "counts_path": str(counts_path),
                "local_data_dir": str(root),
                "remote_data_dir": "AUTO",
                "items": [
                    {"sample_id": f"c{i}", "condition": "control"}
                    for i in range(1, 4)
                ]
                + [
                    {"sample_id": f"t{i}", "condition": "treated"}
                    for i in range(1, 4)
                ],
            }
            for step in config["pipeline"].values():
                step["enabled"] = False
            config["pipeline"]["diffexp"] = {"enabled": True, "version": "DESeq2"}
            config["pipeline"]["cms"] = {"enabled": False, "version": "CMScaller"}
            config["diffexp"] = {
                "formula": "~ condition",
                "reference_condition": "control",
            }
            config["cms"] = {"run_mode": "counts"}
            config["container"] = {"enabled": False}
            config_path = root / "project" / "project.json"
            save_json(config_path, config)

            transport = FakeTransport()
            create_transport.return_value = transport

            outcome = run_stage_project(config_path, "counts", wait=False)

            self.assertEqual(outcome.state, "submitted")
            status = load_json(config_path)["status"]
            manifest_path = root / "project" / "attempts" / status["run_id"] / "run_manifest.json"
            self.assertTrue(manifest_path.is_file())
            self.assertTrue(
                any(
                    path.name == "run_manifest.json"
                    for paths, _ in transport.uploaded
                    for path in paths
                )
            )

            snapshot_path = manifest_path.parent / "project.snapshot.json"
            original_snapshot = snapshot_path.read_bytes()
            project = load_json(config_path)
            project["project"]["title"] = "changed after the attempt started"
            save_json(config_path, project)

            with patch("rnaseq_agent.run_agent._read_remote_state", return_value="completed"):
                refresh_status(config_path)
            second = run_stage_project(config_path, "de", wait=False)

            self.assertEqual(second.state, "submitted")
            self.assertEqual(snapshot_path.read_bytes(), original_snapshot)
            claims = load_json(config_path)["status"]["execution_claims"]
            self.assertEqual({claim["stage"] for claim in claims.values()}, {"counts", "de"})
            self.assertEqual(len({claim["idempotency_key"] for claim in claims.values()}), 2)
            manifest = load_json(manifest_path)
            self.assertEqual(
                manifest["body"]["fingerprints"]["project_snapshot_sha256"],
                sha256_file(snapshot_path),
            )

    @patch("rnaseq_agent.run_agent.create_remote_transport")
    def test_explicit_rerun_after_terminal_failure_is_isolated_and_keeps_stable_fingerprints(
        self,
        create_transport,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            data_dir = root / "fastq"
            data_dir.mkdir()
            _write_fastq(data_dir / "sample_R1.fastq.gz", "read1/1")
            _write_fastq(data_dir / "sample_R2.fastq.gz", "read1/2")
            config_path = root / "project" / "project.json"
            save_json(config_path, _project_config(data_dir))

            transport = FakeTransport()
            create_transport.return_value = transport

            with patch("rnaseq_agent.run_agent._poll_until_finished", return_value="failed"):
                first_outcome = run_project(config_path, wait=True)
            first_status = load_json(config_path)["status"]
            first_command_count = len(transport.executed)
            second_outcome = run_project(config_path, wait=False)
            second_status = load_json(config_path)["status"]

            self.assertEqual(first_outcome.state, "failed")
            self.assertEqual(second_outcome.state, "submitted")
            first_run_id = first_status["run_id"]
            second_run_id = second_status["run_id"]
            self.assertNotEqual(first_run_id, second_run_id)

            attempts = []
            manifests = []
            for status in (first_status, second_status):
                run_id = status["run_id"]
                attempt_dir = root / "project" / "attempts" / run_id
                expected_remote = f"/remote/projects/isolation_test/attempts/{run_id}"
                self.assertEqual(Path(status["attempt_dir"]), attempt_dir)
                self.assertEqual(status["remote_run_workdir"], expected_remote)
                self.assertTrue((attempt_dir / "project.snapshot.json").is_file())
                self.assertTrue((attempt_dir / "run_manifest.json").is_file())
                self.assertTrue((attempt_dir / "generated_scripts").is_dir())
                self.assertTrue((attempt_dir / "generated_scripts" / "run_pipeline.sh").is_file())
                self.assertTrue((attempt_dir / "agent_logs" / "events.jsonl").is_file())
                self.assertTrue((attempt_dir / "agent_logs" / "commands.jsonl").is_file())
                attempts.append(attempt_dir)
                manifests.append(load_json(attempt_dir / "run_manifest.json"))

                snapshot = load_json(attempt_dir / "project.snapshot.json")
                self.assertEqual(snapshot["samples"]["remote_data_dir"], f"{expected_remote}/raw")

            stable_keys = ("workflow_sha256", "inputs_sha256", "scripts_sha256")
            first_fingerprints = manifests[0]["body"]["fingerprints"]
            second_fingerprints = manifests[1]["body"]["fingerprints"]
            for key in stable_keys:
                self.assertEqual(first_fingerprints[key], second_fingerprints[key], key)
            self.assertNotEqual(
                first_fingerprints["project_snapshot_sha256"],
                second_fingerprints["project_snapshot_sha256"],
            )

            first_commands = transport.executed[:first_command_count]
            second_commands = transport.executed[first_command_count:]
            self.assertTrue(any(first_status["remote_run_workdir"] in command for command in first_commands))
            self.assertTrue(any(second_status["remote_run_workdir"] in command for command in second_commands))
            self.assertFalse(any(second_status["remote_run_workdir"] in command for command in first_commands))
            self.assertFalse(any(first_status["remote_run_workdir"] in command for command in second_commands))

            support_destinations = [
                remote_dir
                for paths, remote_dir in transport.uploaded
                if any(path.name == "run_manifest.json" for path in paths)
            ]
            self.assertCountEqual(
                support_destinations,
                [
                    f"{first_status['remote_run_workdir']}/scripts",
                    f"{second_status['remote_run_workdir']}/scripts",
                ],
            )

    def test_rendered_pipeline_uses_attempt_workdir_and_clears_stale_flags(self) -> None:
        config = _project_config(Path("unused"))

        script = render_remote_pipeline_script(config)

        self.assertIn('WORKDIR="${RNASEQ_RUN_WORKDIR:-$PWD}"', script)
        self.assertIn('INPUTDIR="${RNASEQ_INPUT_DIR:-$WORKDIR/raw}"', script)
        self.assertIn("umask 077", script)
        self.assertIn('chmod 700 "$WORKDIR"', script)
        self.assertIn('cd "$WORKDIR"', script)
        self.assertIn("rm -f status/completed.flag status/failed.flag", script)
        self.assertNotIn(config["server"]["remote_workdir"], script)

    def test_rendered_pipeline_wraps_tools_with_apptainer_when_enabled(self) -> None:
        config = _project_config(Path("unused"))
        config["container"] = {
            "enabled": True,
            "engine": "apptainer",
            "image_path": "/containers/rnaseq.sif",
            "bind_paths": ["/ref"],
        }

        script = render_remote_pipeline_script(config)

        self.assertIn("apptainer exec --cleanenv --bind /ref /containers/rnaseq.sif fastp", script)
        self.assertNotIn("\nfastp \\", script)


if __name__ == "__main__":
    unittest.main()
