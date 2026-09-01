"""Tests for the localhost web workbench (framework section 11).

Single-user loopback UI: project / flow / chat / decision panels plus the
QC checkpoint interrupt surfaced as a resume action.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

fastapi_missing = False
try:
    from fastapi.testclient import TestClient

    from rnaseq_agent.webapp import create_app
except ImportError:
    fastapi_missing = True


pytestmark = pytest.mark.skipif(fastapi_missing, reason="fastapi/httpx not installed")


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(project_dir=tmp_path / "proj")
    return TestClient(app)


def _token(client) -> str:
    page = client.get("/").text
    return re.search(r"const TOKEN = \"([^\"]+)\"", page).group(1)


def _headers(token: str) -> dict:
    return {"x-session-token": token}


def _new_project(client, token: str) -> None:
    resp = client.post(
        "/api/new",
        json={
            "project_id": "web_test",
            "title": "Web test",
            "samples": [
                {"sample_id": "a", "condition": "ctrl", "fastq_1": "a_R1.fastq.gz", "fastq_2": "a_R2.fastq.gz"},
                {"sample_id": "b", "condition": "trt", "fastq_1": "b_R1.fastq.gz", "fastq_2": "b_R2.fastq.gz"},
            ],
        },
        headers=_headers(token),
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "drafting"


class TestWebApp:
    def test_index_renders(self, client) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "SYSU" in resp.text
        assert "const TOKEN" in resp.text

    def test_api_requires_token(self, client) -> None:
        resp = client.post("/api/plan")
        assert resp.status_code == 403

    def test_new_project_flow(self, client, tmp_path: Path) -> None:
        token = _token(client)
        _new_project(client, token)
        state = client.get("/api/state", headers=_headers(token)).json()
        assert state["state"] == "drafting"

    def test_plan_then_edit_then_rollback(self, client, tmp_path: Path) -> None:
        token = _token(client)
        _new_project(client, token)

        plan = client.post("/api/plan", headers=_headers(token)).json()
        assert plan["state"] == "planned"
        assert plan["steps"]

        # Confirm should fail because files are missing (contract needs them).
        confirm = client.post("/api/confirm", headers=_headers(token)).json()
        assert "error" in confirm

        # Apply a change so there is something to roll back.
        edit = client.post(
            "/api/edit",
            json={"patch": {"server": {"threads": 16}}, "note": "web edit"},
            headers=_headers(token),
        ).json()
        assert edit["state"] == "drafting"

        rollback = client.post("/api/rollback", json={}, headers=_headers(token)).json()
        assert rollback["state"] == "drafting"

    def test_qc_resume_only_in_waiting_user(self, client, tmp_path: Path) -> None:
        token = _token(client)
        _new_project(client, token)
        resp = client.post("/api/resume", headers=_headers(token)).json()
        assert "error" in resp  # not in WAITING_USER yet
