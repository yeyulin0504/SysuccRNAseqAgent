"""Zero-dependency reproducibility metrics for RNA-seq Agent run manifests.

The run manifest is intentionally unique for every attempt because its body contains
the run ID and creation time.  Reproducibility is therefore measured from the three
scientific execution fingerprints stored in ``body.fingerprints``, not from
``manifest_id``.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


FINGERPRINT_KEYS = (
    "workflow_sha256",
    "inputs_sha256",
    "scripts_sha256",
)

SUCCESS_OUTCOMES = {"completed", "success", "succeeded"}
FAILURE_OUTCOMES = {
    "download_failed",
    "failed",
    "policy_failed",
    "result_validation_failed",
    "run_failed",
    "timeout",
    "upload_failed",
    "validation_failed",
}


class BenchmarkInputError(ValueError):
    """Raised when a benchmark case or manifest cannot be interpreted safely."""


@dataclass(frozen=True)
class ResultManifestObservation:
    path: str | None
    available: bool
    integrity_ok: bool | None
    validation_ok: bool | None
    files_sha256: str
    scientific_files_sha256: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "available": self.available,
            "integrity_ok": self.integrity_ok,
            "validation_ok": self.validation_ok,
            "files_sha256": self.files_sha256,
            "scientific_files_sha256": self.scientific_files_sha256,
            "error": self.error,
        }


@dataclass(frozen=True)
class RunObservation:
    run_label: str
    manifest_path: str | None
    manifest_available: bool
    manifest_integrity_ok: bool | None
    mode: str | None
    skill_id: str
    contract_id: str
    fingerprints: dict[str, str]
    success: bool | None
    outcome: str
    error: str
    result_manifest: ResultManifestObservation

    @property
    def fingerprint_signature(self) -> tuple[str, str, str] | None:
        values = tuple(self.fingerprints.get(key, "") for key in FINGERPRINT_KEYS)
        if not all(values):
            return None
        return values

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_label": self.run_label,
            "manifest_path": self.manifest_path,
            "manifest_available": self.manifest_available,
            "manifest_integrity_ok": self.manifest_integrity_ok,
            "mode": self.mode,
            "skill_id": self.skill_id,
            "contract_id": self.contract_id,
            "fingerprints": dict(self.fingerprints),
            "success": self.success,
            "outcome": self.outcome,
            "error": self.error,
            "result_manifest": self.result_manifest.to_dict(),
        }


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def outcome_to_success(outcome: str | None) -> bool | None:
    normalized = str(outcome or "").strip().lower()
    if normalized in SUCCESS_OUTCOMES:
        return True
    if normalized in FAILURE_OUTCOMES:
        return False
    return None


def _observe_result_manifest(
    run_spec: dict[str, Any],
    *,
    base_dir: Path,
) -> ResultManifestObservation:
    value = run_spec.get("result_manifest")
    if value in (None, ""):
        return ResultManifestObservation(None, False, None, None, "", "", "")

    path = Path(str(value))
    if not path.is_absolute():
        path = base_dir / path
    path = path.resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ResultManifestObservation(str(path), False, None, None, "", "", str(exc))

    if not isinstance(payload, dict) or not isinstance(payload.get("body"), dict):
        return ResultManifestObservation(
            str(path), True, False, None, "", "", "result manifest body is missing or invalid"
        )

    body = payload["body"]
    expected_id = f"sha256:{canonical_sha256(body)}"
    integrity_ok = payload.get("manifest_id") == expected_id
    validation = body.get("validation", {})
    fingerprints = body.get("fingerprints", {})
    validation = validation if isinstance(validation, dict) else {}
    fingerprints = fingerprints if isinstance(fingerprints, dict) else {}
    validation_ok_value = validation.get("ok")
    validation_ok = validation_ok_value if isinstance(validation_ok_value, bool) else None
    files_sha256 = str(fingerprints.get("files_sha256", "")).strip()
    scientific_files_sha256 = str(fingerprints.get("scientific_files_sha256", "")).strip()

    errors: list[str] = []
    if not integrity_ok:
        errors.append("result manifest_id does not match canonical body hash")
    if validation_ok is None:
        errors.append("result validation.ok is missing or is not boolean")
    if not files_sha256:
        errors.append("result fingerprints.files_sha256 is missing")
    if not scientific_files_sha256:
        errors.append("result fingerprints.scientific_files_sha256 is missing")
    return ResultManifestObservation(
        path=str(path),
        available=True,
        integrity_ok=integrity_ok,
        validation_ok=validation_ok,
        files_sha256=files_sha256,
        scientific_files_sha256=scientific_files_sha256,
        error="; ".join(errors),
    )


def observe_run(
    run_spec: dict[str, Any],
    *,
    base_dir: Path,
    ordinal: int,
) -> RunObservation:
    """Load one run declaration and its optional manifest.

    ``success`` in the case file takes precedence over the textual ``outcome``.
    A missing manifest is allowed so that failures before manifest creation can
    still contribute to the success-rate denominator.
    """

    run_label = str(run_spec.get("run_label") or run_spec.get("run_id") or f"run-{ordinal}")
    outcome = str(run_spec.get("outcome", "")).strip()
    explicit_success = run_spec.get("success")
    if explicit_success is not None and not isinstance(explicit_success, bool):
        raise BenchmarkInputError(f"{run_label}: success must be true, false, or null")
    success = explicit_success if isinstance(explicit_success, bool) else outcome_to_success(outcome)
    result_manifest = _observe_result_manifest(run_spec, base_dir=base_dir)

    manifest_value = run_spec.get("manifest")
    if manifest_value in (None, ""):
        return RunObservation(
            run_label=run_label,
            manifest_path=None,
            manifest_available=False,
            manifest_integrity_ok=None,
            mode=None,
            skill_id="",
            contract_id="",
            fingerprints={},
            success=success,
            outcome=outcome,
            error="manifest not supplied",
            result_manifest=result_manifest,
        )

    manifest_path = Path(str(manifest_value))
    if not manifest_path.is_absolute():
        manifest_path = base_dir / manifest_path
    manifest_path = manifest_path.resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return RunObservation(
            run_label=run_label,
            manifest_path=str(manifest_path),
            manifest_available=False,
            manifest_integrity_ok=None,
            mode=None,
            skill_id="",
            contract_id="",
            fingerprints={},
            success=success,
            outcome=outcome,
            error=str(exc),
            result_manifest=result_manifest,
        )

    if not isinstance(payload, dict) or not isinstance(payload.get("body"), dict):
        return RunObservation(
            run_label=run_label,
            manifest_path=str(manifest_path),
            manifest_available=True,
            manifest_integrity_ok=False,
            mode=None,
            skill_id="",
            contract_id="",
            fingerprints={},
            success=success,
            outcome=outcome,
            error="manifest body is missing or invalid",
            result_manifest=result_manifest,
        )

    body = payload["body"]
    expected_id = f"sha256:{canonical_sha256(body)}"
    integrity_ok = payload.get("manifest_id") == expected_id
    execution = body.get("execution", {})
    fingerprints_value = body.get("fingerprints", {})
    execution = execution if isinstance(execution, dict) else {}
    fingerprints_value = fingerprints_value if isinstance(fingerprints_value, dict) else {}
    fingerprints = {
        key: str(fingerprints_value.get(key, "")).strip()
        for key in FINGERPRINT_KEYS
    }
    missing = [key for key, value in fingerprints.items() if not value]
    error_parts: list[str] = []
    if not integrity_ok:
        error_parts.append("manifest_id does not match canonical body hash")
    if missing:
        error_parts.append("missing fingerprints: " + ", ".join(missing))

    return RunObservation(
        run_label=run_label,
        manifest_path=str(manifest_path),
        manifest_available=True,
        manifest_integrity_ok=integrity_ok,
        mode=str(execution.get("mode", "")).strip() or None,
        skill_id=str(execution.get("skill_id", "")).strip(),
        contract_id=str(execution.get("contract_id", "")).strip(),
        fingerprints=fingerprints,
        success=success,
        outcome=outcome,
        error="; ".join(error_parts),
        result_manifest=result_manifest,
    )


def _modal_rate(values: Iterable[Any]) -> float | None:
    collected = list(values)
    if not collected:
        return None
    return max(Counter(collected).values()) / len(collected)


def _pairwise_rate(values: Iterable[Any]) -> float | None:
    collected = list(values)
    count = len(collected)
    if count == 0:
        return None
    if count == 1:
        return 1.0
    counts = Counter(collected)
    matching_pairs = sum(value * (value - 1) // 2 for value in counts.values())
    all_pairs = count * (count - 1) // 2
    return matching_pairs / all_pairs


def summarize_case(
    case_id: str,
    observations: list[RunObservation],
    *,
    expected_mode: str | None = None,
) -> dict[str, Any]:
    comparable = [item for item in observations if item.fingerprint_signature is not None]
    signatures = [item.fingerprint_signature for item in comparable]
    known_outcomes = [item for item in observations if item.success is not None]
    success_count = sum(item.success is True for item in known_outcomes)
    integrity_known = [item for item in observations if item.manifest_integrity_ok is not None]
    mode_mismatches = [
        item.run_label
        for item in observations
        if expected_mode and item.mode is not None and item.mode != expected_mode
    ]

    per_fingerprint: dict[str, dict[str, Any]] = {}
    for key in FINGERPRINT_KEYS:
        values = [item.fingerprints[key] for item in comparable]
        per_fingerprint[key] = {
            "unique_count": len(set(values)),
            "modal_consistency_rate": _modal_rate(values),
            "pairwise_consistency_rate": _pairwise_rate(values),
        }

    contract_values = [item.contract_id for item in observations if item.contract_id]
    modes = sorted({item.mode for item in observations if item.mode})
    skill_ids = sorted({item.skill_id for item in observations if item.skill_id})
    available_results = [item.result_manifest for item in observations if item.result_manifest.available]
    trusted_results = [item for item in available_results if item.integrity_ok is True]
    result_validations = [item.validation_ok for item in trusted_results if item.validation_ok is not None]
    scientific_fingerprints = [
        item.scientific_files_sha256 for item in trusted_results if item.scientific_files_sha256
    ]
    all_file_fingerprints = [item.files_sha256 for item in trusted_results if item.files_sha256]
    return {
        "case_id": case_id,
        "expected_mode": expected_mode,
        "observed_modes": modes,
        "mode_mismatch_runs": mode_mismatches,
        "run_count": len(observations),
        "manifest_count": sum(item.manifest_available for item in observations),
        "comparable_manifest_count": len(comparable),
        "manifest_integrity_rate": (
            sum(item.manifest_integrity_ok is True for item in integrity_known) / len(integrity_known)
            if integrity_known
            else None
        ),
        "outcome_known_count": len(known_outcomes),
        "outcome_coverage": len(known_outcomes) / len(observations) if observations else None,
        "success_count": success_count,
        "success_rate": success_count / len(known_outcomes) if known_outcomes else None,
        "exact_fingerprint_unique_count": len(set(signatures)),
        "exact_modal_consistency_rate": _modal_rate(signatures),
        "exact_pairwise_consistency_rate": _pairwise_rate(signatures),
        "fingerprint_metrics": per_fingerprint,
        "skill_ids": skill_ids,
        "contract_id_nonempty_count": len(contract_values),
        "contract_id_coverage": len(contract_values) / len(observations) if observations else None,
        "contract_id_unique_count": len(set(contract_values)),
        "contract_id_consistency_rate": _modal_rate(contract_values),
        "result_manifest_count": len(available_results),
        "result_manifest_coverage": len(available_results) / len(observations) if observations else None,
        "result_manifest_integrity_rate": (
            sum(item.integrity_ok is True for item in available_results) / len(available_results)
            if available_results
            else None
        ),
        "result_validation_known_count": len(result_validations),
        "result_validation_coverage": len(result_validations) / len(observations) if observations else None,
        "result_validation_success_count": sum(value is True for value in result_validations),
        "result_validation_success_rate": (
            sum(value is True for value in result_validations) / len(result_validations)
            if result_validations
            else None
        ),
        "result_files_unique_count": len(set(all_file_fingerprints)),
        "result_files_modal_consistency_rate": _modal_rate(all_file_fingerprints),
        "result_files_pairwise_consistency_rate": _pairwise_rate(all_file_fingerprints),
        "scientific_files_comparable_count": len(scientific_fingerprints),
        "scientific_files_unique_count": len(set(scientific_fingerprints)),
        "scientific_files_modal_consistency_rate": _modal_rate(scientific_fingerprints),
        "scientific_files_pairwise_consistency_rate": _pairwise_rate(scientific_fingerprints),
        "runs": [item.to_dict() for item in observations],
    }
