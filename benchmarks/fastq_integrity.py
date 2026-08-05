"""Offline, content-redacted integrity checks for project FASTQ files.

This module intentionally uses only the Python standard library.  It validates
the compressed stream and basic FASTQ structure while ensuring that read names,
sequences, quality strings, absolute paths, and raw exception text are never
included in its JSON report.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, BinaryIO, Sequence


TOOL_VERSION = "1.0.0"
MAX_FASTQ_LINE_BYTES = 16 * 1024 * 1024

_SEQUENCE_ALPHABET = frozenset(
    b"ACGTUNRYSWKMBDHVacgtunryswkmbdhv"
)

_FILE_ERROR_MESSAGES = {
    "unsafe_path": "The configured FASTQ path is not a safe relative path.",
    "missing_file": "The configured FASTQ file is missing.",
    "not_regular_file": "The configured FASTQ path is not a regular file.",
    "metadata_error": "File metadata or checksum could not be read.",
    "gzip_error": "The gzip stream is invalid or incomplete.",
    "incomplete_record": "The FASTQ stream ends inside a four-line record.",
    "line_too_long": "A FASTQ line exceeds the integrity-check limit.",
    "invalid_header": "A FASTQ header does not begin with '@' or has no read ID.",
    "invalid_plus": "A FASTQ separator line does not begin with '+'.",
    "invalid_sequence": "A sequence is empty or contains unsupported characters.",
    "invalid_quality": "A quality string contains characters outside printable Phred ASCII.",
    "length_mismatch": "A sequence and its quality string have different lengths.",
    "empty_file": "The FASTQ stream contains no complete records.",
}

_PAIR_ERROR_MESSAGES = {
    "file_validation_failed": "At least one FASTQ file failed integrity validation.",
    "record_count_mismatch": "R1 and R2 contain different numbers of complete records.",
    "read_id_mismatch": "At least one paired R1/R2 read ID differs.",
    "read_ids_unverifiable": "All paired read IDs could not be verified.",
}


class FastqIntegrityConfigError(ValueError):
    """Raised when a project cannot be interpreted without touching FASTQ data."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_report_label(configured_name: str) -> str:
    """Return a project-relative label without exposing an absolute/path-escape value."""

    normalized = configured_name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".", "..")]
    if not parts:
        return "<invalid-fastq-path>"
    if normalized.startswith("/") or ".." in normalized.split("/"):
        return parts[-1]
    return "/".join(parts)


def _add_file_error(report: dict[str, Any], code: str) -> None:
    errors = report["errors"]
    if any(error["code"] == code for error in errors):
        return
    errors.append({"code": code, "message": _FILE_ERROR_MESSAGES[code]})


def _add_pair_error(report: dict[str, Any], code: str) -> None:
    errors = report["errors"]
    if any(error["code"] == code for error in errors):
        return
    errors.append({"code": code, "message": _PAIR_ERROR_MESSAGES[code]})


class _FileState:
    def __init__(self, data_root: Path, configured_name: str):
        self.label = _safe_report_label(configured_name)
        self.path: Path | None = None
        self.stream: BinaryIO | None = None
        self.ended_cleanly = False
        self.report: dict[str, Any] = {
            "path": self.label,
            "size_bytes": None,
            "sha256": None,
            "records": None,
            "gzip_valid": False,
            "fastq_valid": False,
            "status": "fail",
            "errors": [],
        }

        relative = Path(configured_name)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            _add_file_error(self.report, "unsafe_path")
            return

        try:
            root = data_root.resolve()
            candidate = (root / relative).resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            _add_file_error(self.report, "unsafe_path")
            return

        self.path = candidate
        try:
            if not candidate.exists():
                _add_file_error(self.report, "missing_file")
                return
            if not candidate.is_file():
                _add_file_error(self.report, "not_regular_file")
                return
            self.report["size_bytes"] = candidate.stat().st_size
            self.report["sha256"] = _sha256(candidate)
            self.report["records"] = 0
        except OSError:
            _add_file_error(self.report, "metadata_error")
            return

        try:
            self.stream = gzip.open(candidate, "rb")
        except (OSError, gzip.BadGzipFile):
            _add_file_error(self.report, "gzip_error")

    @property
    def can_read(self) -> bool:
        return self.stream is not None and not self.ended_cleanly

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                _add_file_error(self.report, "gzip_error")
            self.stream = None

    def finalize(self) -> dict[str, Any]:
        self.close()
        if self.report["records"] == 0 and not self.report["errors"]:
            _add_file_error(self.report, "empty_file")
        self.report["gzip_valid"] = self.ended_cleanly
        self.report["fastq_valid"] = self.ended_cleanly and not self.report["errors"]
        self.report["status"] = "pass" if self.report["fastq_valid"] else "fail"
        return self.report


