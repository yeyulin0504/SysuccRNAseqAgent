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
def client(tmp_path: Path, monkeypatch):
    # 隔离全局连接配置目录，避免测试污染真实用户的 ~/.rnaseq_agent。
    monkeypatch.setenv("RNASEQ_AGENT_HOME", str(tmp_path / "agent_home"))
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

    def test_config_save_can_initialize_idle_settings_session(self, client) -> None:
        token = _token(client)
        result = client.post(
            "/api/config",
            json={"server": {"host": "10.30.24.1", "user": "yeyulin", "port": 22}},
            headers=_headers(token),
        ).json()
        assert "error" not in result
        assert result["state"] == "drafting"
        assert result["config"]["server"]["host"] == "10.30.24.1"

    def test_saving_unchanged_config_is_idempotent(self, client) -> None:
        token = _token(client)
        _new_project(client, token)
        first = client.post(
            "/api/config",
            json={"llm": {"enabled": True, "api_base": "https://llm.example/v1", "model": "demo", "api_key": "secret"}},
            headers=_headers(token),
        ).json()
        second = client.post(
            "/api/config",
            json={"llm": {"enabled": True, "api_base": "https://llm.example/v1", "model": "demo", "api_key": "secret"}},
            headers=_headers(token),
        ).json()
        assert "error" not in first
        assert "error" not in second
        assert second["unchanged"] is True

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

    def test_demo_config_can_initialize_idle_settings_session(self, client) -> None:
        token = _token(client)
        result = client.post("/api/config/demo", headers=_headers(token)).json()
        assert "error" not in result
        assert result["state"] == "drafting"
        assert result["config"]["server"]["host"] == "10.30.24.1"

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

    def test_server_connection_failure_surfaces_real_reason(self, client, monkeypatch) -> None:
        """连接失败时必须回传可读原因，而不是只回传异常类名。"""
        import rnaseq_agent.webapp as webapp

        token = _token(client)
        _new_project(client, token)

        def _boom(config):
            raise RuntimeError(
                "Command failed with exit code 255: ssh -o BatchMode=yes user@host\n"
                "STDOUT:\n\nSTDERR:\n"
                "user@host: Permission denied (publickey,password).\n"
            )

        monkeypatch.setattr(webapp, "test_server_connection", _boom)
        result = client.post("/api/test-server", headers=_headers(token)).json()
        assert result["ok"] is False
        assert "RuntimeError" != result["message"]
        assert "认证被拒绝" in result["message"]

    def test_password_connection_failure_surfaces_password_hint(self, client, monkeypatch) -> None:
        """密码认证失败应给出密码相关的可读提示。"""
        import rnaseq_agent.webapp as webapp

        token = _token(client)
        _new_project(client, token)

        def _boom_password(config):
            raise RuntimeError("Authentication failed.")

        monkeypatch.setattr(webapp, "test_server_connection", _boom_password)
        result = client.post("/api/test-server", headers=_headers(token)).json()
        assert result["ok"] is False
        assert "RuntimeError" != result["message"]
        assert "密码认证失败" in result["message"]

    def test_browse_samples_surfaces_readable_failure(self, client, monkeypatch) -> None:
        """「浏览服务器目录」失败时也要给出可读原因，而不是只报异常类名。"""
        import rnaseq_agent.webapp as webapp

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={
                "server": {
                    "host": "h.example",
                    "user": "u",
                    "auth_mode": "password",
                    "password": "x",
                    "remote_workdir": "/data/reads",
                }
            },
            headers=_headers(token),
        )

        # 让底层传输抛出带 SSH stderr 的 RuntimeError（真实失败形态）。
        def _boom(config):
            raise RuntimeError(
                "Command failed with exit code 255: ssh -o BatchMode=yes u@h\n"
                "STDOUT:\n\nSTDERR:\n"
                "u@h: Permission denied (publickey,password).\n"
            )

        monkeypatch.setattr(webapp, "create_remote_transport", _boom)
        result = client.post(
            "/api/chat",
            json={"message": "浏览我的服务器目录"},
            headers=_headers(token),
        ).json()
        assert "RuntimeError" not in result["reply"]
        assert "认证被拒绝" in result["reply"]

    def test_password_auth_mode_from_web_form_is_used_and_echoed(self, client) -> None:
        """前端认证按钮值为 'pass'，后端须按 password 模式保存并回显。"""
        from rnaseq_agent.ssh_auth import clear_ssh_credential

        token = _token(client)
        _new_project(client, token)
        clear_ssh_credential("10.30.24.1", "yeyulin")
        resp = client.post(
            "/api/config",
            json={"server": {"host": "10.30.24.1", "user": "yeyulin", "auth_mode": "pass", "password": "p@ssw0rd"}},
            headers=_headers(token),
        ).json()
        assert "error" not in resp
        assert resp["config"]["server"]["auth_mode"] == "password"

        from rnaseq_agent.ssh_auth import get_ssh_credential

        credential = get_ssh_credential("10.30.24.1", "yeyulin")
        assert credential.mode == "password"
        assert credential.password == "p@ssw0rd"

    def test_pass_alias_is_persisted_as_canonical_password_mode(self, client, tmp_path) -> None:
        """落盘前须把前端别名 'pass' 归一化为 'password'，避免状态歧义。"""
        from rnaseq_agent.ssh_auth import clear_ssh_credential

        token = _token(client)
        _new_project(client, token)
        clear_ssh_credential("10.30.24.1", "yeyulin")
        client.post(
            "/api/config",
            json={"server": {"host": "10.30.24.1", "user": "yeyulin", "auth_mode": "pass", "password": "x"}},
            headers=_headers(token),
        )
        project = json.loads((tmp_path / "proj" / "project.json").read_text(encoding="utf-8"))
        assert project["server"]["auth_mode"] == "password"

    def test_resaving_password_mode_without_password_keeps_stored_secret(self, client) -> None:
        """密码框留空重保存时不得把已存的临时密码清空。"""
        from rnaseq_agent.ssh_auth import clear_ssh_credential, get_ssh_credential

        token = _token(client)
        _new_project(client, token)
        clear_ssh_credential("10.30.24.1", "yeyulin")
        client.post(
            "/api/config",
            json={"server": {"host": "10.30.24.1", "user": "yeyulin", "auth_mode": "password", "password": "keep-me"}},
            headers=_headers(token),
        )
        client.post(
            "/api/config",
            json={"server": {"host": "10.30.24.1", "user": "yeyulin", "auth_mode": "password"}},
            headers=_headers(token),
        )
        assert get_ssh_credential("10.30.24.1", "yeyulin").password == "keep-me"

    def test_password_auth_mode_persists_without_runtime_credential(self, client) -> None:
        """进程重启后内存凭据丢失，仍应从持久化配置回显 password 模式。"""
        from rnaseq_agent.ssh_auth import clear_ssh_credential

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={"server": {"host": "10.30.24.1", "user": "yeyulin", "auth_mode": "password", "password": "temp"}},
            headers=_headers(token),
        )
        clear_ssh_credential("10.30.24.1", "yeyulin")
        config = client.get("/api/config", headers=_headers(token)).json()["config"]
        assert config["server"]["auth_mode"] == "password"

    def test_connection_is_shared_across_projects(self, client, tmp_path) -> None:
        """核心需求：配一次，所有项目共用。

        在设置页保存连接后，新建项目应自动继承该连接（host/user/scheduler），
        而不是要求用户在每个项目里重新填写。
        """
        from rnaseq_agent.connection_store import load_connection

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={
                "server": {
                    "host": "10.30.24.1",
                    "user": "yeyulin",
                    "port": 22,
                    "scheduler": "slurm",
                    "remote_base_dir": "/hwdata/home/yeyulin/",
                    "auth_mode": "password",
                    "password": "shared-secret",
                }
            },
            headers=_headers(token),
        )

        stored = load_connection()
        assert stored["host"] == "10.30.24.1"
        assert stored["scheduler"] == "slurm"
        assert stored["password"] == "shared-secret"

    def test_new_project_inherits_shared_connection(self, client, tmp_path) -> None:
        """新建项目应自动继承全局连接，无需再次填写服务器信息。"""
        from rnaseq_agent.connection_store import save_connection

        token = _token(client)
        # 先保存一次全局连接（模拟用户在设置页配过），再新建项目。
        save_connection(
            {
                "host": "10.30.24.1",
                "user": "yeyulin",
                "port": 22,
                "scheduler": "slurm",
                "remote_base_dir": "/hwdata/home/yeyulin/",
                "auth_mode": "password",
            }
        )

        resp = client.post(
            "/api/new",
            json={
                "project_id": "inherit_test",
                "title": "inherit",
                "samples": [
                    {"sample_id": "a", "condition": "ctrl", "fastq_1": "a_R1.fastq.gz"},
                ],
            },
            headers=_headers(token),
        )
        assert resp.status_code == 200
        assert "error" not in resp.json()

        config = client.get("/api/config", headers=_headers(token)).json()["config"]
        assert config["server"]["host"] == "10.30.24.1"
        assert config["server"]["user"] == "yeyulin"
        assert config["server"]["scheduler"] == "slurm"

    def test_stored_password_restores_ssh_credential_after_restart(self, client) -> None:
        """模拟进程重启：内存凭据清空后，应从全局配置恢复密码模式凭据。"""
        from rnaseq_agent.ssh_auth import clear_ssh_credential

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={"server": {"host": "10.30.24.1", "user": "yeyulin", "auth_mode": "password", "password": "restore-me"}},
            headers=_headers(token),
        )

        # 模拟重启：清空进程内凭据。
        clear_ssh_credential("10.30.24.1", "yeyulin")
        # 触发一次读取（例如打开设置页），应把全局密码重新注入运行时凭据。
        client.get("/api/config", headers=_headers(token))

        from rnaseq_agent.ssh_auth import get_ssh_credential

        credential = get_ssh_credential("10.30.24.1", "yeyulin")
        assert credential.mode == "password"
        assert credential.password == "restore-me"

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

    def test_chat_browses_remote_samples_before_calling_llm(self, client, monkeypatch) -> None:
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.execution import CommandResult

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={"server": {"remote_workdir": "/hwdata/home/yeyulin/rna"}},
            headers=_headers(token),
        )

        calls = {}

        class Transport:
            def execute(self, command):
                calls["command"] = command
                return CommandResult([], 0, "/data/P1_R1.fastq.gz\n/data/P1_R2.fastq.gz\n", "")

        monkeypatch.setattr(webapp, "create_remote_transport", lambda config: Transport())
        monkeypatch.setattr(webapp, "_llm_reply_or_none", lambda config, text: (_ for _ in ()).throw(AssertionError("LLM must not handle tool intents")))
        result = client.post(
            "/api/chat",
            json={"message": "浏览服务器目录找 RNA-seq 样本"},
            headers=_headers(token),
        ).json()

        assert result["action"] == "browse_samples"
        assert result["scanned_path"] == "/hwdata/home/yeyulin/rna"
        assert result["samples"][0]["sample_id"] == "P1"
        assert "find /hwdata/home/yeyulin/rna" in calls["command"]

    def test_container_config_pull_and_test(self, client, monkeypatch) -> None:
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.execution import CommandResult

        token = _token(client)
        _new_project(client, token)
        container = {
            "enabled": True,
            "engine": "apptainer",
            "image_uri": "docker://ghcr.io/sysucc/rnaseq-downstream:2026.09",
            "image_path": "/hwdata/home/yeyulin/containers/rnaseq-downstream.sif",
            "bind_paths": ["/hwdata/home/yeyulin/projects"],
        }
        saved = client.post("/api/config", json={"container": container}, headers=_headers(token)).json()
        assert saved["config"]["container"] == container

        commands = []

        class Transport:
            def execute(self, command):
                commands.append(command)
                output = "RNASEQ_DOWNSTREAM_OK\n" if "Rscript" in command else "pulled\n"
                return CommandResult([], 0, output, "")

        monkeypatch.setattr(webapp, "create_remote_transport", lambda config: Transport())
        pulled = client.post("/api/container/pull", headers=_headers(token)).json()
        tested = client.post("/api/container/test", headers=_headers(token)).json()
        assert pulled["ok"] is True
        assert tested["ok"] is True
        assert "apptainer pull --force" in commands[0]
        assert "exec --cleanenv" in commands[1]

    def test_settings_page_has_container_controls(self, client) -> None:
        page = client.get("/settings").text
        for control_id in ("ctrEnabled", "ctrEngine", "ctrUri", "ctrPath", "ctrBinds", "pullContainer", "testContainer"):
            assert f'id="{control_id}"' in page

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


