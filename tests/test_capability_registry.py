"""Tests for the declarative capability and pipeline registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rnaseq_agent.capability import NOT_EVALUABLE, PASS
from rnaseq_agent.capability_registry import (
    CapabilityDiscoveryResult,
    CapabilityRegistryError,
    load_capability_registry,
)


def _registry_payload() -> dict:
    return {
        "schema_version": 1,
        "capabilities": [
            {
                "id": "workflow.test.qc",
                "version": "1.2.0",
                "entry_stage": "qc",
                "required_inputs": ["samples"],
                "required_tools": ["fastp"],
                "required_artifacts": ["fastp/*.json"],
                "supports": {"input_kind": ["bulk_rna_seq_fastq"]},
                "gates": {"required_sample_fields": ["sample_id", "fastq_1"]},
            }
        ],
    }


def test_load_registry_validates_declarative_schema_and_fields(tmp_path: Path) -> None:
    path = tmp_path / "capability-registry.json"
    path.write_text(json.dumps(_registry_payload()), encoding="utf-8")

    registry = load_capability_registry(path)

    record = registry.get("workflow.test.qc", version="1.2.0")
    assert record["entry_stage"] == "qc"
    assert record["required_inputs"] == ["samples"]
    assert registry.list_available() == [record]


def test_registry_unknown_and_incompatible_versions_are_structured_not_evaluable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capability-registry.json"
    path.write_text(json.dumps(_registry_payload()), encoding="utf-8")
    registry = load_capability_registry(path)

    unknown = registry.validate_capability("missing", {})
    incompatible = registry.validate_capability("workflow.test.qc", {}, version="9.0.0")

    assert unknown.verdict == NOT_EVALUABLE
    assert incompatible.verdict == NOT_EVALUABLE
    assert unknown.code == "UNKNOWN_CAPABILITY"
    assert incompatible.code == "INCOMPATIBLE_VERSION"
    assert unknown.to_dict()["status"] == NOT_EVALUABLE


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["capabilities"][0].update({"entry_stage": 1}),
        lambda payload: payload["capabilities"][0].update({"entry_stage": "unknown"}),
        lambda payload: payload["capabilities"][0].update({"supports": {"layout": "paired"}}),
        lambda payload: payload["capabilities"][0].update({"supports": {"layout": ["paired", 1]}}),
        lambda payload: payload["capabilities"][0].update({"gates": []}),
        lambda payload: payload["capabilities"][0].update({"required_tools": ["fastp", None]}),
        lambda payload: payload["capabilities"][0].update({"required_inputs": "samples"}),
        lambda payload: payload["capabilities"][0].update({"required_artifacts": [" "]}),
    ],
)
def test_malformed_registry_payload_fails_closed(tmp_path: Path, mutate) -> None:
    path = tmp_path / "capability-registry.json"
    payload = _registry_payload()
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CapabilityRegistryError):
        load_capability_registry(path)


def test_registry_gate_reads_required_inputs_tools_and_artifacts(tmp_path: Path) -> None:
    path = tmp_path / "capability-registry.json"
    path.write_text(json.dumps(_registry_payload()), encoding="utf-8")
    registry = load_capability_registry(path)

    result = registry.validate_capability(
        "workflow.test.qc",
        {
            "inputs": ["samples"],
            "tools": {"fastp": "0.24.1"},
            "artifacts": ["fastp/sample.json"],
            "input_kind": "bulk_rna_seq_fastq",
            "samples": [{"sample_id": "s1", "fastq_1": "s1_R1.fastq.gz"}],
        },
        version="1.2.0",
    )

    assert result.verdict == PASS
    assert result.details["required_inputs"] == ["samples"]
    assert result.details["required_tools"] == ["fastp"]
    assert result.details["required_artifacts"] == ["fastp/*.json"]


def test_registry_gate_applies_declared_sample_gate(tmp_path: Path) -> None:
    path = tmp_path / "capability-registry.json"
    payload = _registry_payload()
    payload["capabilities"][0]["gates"]["min_samples"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")
    registry = load_capability_registry(path)

    result = registry.validate_capability(
        "workflow.test.qc",
        {
            "inputs": ["samples"],
            "tools": {"fastp": "0.24.1"},
            "artifacts": ["fastp/sample.json"],
            "input_kind": "bulk_rna_seq_fastq",
            "samples": [{"sample_id": "s1", "fastq_1": "s1_R1.fastq.gz"}],
        },
    )

    assert result.verdict == NOT_EVALUABLE
    assert result.code == "GATE_NOT_MET"


def test_capability_loader_can_use_legacy_python_registry_when_json_missing() -> None:
    registry = load_capability_registry(Path("does-not-exist.json"))

    record = registry.get("workflow.bulk_rna.grch38_pe_expression_fusion")
    assert record["version"] == "1.0.0"
    assert registry.source == "legacy-python"


def test_discovery_result_has_one_type_for_success_and_failure(tmp_path: Path) -> None:
    path = tmp_path / "capability-registry.json"
    path.write_text(json.dumps(_registry_payload()), encoding="utf-8")
    registry = load_capability_registry(path)

    success = registry.discover("workflow.test.qc", version="1.2.0")
    failure = registry.discover("missing")

    assert isinstance(success, CapabilityDiscoveryResult)
    assert isinstance(failure, CapabilityDiscoveryResult)
    assert success.ok and success.capability["id"] == "workflow.test.qc"
    assert not failure.ok and failure.status == NOT_EVALUABLE
