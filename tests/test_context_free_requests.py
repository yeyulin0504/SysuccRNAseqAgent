from __future__ import annotations

from pathlib import Path
import pytest

from rnaseq_agent.model_context import ModelContextBuilder
from rnaseq_agent.model_context import read_model_data_revisions
from rnaseq_agent.model_disclosure import (
    ClaimedDataGrant,
    GrantBindings,
    ProviderCredentials,
)
from rnaseq_agent.model_provider import context_free_prepared_request, normalize_provider_config, provider_identity
import json


def _config(mode: str):
    return normalize_provider_config({
        "provider": "openai",
        "api_base": "https://llm.example/v1",
        "model": "gpt-test",
        "api_mode": mode,
    })


def test_context_free_builder_does_not_discover_project(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    context = ModelContextBuilder.build(
        project_dir=missing,
        project_id="",
        thread_id="",
        provider=_config("chat_completions"),
        system_prompt="router",
        current_user_message="hello",
        durable_messages=(),
        claimed_grant=None,
    )

    assert [message["content"] for message in context.messages] == ["router", "hello"]
    assert context.disclosure_manifest.fields == ()
    assert context.disclosure_manifest.grant_id_hash is None


def test_context_free_responses_request_is_store_false_and_has_no_tools() -> None:
    config = _config("responses")
    context = ModelContextBuilder.build(
        project_dir=None,
        project_id="",
        thread_id="",
        provider=config,
        system_prompt="router",
        current_user_message="hello",
        durable_messages=(),
        claimed_grant=None,
    )

    request = context_free_prepared_request(
        config=config,
        credentials=ProviderCredentials(api_key="secret"),
        context=context,
        timeout_seconds=12,
    )

    assert request.payload == {
        "model": "gpt-test",
        "instructions": "router",
        "input": [{"role": "user", "content": "hello"}],
        "store": False,
    }
    assert "tools" not in request.payload
    assert "tool_choice" not in request.payload
    assert request.disclosure_manifest is None


def test_context_free_chat_request_has_only_context_messages() -> None:
    config = _config("chat_completions")
    context = ModelContextBuilder.build(
        project_dir=None,
        project_id="",
        thread_id="",
        provider=config,
        system_prompt="router",
        current_user_message="hello",
        durable_messages=(),
        claimed_grant=None,
    )

    request = context_free_prepared_request(
        config=config,
        credentials=ProviderCredentials(),
        context=context,
        timeout_seconds=12,
    )

    assert request.payload["messages"] == [
        {"role": "system", "content": "router"},
        {"role": "user", "content": "hello"},
    ]
    assert "tools" not in request.payload
    assert "tool_choice" not in request.payload


def _local_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.json").write_text(json.dumps({
        "route": {"id": "bulk_rna"},
        "samples": {"items": [{
            "sample_id": "SENSITIVE_SAMPLE_001",
            "condition": "tumor",
            "fastq_1": "/private/raw/SENSITIVE_SAMPLE_001_R1.fastq.gz",
            "fastq_2": "/private/raw/SENSITIVE_SAMPLE_001_R2.fastq.gz",
        }]},
    }), encoding="utf-8")
    return project


def test_claimed_sample_ids_are_request_local_and_manifest_is_metadata_only(tmp_path: Path) -> None:
    project = _local_project(tmp_path)
    config = _config("chat_completions")
    revisions = read_model_data_revisions(project, config)
    claim = ClaimedDataGrant(
        grant_id="data_grant_sensitive",
        claim_token="one-time-token",
        bindings=GrantBindings("project-1", "thread-1", provider_identity(config).digest, "read_only", revisions),
        fields=("sample_ids",),
        exact_values={"sample_ids": ("SENSITIVE_SAMPLE_001",)},
    )

    context = ModelContextBuilder.build(
        project_dir=project,
        project_id="project-1",
        thread_id="thread-1",
        provider=config,
        system_prompt="system",
        current_user_message="review the approved sample id",
        durable_messages=(),
        claimed_grant=claim,
    )
    exact_text = "\n".join(str(message.get("content")) for message in context.messages)
    assert "SENSITIVE_SAMPLE_001" in exact_text
    assert "SENSITIVE_SAMPLE_001_R1.fastq.gz" not in exact_text
    assert "/private/raw" not in exact_text
    manifest = context.disclosure_manifest
    assert manifest.fields == ("sample_ids",)
    assert manifest.record_counts == {"sample_ids": 1}
    assert manifest.byte_length > 0
    assert "SENSITIVE_SAMPLE_001" not in json.dumps(manifest.__dict__, ensure_ascii=False, default=str)
    assert "data_grant_sensitive" not in json.dumps(manifest.__dict__, ensure_ascii=False, default=str)

    request = context_free_prepared_request(
        config=config,
        credentials=ProviderCredentials(),
        context=context,
        timeout_seconds=12,
        exact_attempt=True,
    )
    assert request.exact_attempt is True
    assert request.disclosure_manifest == manifest
    assert "tools" not in request.payload
    assert "tool_choice" not in request.payload


def test_claimed_non_sample_scope_fails_closed_without_disclosing_values(tmp_path: Path) -> None:
    project = _local_project(tmp_path)
    config = _config("chat_completions")
    revisions = read_model_data_revisions(project, config)
    claim = ClaimedDataGrant(
        grant_id="data_grant_fastq",
        claim_token="one-time-token",
        bindings=GrantBindings("project-1", "thread-1", provider_identity(config).digest, "read_only", revisions),
        fields=("fastq_filenames",),
        exact_values={"fastq_filenames": ("SENSITIVE_SAMPLE_001_R1.fastq.gz",)},
    )
    with pytest.raises(ValueError, match="MODEL_DATA_SCOPE_UNSUPPORTED"):
        ModelContextBuilder.build(
            project_dir=project,
            project_id="project-1",
            thread_id="thread-1",
            provider=config,
            system_prompt="system",
            current_user_message="show approved data",
            durable_messages=(),
            claimed_grant=claim,
        )