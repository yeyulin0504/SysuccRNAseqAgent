"""M1.5 tests: three-pane workbench and project-bound legacy session endpoints.

These cover the two upgrades on top of M1.4:

- the legacy single-project session endpoints (``/api/new``, ``/api/plan``,
  ``/api/state``, ...) accept an optional ``?project=<id>`` (or a registered
  ``project_id`` in the JSON body) and operate on that workspace project;
- the rewritten ``workbench.html`` renders a three-pane surface that binds to
  the selected project via ``?project=``.
"""

from __future__ import annotations

import re
import json
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
    app = create_app(project_dir=tmp_path / "legacy")
    return TestClient(app)


def _token(client) -> str:
    page = client.get("/").text
    return re.search(r'const TOKEN = "([^"]+)"', page).group(1)


def _headers(token: str) -> dict:
    return {"x-session-token": token}


def _create_project(client, token: str, project_id: str, **extra) -> dict:
    resp = client.post(
        "/api/projects",
        json={"project_id": project_id, "title": f"P {project_id}", **extra},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _samples() -> list[dict]:
    return [
        {"sample_id": "a", "condition": "ctrl", "fastq_1": "a_R1.fastq.gz", "fastq_2": "a_R2.fastq.gz"},
        {"sample_id": "b", "condition": "trt", "fastq_1": "b_R1.fastq.gz", "fastq_2": "b_R2.fastq.gz"},
    ]


class TestProjectBoundEndpoints:
    def test_session_state_is_isolated_between_projects(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_a")
        _create_project(client, token, "proj_b")

        # 两个项目各自创建草稿。
        assert client.post(
            "/api/new?project=proj_a",
            json={"project_id": "proj_a", "title": "A", "samples": _samples()},
            headers=h,
        ).json()["state"] == "drafting"
        assert client.post(
            "/api/new?project=proj_b",
            json={"project_id": "proj_b", "title": "B", "samples": _samples()},
            headers=h,
        ).json()["state"] == "drafting"

        # 只对 proj_a 生成计划。
        assert client.post("/api/plan?project=proj_a", headers=h).json()["state"] == "planned"

        # 两个项目的 state 互不干扰。
        assert client.get("/api/state?project=proj_a", headers=h).json()["state"] == "planned"
        assert client.get("/api/state?project=proj_b", headers=h).json()["state"] == "drafting"

    def test_new_binds_via_body_project_id_and_leaves_legacy_untouched(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_c")

        # 不带 query 参数，仅靠 body 的 project_id 绑定（且为注册项目）。
        resp = client.post(
            "/api/new",
            json={"project_id": "proj_c", "title": "C", "samples": _samples()},
            headers=h,
        ).json()
        assert resp["state"] == "drafting"

        # 绑定到了 proj_c 目录。
        assert client.get("/api/state?project=proj_c", headers=h).json()["state"] == "drafting"
        # 默认 legacy 目录未被写入（仍为 idle）。
        assert client.get("/api/state", headers=h).json()["state"] == "idle"

    def test_unknown_project_in_query_falls_back_to_legacy(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        # 未注册的 id 不绑定，回退到默认 legacy 目录（不 404）。
        resp = client.get("/api/state?project=nope", headers=h)
        assert resp.status_code == 200
        assert resp.json()["state"] == "idle"


class TestWorkbenchPage:
    def test_graph_state_projects_durable_session_attempt_and_report(self, client, tmp_path: Path) -> None:
        token = _token(client)
        _create_project(client, token, "durable")
        project_dir = Path(client.get("/api/state?project=durable", headers=_headers(token)).json()["project_dir"])
        (project_dir / "project.json").write_text(json.dumps({
            "status": {"state": "submitted", "run_id": "run-1", "attempt_dir": str(project_dir / "attempts" / "run-1")}
        }), encoding="utf-8")
        (project_dir / "session.json").write_text(json.dumps({"state": "confirmed"}), encoding="utf-8")
        attempt = project_dir / "attempts" / "run-1"
        attempt.mkdir(parents=True)
        (attempt / "result_manifest.json").write_text(json.dumps({"summary": {"status": "pass"}}), encoding="utf-8")
        (project_dir / "report.md").write_text("# report", encoding="utf-8")
        (project_dir / "history.json").write_text(json.dumps({"items": [{"type": "contract", "state": "confirmed"}]}), encoding="utf-8")

        response = client.get("/api/projects/durable/graph/state", headers=_headers(token))
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["durable"]["status"] == "queued"
        assert data["durable"]["session_state"] == "confirmed"
        assert data["durable"]["result_manifest"]["available"] is True
        assert data["durable"]["report"]["available"] is True
        assert data["durable"]["history"][0]["type"] == "contract"

    def test_graph_state_completed_without_manifest_is_result_missing(self, client, tmp_path: Path) -> None:
        token = _token(client)
        _create_project(client, token, "missing")
        project_dir = Path(client.get("/api/state?project=missing", headers=_headers(token)).json()["project_dir"])
        (project_dir / "project.json").write_text(json.dumps({"status": {"state": "completed", "run_id": "run-2"}}), encoding="utf-8")
        response = client.get("/api/projects/missing/graph/state", headers=_headers(token))
        assert response.json()["durable"]["status"] == "result_missing"

    def test_workbench_renders_with_project(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_d")

        resp = client.get("/workbench?project=proj_d")
        assert resp.status_code == 200
        assert "SYSU" in resp.text
        assert "const TOKEN" in resp.text

    def test_workbench_renders_without_project(self, client) -> None:
        resp = client.get("/workbench")
        assert resp.status_code == 200
        assert "SYSU" in resp.text
        assert "const TOKEN" in resp.text

    def test_workbench_restores_persisted_remote_fastq_directory_on_intake_reload(self, client) -> None:
        """The intake refresh must repopulate the remote FASTQ input field."""
        resp = client.get("/workbench")
        assert resp.status_code == 200
        script = resp.text
        start = script.index("function renderInputSelection(d) {")
        end = script.index("\nfunction setInputPaneEnabled", start)
        render_input_selection = script[start:end]

        assert "intake.fastq" in render_input_selection
        assert "remote_fastq_dir" in render_input_selection
        assert "remoteFastqDir2" in render_input_selection
        assert "Array.isArray(intake.samples)" in render_input_selection
        assert "remoteRows.innerHTML = \"\"" in render_input_selection
        assert "persistedSamples.forEach(sample => addFastqSampleRow(sample))" in render_input_selection
        assert "sampleEditor.classList.toggle(\"hidden\", !persistedSamples.length)" in render_input_selection