class TestSharedLlmConfig:
    """大模型接入也是「配一次、所有项目共用、永久保存」。

    用户实际遇到的问题：在设置页配好了大模型，但对话仍返回规则式兜底
    答复（「我还没理解成可执行操作…」）。根因是 LLM 配置存在各项目自己的
    ``project.json`` 里，而用户当前打开的项目没有该文件，于是 ``/api/chat``
    的 ``if config is not None`` 直接跳过了 LLM 分支。
    """

    def test_saving_llm_in_settings_persists_to_shared_store(self, client) -> None:
        from rnaseq_agent.connection_store import load_llm

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={
                "llm": {
                    "enabled": True,
                    "provider": "paratera",
                    "api_base": "https://llmapi.paratera.com/v1",
                    "model": "DeepSeek-V4-Flash",
                    "api_key": "sk-shared-key",
                }
            },
            headers=_headers(token),
        )

        stored = load_llm()

        assert stored["enabled"] is True
        assert stored["provider"] == "paratera"
        assert stored["api_base"] == "https://llmapi.paratera.com/v1"
        assert stored["model"] == "DeepSeek-V4-Flash"
        assert stored["api_key"] == "sk-shared-key"
        # 明文绝不能落盘。
        from rnaseq_agent.connection_store import connection_file_path

        assert "sk-shared-key" not in connection_file_path().read_text(encoding="utf-8")

    def test_chat_uses_shared_llm_when_project_has_no_config(self, client, monkeypatch) -> None:
        """核心回归：项目尚未创建（无 project.json）时也必须走 LLM。"""
        import requests

        from rnaseq_agent.connection_store import save_llm

        token = _token(client)
        # 用户已在设置页配过全局大模型，但当前没有任何项目。
        save_llm(
            {
                "enabled": True,
                "provider": "paratera",
                "api_base": "https://llmapi.paratera.com/v1",
                "model": "DeepSeek-V4-Flash",
                "api_key": "sk-shared-key",
            }
        )

        calls = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            calls["url"] = url
            calls["auth"] = headers.get("Authorization")
            calls["model"] = json["model"]

            class FakeResp:
                status_code = 200

                def json(self):
                    return {"choices": [{"message": {"content": "我看到了你的问题。"}}]}

            return FakeResp()

        monkeypatch.setattr(requests, "post", fake_post)
        resp = client.post(
            "/api/chat",
            json={"message": "RNA-seq 分析一般需要多少生物学重复？"},
            headers=_headers(token),
        ).json()

        assert resp["reply"] == "我看到了你的问题。", resp
        assert resp["via"] == "llm"
        assert calls["url"] == "https://llmapi.paratera.com/v1/chat/completions"
        assert calls["auth"] == "Bearer sk-shared-key"

    def test_new_project_inherits_shared_llm(self, client) -> None:
        from rnaseq_agent.connection_store import save_llm

        token = _token(client)
        save_llm(
            {
                "enabled": True,
                "api_base": "https://llm.example/v1",
                "model": "shared-model",
                "api_key": "sk-shared",
            }
        )

        resp = client.post(
            "/api/new",
            json={
                "project_id": "llm_inherit",
                "title": "llm inherit",
                "samples": [{"sample_id": "a", "condition": "ctrl", "fastq_1": "a_R1.fastq.gz"}],
            },
            headers=_headers(token),
        )
        assert "error" not in resp.json()

        cfg = client.get("/api/config", headers=_headers(token)).json()["config"]
        assert cfg["llm"]["enabled"] is True
        assert cfg["llm"]["model"] == "shared-model"
        assert cfg["llm"]["api_key_set"] is True
        # 下发到前端时 api_key 必须被抹掉。
        assert "api_key" not in cfg["llm"]

    def test_config_get_reports_shared_llm_before_any_project_exists(self, client) -> None:
        from rnaseq_agent.connection_store import save_llm

        token = _token(client)
        save_llm(
            {"enabled": True, "api_base": "https://llm.example/v1", "model": "m", "api_key": "sk-1"}
        )

        cfg = client.get("/api/config", headers=_headers(token)).json()["config"]

        assert cfg["llm"]["enabled"] is True
        assert cfg["llm"]["api_base"] == "https://llm.example/v1"
        assert cfg["llm"]["api_key_set"] is True

    def test_llm_models_endpoint_works_with_shared_llm_only(self, client, monkeypatch) -> None:
        import requests

        from rnaseq_agent.connection_store import save_llm

        token = _token(client)
        save_llm({"enabled": True, "api_base": "https://llm.example/v1", "model": "m", "api_key": "sk-1"})

        class Response:
            status_code = 200

            def json(self):
                return {"data": [{"id": "model-b"}, {"id": "model-a"}]}

            def raise_for_status(self):
                return None

        def fake_get(url, **kwargs):
            assert url == "https://llm.example/v1/models"
            assert kwargs["headers"]["Authorization"] == "Bearer sk-1"
            return Response()

        monkeypatch.setattr(requests, "get", fake_get)
        result = client.get("/api/llm/models", headers=_headers(token)).json()

        assert result == {"ok": True, "models": ["model-a", "model-b"]}

    def test_project_specific_llm_is_not_overwritten_by_shared(self, client) -> None:
        from rnaseq_agent.connection_store import save_llm

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={"llm": {"enabled": True, "api_base": "https://project.example/v1", "model": "project-model",
                          "api_key": "sk-project"}},
            headers=_headers(token),
        )
        # 之后再改全局：不应覆盖项目里已经明确填过的模型与地址。
        save_llm({"enabled": True, "api_base": "https://shared.example/v1", "model": "shared-model"})

        cfg = client.get("/api/config", headers=_headers(token)).json()["config"]

        assert cfg["llm"]["api_base"] == "https://project.example/v1"
        assert cfg["llm"]["model"] == "project-model"

    def test_shared_llm_enables_chat_reply_without_action(self, client, monkeypatch) -> None:
        """用户截图里的现象：纯提问得到「我还没理解成可执行操作」。

        配好全局大模型后，同一句话应得到模型答复而不是规则兜底。
        """
        import requests

        from rnaseq_agent.connection_store import save_llm

        token = _token(client)
        save_llm({"enabled": True, "api_base": "https://llm.example/v1", "model": "m", "api_key": "sk-1"})

        def fake_post(url, headers=None, json=None, timeout=None):
            class FakeResp:
                status_code = 200

                def json(self):
                    return {"choices": [{"message": {"content": "这是模型给出的解释。"}}]}

            return FakeResp()

        monkeypatch.setattr(requests, "post", fake_post)
        reply = client.post(
            "/api/chat", json={"message": "这个流程大概要跑多久"}, headers=_headers(token)
        ).json()

        assert reply["reply"] == "这是模型给出的解释。"
        assert "我还没理解成可执行操作" not in reply["reply"]
