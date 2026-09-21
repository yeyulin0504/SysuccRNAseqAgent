from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rnaseq_agent.model_disclosure import ProviderEvent
from rnaseq_agent.webapp import create_app

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None


pytestmark = pytest.mark.skipif(TestClient is None, reason="fastapi/httpx not installed")


def _token(client: TestClient) -> str:
    page = client.get("/").text
    return re.search(r'const TOKEN = "([^"]+)"', page).group(1)


def _headers(token: str) -> dict[str, str]:
    return {"x-session-token": token}


def _seed_project(tmp_path: Path, project_id: str) -> None:
    path = tmp_path / "workspace" / project_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "project.json").write_text(
        json.dumps(
            {
                "project": {"id": project_id},
                "samples": {
                    "items": [
                        {
                            "sample_id": "SENTINEL_SAMPLE_001",
                            "fastq_1": "SENTINEL_R1.fastq.gz",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def test_data_disclosure_request_returns_metadata_card_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RNASEQ_AGENT_HOME", str(tmp_path / "agent_home"))
    app = create_app(workspace_dir=tmp_path / "workspace")
    client = TestClient(app)
    token = _token(client)
    client.post("/api/projects", json={"project_id": "p1"}, headers=_headers(token))
    _seed_project(tmp_path, "p1")

    response = client.post(
        "/api/projects/p1/data-disclosures",
        json={"thread_id": "main", "fields": ["sample_ids"], "purpose": "核对样本命名"},
        headers=_headers(token),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["card"]["type"] == "model_data_disclosure_confirmation"
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "SENTINEL_SAMPLE_001" not in encoded
    assert "SENTINEL_R1.fastq.gz" not in encoded


def test_data_disclosure_requires_boolean_decision_and_exact_send_is_transient(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("RNASEQ_AGENT_HOME", str(tmp_path / "agent_home"))
    from rnaseq_agent.connection_store import save_llm

    save_llm(
        {
            "enabled": True,
            "api_base": "https://llm.example/v1",
            "model": "test-model",
            "api_key": "sk-test",
            "tool_mode": "approved_execute",
        }
    )
    app = create_app(workspace_dir=tmp_path / "workspace")
    client = TestClient(app)
    token = _token(client)
    client.post("/api/projects", json={"project_id": "p1"}, headers=_headers(token))
    _seed_project(tmp_path, "p1")

    pending = client.post(
        "/api/projects/p1/data-disclosures",
        json={"thread_id": "main", "fields": ["sample_ids"], "purpose": "核对样本命名"},
        headers=_headers(token),
    ).json()
    grant_id = pending["grant_id"]
    invalid = client.post(
        f"/api/projects/p1/data-disclosures/{grant_id}/decision",
        json={"approved": "yes"},
        headers=_headers(token),
    )
    assert invalid.status_code == 400

    approved = client.post(
        f"/api/projects/p1/data-disclosures/{grant_id}/decision",
        json={"approved": True},
        headers=_headers(token),
    )
    assert approved.status_code == 200, approved.text

    seen: dict[str, object] = {}

    def fake_dispatch(self, request):
        seen["payload"] = request.payload
        return iter([ProviderEvent("delta", "SENTINEL_SAMPLE_001 已核对", 1)])

    monkeypatch.setattr(
        "rnaseq_agent.model_provider.ModelProviderGateway.dispatch_exact",
        fake_dispatch,
    )
    sent = client.post(
        f"/api/projects/p1/data-disclosures/{grant_id}/send",
        json={"thread_id": "main", "prompt": "请核对样本命名"},
        headers=_headers(token),
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["text"] == "SENTINEL_SAMPLE_001 已核对"
    assert "SENTINEL_SAMPLE_001" in json.dumps(seen["payload"], ensure_ascii=False)
    assert not (tmp_path / "workspace" / "p1" / "history.json").exists()


def test_exact_send_maps_failed_result_to_http_502(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RNASEQ_AGENT_HOME", str(tmp_path / "agent_home"))
    app = create_app(workspace_dir=tmp_path / "workspace")
    client = TestClient(app)
    token = _token(client)
    client.post("/api/projects", json={"project_id": "p1"}, headers=_headers(token))
    _seed_project(tmp_path, "p1")

    monkeypatch.setattr(
        "rnaseq_agent.webapp.send_exact_disclosure",
        lambda *args, **kwargs: {"ok": False, "error_code": "MODEL_PROVIDER_REQUEST_FAILED", "transmission_started": True},
    )
    response = client.post(
        "/api/projects/p1/data-disclosures/grant/send",
        json={"thread_id": "main", "prompt": "请核对样本命名"},
        headers=_headers(token),
    )

    assert response.status_code == 502
    assert response.json()["ok"] is False


def test_exact_send_requires_non_empty_string_thread_id_and_bounded_prompt(tmp_path: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("RNASEQ_AGENT_HOME", str(tmp_path / "agent_home"))
    try:
        app = create_app(workspace_dir=tmp_path / "workspace")
        client = TestClient(app)
        token = _token(client)
        client.post("/api/projects", json={"project_id": "p1"}, headers=_headers(token))
        _seed_project(tmp_path, "p1")

        for body in (
            {"prompt": "请核对样本命名"},
            {"thread_id": "   ", "prompt": "请核对样本命名"},
            {"thread_id": 123, "prompt": "请核对样本命名"},
            {"thread_id": "main", "prompt": "x" * (1024 * 1024 + 1)},
        ):
            response = client.post(
                "/api/projects/p1/data-disclosures/grant/send",
                json=body,
                headers=_headers(token),
            )
            assert response.status_code == 400
            assert response.json()["ok"] is False
    finally:
        monkeypatch.undo()


def test_exact_send_rejects_unknown_thread_binding(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RNASEQ_AGENT_HOME", str(tmp_path / "agent_home"))
    app = create_app(workspace_dir=tmp_path / "workspace")
    client = TestClient(app)
    token = _token(client)
    client.post("/api/projects", json={"project_id": "p1"}, headers=_headers(token))
    _seed_project(tmp_path, "p1")
    called = False

    def forbidden_send(*args, **kwargs):
        nonlocal called
        called = True
        return {"ok": True, "text": "should not send"}

    monkeypatch.setattr("rnaseq_agent.webapp.send_exact_disclosure", forbidden_send)
    response = client.post(
        "/api/projects/p1/data-disclosures/grant/send",
        json={"thread_id": "unknown", "prompt": "请核对样本命名"},
        headers=_headers(token),
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error_code": "MODEL_DATA_GRANT_INVALID"}
    assert called is False
