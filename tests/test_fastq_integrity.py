from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.fastq_integrity import check_project, main


def _write_gzip(path: Path, content: bytes) -> None:
    path.write_bytes(gzip.compress(content, mtime=0))


def _project(root: Path, r1: str = "sample_R1.fastq.gz", r2: str = "sample_R2.fastq.gz") -> Path:
    path = root / "project.json"
    path.write_text(
        json.dumps(
            {
                "project": {"id": "offline_fixture"},
                "samples": {
                    "local_data_dir": "fastq",
                    "items": [
                        {
                            "sample_id": "sample_1",
                            "fastq_1": r1,
                            "fastq_2": r2,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class FastqIntegrityTests(unittest.TestCase):
    def test_valid_pair_checks_hash_structure_counts_and_normalized_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            fastq = root / "fastq"
            fastq.mkdir()
            r1_content = b"@read-A/1 metadata\nACGTN\n+\nIIIII\n@read-B 1:N:0:1\nTTAA\n+\n####\n"
            r2_content = b"@read-A/2 other\nTGCAN\n+\nIIIII\n@read-B 2:N:0:1\nAATT\n+\n####\n"
            _write_gzip(fastq / "sample_R1.fastq.gz", r1_content)
            _write_gzip(fastq / "sample_R2.fastq.gz", r2_content)

            report = check_project(_project(root))

            self.assertEqual(report["overall"]["status"], "pass")
            self.assertEqual([item["records"] for item in report["files"]], [2, 2])
            self.assertTrue(report["pairs"][0]["read_ids_match"])
            self.assertEqual(
                report["files"][0]["sha256"],
                hashlib.sha256((fastq / "sample_R1.fastq.gz").read_bytes()).hexdigest(),
            )
            rendered = json.dumps(report)
            self.assertNotIn("read-A", rendered)
            self.assertNotIn("ACGTN", rendered)
            self.assertNotIn(str(root), rendered)

    def test_reports_structure_errors_without_read_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            fastq = root / "fastq"
            fastq.mkdir()
            sensitive = b"SENSITIVE_READ_IDENTIFIER"
            bad = b">" + sensitive + b"\nACGTX\nnot-plus\n!!\n"
            mate = b"@mate/2\nACGTA\n+\nIIIII\n"
            _write_gzip(fastq / "sample_R1.fastq.gz", bad)
            _write_gzip(fastq / "sample_R2.fastq.gz", mate)

            report = check_project(_project(root))

            codes = {error["code"] for error in report["files"][0]["errors"]}
            self.assertTrue(
                {"invalid_header", "invalid_plus", "invalid_sequence", "length_mismatch"}
                <= codes
            )
            self.assertEqual(report["overall"]["status"], "fail")
            rendered = json.dumps(report)
            self.assertNotIn(sensitive.decode(), rendered)
            self.assertNotIn("ACGTX", rendered)

    def test_detects_truncated_gzip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            fastq = root / "fastq"
            fastq.mkdir()
            content = b"@read/1\nACGT\n+\nIIII\n"
            compressed = gzip.compress(content, mtime=0)
            (fastq / "sample_R1.fastq.gz").write_bytes(compressed[:-6])
            _write_gzip(fastq / "sample_R2.fastq.gz", content.replace(b"/1", b"/2"))

            report = check_project(_project(root))

            codes = {error["code"] for error in report["files"][0]["errors"]}
            self.assertIn("gzip_error", codes)
            self.assertFalse(report["files"][0]["gzip_valid"])
            self.assertEqual(report["overall"]["status"], "fail")

    def test_detects_pair_record_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            fastq = root / "fastq"
            fastq.mkdir()
            _write_gzip(
                fastq / "sample_R1.fastq.gz",
                b"@read-1/1\nACGT\n+\nIIII\n@read-2/1\nACGT\n+\nIIII\n",
            )
            _write_gzip(
                fastq / "sample_R2.fastq.gz",
                b"@read-1/2\nTGCA\n+\nIIII\n",
            )

            report = check_project(_project(root))

            pair = report["pairs"][0]
            self.assertEqual((pair["r1_records"], pair["r2_records"]), (2, 1))
            self.assertFalse(pair["record_counts_match"])
            self.assertIn("record_count_mismatch", {error["code"] for error in pair["errors"]})

    def test_detects_read_id_mismatch_without_leaking_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            fastq = root / "fastq"
            fastq.mkdir()
            _write_gzip(
                fastq / "sample_R1.fastq.gz",
                b"@PRIVATE_ALPHA/1\nACGT\n+\nIIII\n",
            )
            _write_gzip(
                fastq / "sample_R2.fastq.gz",
                b"@PRIVATE_BETA/2\nTGCA\n+\nIIII\n",
            )

            report = check_project(_project(root))

            pair = report["pairs"][0]
            self.assertFalse(pair["read_ids_match"])
            self.assertIn("read_id_mismatch", {error["code"] for error in pair["errors"]})
            rendered = json.dumps(report)
            self.assertNotIn("PRIVATE_ALPHA", rendered)
            self.assertNotIn("PRIVATE_BETA", rendered)

    def test_rejects_path_escape_and_redacts_configured_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            (root / "fastq").mkdir()

            report = check_project(_project(root, "../secret_R1.fastq.gz"))

            first = report["files"][0]
            self.assertEqual(first["path"], "secret_R1.fastq.gz")
            self.assertIn("unsafe_path", {error["code"] for error in first["errors"]})
            self.assertNotIn("..", json.dumps(report))

    def test_cli_writes_json_and_returns_failure_for_bad_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            fastq = root / "fastq"
            fastq.mkdir()
            _write_gzip(fastq / "sample_R1.fastq.gz", b"@one/1\nAC\n+\nII\n")
            _write_gzip(fastq / "sample_R2.fastq.gz", b"@two/2\nGT\n+\nII\n")
            output = root / "integrity.json"

            return_code = main([str(_project(root)), "--output", str(output)])

            self.assertEqual(return_code, 1)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["overall"]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
