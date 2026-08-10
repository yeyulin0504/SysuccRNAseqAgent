from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis_contract import canonical_sha256, sha256_file
from .storage import save_json


RESULT_MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ResultManifestSummary:
    path: Path
    ok: bool
    errors: list[str]
    files_sha256: str


def create_result_manifest(
    config: dict[str, Any],
    extracted_dir: Path,
    output_path: Path,
) -> ResultManifestSummary:
    """Inventory downloaded artifacts and validate required workflow outputs."""

    extracted_dir = extracted_dir.resolve()
    files: list[dict[str, Any]] = []
    if extracted_dir.exists():
        for path in sorted(item for item in extracted_dir.rglob("*") if item.is_file()):
            relative_path = path.relative_to(extracted_dir).as_posix()
            files.append(
                {
                    "path": relative_path,
                    "category": _artifact_category(relative_path),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )

    errors = _validate_required_outputs(config, extracted_dir)
    files_sha256 = canonical_sha256(files)
    scientific_files = [item for item in files if item["category"] == "scientific"]
    body = {
        "run_id": config.get("run", {}).get("id", ""),
        "project_id": config.get("project", {}).get("id", ""),
        "execution_mode": config.get("execution", {}).get("mode", "free"),
        "validation": {"ok": not errors, "errors": errors},
        "fingerprints": {
            "files_sha256": files_sha256,
            "scientific_files_sha256": canonical_sha256(scientific_files),
        },
        "files": files,
    }
    manifest = {
        "schema_version": RESULT_MANIFEST_SCHEMA_VERSION,
        "manifest_id": f"sha256:{canonical_sha256(body)}",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "body": body,
    }
    save_json(output_path, manifest)
    return ResultManifestSummary(output_path, not errors, errors, files_sha256)


def _artifact_category(relative_path: str) -> str:
    top_level = relative_path.split("/", 1)[0]
    if top_level in {"fastp", "star", "arriba", "featurecounts", "rsem", "downstream"}:
        return "scientific"
    return "audit"


def _validate_required_outputs(config: dict[str, Any], root: Path) -> list[str]:
    expected: list[tuple[str, bool]] = [
        ("status/completed.flag", True),
        ("status/state.txt", False),
    ]
    paired = config.get("sequencing", {}).get("layout", "paired") == "paired"
    pipeline = config.get("pipeline", {})
    samples = config.get("samples", {}).get("items", [])

    for sample in samples:
        sample_id = str(sample.get("sample_id", ""))
        if pipeline.get("fastp", {}).get("enabled"):
            expected.extend(
                [
                    (f"fastp/{sample_id}.R1.fastq.gz", False),
                    (f"fastp/{sample_id}.json", False),
                    (f"fastp/{sample_id}.html", False),
                ]
            )
            if paired:
                expected.append((f"fastp/{sample_id}.R2.fastq.gz", False))
        if pipeline.get("star", {}).get("enabled"):
            expected.extend(
                [
                    (f"star/{sample_id}.Aligned.sortedByCoord.out.bam", False),
                    (f"star/{sample_id}.Log.final.out", False),
                ]
            )
        if pipeline.get("arriba", {}).get("enabled"):
            expected.extend(
                [
                    (f"arriba/{sample_id}.fusions.tsv", False),
                    (f"arriba/{sample_id}.fusions.discarded.tsv", False),
                ]
            )
        if pipeline.get("rsem", {}).get("enabled"):
            expected.extend(
                [
                    (f"rsem/{sample_id}.genes.results", False),
                    (f"rsem/{sample_id}.isoforms.results", False),
                ]
            )

    if pipeline.get("featurecounts", {}).get("enabled"):
        expected.extend(
            [
                ("featurecounts/gene_counts.txt", False),
                ("featurecounts/gene_counts.txt.summary", False),
            ]
        )

    if config.get("downstream", {}).get("enabled", False):
        expected.extend(
            [
                ("downstream/summary.json", False),
                ("downstream/count_filter_audit.csv", False),
                ("downstream/id_mapping_audit.csv", False),
                ("downstream/vst_counts.csv", False),
                ("downstream/qc/pca.csv", False),
                ("downstream/session_info.txt", False),
            ]
        )
        for contrast in config["downstream"].get("contrasts", []):
            contrast_id = str(contrast.get("id", "")).strip()
            if contrast_id:
                expected.extend(
                    [
                        (f"downstream/de/{contrast_id}_results.csv", False),
                        (f"downstream/de/{contrast_id}_deg.csv", False),
                    ]
                )

    errors: list[str] = []
    for relative_path, allow_empty in expected:
        path = root / relative_path
        if not path.is_file():
            errors.append(f"Required result is missing: {relative_path}")
        elif not allow_empty and path.stat().st_size == 0:
            errors.append(f"Required result is empty: {relative_path}")

    state_path = root / "status" / "state.txt"
    if state_path.is_file() and state_path.read_text(encoding="utf-8", errors="replace").strip() != "completed":
        errors.append("Downloaded status/state.txt does not contain 'completed'.")
    return errors
