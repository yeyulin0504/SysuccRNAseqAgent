"""Declarative capability and pipeline registry.

The JSON file is the discovery source.  The historical Python registry is
kept as a compatibility fallback for installations that do not ship the
declarative file or for old projects whose capability is not declared there.
This module only validates metadata; adapters remain responsible for inspect,
plan, and materialize behavior.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REGISTRY_SCHEMA_VERSION = 1
REQUIRED_FIELDS = (
    "id",
    "version",
    "entry_stage",
    "required_inputs",
    "required_tools",
    "required_artifacts",
    "supports",
    "gates",
)
ALLOWED_ENTRY_STAGES = {"qc", "counts", "de", "cms"}
ALLOWED_SUPPORT_KEYS = {"data_type", "layout", "design", "cancer_type", "input_kind"}
ALLOWED_GATE_KEYS = {
    "min_samples",
    "min_replicates_per_group",
    "required_sample_fields",
    "required_reference_keys",
}


class CapabilityRegistryError(ValueError):
    """Raised when a declarative registry is present but invalid."""


@dataclass(frozen=True)
class CapabilityDiscoveryResult:
    """Stable result returned by every capability/adapter discovery boundary."""

    status: str
    capability: dict[str, Any] | None = None
    code: str = ""
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        from .capability import PASS

        return self.status == PASS and self.capability is not None

    @property
    def verdict(self) -> str:
        return self.status

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "verdict": self.status,
            "capability": deepcopy(self.capability),
            "code": self.code,
            "reasons": list(self.reasons),
            "details": deepcopy(self.details),
        }


class CapabilityRegistry:
    def __init__(self, records: list[dict[str, Any]], *, source: str) -> None:
        self._records = {record["id"]: deepcopy(record) for record in records}
        self.source = source

    def get(self, capability_id: str, version: str | None = None) -> dict[str, Any]:
        record = self._records.get(capability_id)
        if record is None:
            raise KeyError(capability_id)
        if version is not None and str(record["version"]) != str(version):
            raise KeyError(f"{capability_id}@{version}")
        return deepcopy(record)

    def discover(self, capability_id: str, version: str | None = None) -> CapabilityDiscoveryResult:
        from .capability import NOT_EVALUABLE, PASS

        record = self._records.get(capability_id)
        if record is None:
            return CapabilityDiscoveryResult(
                status=NOT_EVALUABLE,
                code="UNKNOWN_CAPABILITY",
                reasons=[f"Unknown capability: {capability_id}"],
            )
        if version is not None and str(record["version"]) != str(version):
            return CapabilityDiscoveryResult(
                status=NOT_EVALUABLE,
                code="INCOMPATIBLE_VERSION",
                reasons=[
                    f"Capability {capability_id} requires version {record['version']}, requested {version}."
                ],
            )
        return CapabilityDiscoveryResult(status=PASS, capability=deepcopy(record))

    def list_available(self) -> list[dict[str, Any]]:
        return [deepcopy(self._records[key]) for key in sorted(self._records)]

    def validate_capability(
        self,
        capability_id: str,
        request: dict[str, Any],
        *,
        version: str | None = None,
    ):
        from .capability import GateResult, NOT_EVALUABLE, PASS

        record = self._records.get(capability_id)
        if record is None:
            return GateResult(
                verdict=NOT_EVALUABLE,
                reasons=[f"Unknown capability: {capability_id}"],
                code="UNKNOWN_CAPABILITY",
            )
        if version is not None and str(record["version"]) != str(version):
            return GateResult(
                verdict=NOT_EVALUABLE,
                reasons=[
                    f"Capability {capability_id} requires version {record['version']}, "
                    f"requested {version}."
                ],
                code="INCOMPATIBLE_VERSION",
            )

        reasons: list[str] = []
        inputs = set(request.get("inputs", []))
        missing_inputs = [item for item in record["required_inputs"] if item not in inputs and item not in request]
        tools = request.get("tools", {})
        available_tools = set(tools) if isinstance(tools, dict) else set(tools or [])
        missing_tools = [item for item in record["required_tools"] if item not in available_tools]
        artifacts = set(request.get("artifacts", []))
        missing_artifacts = [
            pattern for pattern in record["required_artifacts"]
            if not any(_artifact_matches(pattern, artifact) for artifact in artifacts)
        ]
        for item in missing_inputs:
            reasons.append(f"Missing required input: {item}")
        for item in missing_tools:
            reasons.append(f"Missing required tool: {item}")
        for item in missing_artifacts:
            reasons.append(f"Missing required artifact: {item}")

        gate_reasons: list[str] = []
        samples = request.get("samples")
        min_samples = record.get("gates", {}).get("min_samples")
        if min_samples is not None and isinstance(samples, list) and len(samples) < int(min_samples):
            gate_reasons.append(f"Requires at least {min_samples} samples, received {len(samples)}")
        required_sample_fields = record.get("gates", {}).get("required_sample_fields", [])
        if isinstance(samples, list):
            for index, sample in enumerate(samples, start=1):
                missing_fields = [field for field in required_sample_fields if not sample.get(field)]
                if missing_fields:
                    gate_reasons.append(f"Sample {index} missing fields: {', '.join(missing_fields)}")
        reasons.extend(gate_reasons)

        for key, expected in record.get("supports", {}).items():
            actual = request.get(key)
            if actual is not None and isinstance(expected, list) and actual not in expected:
                reasons.append(f"Unsupported {key}: {actual}")

        if reasons:
            return GateResult(
                verdict=NOT_EVALUABLE,
                reasons=reasons,
                code="GATE_NOT_MET" if gate_reasons else "REQUIREMENTS_NOT_MET",
                details={
                    "required_inputs": record["required_inputs"],
                    "required_tools": record["required_tools"],
                    "required_artifacts": record["required_artifacts"],
                },
            )
        return GateResult(
            verdict=PASS,
            details={
                "required_inputs": record["required_inputs"],
                "required_tools": record["required_tools"],
                "required_artifacts": record["required_artifacts"],
            },
        )


def _artifact_matches(pattern: str, artifact: str) -> bool:
    if pattern == artifact:
        return True
    if "*" not in pattern:
        return False
    prefix, suffix = pattern.split("*", 1)
    return artifact.startswith(prefix) and artifact.endswith(suffix)


def _default_registry_path() -> Path:
    return Path(__file__).resolve().parents[2] / "references" / "capability-registry.json"


def _validate_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise CapabilityRegistryError("Unsupported capability registry schema_version")
    records = payload.get("capabilities")
    if not isinstance(records, list):
        raise CapabilityRegistryError("capabilities must be a list")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise CapabilityRegistryError(f"capabilities[{index}] must be an object")
        missing = [field for field in REQUIRED_FIELDS if field not in record]
        if missing:
            raise CapabilityRegistryError(f"capabilities[{index}] missing fields: {', '.join(missing)}")
        capability_id = record["id"]
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise CapabilityRegistryError(f"capabilities[{index}].id must be a non-empty string")
        if capability_id in seen:
            raise CapabilityRegistryError(f"duplicate capability id: {capability_id}")
        seen.add(capability_id)
        if not isinstance(record["version"], str) or not record["version"].strip():
            raise CapabilityRegistryError(f"{capability_id}.version must be a non-empty string")
        if record["entry_stage"] not in ALLOWED_ENTRY_STAGES:
            raise CapabilityRegistryError(f"{capability_id}.entry_stage is unsupported")
        for field in ("required_inputs", "required_tools", "required_artifacts"):
            if not isinstance(record[field], list) or not all(isinstance(item, str) for item in record[field]):
                raise CapabilityRegistryError(f"{capability_id}.{field} must be a list of strings")
        for field in ("supports", "gates"):
            if not isinstance(record[field], dict):
                raise CapabilityRegistryError(f"{capability_id}.{field} must be an object")
        unknown_supports = set(record["supports"]) - ALLOWED_SUPPORT_KEYS
        if unknown_supports:
            raise CapabilityRegistryError(f"{capability_id}.supports has unknown keys: {sorted(unknown_supports)}")
        for key, value in record["supports"].items():
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
                raise CapabilityRegistryError(f"{capability_id}.supports.{key} must be a non-empty list of strings")
        unknown_gates = set(record["gates"]) - ALLOWED_GATE_KEYS
        if unknown_gates:
            raise CapabilityRegistryError(f"{capability_id}.gates has unknown keys: {sorted(unknown_gates)}")
        gates = record["gates"]
        for key in ("min_samples", "min_replicates_per_group"):
            if key in gates and (not isinstance(gates[key], int) or isinstance(gates[key], bool) or gates[key] < 1):
                raise CapabilityRegistryError(f"{capability_id}.gates.{key} must be a positive integer")
        for key in ("required_sample_fields", "required_reference_keys"):
            if key in gates and (not isinstance(gates[key], list) or not gates[key] or not all(isinstance(item, str) and item.strip() for item in gates[key])):
                raise CapabilityRegistryError(f"{capability_id}.gates.{key} must be a non-empty list of strings")
        for field in ("required_inputs", "required_tools", "required_artifacts"):
            if not record[field] or any(not item.strip() for item in record[field]):
                raise CapabilityRegistryError(f"{capability_id}.{field} must not contain empty strings")
        validated.append(deepcopy(record))
    return validated


def _legacy_records() -> list[dict[str, Any]]:
    from .capability import register_legacy_capabilities

    return [register_legacy_capabilities()[key] for key in sorted(register_legacy_capabilities())]


def load_capability_registry(path: str | Path | None = None) -> CapabilityRegistry:
    """Load the JSON registry, falling back to the historical Python registry."""

    registry_path = Path(path) if path is not None else _default_registry_path()
    if not registry_path.is_file():
        return CapabilityRegistry(_legacy_records(), source="legacy-python")
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
        records = _validate_payload(payload)
    except (OSError, json.JSONDecodeError, CapabilityRegistryError) as exc:
        raise CapabilityRegistryError(f"Invalid capability registry {registry_path}: {exc}") from exc
    return CapabilityRegistry(records, source="json")
