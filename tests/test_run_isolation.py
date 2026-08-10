from __future__ import annotations

import gzip
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rnaseq_agent.configuration import normalize_config
from rnaseq_agent.execution import CommandResult
from rnaseq_agent.pipeline import render_remote_pipeline_script
from rnaseq_agent import run_agent
from rnaseq_agent.run_agent import run_project, run_standalone_downstream_project
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


def _standalone_config(root: Path) -> dict:
    config = _project_config(root)
    input_root = root / "downstream_inputs"
    input_root.mkdir(exist_ok=True)
    counts = input_root / "source_counts.tsv"
    metadata = input_root / "source_metadata.tsv"
    counts.write_text(
        "gene_id\tcontrol_1\tcontrol_2\ttreated_1\ttreated_2\n"
        "ENSG000001\t10\t11\t20\t21\n",
        encoding="utf-8",
    )
    metadata.write_text(
        "sample_id\tcondition\ncontrol_1\tcontrol\ncontrol_2\tcontrol\n"
        "treated_1\ttreated\ntreated_2\ttreated\n",
        encoding="utf-8",
    )
    config["downstream"] = {
        "enabled": True, "source_mode": "standalone_count_matrix",
        "profile_id": "bulk_rnaseq_deseq2_v1", "input_root": str(input_root),
        "input": {"source_filename": counts.name, "source_path": str(counts)},
        "metadata_input": {"source_filename": metadata.name, "source_path": str(metadata)},
        "design": {"condition_column": "condition", "batch_column": "", "formula": "~ condition"},
        "contrasts": [], "filtering": {"min_count": 10, "min_samples": 2},
        "differential_expression": {"padj_threshold": 0.05, "abs_log2_fold_change": 1.0},
        "enrichment": {"enabled": False, "organism": "", "go_ora": False, "kegg_ora": False, "gsea": False, "id_type": "ENSEMBL", "gmt": {"enabled": False}},
        "runtime": {"environment_kind": "apptainer", "image_path": "/containers/downstream.sif", "image_sha256": "a" * 64, "rscript_path": "Rscript"},
    }
    return config


class FakeTransport:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.uploaded: list[tuple[list[Path], str]] = []
        self.submission_count = 0

    def execute(self, remote_command: str) -> CommandResult:
        self.executed.append(remote_command)
        stdout = ""
        if "nohup bash scripts/submit.sh" in remote_command:
            self.submission_count += 1
            stdout = f"fake-job-{self.submission_count}\n"
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


class FailingStandaloneTransport(FakeTransport):
    def __init__(self, *, fail_upload: bool = False) -> None:
        super().__init__()
        self.fail_upload = fail_upload

    def execute(self, remote_command: str) -> CommandResult:
        result = super().execute(remote_command)
        if "mkdir -p" in remote_command and "/inputs" in remote_command:
            return CommandResult(result.command, 1, "", "mkdir failed")
        return result

    def upload(self, local_paths, remote_dir: str) -> CommandResult:
        result = super().upload(local_paths, remote_dir)
        if self.fail_upload and remote_dir.endswith("/inputs"):
            return CommandResult(result.command, 1, "", "upload failed")
        return result

