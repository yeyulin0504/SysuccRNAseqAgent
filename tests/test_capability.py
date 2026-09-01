"""Tests for the capability registry, Gate-A checks, and execution plans."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rnaseq_agent.capability import (
    NOT_EVALUABLE,
    PASS,
    build_execution_plan,
    gate_a_check,
    list_capabilities,
    resolve_capability,
)
from rnaseq_agent.defaults import DEFAULT_REFERENCE


def _base_config() -> dict:
    return {
        "schema_version": 1,
        "project": {"id": "demo", "title": "Demo", "owner": "test"},
        "server": {
            "host": "localhost",
            "user": "test",
            "remote_base_dir": "/data/users/test/rnaseq_projects",
            "remote_workdir": "/data/users/test/rnaseq_projects/demo",
            "scheduler": "local",
            "threads": 4,
            "memory_gb": 16,
            "shell": "bash",
            "init_commands": [],
        },
        "reference": dict(DEFAULT_REFERENCE),
        "sequencing": {"layout": "paired", "reads_per_sample_million": 20, "strandedness": "auto"},
        "samples": {
            "source": "local_upload",
            "local_data_dir": "tests/fixtures/fastq",
            "remote_data_dir": "remote",
            "items": [
                {
                    "sample_id": "sample_a",
                    "condition": "control",
                    "fastq_1": "a_R1.fastq.gz",
                    "fastq_2": "a_R2.fastq.gz",
                },
                {
                    "sample_id": "sample_b",
                    "condition": "treatment",
                    "fastq_1": "b_R1.fastq.gz",
                    "fastq_2": "b_R2.fastq.gz",
                },
            ],
        },
        "pipeline": {
            "fastp": {"enabled": True, "version": "0.24.1"},
            "star": {"enabled": True, "version": "2.7.11b"},
            "arriba": {"enabled": False, "version": "2.5.0"},
            "featurecounts": {"enabled": True, "version": "Subread 2.1.1"},
            "rsem": {"enabled": False, "version": "1.2.28"},
        },
        "polling": {"interval_seconds": 300, "timeout_hours": 168},
        "notification": {"email_enabled": False},
    }


class TestCapabilityRegistry:
    def test_lists_builtin_capabilities(self) -> None:
        caps = list_capabilities()
        ids = [cap.capability_id for cap in caps]
        assert "bulk_rnaseq_expression_v1" in ids
        assert "bulk_rnaseq_full_v1" in ids

    def test_capability_version_and_contract(self) -> None:
        cap = resolve_capability("bulk_rnaseq_expression_v1")
        assert cap.version == "1.0.0"
        assert cap.input_contract["data_type"] == "bulk_rna_seq_fastq"
        assert "paired" in cap.input_contract["layout"]
        assert cap.requires_reference_keys

    def test_unknown_capability_raises(self) -> None:
        with pytest.raises(ValueError):
            resolve_capability("not_a_capability")


class TestGateA:
    def test_passes_for_valid_two_group_config(self) -> None:
        cap = resolve_capability("bulk_rnaseq_expression_v1")
        result = gate_a_check(cap, _base_config())
        assert result.verdict == PASS
        assert result.ok

    def test_too_many_conditions_is_not_evaluable(self) -> None:
        cap = resolve_capability("bulk_rnaseq_expression_v1")
        config = _base_config()
        config["samples"]["items"].append(
            {
                "sample_id": "sample_c",
                "condition": "third_group",
                "fastq_1": "c_R1.fastq.gz",
                "fastq_2": "c_R2.fastq.gz",
            }
        )
        result = gate_a_check(cap, config)
        assert result.verdict == NOT_EVALUABLE
        assert any("分组" in reason for reason in result.reasons)

    def test_missing_sample_fields_is_not_evaluable(self) -> None:
        cap = resolve_capability("bulk_rnaseq_expression_v1")
        config = _base_config()
        config["samples"]["items"][0]["condition"] = ""
        result = gate_a_check(cap, config)
        assert result.verdict == NOT_EVALUABLE
        assert any("缺少字段" in reason for reason in result.reasons)

    def test_unsupported_layout_is_not_evaluable(self) -> None:
        cap = resolve_capability("bulk_rnaseq_expression_v1")
        config = _base_config()
        config["sequencing"]["layout"] = "amplicon"
        result = gate_a_check(cap, config)
        assert result.verdict == NOT_EVALUABLE

    def test_missing_reference_key_is_not_evaluable(self) -> None:
        cap = resolve_capability("bulk_rnaseq_expression_v1")
        config = _base_config()
        config["reference"]["star_index_dir"] = ""
        result = gate_a_check(cap, config)
        assert result.verdict == NOT_EVALUABLE
        assert any("star_index_dir" in reason for reason in result.reasons)

    def test_passes_validation_errors_through(self) -> None:
        cap = resolve_capability("bulk_rnaseq_expression_v1")
        result = gate_a_check(cap, _base_config(), validation_errors=["fake error"], missing_files=["a.fastq.gz"])
        assert result.verdict == NOT_EVALUABLE
        assert any("fake error" in reason for reason in result.reasons)
        assert any("缺少输入文件" in reason for reason in result.reasons)

    def test_formatted_messages(self) -> None:
        cap = resolve_capability("bulk_rnaseq_expression_v1")
        passed = gate_a_check(cap, _base_config())
        assert passed.formatted()[0].startswith("通过")
        failed = gate_a_check(cap, _base_config(), validation_errors=["boom"])
        assert failed.formatted()[0].startswith("不适用")


class TestExecutionPlan:
    def test_plan_is_deterministic(self) -> None:
        cap = resolve_capability("bulk_rnaseq_expression_v1")
        plan_a = build_execution_plan(cap, _base_config())
        plan_b = build_execution_plan(cap, _base_config())
        assert plan_a.summary == plan_b.summary
        assert plan_a.steps == plan_b.steps
        assert plan_a.capability_id == "bulk_rnaseq_expression_v1"

    def test_plan_describes_steps_and_samples(self) -> None:
        cap = resolve_capability("bulk_rnaseq_expression_v1")
        plan = build_execution_plan(cap, _base_config())
        assert any("2 个" in step for step in plan.steps)
        assert any("fastp -> star -> featurecounts" in step.lower() for step in plan.steps)

    def test_plan_does_not_mutate_config(self) -> None:
        cap = resolve_capability("bulk_rnaseq_expression_v1")
        config = _base_config()
        before = repr(config)
        build_execution_plan(cap, config)
        assert repr(config) == before
