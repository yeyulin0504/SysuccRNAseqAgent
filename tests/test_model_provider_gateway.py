from __future__ import annotations

import json
from dataclasses import replace

import pytest

from rnaseq_agent.model_disclosure import (
    MODEL_CONTEXT_SECRET_DETECTED,
    MODEL_EXACT_TOOL_CALL_REJECTED,
    MODEL_PROVIDER_REQUEST_FAILED,
    PreparedModelRequest,
    ProviderCredentials,
    ProviderRequestError,
)
from rnaseq_agent.model_provider import MAX_RESPONSE_BYTES, ModelProviderGateway, normalize_provider_config, provider_identity


@pytest.fixture
def base_request() -> PreparedModelRequest:
    config = normalize_provider_config({"provider": "openai", "api_base": "https://llm.example/v1", "model": "gpt-test"})
    return PreparedModelRequest(
        provider=config,
        identity=provider_identity(config),
        api_mode="chat_completions",
        payload={"model": "gpt-test", "messages": [{"role": "user", "content": "safe"}]},
        credentials=ProviderCredentials(api_key="provider-key-not-in-body"),
        timeout_seconds=10.0,
    )


@pytest.mark.parametrize("value,category", [
    ("API_KEY_SENTINEL_73", "known_secret"),
    ("PASSWORD_SENTINEL_73", "known_secret"),
    ("CIPHERTEXT_SENTINEL_73", "protected_value"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "private_key_marker"),
    ("https://alice:URL_PASSWORD_SENTINEL_73@llm.example/v1", "url_userinfo"),
    ("Bearer APP_TOKEN_SENTINEL_73", "application_token"),
])
def test_gateway_blocks_secret_before_transport(base_request, monkeypatch, value, category) -> None:
    calls = []
    monkeypatch.setattr("rnaseq_agent.model_provider.requests.post", lambda *a, **k: calls.append((a, k)))
    request = replace(base_request, payload={"messages": [{"role": "user", "content": value}]}, credentials=ProviderCredentials(api_key="API_KEY_SENTINEL_73", known_secrets=("PASSWORD_SENTINEL_73",), protected_values=("CIPHERTEXT_SENTINEL_73",)))
    with pytest.raises(ProviderRequestError) as caught:
        ModelProviderGateway().complete(request)
    assert caught.value.code == MODEL_CONTEXT_SECRET_DETECTED
    assert caught.value.transmission_started is False
    assert category in str(caught.value)
    assert value not in str(caught.value)
    assert calls == []


def test_configured_api_key_is_scanned(base_request, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("rnaseq_agent.model_provider.requests.post", lambda *a, **k: calls.append((a, k)))
    request = replace(base_request, payload={"messages": [{"role": "user", "content": "provider-key-not-in-body"}]}, credentials=ProviderCredentials(api_key="provider-key-not-in-body"))
    with pytest.raises(ProviderRequestError) as caught:
        ModelProviderGateway().complete(request)
    assert caught.value.code == MODEL_CONTEXT_SECRET_DETECTED
    assert caught.value.transmission_started is False
    assert calls == []


def test_secret_event_ids_are_random(base_request) -> None:
    ids = []
    for _ in range(2):
        with pytest.raises(ProviderRequestError) as caught:
            ModelProviderGateway().complete(replace(base_request, payload={"messages": [{"role": "user", "content": "LOW_ENTROPY_PASSWORD"}]}, credentials=ProviderCredentials(known_secrets=("LOW_ENTROPY_PASSWORD",))))
        ids.append(caught.value.occurrence_id)
    assert ids[0] != ids[1]
    assert all(value.startswith("evt_") for value in ids)


class _Response:
    status_code = 200
    text = ""

    def json(self):
        return {"choices": [{"message": {"content": "hello"}}]}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_complete_uses_gateway_and_maps_reply(base_request, monkeypatch):
    seen = []
    monkeypatch.setattr("rnaseq_agent.model_provider.requests.post", lambda *args, **kwargs: (seen.append((args, kwargs)) or _Response()))
    result = ModelProviderGateway().complete(base_request)
    assert result.text == "hello"
    assert seen[0][1]["headers"]["Authorization"] == "Bearer provider-key-not-in-body"


def test_stream_maps_sse_events(base_request, monkeypatch):
    class StreamResponse(_Response):
        def iter_lines(self, **kwargs):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            yield b"data: [DONE]"

    monkeypatch.setattr("rnaseq_agent.model_provider.requests.post", lambda *args, **kwargs: StreamResponse())
    events = list(ModelProviderGateway().stream(base_request))
    assert [event.kind for event in events] == ["delta", "message"]
    assert events[0].value == "hi"


def test_exact_attempt_is_required(base_request):
    with pytest.raises(ProviderRequestError) as caught:
        list(ModelProviderGateway().dispatch_exact(base_request))
    assert caught.value.code == MODEL_EXACT_TOOL_CALL_REJECTED


def test_exact_request_omits_tools_keys_and_buffers_tool_failures(base_request, monkeypatch):
    seen = []

    class ExactResponse(_Response):
        def iter_lines(self, **kwargs):
            yield b'data: {"choices":[{"delta":{"content":"safe"}}]}'
            yield b'data: {"choices":[{"delta":{"tool_calls":[{"id":"x1","function":{"name":"evil","arguments":"{}"}}]}}]}'
            yield b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}'
            yield b"data: [DONE]"

    def post(*args, **kwargs):
        seen.append(kwargs["data"])
        return ExactResponse()

    monkeypatch.setattr("rnaseq_agent.model_provider.requests.post", post)
    request = replace(base_request, exact_attempt=True, payload={**base_request.payload, "tools": [{"type": "function"}], "tool_choice": "auto", "stream": True})
    with pytest.raises(ProviderRequestError) as caught:
        list(ModelProviderGateway().dispatch_exact(request))
    assert caught.value.code == MODEL_EXACT_TOOL_CALL_REJECTED
    assert b'"tools"' not in seen[0]
    assert b'"tool_choice"' not in seen[0]


