from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import pytest

from rnaseq_agent.model_context import (
    build_safe_project_summary,
    EphemeralArgumentError,
    EphemeralToolCallStore,
    MAX_ENTRY_BYTES,
    read_model_data_revisions,
    project_tool_arguments_for_model,
    project_tool_result_for_log,
    project_tool_result_for_model,
)
from rnaseq_agent.agent_tools import TOOL_SPECS
from rnaseq_agent.model_disclosure import POLICY_VERSION
from rnaseq_agent.model_provider import (
    normalize_provider_config,
    provider_config_revision,
    provider_identity,
)


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _seed_revision_project(project_dir: Path) -> None:
    project_dir.mkdir()
    _write_json(project_dir / "project.json", {
        "project": {"id": "project-73"},
        "route": {"id": "bulk_rna"},
        "sequencing": {"layout": "paired", "strandedness": "unknown"},
        "samples": {"source": "local_upload", "items": [{
            "sample_id": "PATIENT_SENTINEL_73",
            "condition": "tumor",
            "fastq_1": "TUMOR_SENTINEL_R1.fastq.gz",
            "fastq_2": "TUMOR_SENTINEL_R2.fastq.gz",
        }]},
        "pipeline": {"fastp": {"enabled": True}},
        "status": {"state": "input_ready", "job_id": "JOB_SENTINEL_73"},
    })
    _write_json(project_dir / "session.json", {"state": "input_ready"})
    _write_json(project_dir / "intake.json", {"route": "bulk_rna", "input_kind": "fastq"})
    (project_dir / "report.md").write_text("REPORT_SENTINEL_73", encoding="utf-8")


def test_provider_identity_is_normalized_and_secret_independent() -> None:
    first = normalize_provider_config({
        "provider": " OpenAI ",
        "api_base": "HTTPS://LLM.Example:443/v1/",
        "model": "gpt-test",
        "api_key": "API_KEY_SENTINEL_A",
    })
    second = normalize_provider_config({
        "provider": "openai",
        "api_base": "https://llm.example/v1",
        "model": "gpt-test",
        "api_key": "API_KEY_SENTINEL_B",
    })
    assert first == second
    assert provider_identity(first) == provider_identity(second)
    assert provider_identity(first).origin == "https://llm.example"
    assert "API_KEY_SENTINEL" not in repr(provider_identity(first))
    assert provider_identity(replace(first, model="gpt-other")).digest != provider_identity(first).digest


def test_origin_identity_ignores_path_but_config_revision_does_not() -> None:
    v1 = normalize_provider_config({
        "provider": "openai", "api_base": "https://llm.example/v1", "model": "gpt-test",
    })
    tenant = normalize_provider_config({
        "provider": "openai", "api_base": "https://llm.example/tenant-a/v1", "model": "gpt-test",
    })
    assert provider_identity(v1) == provider_identity(tenant)
    assert provider_config_revision(v1) != provider_config_revision(tenant)


def test_idna_equivalent_hosts_normalize_identically() -> None:
    unicode_host = normalize_provider_config({
        "provider": "openai", "api_base": "https://bücher.example/v1", "model": "gpt-test",
    })
    ascii_host = normalize_provider_config({
        "provider": "openai", "api_base": "https://xn--bcher-kva.example/v1", "model": "gpt-test",
    })
    assert unicode_host == ascii_host
    assert provider_identity(unicode_host) == provider_identity(ascii_host)
    assert provider_config_revision(unicode_host) == provider_config_revision(ascii_host)


def test_revisions_separate_sample_report_and_provider_changes(tmp_path: Path) -> None:
    project_dir = tmp_path / "project-73"
    _seed_revision_project(project_dir)
    provider = normalize_provider_config({
        "provider": "openai", "api_base": "https://llm.example/v1", "model": "gpt-test"
    })
    before = read_model_data_revisions(project_dir, provider)
    assert before.policy_version == POLICY_VERSION == 1
    assert before.remote_scan_revision is None

    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    project["server"] = {"threads": 32}
    _write_json(project_dir / "project.json", project)
    resources_changed = read_model_data_revisions(project_dir, provider)
    assert resources_changed.project_revision != before.project_revision
    assert resources_changed.sample_revision == before.sample_revision

    project["samples"]["items"][0]["sample_id"] = "PATIENT_SENTINEL_74"
    _write_json(project_dir / "project.json", project)
    sample_changed = read_model_data_revisions(project_dir, provider)
    assert sample_changed.sample_revision != before.sample_revision

    (project_dir / "report.md").write_text("REPORT_SENTINEL_74", encoding="utf-8")
    report_changed = read_model_data_revisions(project_dir, provider)
    assert report_changed.report_revision != sample_changed.report_revision

    other_provider = replace(provider, model="gpt-other")
    assert read_model_data_revisions(project_dir, other_provider).provider_config_revision != report_changed.provider_config_revision


FORBIDDEN_SENTINELS = (
    "PATIENT_SENTINEL_73", "TUMOR_SENTINEL_R1.fastq.gz",
    "/restricted/SENTINEL_73/fastq", "HOST_SENTINEL_73",
    "USER_SENTINEL_73", "JOB_SENTINEL_73", "API_KEY_SENTINEL_73",
)