class RunIsolationTests(unittest.TestCase):
    @patch("rnaseq_agent.run_agent.create_remote_transport")
    def test_repeated_submissions_are_isolated_and_keep_stable_fingerprints(
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

            first_outcome = run_project(config_path, wait=False)
            first_status = load_json(config_path)["status"]
            first_command_count = len(transport.executed)
            second_outcome = run_project(config_path, wait=False)
            second_status = load_json(config_path)["status"]

            self.assertEqual(first_outcome.state, "submitted")
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

    @patch("rnaseq_agent.run_agent.create_remote_transport")
    def test_invalid_standalone_count_blocks_transport(self, create_transport) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = _standalone_config(root)
            counts = Path(config["downstream"]["input"]["source_path"])
            counts.write_text(
                "gene_id\tcontrol_1\tcontrol_2\ttreated_1\ttreated_2\n"
                "ENSG000001\t10\tnot-an-integer\t20\t21\n",
                encoding="utf-8",
            )
            config_path = root / "project" / "project.json"
            save_json(config_path, config)

            with self.assertRaisesRegex(RuntimeError, "nonnegative integer"):
                run_standalone_downstream_project(config_path, wait=False)

            create_transport.assert_not_called()

    @patch("rnaseq_agent.run_agent.create_remote_transport")
    def test_failed_standalone_input_setup_does_not_submit(self, create_transport) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "project" / "project.json"
            save_json(config_path, _standalone_config(root))
            transport = FailingStandaloneTransport()
            create_transport.return_value = transport

            with self.assertRaisesRegex(RuntimeError, "mkdir failed"):
                run_standalone_downstream_project(config_path, wait=False)

            self.assertFalse(any("nohup bash scripts/submit.sh" in command for command in transport.executed))


    @patch("rnaseq_agent.run_agent.validate_local_fastqs")
    @patch("rnaseq_agent.run_agent.validate_standalone_downstream_inputs")
    @patch("rnaseq_agent.run_agent.create_remote_transport")
    def test_standalone_submission_uses_dedicated_validation(
        self, create_transport, validate_standalone, validate_fastqs
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "project" / "project.json"
            save_json(config_path, _standalone_config(root))
            create_transport.return_value = FakeTransport()

            run_standalone_downstream_project(config_path, wait=False)

            validate_standalone.assert_called_once()
            validate_fastqs.assert_not_called()

    @patch("rnaseq_agent.run_agent.create_remote_transport")
    def test_standalone_submission_stages_only_fixed_input_names(self, create_transport) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "project" / "project.json"
            save_json(config_path, _standalone_config(root))
            transport = FakeTransport()
            create_transport.return_value = transport

            first = run_standalone_downstream_project(config_path, wait=False)
            first_status = load_json(config_path)["status"]
            second = run_standalone_downstream_project(config_path, wait=False)
            second_status = load_json(config_path)["status"]

            self.assertEqual(first.state, "submitted")
            self.assertEqual(second.state, "submitted")
            self.assertNotEqual(first_status["run_id"], second_status["run_id"])
            for status in (first_status, second_status):
                attempt = Path(status["attempt_dir"])
                self.assertTrue((attempt / "inputs" / "counts.tsv").is_file())
                self.assertTrue((attempt / "inputs" / "metadata.tsv").is_file())
                self.assertTrue((attempt / "generated_scripts" / "run_downstream.sh").is_file())
                self.assertFalse((attempt / "generated_scripts" / "run_pipeline.sh").exists())
                self.assertIn("bash scripts/run_downstream.sh", (attempt / "generated_scripts" / "submit.sh").read_text())

            input_uploads = [(paths, dest) for paths, dest in transport.uploaded if dest.endswith("/inputs")]
            self.assertEqual(len(input_uploads), 2)
            for paths, destination in input_uploads:
                self.assertEqual([path.name for path in paths], ["counts.tsv", "metadata.tsv"])
                self.assertIn("/attempts/", destination)
            self.assertFalse(any(path.suffix == ".gz" for paths, _ in transport.uploaded for path in paths))
            self.assertTrue(any("chmod 600" in command and "/inputs/" in command for command in transport.executed))


    def test_stages_gmt_with_remote_config_path_and_digest_recheck(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            gmt_path = root / "sets.gmt"
            gmt_path.write_text("SET_A\tExample\t1\t2\n", encoding="utf-8")
            config = normalize_config(_project_config(root))
            config["downstream"] = normalize_config({"downstream": {
                "enabled": True,
                "enrichment": {"gmt": {
                    "enabled": True,
                    "source_filename": gmt_path.name,
                    "source_path": str(gmt_path),
                    "sha256": hashlib.sha256(gmt_path.read_bytes()).hexdigest(),
                }},
            }})["downstream"]
            logs_dir = root / "logs"
            logs_dir.mkdir()

            paths = run_agent._prepare_remote_scripts(config, root / "project.json", root, logs_dir)

            staged = root / "generated_scripts" / gmt_path.name
            rendered_config = (root / "generated_scripts" / "downstream_config.json").read_text(encoding="utf-8")
            self.assertIn(staged, paths)
            self.assertEqual(staged.read_bytes(), gmt_path.read_bytes())
            self.assertIn('"source_path": "scripts/sets.gmt"', rendered_config)
            self.assertNotIn(str(gmt_path), rendered_config)

            gmt_path.write_text("SET_A\tChanged\t3\t4\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "GMT changed after validation"):
                run_agent._prepare_remote_scripts(config, root / "project.json", root, logs_dir)

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

        self.assertIn("apptainer exec --bind /ref /containers/rnaseq.sif fastp", script)
        self.assertNotIn("\nfastp \\", script)


if __name__ == "__main__":
    unittest.main()