@pytest.mark.parametrize("marker", [
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN PGP PRIVATE KEY BLOCK-----",
])
def test_gateway_blocks_all_private_key_markers(base_request, marker):
    with pytest.raises(ProviderRequestError) as caught:
        ModelProviderGateway().complete(replace(base_request, payload={"messages": [{"role": "user", "content": marker}]}))
    assert caught.value.code == MODEL_CONTEXT_SECRET_DETECTED


def test_transport_error_marks_transmission_started(base_request, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("network failed")
    monkeypatch.setattr("rnaseq_agent.model_provider.requests.post", fail)
    with pytest.raises(ProviderRequestError) as caught:
        ModelProviderGateway().complete(base_request)
    assert caught.value.code == MODEL_PROVIDER_REQUEST_FAILED
    assert caught.value.transmission_started is True


def test_exact_responses_rejects_nested_function_call_before_exposing_text(base_request, monkeypatch):
    class ResponsesResponse(_Response):
        def json(self):
            return {
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "safe"}]},
                    {"type": "function_call", "name": "exfiltrate", "arguments": "{}", "call_id": "call-1"},
                ]
            }

    monkeypatch.setattr("rnaseq_agent.model_provider.requests.post", lambda *args, **kwargs: ResponsesResponse())
    request = replace(base_request, api_mode="responses", exact_attempt=True)

    with pytest.raises(ProviderRequestError) as caught:
        list(ModelProviderGateway().dispatch_exact(request))

    assert caught.value.code == MODEL_EXACT_TOOL_CALL_REJECTED


def test_responses_rejects_oversized_output_text(base_request, monkeypatch):
    class ResponsesResponse(_Response):
        def json(self):
            return {"output_text": "x" * (MAX_RESPONSE_BYTES + 1)}

    monkeypatch.setattr("rnaseq_agent.model_provider.requests.post", lambda *args, **kwargs: ResponsesResponse())
    request = replace(base_request, api_mode="responses")

    with pytest.raises(ProviderRequestError):
        ModelProviderGateway().responses(request)


def test_responses_rejects_oversized_body_before_json_return(base_request, monkeypatch):
    class ResponsesResponse(_Response):
        content = b"x" * (MAX_RESPONSE_BYTES + 1)

        def json(self):
            raise AssertionError("oversized body must be rejected before parsing")

    monkeypatch.setattr("rnaseq_agent.model_provider.requests.post", lambda *args, **kwargs: ResponsesResponse())
    request = replace(base_request, api_mode="responses")

    with pytest.raises(ProviderRequestError):
        ModelProviderGateway().responses(request)


def test_list_models_rejects_oversized_body(base_request, monkeypatch):
    class ModelsResponse:
        def read(self):
            return b"x" * (MAX_RESPONSE_BYTES + 1)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("rnaseq_agent.model_provider.urllib.request.urlopen", lambda *args, **kwargs: ModelsResponse())

    with pytest.raises(ProviderRequestError):
        ModelProviderGateway().list_models(base_request.provider, base_request.credentials, 10)
