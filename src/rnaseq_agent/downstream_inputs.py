from __future__ import annotations

import csv
import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_ROWS = 1_000_000
MAX_COLUMNS = 2_048
MAX_FIELD_LENGTH = 256


@dataclass(frozen=True)
class StandaloneInputSummary:
    gene_count: int
    sample_count: int
    condition_counts: dict[str, int]
    has_batch: bool
    count_size_bytes: int
    metadata_size_bytes: int
    count_sha256: str
    metadata_sha256: str


class StandaloneInputError(ValueError):
    pass


def standalone_input_paths(config: dict) -> tuple[Path, Path]:
    downstream = config.get("downstream", {})
    input_config = downstream.get("input", {})
    metadata_config = downstream.get("metadata_input", {})
    root = _approved_root(downstream)
    return (
        _safe_input_path(root, input_config, "count matrix"),
        _safe_input_path(root, metadata_config, "metadata"),
    )


def inspect_standalone_metadata(config: dict) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    count_path, metadata_path = standalone_input_paths(config)
    sample_ids, _ = _inspect_counts(count_path)
    return sample_ids, _metadata_rows(metadata_path, sample_ids)


def inspect_standalone_inputs(config: dict) -> StandaloneInputSummary:
    try:
        count_path, metadata_path = standalone_input_paths(config)
        sample_ids, gene_count = _inspect_counts(count_path)
        metadata = _metadata_rows(metadata_path, sample_ids)
    except UnicodeError as exc:
        raise StandaloneInputError("Standalone inputs must be valid UTF-8 TSV files.") from exc
    conditions = [row["condition"] for row in metadata]
    has_batch = any("batch" in row for row in metadata)
    return StandaloneInputSummary(
        gene_count=gene_count,
        sample_count=len(sample_ids),
        condition_counts=dict(sorted(Counter(conditions).items())),
        has_batch=has_batch,
        count_size_bytes=count_path.stat().st_size,
        metadata_size_bytes=metadata_path.stat().st_size,
        count_sha256=_sha256_file(count_path),
        metadata_sha256=_sha256_file(metadata_path),
    )


def _approved_root(downstream: dict) -> Path:
    value = str(downstream.get("input_root", "")).strip()
    if not value:
        raise StandaloneInputError("Standalone downstream analysis requires downstream.input_root.")
    root = Path(value).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise StandaloneInputError("Standalone downstream input_root must be a directory.")
    return root


def _safe_input_path(root: Path, descriptor: dict, label: str) -> Path:
    source = str(descriptor.get("source_path", "")).strip()
    if not source:
        raise StandaloneInputError(f"Standalone {label} source_path is required.")
    path = Path(source).expanduser().resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise StandaloneInputError(f"Standalone {label} escapes downstream.input_root.") from exc
    if not path.is_file() or path.suffix.lower() != ".tsv":
        raise StandaloneInputError(f"Standalone {label} must be an existing .tsv file.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise StandaloneInputError(f"Standalone {label} exceeds the {MAX_FILE_BYTES // 1024 // 1024} MiB limit.")
    if path.name != str(descriptor.get("source_filename", "")):
        raise StandaloneInputError(f"Standalone {label} source_filename must match source_path.")
    return path


def _reader(path: Path):
    try:
        handle = path.open("r", encoding="utf-8", newline="")
    except UnicodeError as exc:
        raise StandaloneInputError(f"Cannot decode {path.name} as UTF-8.") from exc
    return handle


def _header(row: list[str], path: Path) -> list[str]:
    if not row or len(row) > MAX_COLUMNS or any(not field or len(field) > MAX_FIELD_LENGTH or _unsafe(field) for field in row):
        raise StandaloneInputError(f"Invalid TSV header in {path.name}.")
    if len(set(row)) != len(row):
        raise StandaloneInputError(f"Duplicate TSV header in {path.name}.")
    return row


def _unsafe(value: str) -> bool:
    return any(ord(character) < 32 for character in value)


def _inspect_counts(path: Path) -> tuple[tuple[str, ...], int]:
    with _reader(path) as handle:
        reader = csv.reader(handle, delimiter="\t")
        try:
            header = _header(next(reader), path)
        except StopIteration as exc:
            raise StandaloneInputError(f"Standalone count matrix is empty: {path.name}") from exc
        if header[0] != "gene_id" or len(header) < 3:
            raise StandaloneInputError("Standalone count matrix must have gene_id and at least two sample columns.")
        sample_ids = tuple(header[1:])
        gene_ids: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            if line_number > MAX_ROWS:
                raise StandaloneInputError("Standalone count matrix exceeds the row limit.")
            if len(row) != len(header) or not row[0] or len(row[0]) > MAX_FIELD_LENGTH or _unsafe(row[0]):
                raise StandaloneInputError(f"Invalid count row {line_number} in {path.name}.")
            gene_id = row[0]
            normalized_id = gene_id.split(".", 1)[0]
            if normalized_id in gene_ids:
                raise StandaloneInputError(f"Duplicate gene_id after version normalization: {gene_id}")
            gene_ids.add(normalized_id)
            for value in row[1:]:
                if not value.isascii() or not value.isdecimal():
                    raise StandaloneInputError(f"Count matrix row {line_number} contains a nonnegative integer requirement violation.")
        if not gene_ids:
            raise StandaloneInputError("Standalone count matrix has no genes.")
    return sample_ids, len(gene_ids)


def _metadata_rows(path: Path, sample_ids: tuple[str, ...]) -> list[dict[str, str]]:
    with _reader(path) as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames is None:
            raise StandaloneInputError(f"Standalone metadata is empty: {path.name}")
        header = _header(reader.fieldnames, path)
        allowed = {"sample_id", "condition", "batch"}
        if set(header) - allowed or not {"sample_id", "condition"}.issubset(header):
            raise StandaloneInputError("Standalone metadata requires sample_id and condition, with optional batch only.")
        has_batch = "batch" in header
        rows: dict[str, dict[str, str]] = {}
        for line_number, row in enumerate(reader, start=2):
            if line_number > MAX_ROWS:
                raise StandaloneInputError("Standalone metadata exceeds the row limit.")
            sample_id = str(row.get("sample_id", ""))
            condition = str(row.get("condition", ""))
            batch = str(row.get("batch", ""))
            values = [sample_id, condition, batch]
            if any(not value or len(value) > MAX_FIELD_LENGTH or _unsafe(value) for value in values[: 3 if has_batch else 2]):
                raise StandaloneInputError(f"Invalid metadata row {line_number} in {path.name}.")
            if sample_id in rows:
                raise StandaloneInputError(f"Duplicate metadata sample_id: {sample_id}")
            rows[sample_id] = {"sample_id": sample_id, "condition": condition}
            if has_batch:
                rows[sample_id]["batch"] = batch
        if set(rows) != set(sample_ids) or len(rows) != len(sample_ids):
            raise StandaloneInputError("Metadata sample_id values must exactly match count-matrix sample columns.")
    return [rows[sample_id] for sample_id in sample_ids]


def _inspect_metadata(path: Path, sample_ids: tuple[str, ...]) -> tuple[list[str], bool]:
    rows = _metadata_rows(path, sample_ids)
    return [row["condition"] for row in rows], any("batch" in row for row in rows)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
