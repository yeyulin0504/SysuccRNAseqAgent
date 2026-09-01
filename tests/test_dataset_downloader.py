from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.download_dataset import download_dataset


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_spec(
    path: Path,
    *,
    source: Path,
    content: bytes,
    relative_path: str = "fastq/sample_R1.fastq.gz",
    expected_content: bytes | None = None,
) -> None:
    expected = content if expected_content is None else expected_content
    spec = {
        "schema_version": 1,
        "dataset_id": "local_fixture_v1",
        "files": [
            {
                "path": relative_path,
                "size_bytes": len(expected),
                "sha256": _sha256_bytes(expected),
                "url": source.resolve().as_uri(),
            }
        ],
    }
    path.write_text(json.dumps(spec), encoding="utf-8")


def _add_archive_members(
    spec_path: Path,
    *,
    root: str,
    members: list[tuple[str, bytes]],
) -> None:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["archive_members_root"] = root
    spec["archive_members"] = [
        {
            "path": relative_path,
            "size_bytes": len(content),
            "sha256": _sha256_bytes(content),
        }
        for relative_path, content in members
    ]
    spec_path.write_text(json.dumps(spec), encoding="utf-8")


class DatasetDownloaderTests(unittest.TestCase):
    def test_downloads_verifies_and_writes_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            content = b"offline public fixture\n"
            source = root / "source.bin"
            source.write_bytes(content)
            spec_path = root / "dataset.json"
            _write_spec(spec_path, source=source, content=content)
            destination = root / "dataset"

            lock_path = download_dataset(spec_path, destination, verify_only=False)

            self.assertEqual(
                (destination / "fastq/sample_R1.fastq.gz").read_bytes(),
                content,
            )
            self.assertEqual(lock_path, destination / "dataset.lock.json")
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(lock["dataset_id"], "local_fixture_v1")
            self.assertEqual(lock["source_spec"], "dataset.json")
            self.assertFalse(lock["absolute_source_spec_path_saved"])
            self.assertNotIn(str(root), json.dumps(lock))
            self.assertEqual(lock["dataset_spec_sha256"], _sha256_bytes(spec_path.read_bytes()))
            self.assertEqual(
                lock["files"],
                [
                    {
                        "path": "fastq/sample_R1.fastq.gz",
                        "size_bytes": len(content),
                        "sha256": _sha256_bytes(content),
                    }
                ],
            )

    def test_verify_only_uses_existing_file_without_opening_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            content = b"already downloaded\n"
            source = root / "source.bin"
            source.write_bytes(content)
            spec_path = root / "dataset.json"
            _write_spec(spec_path, source=source, content=content)
            destination = root / "dataset"
            target = destination / "fastq/sample_R1.fastq.gz"
            target.parent.mkdir(parents=True)
            target.write_bytes(content)

            with patch("benchmarks.download_dataset.urllib.request.urlopen") as urlopen:
                lock_path = download_dataset(spec_path, destination, verify_only=True)

            urlopen.assert_not_called()
            self.assertTrue(lock_path.is_file())

    def test_verify_only_rejects_missing_file_without_opening_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            content = b"not downloaded\n"
            source = root / "source.bin"
            source.write_bytes(content)
            spec_path = root / "dataset.json"
            _write_spec(spec_path, source=source, content=content)

            with patch("benchmarks.download_dataset.urllib.request.urlopen") as urlopen:
                with self.assertRaisesRegex(RuntimeError, "missing"):
                    download_dataset(spec_path, root / "dataset", verify_only=True)

            urlopen.assert_not_called()

    def test_existing_checksum_mismatch_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            expected = b"expected bytes\n"
            source = root / "source.bin"
            source.write_bytes(expected)
            spec_path = root / "dataset.json"
            _write_spec(spec_path, source=source, content=expected)
            destination = root / "dataset"
            target = destination / "fastq/sample_R1.fastq.gz"
            target.parent.mkdir(parents=True)
            existing = b"keep this existing file"
            target.write_bytes(existing)

            with patch("benchmarks.download_dataset.urllib.request.urlopen") as urlopen:
                with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
                    download_dataset(spec_path, destination, verify_only=False)

            urlopen.assert_not_called()
            self.assertEqual(target.read_bytes(), existing)

    def test_bad_download_never_reaches_final_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            downloaded = b"wrong downloaded bytes"
            expected = b"expected bytes"
            source = root / "source.bin"
            source.write_bytes(downloaded)
            spec_path = root / "dataset.json"
            _write_spec(
                spec_path,
                source=source,
                content=downloaded,
                expected_content=expected,
            )
            destination = root / "dataset"
            target = destination / "fastq/sample_R1.fastq.gz"

            with self.assertRaisesRegex(RuntimeError, "failed verification"):
                download_dataset(spec_path, destination, verify_only=False)

            self.assertFalse(target.exists())
            self.assertFalse(target.with_name(target.name + ".part").exists())

    def test_rejects_path_escape_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            content = b"escape attempt"
            source = root / "source.bin"
            source.write_bytes(content)
            spec_path = root / "dataset.json"
            _write_spec(
                spec_path,
                source=source,
                content=content,
                relative_path="../outside.bin",
            )
            destination = root / "dataset"

            with patch("benchmarks.download_dataset.urllib.request.urlopen") as urlopen:
                with self.assertRaisesRegex(ValueError, "Unsafe dataset relative path"):
                    download_dataset(spec_path, destination, verify_only=False)

            urlopen.assert_not_called()
            self.assertFalse((root / "outside.bin").exists())

    def test_verifies_extracted_archive_members_and_records_them_in_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            archive = b"fixture archive bytes"
            source = root / "source.tar"
            source.write_bytes(archive)
            spec_path = root / "dataset.json"
            _write_spec(
                spec_path,
                source=source,
                content=archive,
                relative_path="source/reads.tar",
            )
            member_1 = ("sample_1_R1.fastq.gz", b"read one")
            member_2 = ("nested/sample_1_R2.fastq.gz", b"read two")
            _add_archive_members(
                spec_path,
                root="fastq",
                members=[member_1, member_2],
            )
            destination = root / "dataset"
            download_dataset(spec_path, destination, verify_only=False)
            for relative_path, content in (member_1, member_2):
                member_path = destination / "fastq" / relative_path
                member_path.parent.mkdir(parents=True, exist_ok=True)
                member_path.write_bytes(content)

            with patch("benchmarks.download_dataset.urllib.request.urlopen") as urlopen:
                lock_path = download_dataset(
                    spec_path,
                    destination,
                    verify_only=True,
                    verify_archive_members=True,
                )

            urlopen.assert_not_called()

            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            self.assertEqual(lock["archive_members_root"], "fastq")
            self.assertEqual(
                lock["archive_members"],
                [
                    {
                        "path": relative_path,
                        "size_bytes": len(content),
                        "sha256": _sha256_bytes(content),
                    }
                    for relative_path, content in (member_1, member_2)
                ],
            )

    def test_archive_member_verification_does_not_extract_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            archive = b"not a real tar, and must not be extracted"
            source = root / "source.tar"
            source.write_bytes(archive)
            spec_path = root / "dataset.json"
            _write_spec(
                spec_path,
                source=source,
                content=archive,
                relative_path="source/reads.tar",
            )
            _add_archive_members(
                spec_path,
                root="fastq",
                members=[("sample_R1.fastq.gz", b"expected member")],
            )
            destination = root / "dataset"

            with self.assertRaisesRegex(RuntimeError, "archive member.*missing"):
                download_dataset(
                    spec_path,
                    destination,
                    verify_only=False,
                    verify_archive_members=True,
                )

            self.assertFalse((destination / "fastq").exists())
            self.assertTrue((destination / "source/reads.tar").is_file())
            self.assertFalse((destination / "dataset.lock.json").exists())

    def test_archive_member_path_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            content = b"archive"
            source = root / "source.tar"
            source.write_bytes(content)
            spec_path = root / "dataset.json"
            _write_spec(spec_path, source=source, content=content)
            _add_archive_members(
                spec_path,
                root="fastq",
                members=[("../outside.fastq.gz", b"escape")],
            )

            with self.assertRaisesRegex(ValueError, "Unsafe dataset relative path"):
                download_dataset(
                    spec_path,
                    root / "dataset",
                    verify_only=False,
                    verify_archive_members=True,
                )

            self.assertFalse((root / "outside.fastq.gz").exists())


if __name__ == "__main__":
    unittest.main()
