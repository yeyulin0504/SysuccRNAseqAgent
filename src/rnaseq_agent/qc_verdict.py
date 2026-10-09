"""Deterministic QC verdicts and downstream DE readiness."""

from __future__ import annotations

from typing import Any

DIMENSIONS = (
    "read_quality",
    "mapping",
    "duplication",
    "strandedness",
    "sample_outlier",
)
STATUSES = ("pass", "warn", "fail", "abstain")
READINESS = ("ready", "caution", "blocked")

DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "read_quality": {"warn_below": 0.80, "fail_below": 0.60},
    "mapping": {"warn_below": 0.80, "fail_below": 0.60},
    "duplication": {"warn_above": 0.50, "fail_above": 0.80},
}


def compute_qc_verdict(
    qc_inputs: dict[str, Any],
    thresholds: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute a stable, fail-closed verdict from structured QC evidence.

    Each dimension may contain the metric expected by its name. Missing or
    unusable evidence is explicitly ``abstain``; it is never treated as pass.
    ``qc_inputs`` is deliberately plain data so the result can be persisted
    in a manifest or LangGraph checkpoint without carrying runtime objects.
    """

    configured = {name: dict(values) for name, values in DEFAULT_THRESHOLDS.items()}
    for name, values in (thresholds or {}).items():
        configured.setdefault(name, {}).update(values)

    dimensions = {
        name: _dimension_verdict(name, qc_inputs.get(name), configured.get(name, {}))
        for name in DIMENSIONS
    }
    statuses = [item["status"] for item in dimensions.values()]
    if "fail" in statuses:
        status = "fail"
    elif "abstain" in statuses:
        status = "abstain"
    elif "warn" in statuses:
        status = "warn"
    else:
        status = "pass"

    readiness = {
        "pass": "ready",
        "warn": "caution",
        "fail": "blocked",
        "abstain": "blocked",
    }[status]
    return {
        "status": status,
        "de_readiness": readiness,
        "dimensions": dimensions,
    }


def bind_qc_verdict(
    verdict: dict[str, Any],
    *,
    run_id: str,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Attach the immutable execution identity to a computed verdict."""

    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        raise ValueError("QC verdict requires a non-empty run_id.")
    normalized_attempt_id = str(attempt_id or normalized_run_id).strip()
    if not normalized_attempt_id:
        raise ValueError("QC verdict requires a non-empty attempt_id.")
    bound = dict(verdict)
    existing_run_id = str(bound.get("run_id") or "").strip()
    if existing_run_id and existing_run_id != normalized_run_id:
        raise ValueError("QC verdict run_id does not match execution attempt.")
    existing_attempt_id = str(bound.get("attempt_id") or "").strip()
    if existing_attempt_id and existing_attempt_id != normalized_attempt_id:
        raise ValueError("QC verdict attempt_id does not match execution attempt.")
    bound["run_id"] = normalized_run_id
    bound["attempt_id"] = normalized_attempt_id
    return bound


def _dimension_verdict(
    name: str,
    evidence: Any,
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        return {"status": "abstain", "reason": "missing evidence"}

    if name == "read_quality":
        value = _number(evidence.get("q30", evidence.get("q30_fraction")))
        return _numeric_verdict(value, thresholds, "below", "q30")
    if name == "mapping":
        value = _number(evidence.get("alignment_rate"))
        return _numeric_verdict(value, thresholds, "below", "alignment_rate")
    if name == "duplication":
        value = _number(evidence.get("duplicate_rate"))
        return _numeric_verdict(value, thresholds, "above", "duplicate_rate")
    if name == "strandedness":
        observed = evidence.get("observed")
        expected = evidence.get("expected")
        if not _present(observed) or not _present(expected):
            return {"status": "abstain", "reason": "missing strandedness evidence"}
        if str(observed).strip().lower() != str(expected).strip().lower():
            return {
                "status": "fail",
                "observed": observed,
                "expected": expected,
                "reason": "observed strandedness does not match expected",
            }
        return {"status": "pass", "observed": observed, "expected": expected}
    if name == "sample_outlier":
        if "outliers" not in evidence or not isinstance(evidence["outliers"], list):
            return {"status": "abstain", "reason": "missing outlier evidence"}
        outliers = list(evidence["outliers"])
        if outliers:
            return {"status": "warn", "outliers": outliers, "reason": "sample outlier detected"}
        return {"status": "pass", "outliers": []}
    return {"status": "abstain", "reason": "unsupported dimension"}


def _numeric_verdict(
    value: float | None,
    thresholds: dict[str, Any],
    direction: str,
    label: str,
) -> dict[str, Any]:
    if value is None:
        return {"status": "abstain", "reason": f"missing {label} evidence"}
    warn_key = "warn_below" if direction == "below" else "warn_above"
    fail_key = "fail_below" if direction == "below" else "fail_above"
    fail = _number(thresholds.get(fail_key))
    warn = _number(thresholds.get(warn_key))
    if fail is None or warn is None:
        return {"status": "abstain", "value": value, "reason": f"missing {label} thresholds"}
    if (direction == "below" and value < fail) or (direction == "above" and value > fail):
        status = "fail"
    elif (direction == "below" and value < warn) or (direction == "above" and value > warn):
        status = "warn"
    else:
        status = "pass"
    return {"status": status, "value": value}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""
