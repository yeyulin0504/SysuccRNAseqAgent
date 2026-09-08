"""Tests for the localhost web workbench (framework section 11).

Single-user loopback UI: project / flow / chat / decision panels plus the
QC checkpoint interrupt surfaced as a resume action.
"""

from __future__ import annotations

import json
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

    def test_chat_routes_intent_to_session(self, client, tmp_path: Path) -> None:
        token = _token(client)
        _new_project(client, token)

        # Chat requests the plan in natural language.
        resp = client.post(
            "/api/chat",
            json={"message": "生成执行计划"},
            headers=_headers(token),
        ).json()
        assert resp.get("state") == "planned"
        assert "steps" in resp

        # Chat edits the thread count.
        resp = client.post(
            "/api/chat",
            json={"message": "把线程改成 24"},
            headers=_headers(token),
        ).json()
        assert resp.get("state") == "drafting"
        assert "操作未完成" not in resp.get("reply", "")

        # Threads change is persisted in the config snapshot.
        config_path = tmp_path / "proj" / "project.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["server"]["threads"] == 24

    def test_chat_unrecognized_returns_hint(self, client, tmp_path: Path) -> None:
        token = _token(client)
        _new_project(client, token)
        resp = client.post(
            "/api/chat",
            json={"message": "今天的天气怎么样"},
            headers=_headers(token),
        ).json()
        assert "reply" in resp

    def test_deg_endpoint_reports_and_enables(self, client, tmp_path: Path) -> None:
        token = _token(client)
        _new_project(client, token)

        # 初始未启用。
        status = client.post("/api/deg", json={}, headers=_headers(token)).json()
        assert status["requested"] is False

        # 启用：2 样本 1v1 会触发设计门禁失败；同时显式声明参考组（M1.6）。
        status = client.post(
            "/api/deg",
            json={"enabled": True, "reference_condition": "ctrl"},
            headers=_headers(token),
        ).json()
        assert status["requested"] is True
        assert status["gate_ok"] is False
        assert any("重复" in reason or "单样本" in reason for reason in status["gate_reasons"])

        # 回读。
        status = client.post("/api/deg", json={}, headers=_headers(token)).json()
        assert status["requested"] is True
        assert status["design"]["contrast"] == "trt_vs_ctrl"

    def test_config_endpoint_reads_and_writes(self, client, tmp_path: Path) -> None:
        token = _token(client)
        _new_project(client, token)

        # GET: llm api_key must be masked.
        cfg = client.get("/api/config", headers=_headers(token)).json()["config"]
        assert cfg["server"]["host"] == "localhost"
        assert cfg["llm"]["api_key_set"] is False
        assert "api_key" not in cfg["llm"]

        # POST: edit server settings through the audited session.
        resp = client.post(
            "/api/config",
            json={"server": {"host": "login.hpc", "user": "alynn", "threads": 48, "memory_gb": 128}},
            headers=_headers(token),
        ).json()
        assert resp["state"] == "drafting"
        cfg = resp["config"]
        assert cfg["server"]["host"] == "login.hpc"
        assert cfg["server"]["threads"] == 48
        assert cfg["server"]["memory_gb"] == 128

        # Persisted in project.json.
        config_path = tmp_path / "proj" / "project.json"
        stored = json.loads(config_path.read_text(encoding="utf-8"))
        assert stored["server"]["user"] == "alynn"
        assert stored["server"]["host"] == "login.hpc"

    def test_config_endpoint_llm_key_is_write_only(self, client, tmp_path: Path) -> None:
        token = _token(client)
        _new_project(client, token)

        resp = client.post(
            "/api/config",
            json={"llm": {"enabled": True, "provider": "openai", "api_base": "https://api.openai.com/v1",
                          "model": "gpt-4o-mini", "api_key": "sk-secret-123"}},
            headers=_headers(token),
        ).json()
        assert resp["state"] == "drafting"
        assert resp["config"]["llm"]["api_key_set"] is True
        assert "api_key" not in resp["config"]["llm"]

        # Second read (no write) still masks the key.
        cfg = client.get("/api/config", headers=_headers(token)).json()["config"]
        assert cfg["llm"]["enabled"] is True
        assert cfg["llm"]["api_key_set"] is True

        # Clear the key.
        resp = client.post(
            "/api/config",
            json={"llm": {"api_key_clear": True}},
            headers=_headers(token),
        ).json()
        assert resp["config"]["llm"]["api_key_set"] is False

    def test_demo_config_prefills_real_connections(self, client) -> None:
        token = _token(client)
        _new_project(client, token)
        result = client.post("/api/config/demo", headers=_headers(token)).json()
        assert result["config"]["server"]["host"] == "10.30.24.1"
        assert result["config"]["server"]["user"] == "yeyulin"
        assert result["config"]["server"]["port"] == 22
        assert result["config"]["server"]["scheduler"] == "slurm"
        assert result["config"]["server"]["remote_workdir"] == "/hwdata/home/yeyulin/"
        assert result["config"]["llm"]["api_base"] == "https://llmapi.paratera.com/v1"
        assert result["config"]["llm"]["model"] == "Deepseek-V4-Flash"

    def test_server_connection_endpoint_calls_real_probe(self, client, monkeypatch) -> None:
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.execution import CommandResult

        token = _token(client)
        _new_project(client, token)
        monkeypatch.setattr(
            webapp,
            "test_server_connection",
            lambda config: CommandResult([], 0, "RNASEQ_AGENT_SSH_OK", ""),
        )
        result = client.post("/api/test-server", headers=_headers(token)).json()
        assert result["ok"] is True
        assert result["message"] == "SSH 连接成功"

    def test_llm_test_and_model_list_use_configured_api(self, client, monkeypatch) -> None:
        import requests

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={"llm": {"enabled": True, "api_base": "https://llm.example/v1", "model": "demo", "api_key": "secret"}},
            headers=_headers(token),
        )

        class Response:
            status_code = 200
            text = ""
            def __init__(self, payload): self.payload = payload
            def json(self): return self.payload
            def raise_for_status(self): return None

        def fake_get(url, **kwargs):
            assert url == "https://llm.example/v1/models"
            return Response({"data": [{"id": "model-b"}, {"id": "model-a"}]})

        def fake_post(url, **kwargs):
            assert url == "https://llm.example/v1/chat/completions"
            return Response({"choices": [{"message": {"content": "连接正常"}}]})

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        models = client.get("/api/llm/models", headers=_headers(token)).json()
        tested = client.post("/api/test-llm", headers=_headers(token)).json()
        assert models == {"ok": True, "models": ["model-a", "model-b"]}
        assert tested["ok"] is True
        assert tested["reply"] == "连接正常"

    def test_counts_preview_detects_samples(self, client) -> None:
        token = _token(client)
        result = client.post(
            "/api/samples/counts-preview",
            files={"file": ("counts.tsv", b"gene\tA1\tA2\tB1\nG1\t1\t2\t3\n", "text/tab-separated-values")},
            headers=_headers(token),
        ).json()
        assert result == {"ok": True, "samples": ["A1", "A2", "B1"]}

    def test_remote_scan_returns_paired_preview(self, client, monkeypatch) -> None:
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.execution import CommandResult

        token = _token(client)
        _new_project(client, token)

        class Transport:
            def execute(self, command):
                return CommandResult([], 0, "/reads/A_R1.fastq.gz\n/reads/A_R2.fastq.gz\n", "")

        monkeypatch.setattr(webapp, "create_remote_transport", lambda config: Transport())
        result = client.post(
            "/api/samples/scan-remote", json={"path": "/reads"}, headers=_headers(token)
        ).json()
        assert result["ok"] is True
        assert result["samples"][0]["sample_id"] == "A"

    def test_chat_uses_llm_when_configured(self, client, tmp_path: Path, monkeypatch) -> None:
        import requests

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={"llm": {"enabled": True, "api_base": "https://api.openai.com/v1",
                          "model": "gpt-4o-mini", "api_key": "sk-secret-123"}},
            headers=_headers(token),
        )

        calls = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            calls["url"] = url
            calls["auth"] = headers.get("Authorization")
            calls["model"] = json["model"]
            calls["content"] = json["messages"][-1]["content"]

            class FakeResp:
                status_code = 200

                def json(self):
                    return {"choices": [{"message": {"content": "你好，我在。"}}]}

            return FakeResp()

        monkeypatch.setattr(requests, "post", fake_post)
        resp = client.post(
            "/api/chat",
            json={"message": "你好"},
            headers=_headers(token),
        ).json()
        assert resp["via"] == "llm"
        assert resp["reply"] == "你好，我在。"
        assert calls["url"] == "https://api.openai.com/v1/chat/completions"
        assert calls["auth"] == "Bearer sk-secret-123"

    def test_chat_falls_back_to_rule_when_llm_fails(self, client, tmp_path: Path, monkeypatch) -> None:
        import requests

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={"llm": {"enabled": True, "api_base": "https://api.openai.com/v1",
                          "model": "gpt-4o-mini", "api_key": "sk-secret-123"}},
            headers=_headers(token),
        )

        def fake_post(url, headers=None, json=None, timeout=None):
            class FakeResp:
                status_code = 500

                def json(self):
                    return {}

            return FakeResp()

        monkeypatch.setattr(requests, "post", fake_post)
        # A non-actionable question hits the LLM, fails, then falls to hint.
        resp = client.post(
            "/api/chat",
            json={"message": "生成执行计划"},
            headers=_headers(token),
        ).json()
        # Rule router takes over.
        assert resp.get("state") == "planned"
        assert "steps" in resp
