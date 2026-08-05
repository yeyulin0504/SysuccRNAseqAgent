from __future__ import annotations

import gzip
import tempfile
import unittest
from pathlib import Path

from rnaseq_agent.validation import validate_local_fastqs


def _write_fastq(path: Path, records: list[tuple[str, str]]) -> None:
    with gzip.open(path, "wt", encoding="ascii") as handle:
        for read_id, sequence in records:
            handle.write(f"@{read_id}\n{sequence}\n+\n{'I' * len(sequence)}\n")


def _config(root: Path) -> dict:
    return {
        "project": {"id": "validation_test"},
        "sequencing": {"layout": "paired"},
        "samples": {
            "local_data_dir": str(root),
            "remote_data_dir": "/remote/raw",
            "items": [
                {
                    "sample_id": "sample_1",
                    "fastq_1": "sample_R1.fastq.gz",
                    "fastq_2": "sample_R2.fastq.gz",
                }
            ],
        },
        "server": {"remote_workdir": "/remote/project"},
        "pipeline": {
            "fastp": {"enabled": True},
            "star": {"enabled": False},
            "arriba": {"enabled": False},
            "featurecounts": {"enabled": False},
            "rsem": {"enabled": False},
        },
        "reference": {},
    }


class FastqValidationTests(unittest.TestCase):
    def test_accepts_complete_paired_fastq(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            records = [("read1", "ACGT"), ("read2", "TGCA")]
            _write_fastq(root / "sample_R1.fastq.gz", records)
            _write_fastq(root / "sample_R2.fastq.gz", records)

            result = validate_local_fastqs(_config(root))

            self.assertTrue(result.ok)
            self.assertEqual(result.checked_files, 2)

    def test_rejects_truncated_gzip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            records = [("read1", "ACGT"), ("read2", "TGCA")]
            r1 = root / "sample_R1.fastq.gz"
            _write_fastq(r1, records)
            _write_fastq(root / "sample_R2.fastq.gz", records)
            r1.write_bytes(r1.read_bytes()[:-8])

            result = validate_local_fastqs(_config(root))

            self.assertFalse(result.ok)
            self.assertTrue(
                any("损坏或截断" in error for error in result.errors)
            )

    def test_rejects_mismatched_read_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            _write_fastq(root / "sample_R1.fastq.gz", [("read1", "ACGT")])
            _write_fastq(root / "sample_R2.fastq.gz", [("other", "ACGT")])

            result = validate_local_fastqs(_config(root))

            self.assertFalse(result.ok)
            self.assertTrue(any("read ID" in error and "不一致" in error for error in result.errors))

    def test_rejects_different_pair_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            _write_fastq(
                root / "sample_R1.fastq.gz",
                [("read1", "ACGT"), ("read2", "TGCA")],
            )
            _write_fastq(root / "sample_R2.fastq.gz", [("read1", "ACGT")])

            result = validate_local_fastqs(_config(root))

            self.assertFalse(result.ok)
            self.assertTrue(
                any("reads 数量不一致" in error for error in result.errors)
            )

    def test_rejects_reusing_the_same_fastq_for_both_mates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            _write_fastq(root / "sample_R1.fastq.gz", [("read1", "ACGT")])
            config = _config(root)
            config["samples"]["items"][0]["fastq_2"] = "sample_R1.fastq.gz"

            result = validate_local_fastqs(config)

            self.assertFalse(result.ok)
            self.assertTrue(any("FASTQ file is reused" in error for error in result.errors))

    def test_rejects_pipeline_with_no_enabled_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            records = [("read1", "ACGT")]
            _write_fastq(root / "sample_R1.fastq.gz", records)
            _write_fastq(root / "sample_R2.fastq.gz", records)
            config = _config(root)
            for step in config["pipeline"].values():
                step["enabled"] = False

            result = validate_local_fastqs(config)

            self.assertFalse(result.ok)
            self.assertIn("At least one pipeline step must be enabled.", result.errors)

    def test_rejects_enabled_container_without_image_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            records = [("read1", "ACGT")]
            _write_fastq(root / "sample_R1.fastq.gz", records)
            _write_fastq(root / "sample_R2.fastq.gz", records)
            config = _config(root)
            config["container"] = {"enabled": True, "engine": "apptainer", "image_path": ""}

            result = validate_local_fastqs(config)

            self.assertFalse(result.ok)
            self.assertIn("Missing container.image_path for Apptainer execution.", result.errors)


if __name__ == "__main__":
    unittest.main()
