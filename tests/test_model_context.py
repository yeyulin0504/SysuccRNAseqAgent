from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from rnaseq_agent.model_context import read_model_data_revisions
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
