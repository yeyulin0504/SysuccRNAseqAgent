from __future__ import annotations

import hashlib
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from .downstream_inputs import StandaloneInputError, inspect_standalone_metadata


DOWNSTREAM_PROFILE_ID = "bulk_rnaseq_deseq2_v1"
SUPPORTED_SPECIES = {"human", "mouse"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def downstream_errors(config: dict[str, Any]) -> list[str]:
    downstream = config.get("downstream", {})
    if not isinstance(downstream, dict) or not downstream.get("enabled", False):
        return []

    source_mode = downstream.get("source_mode", "pipeline_featurecounts")
    if source_mode not in {"pipeline_featurecounts", "standalone_count_matrix"}:
        return [f"Unsupported downstream source mode: {source_mode}"]

    errors: list[str] = []
    if downstream.get("profile_id") != DOWNSTREAM_PROFILE_ID:
        errors.append(f"Unsupported downstream profile: {downstream.get('profile_id', '')}")

    if source_mode == "pipeline_featurecounts":
        errors.extend(_pipeline_input_errors(config, downstream))
        metadata = downstream.get("metadata", {}).get("samples", [])
    else:
        metadata, standalone_errors = _standalone_metadata(config)
        errors.extend(standalone_errors)

    errors.extend(_analysis_errors(config, downstream, metadata))
    return errors


def _pipeline_input_errors(config: dict[str, Any], downstream: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    pipeline = config.get("pipeline", {})
    if not pipeline.get("star", {}).get("enabled", False):
        errors.append("Downstream analysis requires star to be enabled.")
    if not pipeline.get("featurecounts", {}).get("enabled", False):
        errors.append("Downstream analysis requires featurecounts to be enabled.")
    input_config = downstream.get("input", {})
    if input_config.get("kind") != "featurecounts_raw_counts" or input_config.get("path") != "featurecounts/gene_counts.txt":
        errors.append("Downstream analysis only accepts featurecounts/gene_counts.txt raw counts.")
    metadata = downstream.get("metadata", {}).get("samples", [])
    upstream_ids = [str(item.get("sample_id", "")) for item in config.get("samples", {}).get("items", [])]
    metadata_ids = [str(item.get("sample_id", "")) for item in metadata if isinstance(item, dict)]
    if set(metadata_ids) != set(upstream_ids) or len(metadata_ids) != len(upstream_ids):
        errors.append("Downstream metadata sample IDs must exactly match configured samples.")
    return errors


def _standalone_metadata(config: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    try:
        _, metadata = inspect_standalone_metadata(config)
    except (OSError, StandaloneInputError) as exc:
        return [], [str(exc)]
    return metadata, []


def _analysis_errors(config: dict[str, Any], downstream: dict[str, Any], metadata: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    metadata_ids = [str(row.get("sample_id", "")) for row in metadata if isinstance(row, dict)]
    for sample_id, count in Counter(metadata_ids).items():
        if sample_id and count > 1:
            errors.append(f"Duplicate downstream metadata sample_id: {sample_id}")
    conditions: dict[str, str] = {}
    for row in metadata:
        if not isinstance(row, dict):
            errors.append("Downstream metadata rows must be objects.")
            continue
        sample_id = str(row.get("sample_id", ""))
        condition = str(row.get("condition", "")).strip()
        if not condition:
            errors.append(f"Downstream metadata sample {sample_id or '<missing>'} is missing condition.")
        else:
            conditions[sample_id] = condition

    design = downstream.get("design", {})
    batch_column = str(design.get("batch_column", "")).strip()
    expected_formula = "~ batch + condition" if batch_column else "~ condition"
    if design.get("condition_column") != "condition" or design.get("formula") != expected_formula:
        errors.append("Downstream design must be ~ condition or ~ batch + condition.")
    if batch_column not in {"", "batch"}:
        errors.append("Downstream design only supports the optional batch column named batch.")
    if batch_column:
        batches = [str(row.get("batch", "")).strip() for row in metadata if isinstance(row, dict)]
        if any(not batch for batch in batches):
            errors.append("Downstream batch design requires a batch value for every sample.")
        if len(set(batches)) < 2:
            errors.append("Downstream batch design requires at least two batch levels.")
        if any(count < 2 for count in Counter(batches).values()):
            errors.append("Downstream batch design does not allow batches with fewer than two samples.")
        pairs = {(conditions.get(str(row.get("sample_id", "")), ""), str(row.get("batch", "")).strip()) for row in metadata if isinstance(row, dict)}
        if len(pairs) == len(set(conditions.values())) == len(set(batches)):
            errors.append("Downstream batch is perfectly confounded with condition.")

    contrast_ids: set[str] = set()
    condition_counts = Counter(conditions.values())
    for contrast in downstream.get("contrasts", []):
        if not isinstance(contrast, dict):
            errors.append("Downstream contrasts must be objects.")
            continue
        contrast_id = str(contrast.get("id", "")).strip()
        if not contrast_id:
            errors.append("Downstream contrast is missing id.")
            continue
        if contrast_id in contrast_ids:
            errors.append(f"Duplicate downstream contrast id: {contrast_id}")
        contrast_ids.add(contrast_id)
        numerator = str(contrast.get("numerator", "")).strip()
        denominator = str(contrast.get("denominator", "")).strip()
        if contrast.get("factor") != "condition":
            errors.append(f"Contrast {contrast_id} must use the condition factor.")
        if numerator == denominator:
            errors.append(f"Contrast {contrast_id} must use different numerator and denominator.")
        for level in (numerator, denominator):
            if level not in condition_counts:
                errors.append(f"Contrast {contrast_id} references missing condition level {level or '<missing>'}.")
            elif condition_counts[level] < 2:
                errors.append(f"Contrast {contrast_id} requires at least 2 samples in condition level {level}.")

    filtering = downstream.get("filtering", {})
    if not _positive_integer(filtering.get("min_count")) or not _positive_integer(filtering.get("min_samples")):
        errors.append("Downstream filtering thresholds must be positive integers.")
    de = downstream.get("differential_expression", {})
    if not _probability(de.get("padj_threshold")) or not _positive_number(de.get("abs_log2_fold_change")):
        errors.append("Downstream differential-expression thresholds are invalid.")

    enrichment = downstream.get("enrichment", {})
    if enrichment.get("enabled", False):
        reference = config.get("reference", {})
        if reference.get("species") not in SUPPORTED_SPECIES or enrichment.get("organism") != reference.get("species") or not reference.get("catalog_id") or reference.get("index_state") not in {"existing_confirmed", "existing_preflight_passed"}:
            errors.append("Downstream enrichment requires a confirmed human or mouse reference identity.")
        if enrichment.get("id_type") != "ENSEMBL":
            errors.append("Downstream enrichment only supports ENSEMBL identifiers.")
    gmt = enrichment.get("gmt", {})
    if gmt.get("enabled", False):
        source_filename = str(gmt.get("source_filename", ""))
        source_path = str(gmt.get("source_path", ""))
        if not source_filename.endswith(".gmt"):
            errors.append("Enabled downstream GMT must be a .gmt text file.")
        if not _safe_local_gmt_path(source_path):
            errors.append("Enabled downstream GMT must use a readable local source_path.")
        elif Path(source_path).name != source_filename:
            errors.append("Enabled downstream GMT source_filename must match source_path.")
        expected_sha256 = gmt.get("sha256")
        if not _sha256(expected_sha256):
            errors.append("Enabled downstream GMT requires a SHA-256 digest.")
        elif _safe_local_gmt_path(source_path) and _sha256_file(Path(source_path)) != expected_sha256:
            errors.append("Enabled downstream GMT SHA-256 does not match source_path.")

    runtime = downstream.get("runtime", {})
    if runtime.get("environment_kind") != "apptainer":
        errors.append("Downstream runtime must use the approved Apptainer environment.")
    if not _safe_absolute_path(runtime.get("image_path")):
        errors.append("Downstream runtime.image_path must be a safe absolute path.")
    if not _sha256(runtime.get("image_sha256")):
        errors.append("Downstream runtime.image_sha256 must be a SHA-256 digest.")
    if runtime.get("rscript_path") != "Rscript":
        errors.append("Downstream runtime.rscript_path must be Rscript.")
    return errors


def normalized_downstream_config(config: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(config.get("downstream", {}))


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and value > 0


def _positive_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _probability(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 1


def _sha256(value: object) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def _safe_absolute_path(value: object) -> bool:
    return isinstance(value, str) and value.startswith("/") and not any(character in value for character in " \t\x00\n\r;|&`$") and "/../" not in value and not value.endswith("/..")


def _safe_local_gmt_path(value: str) -> bool:
    path = Path(value).expanduser()
    return path.is_file() and path.suffix == ".gmt"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
