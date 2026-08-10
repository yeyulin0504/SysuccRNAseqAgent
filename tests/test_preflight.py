from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rnaseq_agent.cli import main
from rnaseq_agent.execution import CommandResult
from rnaseq_agent.preflight import PreflightError, build_read_only_probe, run_preflight
from rnaseq_agent.storage import load_json, save_json


PREFIX = "RNASEQ_PREFLIGHT"
SECRET_HOST = "secret-hpc.internal.example"
SECRET_USER = "private-user"
SECRET_HOME = "/home/private-user"
INIT_CANARY = "touch /tmp/MUST_NOT_RUN_INIT_CANARY"


def _config() -> dict:
    return {
        "project": {"id": "preflight_test", "title": "Preflight test"},
        "server": {
            "host": SECRET_HOST,
            "user": SECRET_USER,
            "port": 2222,
            "remote_workdir": f"{SECRET_HOME}/projects/preflight_test",
            "scheduler": "slurm",
            "threads": 2,
            "memory_gb": 4,
            "init_commands": [INIT_CANARY],
        },
        "samples": {
            "local_data_dir": "unused",
            "remote_data_dir": f"{SECRET_HOME}/projects/preflight_test/raw",
            "items": [],
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
            "remote_gtf_path": f"{SECRET_HOME}/ref/genes.gtf",
            "remote_genome_fasta_path": f"{SECRET_HOME}/ref/genome.fa",
            "star_index_dir": f"{SECRET_HOME}/ref/star",
            "rsem_index_prefix": f"{SECRET_HOME}/ref/rsem/index",
        },
        "container": {"enabled": False},
        "notification": {"email_enabled": False},
    }


def _probe_stdout() -> str:
    rows = [
        ("metadata", "home", SECRET_HOME),
        ("metadata", "hostname", "private-compute-node-07"),
        ("metadata", "umask", "0022"),
        ("command", "sbatch", "/usr/bin/sbatch"),
        ("command", "squeue", "/usr/bin/squeue"),
        ("command", "sacct", "/usr/bin/sacct"),
        ("tool_path", "fastp", f"{SECRET_HOME}/bin/fastp"),
        ("tool_version", "fastp", "fastp 0.24.1 PRIVATE_VERSION_NOISE"),
        ("tool_path", "star", "missing"),
        ("tool_path", "arriba", "missing"),
        ("tool_path", "featurecounts", "missing"),
        ("tool_path", "rsem", "missing"),
        ("workdir", "target_kind", "no"),
        ("workdir", "writable", "yes"),
        ("workdir", "searchable", "yes"),
        ("storage", "df_kb", "1000000|250000|750000|25%"),
    ]
    structured = "\n".join("\t".join((PREFIX, *row)) for row in rows)
    return "RAW_STDOUT_SECRET_SHOULD_NOT_BE_SAVED\n" + structured + "\n"


class FakeTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.execute_calls: list[str] = []
        self.upload_calls = 0
        self.download_calls = 0

    def execute(self, remote_command: str) -> CommandResult:
        self.execute_calls.append(remote_command)
        if self.fail:
            raise RuntimeError(
                f"ssh {SECRET_USER}@{SECRET_HOST}: {SECRET_HOME}: RAW_STDERR_SECRET"
            )
        return CommandResult(
            ["fake-ssh", SECRET_HOST, remote_command],
            0,
            _probe_stdout(),
            "RAW_STDERR_SECRET_SHOULD_NOT_BE_SAVED",
        )

    def upload(self, local_paths, remote_dir: str) -> CommandResult:
        self.upload_calls += 1
        raise AssertionError("preflight must not upload")

    def download(self, remote_file: str, local_path: Path) -> CommandResult:
        self.download_calls += 1
        raise AssertionError("preflight must not download")


