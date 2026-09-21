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
