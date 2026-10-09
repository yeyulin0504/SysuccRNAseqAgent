from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis_contract import canonical_sha256, sha256_file
from .qc_verdict import bind_qc_verdict
from .run_audit import write_artifact_index
from .storage import save_json
from .storage import load_json


RESULT_MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ResultManifestSummary:
    path: Path
    ok: bool
    errors: list[str]
    files_sha256: str


def register_artifacts(attempt_dir: Path, records: list[dict[str, Any]]) -> Path:
    """Persist the audited artifact index for one immutable attempt."""

    return write_artifact_index(Path(attempt_dir), records)


def create_result_manifest(
    config: dict[str, Any],
    extracted_dir: Path,
    output_path: Path,
    *,
    stage: str | None = None,
    qc_verdict: dict[str, Any] | None = None,
) -> ResultManifestSummary:
    """Inventory downloaded artifacts and validate required workflow outputs.

    ``stage`` restricts validation to the artifacts that this stage actually
    produces (``qc`` -> fastp only, ``quant`` -> STAR/quant/fusion outputs,
    ``de`` -> diffexp). The monolithic validation covers every stage and is
    used by the single-shot ``run_project`` flow.
    """

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

    errors = _validate_required_outputs(config, extracted_dir, stage=stage)
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
    if qc_verdict is not None:
        body["qc_verdict"] = bind_qc_verdict(
            qc_verdict,
            run_id=str(body["run_id"] or ""),
            attempt_id=str(body["run_id"] or ""),
        )
    manifest = {
        "schema_version": RESULT_MANIFEST_SCHEMA_VERSION,
        "manifest_id": f"sha256:{canonical_sha256(body)}",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "body": body,
    }
    save_json(output_path, manifest)
    artifact_index_path = register_artifacts(output_path.parent, files)
    body["audit"] = {
        "schema_version": RESULT_MANIFEST_SCHEMA_VERSION,
        "artifact_index": artifact_index_path.name,
        "final_status": "completed" if not errors else "invalid",
        "validation": manifest["body"]["validation"],
    }
    manifest["manifest_id"] = f"sha256:{canonical_sha256(body)}"
    save_json(output_path, manifest)
    return ResultManifestSummary(output_path, not errors, errors, files_sha256)


def write_qc_verdict(
    manifest_path: Path,
    qc_verdict: dict[str, Any],
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Attach an attempt-bound QC verdict to an existing result manifest."""

    manifest_path = Path(manifest_path)
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        raise ValueError("QC verdict persistence requires a non-empty run_id.")
    manifest = load_json(manifest_path)
    body = manifest.setdefault("body", {})
    manifest_run_id = str(body.get("run_id") or "").strip()
    if manifest_run_id and manifest_run_id != normalized_run_id:
        raise ValueError("QC verdict run_id does not match result manifest attempt.")
    bound_verdict = bind_qc_verdict(
        qc_verdict,
        run_id=normalized_run_id,
        attempt_id=manifest_run_id or normalized_run_id,
    )
    body["qc_verdict"] = bound_verdict
    if not manifest_run_id:
        body["run_id"] = normalized_run_id
    manifest["manifest_id"] = f"sha256:{canonical_sha256(body)}"
    save_json(manifest_path, manifest)
    return manifest


def _artifact_category(relative_path: str) -> str:
    top_level = relative_path.split("/", 1)[0]
    if top_level in {"fastp", "star", "arriba", "featurecounts", "rsem", "diffexp", "cms"}:
        return "scientific"
    return "audit"


def _validate_required_outputs(
    config: dict[str, Any],
    root: Path,
    *,
    stage: str | None = None,
) -> list[str]:
    expected: list[tuple[str, bool]] = [
        ("status/completed.flag", True),
        ("status/state.txt", False),
    ]
    # Stage-scoped validation (说明书 §5 分阶段)。None = 全流程单次运行。
    from .pipeline import (
        STAGE_CMS,
        STAGE_COUNTS,
        STAGE_DE,
        STAGE_QC,
        STAGE_QUANT,
    )

    def _enabled(step: str) -> bool:
        return bool(config.get("pipeline", {}).get(step, {}).get("enabled"))

    paired = config.get("sequencing", {}).get("layout", "paired") == "paired"
    pipeline = config.get("pipeline", {})
    samples = config.get("samples", {}).get("items", [])

    for sample in samples:
        sample_id = str(sample.get("sample_id", ""))
        if _enabled("fastp") and (stage is None or stage == STAGE_QC):
            expected.extend(
                [
                    (f"fastp/{sample_id}.R1.fastq.gz", False),
                    (f"fastp/{sample_id}.json", False),
                    (f"fastp/{sample_id}.html", False),
                ]
            )
            if paired:
                expected.append((f"fastp/{sample_id}.R2.fastq.gz", False))
        if _enabled("star") and (stage is None or stage == STAGE_QUANT):
            expected.extend(
                [
                    (f"star/{sample_id}.Aligned.sortedByCoord.out.bam", False),
                    (f"star/{sample_id}.Log.final.out", False),
                ]
            )
        if _enabled("arriba") and (stage is None or stage == STAGE_QUANT):
            expected.extend(
                [
                    (f"arriba/{sample_id}.fusions.tsv", False),
                    (f"arriba/{sample_id}.fusions.discarded.tsv", False),
                ]
            )
        if _enabled("rsem") and (stage is None or stage == STAGE_QUANT):
            expected.extend(
                [
                    (f"rsem/{sample_id}.genes.results", False),
                    (f"rsem/{sample_id}.isoforms.results", False),
                ]
            )

    if _enabled("featurecounts") and (stage is None or stage == STAGE_QUANT):
        expected.extend(
            [
                ("featurecounts/gene_counts.txt", False),
                ("featurecounts/gene_counts.txt.summary", False),
            ]
        )

    if _enabled("diffexp") and (stage is None or stage in (STAGE_DE, STAGE_COUNTS)):
        expected.extend(
            [
                ("diffexp/deseq2_results.tsv", False),
                ("diffexp/deseq2_summary.json", False),
            ]
        )

    if _enabled("cms") and (stage is None or stage in (STAGE_CMS, STAGE_COUNTS)):
        expected.extend(
            [
                ("cms/cms_result.csv", False),
                ("cms/cms_summary.json", False),
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
