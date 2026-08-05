from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rnaseq_agent.report import generate_report
from rnaseq_agent.storage import save_json


def _config(status: dict[str, str]) -> dict:
    return {
        "project": {"id": "report_test", "title": "Report test"},
        "server": {
            "remote_workdir": "/remote/projects/report_test",
            "scheduler": "local",
            "threads": 4,
            "memory_gb": 8,
        },
        "samples": {"items": []},
        "status": status,
    }


def _write_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


class ReportAttemptTests(unittest.TestCase):
    def test_current_attempt_artifacts_are_used_instead_of_legacy_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            project_dir = Path(temp_name)
            run_id = "20260804T120000Z-a1b2c3d4"
            remote_run_workdir = f"/remote/projects/report_test/attempts/{run_id}"
            config_path = project_dir / "project.json"
            save_json(
                config_path,
                _config(
                    {
                        "state": "completed",
                        "run_id": run_id,
                        "remote_run_workdir": remote_run_workdir,
                    }
                ),
            )

            _write_jsonl(
                project_dir / "agent_logs" / "events.jsonl",
                {"timestamp": "old", "event": "legacy_event"},
            )
            legacy_file = project_dir / "downloads" / "extracted" / "legacy.txt"
            legacy_file.parent.mkdir(parents=True, exist_ok=True)
            legacy_file.write_text("legacy", encoding="utf-8")

            attempt_dir = project_dir / "attempts" / run_id
            _write_jsonl(
                attempt_dir / "agent_logs" / "events.jsonl",
                {"timestamp": "new", "event": "attempt_event"},
            )
            result_file = attempt_dir / "downloads" / "extracted" / "counts.tsv"
            result_file.parent.mkdir(parents=True, exist_ok=True)
            result_file.write_text("gene\tcount\n", encoding="utf-8")

            report = generate_report(config_path).read_text(encoding="utf-8")

            self.assertIn(f"- Run ID: {run_id}", report)
            self.assertIn(f"- Local attempt directory: {attempt_dir}", report)
            self.assertIn(f"- Remote run workdir: {remote_run_workdir}", report)
            self.assertIn("attempt_event", report)
            self.assertIn("counts.tsv", report)
            self.assertNotIn("legacy_event", report)
            self.assertNotIn("legacy.txt", report)

    def test_legacy_project_without_run_id_uses_top_level_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            project_dir = Path(temp_name)
            config_path = project_dir / "project.json"
            save_json(config_path, _config({"state": "completed"}))
            _write_jsonl(
                project_dir / "agent_logs" / "events.jsonl",
                {"timestamp": "legacy", "event": "legacy_event"},
            )
            result_file = project_dir / "downloads" / "extracted" / "legacy.txt"
            result_file.parent.mkdir(parents=True, exist_ok=True)
            result_file.write_text("legacy", encoding="utf-8")

            report = generate_report(config_path).read_text(encoding="utf-8")

            self.assertIn("- Run ID: Not recorded (legacy layout)", report)
            self.assertIn(f"- Local attempt directory: {project_dir}", report)
            self.assertIn("- Remote run workdir: /remote/projects/report_test", report)
            self.assertIn("legacy_event", report)
            self.assertIn("legacy.txt", report)


if __name__ == "__main__":
    unittest.main()