class PreflightTests(unittest.TestCase):
    def test_single_probe_is_read_only_and_report_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config_path = root / "project.json"
            save_json(config_path, _config())
            transport = FakeTransport()

            output_path, report = run_preflight(config_path, transport=transport)

            self.assertEqual(len(transport.execute_calls), 1)
            self.assertEqual(transport.upload_calls, 0)
            self.assertEqual(transport.download_calls, 0)
            command = transport.execute_calls[0]
            self.assertNotIn(INIT_CANARY, command)
            self.assertNotIn("init_commands", command)
            self.assertNotRegex(
                command,
                re.compile(r"(^|[;&|]\s*)(mkdir|touch|rm|mv|cp|chmod|chown)\b"),
            )
            self.assertNotIn(">", command)
            self.assertNotRegex(command, re.compile(r"(^|[;&|]\s*)sbatch\s"))
            self.assertNotRegex(command, re.compile(r"(^|[;&|]\s*)qsub\s"))

            self.assertEqual(output_path, root / "preflight.json")
            self.assertEqual(report, load_json(output_path))
            serialized = json.dumps(report, sort_keys=True)
            for secret in (
                SECRET_HOST,
                SECRET_USER,
                SECRET_HOME,
                INIT_CANARY,
                "RAW_STDOUT_SECRET_SHOULD_NOT_BE_SAVED",
                "RAW_STDERR_SECRET_SHOULD_NOT_BE_SAVED",
                "PRIVATE_VERSION_NOISE",
                "private-compute-node-07",
            ):
                self.assertNotIn(secret, serialized)

            self.assertEqual(report["overall"], "warning")
            self.assertFalse(report["environment_setup"]["init_commands_executed"])
            self.assertEqual(report["tools"]["fastp"]["reported_version"], "0.24.1")
            self.assertEqual(report["tools"]["fastp"]["path"], "<private-root>/fastp")
            self.assertEqual(report["storage"]["available_kb"], 750000)
            self.assertEqual(
                report["remote_identity"]["hostname_sha256"],
                hashlib.sha256(b"private-compute-node-07").hexdigest(),
            )
            self.assertNotIn("stdout", serialized.lower())
            self.assertNotIn("stderr", serialized.lower())

    def test_exception_is_replaced_with_connection_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "project.json"
            save_json(config_path, _config())

            with self.assertRaises(PreflightError) as caught:
                run_preflight(config_path, transport=FakeTransport(fail=True))

            message = str(caught.exception)
            self.assertIn("VPN/SSH", message)
            self.assertNotIn(SECRET_HOST, message)
            self.assertNotIn(SECRET_USER, message)
            self.assertNotIn(SECRET_HOME, message)
            self.assertNotIn("RAW_STDERR_SECRET", message)

    def test_exception_summary_keeps_type_without_leaking_identifiers(self) -> None:
        class TypeErrorTransport(FakeTransport):
            def execute(self, remote_command: str) -> CommandResult:
                raise TypeError(
                    f"bad argument for {SECRET_USER}@{SECRET_HOST} under {SECRET_HOME}"
                )

        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "project.json"
            save_json(config_path, _config())

            with self.assertRaises(PreflightError) as caught:
                run_preflight(config_path, transport=TypeErrorTransport())

            message = str(caught.exception)
            self.assertIn("TypeError", message)
            self.assertIn("<redacted>", message)
            self.assertNotIn(SECRET_HOST, message)
            self.assertNotIn(SECRET_USER, message)
            self.assertNotIn(SECRET_HOME, message)

    def test_downstream_report_marks_missing_image_or_packages_as_errors(self) -> None:
        config = _config()
        config["pipeline"]["star"]["enabled"] = True
        config["pipeline"]["featurecounts"]["enabled"] = True
        config["reference"].update(
            {"species": "human", "catalog_id": "GENCODE_R47_GRCh38p14_ALL", "index_state": "existing_confirmed"}
        )
        config["downstream"] = {
            "enabled": True,
            "enrichment": {"organism": "human"},
            "runtime": {
                "environment_kind": "apptainer",
                "image_path": "/containers/downstream.sif",
                "image_sha256": "a" * 64,
                "rscript_path": "Rscript",
            },
        }
        stdout = _probe_stdout() + "\n".join(
            "\t".join((PREFIX, *row))
            for row in [
                ("reference", "downstream_runtime_image", "readable"),
                ("downstream", "r_packages", "available"),
            ]
        ) + "\n"

        class DownstreamTransport(FakeTransport):
            def execute(self, remote_command: str) -> CommandResult:
                self.execute_calls.append(remote_command)
                return CommandResult(["fake-ssh", remote_command], 0, stdout, "")

        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "project.json"
            save_json(config_path, config)
            _, report = run_preflight(config_path, transport=DownstreamTransport())

        self.assertTrue(report["downstream"]["enabled"])
        self.assertEqual(report["downstream"]["image_state"], "readable")
        self.assertEqual(report["downstream"]["required_packages"], "available")
        self.assertFalse(any(item["code"].startswith("downstream_") for item in report["findings"]))

    def test_downstream_probe_checks_immutable_image_and_packages_read_only(self) -> None:
        config = _config()
        config["downstream"] = {
            "enabled": True,
            "runtime": {
                "environment_kind": "apptainer",
                "image_path": "/containers/downstream.sif",
                "image_sha256": "a" * 64,
                "rscript_path": "Rscript",
            },
        }

        command = build_read_only_probe(config)

        self.assertIn("probe_file downstream_runtime_image", command)
        self.assertIn("apptainer exec", command)
        self.assertIn("Rscript -e", command)
        self.assertIn("jsonlite DESeq2 ggplot2 pheatmap", command)
        self.assertIn("probe_emit downstream r_packages available", command)
        self.assertIn("probe_emit downstream r_packages unavailable", command)
        self.assertNotIn("install.packages", command)
        self.assertNotIn("BiocManager::install", command)
        self.assertNotIn("download.file", command)
        self.assertNotRegex(command, re.compile(r"(^|[;&|]\s*)(mkdir|touch|rm|mv|cp|chmod|chown|sbatch|qsub)\b"))

    def test_star_and_rsem_probe_core_reference_artifacts(self) -> None:
        config = _config()
        config["pipeline"]["star"]["enabled"] = True
        config["pipeline"]["rsem"]["enabled"] = True

        command = build_read_only_probe(config)

        for filename in (
            "Genome",
            "SA",
            "SAindex",
            "chrLength.txt",
            "chrName.txt",
            "chrNameLength.txt",
            "chrStart.txt",
            "genomeParameters.txt",
        ):
            self.assertIn(filename, command)
        for suffix in (".grp", ".ti", ".seq"):
            self.assertIn(f"index{suffix}", command)
        self.assertIn('[ -r "$probe_file_path" ]', command)
        self.assertIn('test -w "$probe_work_candidate"', command)
        self.assertIn('test -x "$probe_work_candidate"', command)
        self.assertNotIn(INIT_CANARY, command)
        self.assertNotIn(">", command)

    def test_container_preflight_checks_apptainer_image_and_container_tools(self) -> None:
        config = _config()
        config["container"] = {
            "enabled": True,
            "engine": "apptainer",
            "image_path": f"{SECRET_HOME}/containers/rnaseq.sif",
            "bind_paths": [f"{SECRET_HOME}/ref"],
        }
        stdout = "\n".join(
            "\t".join((PREFIX, *row))
            for row in [
                ("metadata", "home", SECRET_HOME),
                ("metadata", "hostname", "private-compute-node-07"),
                ("metadata", "umask", "0077"),
                ("command", "sbatch", "/usr/bin/sbatch"),
                ("command", "squeue", "/usr/bin/squeue"),
                ("command", "sacct", "/usr/bin/sacct"),
                ("command", "apptainer", "/usr/bin/apptainer"),
                ("reference", "container_image", "readable"),
                ("tool_path", "fastp", "container:fastp"),
                ("tool_version", "fastp", "fastp 0.24.1"),
                ("tool_path", "star", "missing"),
                ("tool_path", "arriba", "missing"),
                ("tool_path", "featurecounts", "missing"),
                ("tool_path", "rsem", "missing"),
                ("workdir", "target_kind", "yes"),
                ("workdir", "writable", "yes"),
                ("workdir", "searchable", "yes"),
                ("storage", "df_kb", "1000000|250000|750000|25%"),
            ]
        )
        class ContainerTransport(FakeTransport):
            def execute(self, remote_command: str) -> CommandResult:
                self.execute_calls.append(remote_command)
                return CommandResult(["fake-ssh", remote_command], 0, stdout, "")

        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "project.json"
            save_json(config_path, config)
            transport = ContainerTransport()
            output_path, report = run_preflight(config_path, transport=transport)

        command = transport.execute_calls[0]
        self.assertIn("apptainer", command)
        self.assertIn("rnaseq.sif", command)
        self.assertIn("container_image", command)
        self.assertIn("--bind", command)
        self.assertEqual(output_path.name, "preflight.json")
        self.assertTrue(report["container"]["enabled"])
        self.assertTrue(report["container"]["engine_available"])
        self.assertEqual(report["container"]["image_state"], "readable")
        self.assertEqual(report["tools"]["fastp"]["path"], "container/fastp")

    @patch("rnaseq_agent.cli.run_preflight")
    def test_cli_preflight_supports_output_without_password_argument(self, mocked_run) -> None:
        mocked_run.return_value = (
            Path("sanitized.json"),
            {"overall": "pass", "summary": {"errors": 0, "warnings": 0}},
        )

        exit_code = main(["preflight", "project.json", "--output", "sanitized.json"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(mocked_run.call_args.kwargs["output_path"], Path("sanitized.json"))
        with self.assertRaises(SystemExit):
            main(["preflight", "project.json", "--password", "secret"])

    @patch("rnaseq_agent.cli.clear_ssh_credential")
    @patch("rnaseq_agent.cli.set_ssh_credential")
    @patch("rnaseq_agent.cli.getpass.getpass", return_value="runtime-only-secret")
    @patch("rnaseq_agent.cli.run_preflight")
    def test_cli_password_prompt_uses_runtime_store_and_clears_it(
        self,
        mocked_run,
        mocked_getpass,
        mocked_set,
        mocked_clear,
    ) -> None:
        mocked_run.return_value = (
            Path("sanitized.json"),
            {"overall": "pass", "summary": {"errors": 0, "warnings": 0}},
        )
        config = _config()

        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "project.json"
            save_json(config_path, config)
            exit_code = main(
                [
                    "preflight",
                    str(config_path),
                    "--prompt-password",
                    "--output",
                    "sanitized.json",
                ]
            )

        self.assertEqual(exit_code, 0)
        mocked_getpass.assert_called_once()
        mocked_set.assert_called_once_with(
            SECRET_HOST,
            SECRET_USER,
            mode="password",
            password="runtime-only-secret",
        )
        mocked_clear.assert_called_once_with(SECRET_HOST, SECRET_USER)

    @patch("rnaseq_agent.cli.clear_ssh_credential")
    @patch("rnaseq_agent.cli.set_ssh_credential")
    @patch("rnaseq_agent.cli.getpass.getpass", return_value="runtime-only-secret")
    @patch(
        "rnaseq_agent.cli.run_preflight",
        side_effect=PreflightError("sanitized connection failure"),
    )
    def test_cli_password_is_cleared_when_preflight_fails(
        self,
        mocked_run,
        mocked_getpass,
        mocked_set,
        mocked_clear,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            config_path = Path(temp_name) / "project.json"
            save_json(config_path, _config())
            exit_code = main(
                ["preflight", str(config_path), "--prompt-password"]
            )

        self.assertEqual(exit_code, 1)
        mocked_run.assert_called_once()
        mocked_clear.assert_called_once_with(SECRET_HOST, SECRET_USER)


if __name__ == "__main__":
    unittest.main()