def _readline_capped(stream: BinaryIO) -> tuple[bytes, bool]:
    """Read one logical line with bounded retained data.

    Oversize data are drained in bounded chunks but are not returned, preventing
    accidental propagation into either memory-heavy diagnostics or the report.
    """

    line = stream.readline(MAX_FASTQ_LINE_BYTES + 1)
    if len(line) <= MAX_FASTQ_LINE_BYTES:
        return line, False

    while line and not line.endswith(b"\n"):
        line = stream.readline(MAX_FASTQ_LINE_BYTES + 1)
    return b"", True


def _normalized_read_id(header: bytes) -> bytes | None:
    if not header.startswith(b"@"):
        return None
    body = header[1:].split(None, 1)[0]
    if body.endswith((b"/1", b"/2")):
        body = body[:-2]
    return body or None


def _read_record(state: _FileState) -> tuple[str, bytes | None]:
    """Read and validate one record; return (record|eof|error, normalized ID)."""

    if not state.can_read or state.stream is None:
        return "error", None

    try:
        header, header_long = _readline_capped(state.stream)
        if header == b"" and not header_long:
            state.ended_cleanly = True
            return "eof", None

        sequence, sequence_long = _readline_capped(state.stream)
        plus, plus_long = _readline_capped(state.stream)
        quality, quality_long = _readline_capped(state.stream)
    except (EOFError, OSError, gzip.BadGzipFile):
        _add_file_error(state.report, "gzip_error")
        state.close()
        return "error", None

    if header_long or sequence_long or plus_long or quality_long:
        _add_file_error(state.report, "line_too_long")

    if sequence == b"" or plus == b"" or quality == b"":
        _add_file_error(state.report, "incomplete_record")
        # Force one final read so gzip CRC/truncation is still checked when possible.
        try:
            while state.stream.read(1024 * 1024):
                pass
            state.ended_cleanly = True
        except (EOFError, OSError, gzip.BadGzipFile):
            _add_file_error(state.report, "gzip_error")
        state.close()
        return "error", None

    header = header.rstrip(b"\r\n")
    sequence = sequence.rstrip(b"\r\n")
    plus = plus.rstrip(b"\r\n")
    quality = quality.rstrip(b"\r\n")

    read_id = _normalized_read_id(header)
    if read_id is None:
        _add_file_error(state.report, "invalid_header")
    if not plus.startswith(b"+"):
        _add_file_error(state.report, "invalid_plus")
    if not sequence or any(base not in _SEQUENCE_ALPHABET for base in sequence):
        _add_file_error(state.report, "invalid_sequence")
    if not quality or any(symbol < 33 or symbol > 126 for symbol in quality):
        _add_file_error(state.report, "invalid_quality")
    if len(sequence) != len(quality):
        _add_file_error(state.report, "length_mismatch")

    state.report["records"] += 1
    return "record", read_id


