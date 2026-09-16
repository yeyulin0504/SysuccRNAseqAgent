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


class RemotePrestagedValidationTests(unittest.TestCase):
    """``remote_path`` 项目：reads 在服务器上，本地不该去找它们。

    缺陷复现（2026-09-16）：用户把服务器目录

        /hwdata/home/yeyulin/.../runs/fastq_pair_test/fastq

    连同 4 个样本贴进对话、说「你帮我执行」，配置确实写盘了，但回复里紧跟
    8 条 ``不适用：缺少输入文件：F:\\...\\outputs\\mvp_demo_data\\SRR...``。
    根因是 ``validate_local_fastqs`` 不区分数据来源，一律拿 ``local_data_dir``
    去拼样本里的文件名（那些文件名其实只在服务器上存在）。UI 的
    「应用样本表」按钮走同一条链路，同样中招，所以这个测试锁的是两条路径
    共同的底层校验。
    """

    def _remote_config(self, root: Path, **overrides: object) -> dict:
        config = _config(root)
        config["samples"] = {
            "source": "remote_path",
            "local_data_dir": str(root / "not_used_locally"),
            "remote_data_dir": "/hwdata/home/yeyulin/demo_fastq",
            "remote_prestaged": True,
            "items": [
                {"sample_id": "SRR1", "condition": "control", "fastq_1": "SRR1_1.fastq.gz", "fastq_2": "SRR1_2.fastq.gz"},
                {"sample_id": "SRR2", "condition": "treat", "fastq_1": "SRR2_1.fastq.gz", "fastq_2": "SRR2_2.fastq.gz"},
            ],
        }
        config.update(overrides)
        return config

    def test_remote_reads_are_not_reported_as_missing_locally(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)  # 本地一个 FASTQ 都没有，理应通过。

            result = validate_local_fastqs(self._remote_config(root))

            self.assertTrue(result.ok, result.errors)
            self.assertEqual(result.missing_files, [])
            self.assertEqual(result.checked_files, 0)

    def test_source_field_alone_is_enough(self) -> None:
        """``source == "remote_path"`` 单独出现也必须生效（早期配置没有标记位）。"""
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = self._remote_config(root)
            del config["samples"]["remote_prestaged"]

            result = validate_local_fastqs(config)

            self.assertTrue(result.ok, result.errors)
            self.assertEqual(result.missing_files, [])

    def test_sample_metadata_is_still_validated(self) -> None:
        """跳过本地文件不等于不校验样本表：缺 fastq_1 仍要报错。"""
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = self._remote_config(root)
            config["samples"]["items"][0]["fastq_1"] = ""

            result = validate_local_fastqs(config)

            self.assertFalse(result.ok)
            self.assertTrue(
                any("fastq_1" in error for error in result.errors), result.errors
            )

    def test_pipeline_errors_are_still_surfaced(self) -> None:
        """跳过本地文件后，pipeline/container 级错误不能跟着一起被跳过。"""
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            config = self._remote_config(root)
            for step in config["pipeline"].values():
                step["enabled"] = False

            result = validate_local_fastqs(config)

            self.assertFalse(result.ok)
            self.assertIn("At least one pipeline step must be enabled.", result.errors)

    def test_local_upload_still_reports_missing_files(self) -> None:
        """反向守卫：本地项目缺文件必须照旧报出来，不能顺手放过。"""
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)

            result = validate_local_fastqs(_config(root))

            self.assertFalse(result.ok)
            self.assertEqual(len(result.missing_files), 2)


if __name__ == "__main__":
    unittest.main()
