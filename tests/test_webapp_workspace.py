"""M1.4 tests: multi-project Workspace, multi-thread conversations, graph API.

These cover the three-page surface's backend: project registry, per-project
thread/message persistence (with cross-thread isolation), and the LangGraph
control-plane endpoints (run / state / resume).
"""

from __future__ import annotations

import re
import time
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


def _wait_graph_terminal(client, token: str, project_id: str, timeout: float = 8.0) -> dict:
    """Poll the graph state until the background run leaves ``in_flight``."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get(
            f"/api/projects/{project_id}/graph/state", headers=_headers(token)
        ).json()
        if not last.get("in_flight"):
            return last
        time.sleep(0.05)
    return last


def _seed_graph_checkpoint(
    project_dir: Path,
    project_id: str,
    *,
    run_id: str | None,
) -> None:
    """Write a durable graph snapshot with or without a pending QC interrupt."""
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    from rnaseq_agent.agent_graph import sqlite_checkpointer_for

    def checkpoint(state: dict) -> dict:
        if run_id is None:
            return {**state, "status": "WAITING_USER"}
        interrupt(
            {
                "checkpoint": "fastp_qc",
                "state": "WAITING_USER",
                "evidence": {"run_id": run_id},
            }
        )
        return state

    builder = StateGraph(dict)
    builder.add_node("checkpoint", checkpoint)
    builder.add_edge(START, "checkpoint")
    builder.add_edge("checkpoint", END)
    config = {"configurable": {"thread_id": project_id}}
    with sqlite_checkpointer_for(project_dir) as checkpointer:
        builder.compile(checkpointer=checkpointer).invoke({"status": ""}, config=config)


class TestWorkspace:
    def test_home_and_pages_render(self, client) -> None:
        assert "SYSU" in client.get("/").text
        assert "const TOKEN" in client.get("/").text
        assert client.get("/new-analysis").status_code == 200
        assert client.get("/workbench").status_code == 200
        settings = client.get("/settings")
        assert settings.status_code == 200
        for control in ("saveAndTestServer", "pullModels", "saveAndTestLlm", "loadDemoConfig", "runDemoTest"):
            assert f'id="{control}"' in settings.text
        assert 'document.getElementById("llmKey").value = "";' not in settings.text
        assert 'document.getElementById("srvPass").value = "";' not in settings.text
        assert "连接配置与测试" in client.get("/new-analysis").text

    def test_project_lifecycle(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_a", owner="alynn")

        rows = client.get("/api/projects", headers=h).json()["projects"]
        assert any(p["project_id"] == "proj_a" for p in rows)

        client.post("/api/projects/proj_a/archive", headers=h)
        visible = client.get("/api/projects", headers=h).json()["projects"]
        assert all(p["project_id"] != "proj_a" for p in visible)

        with_archived = client.get(
            "/api/projects?include_archived=1", headers=h
        ).json()["projects"]
        assert any(p["project_id"] == "proj_a" and p["archived"] for p in with_archived)

        client.post("/api/projects/proj_a/unarchive", headers=h)
        visible = client.get("/api/projects", headers=h).json()["projects"]
        assert any(p["project_id"] == "proj_a" for p in visible)

    def test_duplicate_project_rejected(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_b")
        resp = client.post(
            "/api/projects", json={"project_id": "proj_b"}, headers=h
        ).json()
        assert "error" in resp

    def test_project_auto_creates_main_thread(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_c")
        threads = client.get("/api/projects/proj_c/threads", headers=h).json()["threads"]
        assert any(t["thread_id"] == "main" for t in threads)


class TestThreads:
    def test_thread_messages_are_isolated(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_d")

        client.post(
            "/api/projects/proj_d/threads",
            json={"thread_id": "qc", "title": "QC 讨论"},
            headers=h,
        )
        client.post(
            "/api/projects/proj_d/threads/main/messages",
            json={"role": "user", "content": "main 的消息"},
            headers=h,
        )
        client.post(
            "/api/projects/proj_d/threads/qc/messages",
            json={"role": "user", "content": "qc 的消息"},
            headers=h,
        )

        main_msgs = client.get(
            "/api/projects/proj_d/threads/main/messages", headers=h
        ).json()["messages"]
        assert [m["content"] for m in main_msgs] == ["main 的消息"]

        qc_msgs = client.get(
            "/api/projects/proj_d/threads/qc/messages", headers=h
        ).json()["messages"]
        assert [m["content"] for m in qc_msgs] == ["qc 的消息"]

    def test_thread_rename_and_archive(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_e")

        renamed = client.patch(
            "/api/projects/proj_e/threads/main",
            json={"title": "主分析流程（重命名）"},
            headers=h,
        ).json()
        assert renamed["title"] == "主分析流程（重命名）"

        client.delete("/api/projects/proj_e/threads/main", headers=h)
        threads = client.get("/api/projects/proj_e/threads", headers=h).json()["threads"]
        assert all(t["thread_id"] != "main" for t in threads)

    def test_unknown_project_404(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        assert client.get("/api/projects/nope/threads", headers=h).status_code == 404


class TestGraphApi:
    def test_state_idle_for_fresh_project(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_f")
        st = client.get("/api/projects/proj_f/graph/state", headers=h).json()
        assert st["state"] == "idle"
        assert st["in_flight"] is False

    def test_resume_without_approved_reports_type_error(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_g")
        resp = client.post(
            "/api/projects/proj_g/graph/resume", json={}, headers=h
        ).json()
        assert "approved" in resp.get("error", "")

    @pytest.mark.parametrize("approved", ["yes", 1, [True]])
    def test_resume_requires_literal_boolean_approval(self, client, approved) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_g_bool")

        resp = client.post(
            "/api/projects/proj_g_bool/graph/resume",
            json={"approved": approved},
            headers=h,
        ).json()

        assert "approved" in resp.get("error", "")

    def test_resume_with_boolean_still_requires_waiting_checkpoint(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_g_idle")

        resp = client.post(
            "/api/projects/proj_g_idle/graph/resume",
            json={"approved": True},
            headers=h,
        ).json()

        assert "不在 QC 检查点" in resp.get("error", "")

    def test_resume_requires_a_real_pending_interrupt(
        self, client, tmp_path
    ) -> None:
        token = _token(client)
        h = _headers(token)
        project_id = "proj_g_stale"
        _create_project(client, token, project_id)
        _seed_graph_checkpoint(tmp_path / project_id, project_id, run_id=None)

        resp = client.post(
            f"/api/projects/{project_id}/graph/resume",
            json={"approved": True},
            headers=h,
        ).json()

        assert "interrupt" in resp.get("error", "")

    def test_resume_binds_approval_to_server_snapshot_attempt(
        self, client, tmp_path, monkeypatch
    ) -> None:
        token = _token(client)
        h = _headers(token)
        project_id = "proj_g_attempt"
        _create_project(client, token, project_id)
        _seed_graph_checkpoint(tmp_path / project_id, project_id, run_id="run-visible")

        class FinishedGraph:
            def invoke(self, _input, config):
                return {"status": "FAIL", "config": config}

        monkeypatch.setattr(
            "rnaseq_agent.webapp.build_bulk_rna_graph",
            lambda _checkpointer: FinishedGraph(),
        )

        resp = client.post(
            f"/api/projects/{project_id}/graph/resume",
            json={"approved": True, "expected_run_id": "run-client-forged"},
            headers=h,
        ).json()

        assert resp["resume"]["approved"] is True
        assert resp["resume"]["expected_run_id"] == "run-visible"

    def test_run_fails_cleanly_without_session(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_h")

        started = client.post("/api/projects/proj_h/graph/run", headers=h).json()
        assert started["state"] == "running"

        st = _wait_graph_terminal(client, token, "proj_h")
        # No session.json -> the graph fails cleanly (never an interrupt).
        assert st["state"] in {"FAIL", "error", "idle"}

    def test_run_is_exclusive_while_in_flight(self, client) -> None:
        token = _token(client)
        h = _headers(token)
        _create_project(client, token, "proj_i")
        client.post("/api/projects/proj_i/graph/run", headers=h)
        second = client.post("/api/projects/proj_i/graph/run", headers=h).json()
        assert "error" in second