def _scan_pair(
    data_root: Path,
    *,
    sample_id: str,
    r1_name: str,
    r2_name: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    r1 = _FileState(data_root, r1_name)
    r2 = _FileState(data_root, r2_name)
    pair: dict[str, Any] = {
        "sample_id": sample_id,
        "r1": r1.label,
        "r2": r2.label,
        "r1_records": None,
        "r2_records": None,
        "record_counts_match": False,
        "read_ids_match": False,
        "status": "fail",
        "errors": [],
    }

    ids_match = True
    ids_verifiable = r1.can_read and r2.can_read
    r1_done = not r1.can_read
    r2_done = not r2.can_read

    while not (r1_done and r2_done):
        status_1, id_1 = ("eof", None) if r1_done else _read_record(r1)
        status_2, id_2 = ("eof", None) if r2_done else _read_record(r2)

        if status_1 in ("eof", "error"):
            r1_done = True
        if status_2 in ("eof", "error"):
            r2_done = True
        if status_1 == "error" or status_2 == "error":
            ids_verifiable = False

        if status_1 == "record" and status_2 == "record":
            if id_1 is None or id_2 is None:
                ids_verifiable = False
            elif id_1 != id_2:
                ids_match = False

    r1_report = r1.finalize()
    r2_report = r2.finalize()
    pair["r1_records"] = r1_report["records"]
    pair["r2_records"] = r2_report["records"]
    pair["record_counts_match"] = (
        r1_report["records"] is not None
        and r1_report["records"] == r2_report["records"]
    )
    pair["read_ids_match"] = bool(
        ids_verifiable and ids_match and pair["record_counts_match"]
    )

    if r1_report["status"] != "pass" or r2_report["status"] != "pass":
        _add_pair_error(pair, "file_validation_failed")
    if not pair["record_counts_match"]:
        _add_pair_error(pair, "record_count_mismatch")
    if ids_verifiable and not ids_match:
        _add_pair_error(pair, "read_id_mismatch")
    elif not pair["read_ids_match"] and pair["record_counts_match"]:
        _add_pair_error(pair, "read_ids_unverifiable")

    pair["status"] = "pass" if not pair["errors"] else "fail"
    return r1_report, r2_report, pair


def check_project(project_path: Path) -> dict[str, Any]:
    """Validate all paired FASTQ files referenced by a project JSON."""

    project_path = project_path.resolve()
    try:
        project_bytes = project_path.read_bytes()
        project = json.loads(project_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FastqIntegrityConfigError("Project JSON could not be read.") from exc

    samples = project.get("samples")
    if not isinstance(samples, dict):
        raise FastqIntegrityConfigError("Project JSON has no samples object.")
    local_data_dir = samples.get("local_data_dir")
    items = samples.get("items")
    if not isinstance(local_data_dir, str) or not local_data_dir:
        raise FastqIntegrityConfigError("samples.local_data_dir must be a non-empty string.")
    if not isinstance(items, list) or not items:
        raise FastqIntegrityConfigError("samples.items must be a non-empty array.")

    data_root = Path(local_data_dir)
    if not data_root.is_absolute():
        data_root = project_path.parent / data_root

    files: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise FastqIntegrityConfigError(f"Sample item {index} must be an object.")
        sample_id = item.get("sample_id")
        r1_name = item.get("fastq_1")
        r2_name = item.get("fastq_2")
        if not all(isinstance(value, str) and value for value in (sample_id, r1_name, r2_name)):
            raise FastqIntegrityConfigError(
                f"Sample item {index} must define sample_id, fastq_1, and fastq_2."
            )
        r1_report, r2_report, pair_report = _scan_pair(
            data_root,
            sample_id=sample_id,
            r1_name=r1_name,
            r2_name=r2_name,
        )
        files.extend((r1_report, r2_report))
        pairs.append(pair_report)

    file_passes = sum(report["status"] == "pass" for report in files)
    pair_passes = sum(report["status"] == "pass" for report in pairs)
    passed = file_passes == len(files) and pair_passes == len(pairs)
    return {
        "schema_version": 1,
        "tool": {"name": "fastq_integrity", "version": TOOL_VERSION},
        "project": {
            "id": str(project.get("project", {}).get("id", "unknown")),
            "config_file": project_path.name,
            "sha256": hashlib.sha256(project_bytes).hexdigest(),
        },
        "files": files,
        "pairs": pairs,
        "overall": {
            "status": "pass" if passed else "fail",
            "files_passed": file_passes,
            "files_total": len(files),
            "pairs_passed": pair_passes,
            "pairs_total": len(pairs),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate gzip FASTQ structure and R1/R2 pairing without third-party packages."
    )
    parser.add_argument("project", type=Path, help="Project JSON containing samples.items.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSON to this file instead of stdout.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = check_project(args.project)
    except FastqIntegrityConfigError:
        report = {
            "schema_version": 1,
            "tool": {"name": "fastq_integrity", "version": TOOL_VERSION},
            "files": [],
            "pairs": [],
            "overall": {
                "status": "fail",
                "error": {
                    "code": "invalid_project_config",
                    "message": "The project configuration could not be validated.",
                },
            },
        }

    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if report["overall"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
