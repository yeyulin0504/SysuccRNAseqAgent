from __future__ import annotations

from pathlib import Path

from rnaseq_agent.model_context import ModelContextBuilder
from rnaseq_agent.model_disclosure import ProviderCredentials
from rnaseq_agent.model_provider import context_free_prepared_request, normalize_provider_config


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

