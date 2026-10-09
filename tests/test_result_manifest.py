from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rnaseq_agent.pipeline import STAGE_COUNTS
import pytest

from rnaseq_agent.result_manifest import create_result_manifest, write_qc_verdict
from rnaseq_agent.storage import load_json


def _config() -> dict:
    return {
        "project": {"id": "result_test"},
        "run": {"id": "run-1"},
        "execution": {"mode": "contract"},
        "sequencing": {"layout": "paired"},
        "samples": {"items": [{"sample_id": "sample_1"}]},
        "pipeline": {
            "fastp": {"enabled": False},
            "star": {"enabled": True},
            "arriba": {"enabled": False},
            "featurecounts": {"enabled": True},
            "rsem": {"enabled": False},
        },
    }


def _write(root: Path, relative_path: str, content: bytes) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class ResultManifestTests(unittest.TestCase):
    def test_write_qc_verdict_requires_and_persists_attempt_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            manifest_path = root / "result_manifest.json"
            manifest_path.write_text(
                '{"schema_version": 1, "body": {"run_id": "run-1"}}\n',
                encoding="utf-8",
            )

            verdict = {"status": "abstain", "de_readiness": "blocked", "dimensions": {}}
            payload = write_qc_verdict(manifest_path, verdict, run_id="run-1")

            self.assertEqual(payload["body"]["qc_verdict"]["run_id"], "run-1")
            self.assertEqual(payload["body"]["qc_verdict"]["attempt_id"], "run-1")

    def test_write_qc_verdict_rejects_missing_or_mismatched_attempt_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            manifest_path = root / "result_manifest.json"
            manifest_path.write_text(
                '{"schema_version": 1, "body": {"run_id": "run-1"}}\n',
                encoding="utf-8",
            )
            verdict = {"status": "pass", "de_readiness": "ready", "dimensions": {}}

            with pytest.raises(ValueError, match="run_id"):
                write_qc_verdict(manifest_path, verdict)
            with pytest.raises(ValueError, match="does not match"):
                write_qc_verdict(manifest_path, verdict, run_id="run-2")

    def test_result_manifest_records_qc_verdict_without_changing_audit_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            extracted = root / "extracted"
            _write(extracted, "status/completed.flag", b"")
            _write(extracted, "status/state.txt", b"completed\n")
            verdict = {"status": "warn", "de_readiness": "caution"}

            summary = create_result_manifest(
                _config(), extracted, root / "result_manifest.json", qc_verdict=verdict
            )
            payload = load_json(summary.path)

            self.assertEqual(payload["body"]["qc_verdict"]["status"], verdict["status"])
            self.assertEqual(payload["body"]["qc_verdict"]["run_id"], "run-1")
            self.assertEqual(payload["body"]["qc_verdict"]["attempt_id"], "run-1")
            self.assertIn("audit", payload["body"])

    def test_result_manifest_contains_audited_envelope_and_artifact_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            extracted = root / "extracted"
            _write(extracted, "status/completed.flag", b"")
            _write(extracted, "status/state.txt", b"completed\n")
            _write(extracted, "star/sample_1.Aligned.sortedByCoord.out.bam", b"bam")
            _write(extracted, "star/sample_1.Log.final.out", b"mapped\n")
            _write(extracted, "featurecounts/gene_counts.txt", b"gene\tcount\nA\t1\n")
            _write(extracted, "featurecounts/gene_counts.txt.summary", b"Assigned\t1\n")

            summary = create_result_manifest(_config(), extracted, root / "result_manifest.json")
            payload = load_json(summary.path)

            self.assertEqual(payload["schema_version"], 1)
            self.assertIn("audit", payload["body"])
            self.assertTrue((root / "artifact_index.json").is_file())
    def test_counts_stage_requires_only_enabled_conditional_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            extracted = root / "extracted"
            config = _config()
            config["samples"]["items"] = []
            config["pipeline"] = {
                "diffexp": {"enabled": True},
                "cms": {"enabled": False},
            }
            _write(extracted, "status/completed.flag", b"")
            _write(extracted, "status/state.txt", b"completed\n")
            _write(extracted, "diffexp/deseq2_results.tsv", b"gene\tpadj\nG1\t0.01\n")
            _write(extracted, "diffexp/deseq2_summary.json", b"{}\n")

            summary = create_result_manifest(
                config,
                extracted,
                root / "result_manifest.json",
                stage=STAGE_COUNTS,
            )

            self.assertTrue(summary.ok, summary.errors)

    def test_hashes_files_and_accepts_complete_expected_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            extracted = root / "extracted"
            _write(extracted, "status/completed.flag", b"")
            _write(extracted, "status/state.txt", b"completed\n")
            _write(extracted, "star/sample_1.Aligned.sortedByCoord.out.bam", b"bam")
            _write(extracted, "star/sample_1.Log.final.out", b"mapped\n")
            _write(extracted, "featurecounts/gene_counts.txt", b"gene\tcount\nA\t1\n")
            _write(extracted, "featurecounts/gene_counts.txt.summary", b"Assigned\t1\n")

            summary = create_result_manifest(_config(), extracted, root / "result_manifest.json")
            payload = load_json(summary.path)

            self.assertTrue(summary.ok)
            self.assertEqual(summary.errors, [])
            self.assertEqual(len(payload["body"]["files"]), 6)
            self.assertEqual(payload["body"]["fingerprints"]["files_sha256"], summary.files_sha256)
            self.assertTrue(payload["manifest_id"].startswith("sha256:"))

    def test_reports_missing_empty_and_noncompleted_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            extracted = root / "extracted"
            _write(extracted, "status/completed.flag", b"")
            _write(extracted, "status/state.txt", b"failed\n")
            _write(extracted, "star/sample_1.Aligned.sortedByCoord.out.bam", b"")

            summary = create_result_manifest(_config(), extracted, root / "result_manifest.json")

            self.assertFalse(summary.ok)
            joined = "\n".join(summary.errors)
            self.assertIn("Required result is empty: star/sample_1.Aligned.sortedByCoord.out.bam", joined)
            self.assertIn("Required result is missing: star/sample_1.Log.final.out", joined)
            self.assertIn("Required result is missing: featurecounts/gene_counts.txt", joined)
            self.assertIn("does not contain 'completed'", joined)


if __name__ == "__main__":
    unittest.main()