def _serialized(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_default_summary_keeps_scientific_counts_but_no_exact_values(tmp_path: Path) -> None:
    project_dir = tmp_path / "project-73"
    _seed_revision_project(project_dir)
    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    project["server"] = {"host": "HOST_SENTINEL_73", "user": "USER_SENTINEL_73", "remote_workdir": "/restricted/SENTINEL_73/fastq"}
    project["status"]["job_id"] = "JOB_SENTINEL_73"
    _write_json(project_dir / "project.json", project)
    summary = build_safe_project_summary(project_dir)
    encoded = _serialized(summary)
    assert summary["sample_count"] == 1
    assert summary["condition_counts"] == {"tumor": 1}
    assert summary["sample_aliases"] == ["sample_001"]
    assert summary["sequencing"] == {"layout": "paired", "strandedness": "unknown"}
    assert all(value not in encoded for value in FORBIDDEN_SENTINELS)


def test_every_tool_result_uses_explicit_safe_projection() -> None:
    full = {"ok": True, "reply": "PATIENT_SENTINEL_73 at /restricted/SENTINEL_73/fastq",
            "samples": [{"sample_id": "PATIENT_SENTINEL_73", "fastq_1": "TUMOR_SENTINEL_R1.fastq.gz"}],
            "scanned_path": "/restricted/SENTINEL_73/fastq", "report": "REPORT_SENTINEL_73",
            "report_path": "/restricted/report.md", "job_id": "JOB_SENTINEL_73",
            "source_ref": "src_0123456789abcdef0123456789abcdef", "state": "running"}
    names = list(TOOL_SPECS) + ["unknown_tool"]
    for name in names:
        projected = project_tool_result_for_model(name, full)
        logged = project_tool_result_for_log(name, full)
        assert all(value not in _serialized(projected) for value in FORBIDDEN_SENTINELS)
        assert "REPORT_SENTINEL_73" not in _serialized(projected)
        assert all(value not in _serialized(logged) for value in FORBIDDEN_SENTINELS)
        assert "REPORT_SENTINEL_73" not in _serialized(logged)
    browse = project_tool_result_for_model("browse_remote_samples", full)
    assert browse == {"ok": True, "sample_count": 1, "paired_count": 0,
                      "unmatched_count": 0, "directory_count": 1,
                      "truncated": False, "authorization": "inside_approved_root",
                      "source_ref": full["source_ref"]}


def test_read_project_state_rebuilds_nested_summary_without_trusting_values() -> None:
    hostile = {
        "ok": True,
        "summary": {
            "project_state": "ready",
            "sample_count": 3,
            "sample_aliases": ["PATIENT_SENTINEL_SUMMARY", "/restricted/SENTINEL_SUMMARY"],
            "condition_counts": {"tumor": 2, "sample_id": 99},
            "references": {"gtf_present": True, "path": "/restricted/SENTINEL_SUMMARY"},
            "errors": [{"category": "run_failed", "message": "REPORT_SENTINEL_SUMMARY"}],
            "unknown": {"secret": "PATIENT_SENTINEL_SUMMARY"},
        },
    }
    projected = project_tool_result_for_model("read_project_state", hostile)
    encoded = _serialized(projected)
    assert "PATIENT_SENTINEL_SUMMARY" not in encoded
    assert "/restricted/SENTINEL_SUMMARY" not in encoded
    assert "REPORT_SENTINEL_SUMMARY" not in encoded
    assert projected["summary"]["sample_aliases"] == ["sample_001", "sample_002"]
    assert "unknown" not in projected["summary"]


def test_tool_argument_projector_is_allowlisted_for_every_registered_tool() -> None:
    hostile = {"sample_id": "PATIENT_SENTINEL_73", "path": "/restricted/SENTINEL_73/fastq",
               "host": "HOST_SENTINEL_73", "user": "USER_SENTINEL_73", "password": "API_KEY_SENTINEL_73",
               "filename": "TUMOR_SENTINEL_R1.fastq.gz", "report": "REPORT_SENTINEL_73",
               "threads": 999999, "memory_gb": 999999, "stage": "de", "unknown": {"secret": "x"}}
    for name in TOOL_SPECS:
        projected = project_tool_arguments_for_model(name, hostile)
        encoded = _serialized(projected)
        assert all(value not in encoded for value in FORBIDDEN_SENTINELS)
        assert "REPORT_SENTINEL_73" not in encoded
    unknown = project_tool_arguments_for_model("unknown_tool", hostile)
    assert unknown["unmapped"] is True


def test_ephemeral_store_rejects_binding_and_duplicate_keys() -> None:
    store = EphemeralToolCallStore()
    ref = store.put("p", "t", "c", "read_project_state", {"x": 1})
    with pytest.raises(EphemeralArgumentError) as mismatch:
        store.get(ref, project_id="other")
    assert mismatch.value.code == "EPHEMERAL_ARGUMENT_BINDING_MISMATCH"
    partial = store.put("p", "t", "f", "read_project_state", None)
    with pytest.raises(EphemeralArgumentError):
        store.append_fragment(partial, '{"x":1,"x":2}', final=True)
    assert store.live_entries == 2


def test_ephemeral_store_fragment_limits_and_no_partial_get() -> None:
    store = EphemeralToolCallStore()
    ref = store.put("p", "t", "c", "read_project_state", None)
    store.append_fragment(ref, '{"x":', final=False)
    with pytest.raises(EphemeralArgumentError) as incomplete:
        store.get(ref)
    assert incomplete.value.code == "EPHEMERAL_ARGUMENT_INCOMPLETE"
    store.append_fragment(ref, '1}', final=True)
    assert store.get(ref) == {"x": 1}
    too_large = store.put("p", "t", "large", "read_project_state", {"x": "ok"})
    with pytest.raises(EphemeralArgumentError):
        store.append_fragment(too_large, "x" * (16 * 1024 + 1))
    assert MAX_ENTRY_BYTES > 0
