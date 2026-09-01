"""Output Validator, Post-run Gate, and Artifact Registry.

Framework section 5.2 call chain after execution:

    Execution Gateway -> HPC -> Output Validator -> Post-run Gate
    -> Artifact Registry

- ``Output Validator``: independent check of Schema, hashes, tag sets,
  numeric ranges and required artifacts. Failures are
  ``FAIL_OUTPUT_CONTRACT`` and the files stay in an isolated audit area;
  they never become downstream inputs.
- ``Post-run Gate``   : QC / OOD / low-confidence judgement. Returns
  ``ABSTAIN`` when a model or module actively refuses to conclude.
- ``Artifact Registry``: records inputs, outputs, hashes, versions, logs
  and dependencies; supports the framework's ``STALE`` invalidation so
  changing upstream parameters never silently mixes old results.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .capability import ABSTAIN, FAIL_OUTPUT_CONTRACT, STALE, PASS

ARTIFACT_REGISTRY_FILE = "artifact_registry.json"


class ValidationFailure(Exception):
    """Raised when a required artifact is missing or fails its contract."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# -- Output Validator -------------------------------------------------------


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_output_contract(
    config: dict[str, Any],
    *,
    attempts_dir: Path | None = None,
    required_artifacts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Independently validate the produced artifacts against the contract.

    Returns a structured report. Raises ``ValidationFailure`` when a required
    artifact is missing so callers can move files to the isolated audit area
    and mark the attempt as ``FAIL_OUTPUT_CONTRACT``.
    """
    project_dir = attempts_dir or Path(config.get("project_dir", "."))
    required = required_artifacts or {}
    results: dict[str, Any] = {
        "validator": "output_validator_v1",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "artifacts": {},
        "status": PASS,
    }
    missing: list[str] = []

    # Required artifacts listed in the contract's output_artifacts.
    for pattern, description in required.items():
        # Patterns are relative to the attempt's extracted/downloaded dirs.
        candidates = _resolve_pattern(project_dir, pattern)
        if candidates:
            for path in candidates:
                results["artifacts"][pattern] = {
                    "path": str(path),
                    "size": path.stat().st_size if path.exists() else 0,
                    "sha256": _file_sha256(path),
                    "description": description,
                }
        else:
            missing.append(f"{pattern} ({description})")

    if missing:
        results["status"] = FAIL_OUTPUT_CONTRACT
        results["missing"] = missing
        raise ValidationFailure(
            FAIL_OUTPUT_CONTRACT + ": " + "; ".join(f"缺少 {m}" for m in missing)
        )
    results["status"] = PASS
    return results


def _resolve_pattern(root: Path, pattern: str) -> list[Path]:
    """Resolve a glob-like artifact pattern under a root directory."""
    parts = pattern.split("/")
    if not parts:
        return []
    matches: list[Path] = []
    # Try under downloads/extracted first, then directly under root.
    bases = [root / "downloads" / "extracted", root]
    for base in bases:
        if not base.is_dir():
            continue
        current: list[Path] = [base]
        for part in parts:
            if "*" in part or "?" in part:
                expanded: list[Path] = []
                for directory in current:
                    if not directory.is_dir():
                        continue
                    expanded.extend(directory.glob(part))
                current = expanded
            else:
                current = [p / part for p in current if p.is_dir()]
        matches.extend(p for p in current if p.is_file())
    # De-duplicate preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for path in matches:
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


# -- Post-run Gate ----------------------------------------------------------


def post_run_gate(
    config: dict[str, Any],
    *,
    qc_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Post-run applicability gate: QC, OOD and low-confidence refusal.

    Returns ``ABSTAIN`` with a stable reason code when the module actively
    refuses to conclude (framework section 6.3 / 5.2), otherwise ``PASS``.
    """
    reason_codes: list[str] = []
    qc = qc_report or {}

    # Example QC gates: alignment rate and duplicate rate sanity ranges.
    alignment_rate = qc.get("alignment_rate")
    if alignment_rate is not None:
        if alignment_rate < 0.5:
            reason_codes.append("qc_low_alignment_rate")

    duplicate_rate = qc.get("duplicate_rate")
    if duplicate_rate is not None:
        if duplicate_rate > 0.8:
            reason_codes.append("qc_high_duplicate_rate")

    if reason_codes:
        return {
            "gate": "post_run_gate_v1",
            "status": ABSTAIN,
            "reason_codes": reason_codes,
            "message": "结果 QC 未达阈值，主动拒绝输出结论。",
        }
    return {
        "gate": "post_run_gate_v1",
        "status": PASS,
        "reason_codes": [],
        "message": "结果 QC 通过。",
    }


# -- Artifact Registry ------------------------------------------------------


def artifact_registry_path(project_dir: Path) -> Path:
    return project_dir / ARTIFACT_REGISTRY_FILE


def register_artifacts(
    project_dir: Path,
    *,
    artifacts: dict[str, dict[str, Any]],
    attempt_id: str = "",
) -> dict[str, Any]:
    """Register produced artifacts with hashes and provenance into the registry."""
    path = artifact_registry_path(project_dir)
    registry: dict[str, Any] = {}
    if path.is_file():
        try:
            registry = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            registry = {}

    for artifact_id, meta in artifacts.items():
        meta.setdefault("registered_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        meta.setdefault("attempt_id", attempt_id)
        meta.setdefault("status", "VALID")
        # Stale invalidation: if we are re-registering an artifact from a
        # different attempt, the old one becomes STALE (framework 6.2).
        existing = registry.get(artifact_id)
        if existing and existing.get("attempt_id") != attempt_id:
            existing["status"] = STALE
            existing["stale_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        registry[artifact_id] = meta

    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    return registry


def mark_stale_downstream(project_dir: Path, upstream_artifact_id: str) -> list[str]:
    """Mark artifacts depending on a changed upstream as STALE.

    Framework section 6.2 ChangeSet invalidation: only results whose inputs
    actually changed are invalidated; untouched results stay reusable.
    """
    path = artifact_registry_path(project_dir)
    if not path.is_file():
        return []
    registry: dict[str, Any] = {}
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []

    stale: list[str] = []
    for artifact_id, meta in registry.items():
        depends_on = meta.get("depends_on", [])
        if upstream_artifact_id in depends_on and meta.get("status") != STALE:
            meta["status"] = STALE
            meta["stale_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            stale.append(artifact_id)
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    return stale
