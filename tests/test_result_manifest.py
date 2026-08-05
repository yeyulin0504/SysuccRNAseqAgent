from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rnaseq_agent.result_manifest import create_result_manifest
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
