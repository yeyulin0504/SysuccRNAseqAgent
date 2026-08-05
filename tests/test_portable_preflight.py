from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from rnaseq_agent.portable_preflight import _publish_fresh_report, _write_debug_log


def _load_exporter():
    source = Path(__file__).resolve().parents[1] / "packaging/export_preflight_config.py"
    spec = importlib.util.spec_from_file_location("portable_config_exporter", source)
    if spec is None or spec.loader is None:
        raise AssertionError("Could not load portable config exporter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PortablePreflightTests(unittest.TestCase):
    def test_export_excludes_fastq_samples_password_and_init_commands(self) -> None:
        exporter = _load_exporter()
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = root / "project.json"
            destination = root / "preflight_project.json"
            source.write_text(
                json.dumps(
                    {
                        "server": {
                            "host": "internal.example",
                            "port": None,
                            "user": "private-user",
                            "remote_workdir": "/private/work",
                            "scheduler": "slurm",
                            "init_commands": ["touch SHOULD_NOT_EXPORT"],
                            "password": "MUST_NOT_EXPORT",
                        },
                        "reference": {"star_index_dir": "/private/ref/star"},
                        "container": {
                            "enabled": True,
                            "engine": "apptainer",
                            "image_path": "/private/containers/rnaseq.sif",
                            "bind_paths": ["/private/ref"],
                        },
                        "pipeline": {"star": {"enabled": True, "version": "2.7"}},
                        "samples": {
                            "local_data_dir": "C:/private/fastq",
                            "items": [
                                {
                                    "sample_id": "PRIVATE_SAMPLE",
                                    "fastq_1": "PRIVATE_R1.fastq.gz",
                                    "fastq_2": "PRIVATE_R2.fastq.gz",
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = exporter.export_preflight_config(source, destination)
            serialized = destination.read_text(encoding="utf-8")

        self.assertEqual(payload["server"]["host"], "internal.example")
        self.assertEqual(payload["server"]["port"], 22)
        self.assertEqual(payload["container"]["engine"], "apptainer")
        self.assertEqual(payload["container"]["image_path"], "/private/containers/rnaseq.sif")
        self.assertNotIn("samples", payload)
        for secret in (
            "PRIVATE_SAMPLE",
            "PRIVATE_R1",
            "C:/private/fastq",
            "MUST_NOT_EXPORT",
            "SHOULD_NOT_EXPORT",
        ):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("init_commands", payload["server"])
        self.assertFalse(payload["server_policy"]["init_commands_included"])

    def test_fresh_report_replaces_destination_and_preserves_old_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            destination = root / "server_preflight.json"
            candidate = root / ".pending.json"
            destination.write_text('{"old": true}\n', encoding="utf-8")
            candidate.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "generated_at": "2026-08-04T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            published = _publish_fresh_report(candidate, destination)
            backups = list(root.glob("server_preflight.json.previous-*"))

            self.assertEqual(published, destination)
            self.assertFalse(candidate.exists())
            self.assertEqual(len(backups), 1)
            self.assertTrue(json.loads(backups[0].read_text())["old"])
            self.assertEqual(json.loads(destination.read_text())["schema_version"], 1)

    def test_debug_log_records_sanitized_failure_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            destination = Path(temp_name) / "server_preflight.json"

            debug_path = _write_debug_log(
                destination,
                "Read-only server preflight failed (TypeError: sanitized)",
            )

            payload = debug_path.read_text(encoding="utf-8")
            self.assertEqual(debug_path.name, "preflight_debug.txt")
            self.assertIn("TypeError", payload)
            self.assertIn("password_saved=false", payload)
            self.assertIn("fastq_or_sample_data_accessed=false", payload)


if __name__ == "__main__":
    unittest.main()
