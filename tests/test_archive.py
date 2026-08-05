from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from rnaseq_agent.archive import UnsafeArchiveError, safe_extract_tar, safe_extract_tar_gz


def _write_archive(path: Path, members: list[tuple[str, bytes, str]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "../outside.txt"
                archive.addfile(info)
            else:
                raise AssertionError(kind)


class SafeArchiveTests(unittest.TestCase):
    def test_extracts_plain_tar_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            archive_path = root / "results.tar"
            with tarfile.open(archive_path, "w") as archive:
                content = b"public benchmark\n"
                info = tarfile.TarInfo("fastq/readme.txt")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

            extracted = safe_extract_tar(archive_path, root / "extracted")

            self.assertEqual(len(extracted), 1)
            self.assertEqual((root / "extracted/fastq/readme.txt").read_bytes(), content)

    def test_extracts_regular_result_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            archive = root / "results.tar.gz"
            _write_archive(
                archive,
                [
                    ("featurecounts/gene_counts.txt", b"gene\tcount\nA\t3\n", "file"),
                    ("logs/run.log", b"ok\n", "file"),
                ],
            )

            extracted = safe_extract_tar_gz(archive, root / "extracted")

            self.assertEqual(len(extracted), 2)
            self.assertEqual(
                (root / "extracted/featurecounts/gene_counts.txt").read_text(),
                "gene\tcount\nA\t3\n",
            )

    def test_rejects_parent_path_before_writing_any_member(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            archive = root / "results.tar.gz"
            _write_archive(
                archive,
                [
                    ("logs/first.txt", b"must-not-be-written", "file"),
                    ("../outside.txt", b"escape", "file"),
                ],
            )

            with self.assertRaises(UnsafeArchiveError):
                safe_extract_tar_gz(archive, root / "extracted")

            self.assertFalse((root / "outside.txt").exists())
            self.assertFalse((root / "extracted/logs/first.txt").exists())

    def test_rejects_absolute_windows_and_backslash_paths(self) -> None:
        for unsafe_name in ("/tmp/outside", "C:/outside.txt", "..\\outside.txt"):
            with self.subTest(unsafe_name=unsafe_name), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                archive = root / "results.tar.gz"
                _write_archive(archive, [(unsafe_name, b"escape", "file")])
                with self.assertRaises(UnsafeArchiveError):
                    safe_extract_tar_gz(archive, root / "extracted")

    def test_rejects_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            archive = root / "results.tar.gz"
            _write_archive(archive, [("results-link", b"", "symlink")])

            with self.assertRaises(UnsafeArchiveError):
                safe_extract_tar_gz(archive, root / "extracted")


if __name__ == "__main__":
    unittest.main()
