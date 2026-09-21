from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rnaseq_agent.connection_store import save_llm
from rnaseq_agent.model_data_grants import (
    DataGrantError,
    decide_grant,
    grant_record_path,
    issue_grant_request,
    load_grant,
)
from rnaseq_agent.model_disclosure import (
    MODEL_DATA_GRANT_CONSUMED,
    MODEL_DATA_GRANT_INVALID,
    MODEL_DATA_REVISION_CHANGED,
    MODEL_DATA_SCOPE_UNSUPPORTED,
    MODEL_PROVIDER_CHANGED,
    MODEL_PROVIDER_REQUEST_FAILED,
    ProviderEvent,
    ProviderRequestError,
)
from rnaseq_agent.model_exact_service import send_exact_disclosure


SAMPLE = "SENTINEL_SAMPLE_001"
FASTQ = "SENTINEL_R1.fastq.gz"
PASSWORD = "PASSWORD_SENTINEL_73"
API_KEY = "API_KEY_SENTINEL_73"
CIPHERTEXT = "CIPHERTEXT_SENTINEL_73"
URL_USERINFO = "URL_PASSWORD_SENTINEL_73"
PRIVATE_KEY = "PRIVATE_KEY_SENTINEL_73"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.json").write_text(
        json.dumps(
            {
                "project": {"id": "p1"},
                "samples": {
                    "items": [
                        {
                            "sample_id": SAMPLE,
                            "fastq_1": FASTQ,
                            "fastq_2": "SENTINEL_R2.fastq.gz",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return project


def _connection(tmp_path: Path) -> Path:
    connection = tmp_path / "connection"
    save_llm(
        {
            "enabled": True,
            "backend": "openai_compatible",
            "provider": "openai",
            "api_base": "https://example.test/v1",
            "model": "test-model",
            "api_mode": "chat_completions",
            "tool_mode": "read_only",
        },
        store_dir=connection,
    )
    return connection


def _approved(project: Path, connection: Path, *, thread_id: str = "t1"):
    grant = issue_grant_request(
        project,
        project_id="p1",
        thread_id=thread_id,
        fields=("sample_ids",),
        purpose="核对样本命名",
        connection_store_dir=connection,
    )
    decide_grant(project, grant.grant_id, approved=True)
    return grant


def _grant_text(project: Path, grant_id: str) -> str:
    return grant_record_path(project, grant_id).read_text(encoding="utf-8")


def test_concurrent_exact_sends_dispatch_once_and_loser_is_consumed(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    connection = _connection(tmp_path)
    grant = _approved(project, connection)
    dispatch_started = threading.Event()
    allow_events = threading.Event()
    calls: list[dict] = []
    calls_lock = threading.Lock()

    def fake_dispatch(self, request):
        with calls_lock:
            calls.append(dict(request.payload))
        dispatch_started.set()

        def events():
            # Keep the claim transmitting while the other sender reaches the
            # grant lock. This forces the loser through the recovery path.
            allow_events.wait(timeout=2)
            yield ProviderEvent("delta", "safe response", 1)

        return events()

    monkeypatch.setattr(
        "rnaseq_agent.model_provider.ModelProviderGateway.dispatch_exact",
        fake_dispatch,
    )
    kwargs = dict(
        project_id="p1",
        thread_id="t1",
        grant_id=grant.grant_id,
        prompt="请核对样本命名",
        connection_store_dir=connection,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(send_exact_disclosure, project, **kwargs) for _ in range(2)]
        assert dispatch_started.wait(timeout=2)
        time.sleep(0.05)
        allow_events.set()
        results = [future.result(timeout=4) for future in futures]

    assert len(calls) == 1
    assert sorted(result["ok"] for result in results) == [False, True]
    loser = next(result for result in results if not result["ok"])
    assert loser["error_code"] == MODEL_DATA_GRANT_CONSUMED
    assert load_grant(project, grant.grant_id).status in {"consumed_success", "consumed_ambiguous"}
    assert all(SAMPLE not in json.dumps(result, ensure_ascii=False) for result in results)
    assert SAMPLE not in _grant_text(project, grant.grant_id)


@pytest.mark.parametrize(
    "case, mutate, project_id, thread_id, expected_code",
    [
        ("thread", lambda project, connection: None, "p1", "wrong-thread", MODEL_DATA_GRANT_INVALID),
        (
            "provider",
            lambda project, connection: save_llm({"provider": "other-provider"}, store_dir=connection),
            "p1",
            "t1",
            MODEL_PROVIDER_CHANGED,
        ),
        (
            "tool-mode",
            lambda project, connection: save_llm({"tool_mode": "disabled"}, store_dir=connection),
            "p1",
            "t1",
            MODEL_DATA_REVISION_CHANGED,
        ),
        (
            "project-revision",
            lambda project, connection: (project / "project.json").write_text(
                json.dumps(
                    {
                        "project": {"id": "p1"},
                        "samples": {"items": [{"sample_id": SAMPLE}, {"sample_id": "NEW_SAMPLE"}]},
                    }
                ),
                encoding="utf-8",
            ),
            "p1",
            "t1",
            MODEL_DATA_REVISION_CHANGED,
        ),
    ],
)
def test_binding_or_revision_races_fail_before_provider_dispatch(
    tmp_path: Path,
    monkeypatch,
    case: str,
    mutate,
    project_id: str,
    thread_id: str,
    expected_code: str,
) -> None:
    project = _project(tmp_path)
    connection = _connection(tmp_path)
    grant = _approved(project, connection)
    mutate(project, connection)
    dispatch_count = 0

    def forbidden_dispatch(self, request):
        nonlocal dispatch_count
        dispatch_count += 1
        return iter([ProviderEvent("delta", SAMPLE, 1)])

    monkeypatch.setattr(
        "rnaseq_agent.model_provider.ModelProviderGateway.dispatch_exact",
        forbidden_dispatch,
    )
    result = send_exact_disclosure(
        project,
        project_id=project_id,
        thread_id=thread_id,
        grant_id=grant.grant_id,
        prompt="请核对样本命名",
        connection_store_dir=connection,
    )

    assert result == {"ok": False, "error_code": expected_code}
    assert dispatch_count == 0, case
    assert load_grant(project, grant.grant_id).status == "consumed_failed"
    assert SAMPLE not in json.dumps(result, ensure_ascii=False)
    assert SAMPLE not in _grant_text(project, grant.grant_id)


@pytest.mark.parametrize("started, expected_status", [(False, "consumed_failed"), (True, "consumed_ambiguous")])
def test_provider_error_terminal_state_tracks_transport_start(
    tmp_path: Path,
    started: bool,
    expected_status: str,
) -> None:
    project = _project(tmp_path)
    connection = _connection(tmp_path)
    grant = _approved(project, connection)

    # Exercise the real claim path through the service; the provider callback
    # fails before or after transport without embedding any exact value.
    import rnaseq_agent.model_exact_service as exact_service

    def failing_dispatch(self, request):
        raise ProviderRequestError(
            "generic provider failure",
            code=MODEL_PROVIDER_REQUEST_FAILED,
            transmission_started=started,
        )

    original = exact_service.ModelProviderGateway.dispatch_exact
    exact_service.ModelProviderGateway.dispatch_exact = failing_dispatch
    try:
        result = send_exact_disclosure(
            project,
            project_id="p1",
            thread_id="t1",
            grant_id=grant.grant_id,
            prompt="请核对样本命名",
            connection_store_dir=connection,
        )
    finally:
        exact_service.ModelProviderGateway.dispatch_exact = original

    assert result["ok"] is False
    assert result["error_code"] == MODEL_PROVIDER_REQUEST_FAILED
    assert result["transmission_started"] is started
    assert load_grant(project, grant.grant_id).status == expected_status
    assert SAMPLE not in json.dumps(result, ensure_ascii=False)
    assert SAMPLE not in _grant_text(project, grant.grant_id)


def test_malformed_exact_event_fails_closed_without_exact_text(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    connection = _connection(tmp_path)
    grant = _approved(project, connection)

    def malformed_dispatch(self, request):
        return iter(
            [
                {"kind": "delta", "value": SAMPLE},
            ]
        )

    monkeypatch.setattr(
        "rnaseq_agent.model_provider.ModelProviderGateway.dispatch_exact",
        malformed_dispatch,
    )
    result = send_exact_disclosure(
        project,
        project_id="p1",
        thread_id="t1",
        grant_id=grant.grant_id,
        prompt="请核对样本命名",
        connection_store_dir=connection,
    )

    assert result["ok"] is False
    assert result["error_code"] == MODEL_PROVIDER_REQUEST_FAILED
    assert result.get("transmission_started") is True
    assert SAMPLE not in json.dumps(result, ensure_ascii=False)
    assert SAMPLE not in _grant_text(project, grant.grant_id)
    assert load_grant(project, grant.grant_id).status == "consumed_ambiguous"


@pytest.mark.parametrize("field", ["fastq_filenames", "report_excerpt", "remote_paths"])
def test_unsupported_fields_are_rejected_when_loading_forged_grant(tmp_path: Path, field: str) -> None:
    project = _project(tmp_path)
    connection = _connection(tmp_path)
    grant = _approved(project, connection)
    record = grant_record_path(project, grant.grant_id)
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["fields"] = [field]
    payload["record_counts"] = {field: 1}
    record.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DataGrantError) as caught:
        load_grant(project, grant.grant_id)
    assert caught.value.code == MODEL_DATA_SCOPE_UNSUPPORTED
    assert SAMPLE not in record.read_text(encoding="utf-8")


def test_secret_sentinels_never_appear_in_provider_error_or_grant(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    connection = _connection(tmp_path)
    grant = _approved(project, connection)
    sentinels = [PASSWORD, API_KEY, CIPHERTEXT, URL_USERINFO, PRIVATE_KEY]

    def secret_failure(self, request):
        raise ProviderRequestError(
            "provider request failed",
            code=MODEL_PROVIDER_REQUEST_FAILED,
            transmission_started=False,
        )

    monkeypatch.setattr(
        "rnaseq_agent.model_provider.ModelProviderGateway.dispatch_exact",
        secret_failure,
    )
    result = send_exact_disclosure(
        project,
        project_id="p1",
        thread_id="t1",
        grant_id=grant.grant_id,
        prompt="请核对样本命名",
        connection_store_dir=connection,
    )
    rendered = json.dumps(result, ensure_ascii=False)
    durable = _grant_text(project, grant.grant_id)
    for sentinel in sentinels + [SAMPLE, FASTQ]:
        assert sentinel not in rendered
        assert sentinel not in durable


@pytest.mark.parametrize("field", ["fastq_filenames", "report_excerpt", "remote_paths"])
def test_issue_rejects_all_unsupported_disclosure_fields(tmp_path: Path, field: str) -> None:
    project = _project(tmp_path)
    with pytest.raises(DataGrantError) as caught:
        issue_grant_request(
            project,
            project_id="p1",
            thread_id="t1",
            fields=(field,),
            purpose="不应创建授权",
        )
    assert caught.value.code == MODEL_DATA_SCOPE_UNSUPPORTED
    assert not list((project / ".model_data_grants").glob("*.json")) if (project / ".model_data_grants").exists() else True
