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
    def test_remote_data_root_preview_uses_bounded_resolver_without_persisting(self, client, monkeypatch, tmp_path):
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.connection_store import save_connection
        from rnaseq_agent.execution import CommandResult

        token = _token(client)
        save_connection({"host": "h.example", "user": "alice", "port": 22})
        calls = {"bounded": [], "generic": 0}

        class Transport:
            def execute_bounded(self, command, *, absolute_deadline, max_capture_bytes, check=False):
                calls["bounded"].append((command, absolute_deadline, max_capture_bytes, check))
                return CommandResult([], 0, "/srv/team\n", "", 10)

            def execute(self, command):
                calls["generic"] += 1
                raise AssertionError("preview must not use generic execute")

        monkeypatch.setattr(webapp, "create_remote_transport", lambda config: Transport())
        response = client.post(
            "/api/settings/remote-data-roots/preview",
            json={"requested_path": "/data/team"},
            headers=_headers(token),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["requested_path"] == "/data/team"
        assert body["canonical_path"] == "/srv/team"
        assert body["host"] == "h.example"
        assert body["user"] == "alice"
        assert body["port"] == 22
        assert body["browse_policy_revision"].startswith("sha256:")
        assert len(calls["bounded"]) == 1
        assert calls["generic"] == 0
        assert not (tmp_path / "agent_home" / "connection.json").read_text(encoding="utf-8").__contains__("approved_data_roots")

    def test_remote_data_root_approve_and_revoke_are_revision_bound(self, client, monkeypatch):
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.connection_store import load_browse_policy, save_connection
        from rnaseq_agent.execution import CommandResult

        token = _token(client)
        save_connection({"host": "h.example", "user": "alice", "port": 22})

        class Transport:
            def execute_bounded(self, command, *, absolute_deadline, max_capture_bytes, check=False):
                return CommandResult([], 0, "/srv/team\n", "", 10)

        monkeypatch.setattr(webapp, "create_remote_transport", lambda config: Transport())
        preview = client.post(
            "/api/settings/remote-data-roots/preview",
            json={"requested_path": "/data/team"},
            headers=_headers(token),
        ).json()
        approved = client.post(
            "/api/settings/remote-data-roots",
            json={**preview, "expected_revision": preview["browse_policy_revision"]},
            headers=_headers(token),
        )
        assert approved.status_code == 200
        root = approved.json()["root"]
        assert root["root_id"].startswith("root_")
        listed = client.get("/api/settings/remote-data-roots", headers=_headers(token)).json()
        assert listed["roots"][0]["canonical_path"] == "/srv/team"
        assert listed["browse_policy_revision"] == load_browse_policy().revision

        stale = client.request(
            "DELETE",
            f"/api/settings/remote-data-roots/{root['root_id']}",
            json={"expected_revision": preview["browse_policy_revision"]},
            headers=_headers(token),
        )
        assert stale.status_code == 409
        assert stale.json()["error_code"] == "REMOTE_ROOT_REVISION_CONFLICT"

        current = client.get("/api/settings/remote-data-roots", headers=_headers(token)).json()
        revoked = client.request(
            "DELETE",
            f"/api/settings/remote-data-roots/{root['root_id']}",
            json={"expected_revision": current["browse_policy_revision"]},
            headers=_headers(token),
        )
        assert revoked.status_code == 200
        assert revoked.json()["root"]["revoked_at"] is not None

    def test_settings_page_has_remote_root_two_step_controls(self, client):
        page = client.get("/settings").text
        for control_id in ("remoteRootPath", "previewRemoteRoot", "approveRemoteRoot", "remoteRoots"):
            assert f'id="{control_id}"' in page

    @pytest.mark.parametrize("requested_path", ["/", "/data/../secret", "/data//reads", "relative"])
    def test_remote_data_root_preview_rejects_invalid_path_before_transport(self, client, monkeypatch, requested_path):
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.connection_store import save_connection

        token = _token(client)
        save_connection({"host": "h.example", "user": "alice", "port": 22})
        monkeypatch.setattr(webapp, "create_remote_transport", lambda _config: (_ for _ in ()).throw(AssertionError("transport must not be created")))
        response = client.post(
            "/api/settings/remote-data-roots/preview",
            json={"requested_path": requested_path},
            headers=_headers(token),
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "REMOTE_PATH_INVALID"

    @pytest.mark.parametrize("returncode", [44, 45])
    def test_remote_data_root_preview_preserves_resolution_errors(self, client, monkeypatch, returncode):
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.connection_store import save_connection
        from rnaseq_agent.execution import CommandResult

        token = _token(client)
        save_connection({"host": "h.example", "user": "alice", "port": 22})

        class Transport:
            def execute_bounded(self, command, *, absolute_deadline, max_capture_bytes, check=False):
                return CommandResult([], returncode, "", "", 0)

        monkeypatch.setattr(webapp, "create_remote_transport", lambda config: Transport())
        response = client.post(
            "/api/settings/remote-data-roots/preview",
            json={"requested_path": "/data/team"},
            headers=_headers(token),
        )
        expected = "REMOTE_PATH_NOT_FOUND" if returncode == 44 else "REMOTE_PATH_NOT_DIRECTORY"
        assert response.status_code in {404, 422}
        assert response.json()["error_code"] == expected

    def test_remote_data_root_preview_missing_credentials_is_connection_error(self, client, monkeypatch):
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.connection_store import save_connection

        token = _token(client)
        save_connection({"host": "h.example", "user": "alice", "port": 22, "auth_mode": "password"})
        called = {"transport": 0}

        def create(_config):
            called["transport"] += 1
            raise RuntimeError("当前选择了密码登录，但本次程序中没有临时密码")

        monkeypatch.setattr(webapp, "create_remote_transport", create)
        response = client.post(
            "/api/settings/remote-data-roots/preview",
            json={"requested_path": "/data/team"},
            headers=_headers(token),
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "REMOTE_CONNECTION_INVALID"
        assert called["transport"] == 0

    def test_remote_data_root_list_does_not_disclose_other_identity(self, client, monkeypatch):
        from rnaseq_agent.connection_store import approve_data_root, load_browse_policy, save_connection
        from rnaseq_agent.connection_store import ApprovedDataRoot

        token = _token(client)
        save_connection({"host": "h.example", "user": "alice", "port": 22})
        policy = load_browse_policy()
        approve_data_root(
            ApprovedDataRoot("root_alice", "h.example", "alice", 22, "/secret/alice", "/srv/alice", "2026-09-17T00:00:00Z", None),
            expected_revision=policy.revision,
        )
        save_connection({"user": "bob"})
        response = client.get("/api/settings/remote-data-roots", headers=_headers(token))
        assert response.status_code == 200
        assert response.json()["roots"] == []
        assert "/srv/alice" not in response.text

    def test_remote_data_root_revoke_cannot_target_other_identity(self, client):
        from rnaseq_agent.connection_store import (
            ApprovedDataRoot,
            approve_data_root,
            load_browse_policy,
            save_connection,
        )

        token = _token(client)
        save_connection({"host": "h.example", "user": "alice", "port": 22})
        policy = load_browse_policy()
        approve_data_root(
            ApprovedDataRoot("root_alice", "h.example", "alice", 22, "/secret/alice", "/srv/alice", "2026-09-17T00:00:00Z", None),
            expected_revision=policy.revision,
        )
        save_connection({"user": "bob"})
        current = client.get("/api/settings/remote-data-roots", headers=_headers(token)).json()
        response = client.request(
            "DELETE",
            "/api/settings/remote-data-roots/root_alice",
            json={"expected_revision": current["browse_policy_revision"]},
            headers=_headers(token),
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "REMOTE_ROOT_REVISION_CONFLICT"

    @pytest.mark.parametrize("bad_port", [None, True, False, "", "  ", "-1", "65536", 0, -1, 65536, 65536 + 1, "-oProxyCommand=calc"])
    def test_remote_data_root_approve_rejects_invalid_port_before_transport(self, client, monkeypatch, bad_port):
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.connection_store import save_connection
        from rnaseq_agent.execution import CommandResult

        token = _token(client)
        save_connection({"host": "h.example", "user": "alice", "port": 22})

        class Transport:
            def execute_bounded(self, command, *, absolute_deadline, max_capture_bytes, check=False):
                return CommandResult([], 0, "/srv/team\n", "", 10)

        monkeypatch.setattr(webapp, "create_remote_transport", lambda config: Transport())
        preview = client.post(
            "/api/settings/remote-data-roots/preview",
            json={"requested_path": "/data/team"},
            headers=_headers(token),
        ).json()
        monkeypatch.setattr(webapp, "create_remote_transport", lambda _config: (_ for _ in ()).throw(AssertionError("invalid port reached transport")))
        preview["port"] = bad_port
        response = client.post(
            "/api/settings/remote-data-roots",
            json={**preview, "expected_revision": preview["browse_policy_revision"]},
            headers=_headers(token),
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "REMOTE_CONNECTION_INVALID"

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

    def test_non_streaming_chat_refuses_actions_without_durable_confirmation(
        self, client, tmp_path: Path
    ) -> None:
        token = _token(client)
        _new_project(client, token)
        config_path = tmp_path / "proj" / "project.json"
        session_path = tmp_path / "proj" / "session.json"
        config_before = config_path.read_text(encoding="utf-8")
        session_before = session_path.read_text(encoding="utf-8")

        # This legacy endpoint has no checkpointer-backed resume path, so it
        # must refuse side effects instead of bypassing the tool guardrail.
        resp = client.post(
            "/api/chat",
            json={"message": "生成执行计划"},
            headers=_headers(token),
        ).json()
        assert resp["state"] == "drafting"
        assert resp["via"] == "confirmation_required"
        assert resp["confirmation_required"] is True
        assert resp["action"] == "plan"

        resp = client.post(
            "/api/chat",
            json={"message": "把线程改成 24"},
            headers=_headers(token),
        ).json()
        assert resp["state"] == "drafting"
        assert resp["via"] == "confirmation_required"
        assert resp["confirmation_required"] is True
        assert resp["action"] == "edit"
        assert config_path.read_text(encoding="utf-8") == config_before
        assert session_path.read_text(encoding="utf-8") == session_before

    @pytest.mark.parametrize(
        ("mode", "message", "expected_via"),
        [
            ("disabled", "查看当前状态", "blocked"),
            ("read_only", "查看当前状态", "tool"),
            ("read_only", "把线程改成 24", "blocked"),
            ("approved_write", "把线程改成 24", "confirmation_required"),
            ("approved_write", "生成执行计划", "blocked"),
            ("approved_execute", "生成执行计划", "confirmation_required"),
        ],
    )
    def test_rule_fallback_obeys_the_llm_tool_mode_without_writing(
        self, client, tmp_path: Path, mode: str, message: str, expected_via: str
    ) -> None:
        from rnaseq_agent.connection_store import save_llm

        token = _token(client)
        _new_project(client, token)
        save_llm({"tool_mode": mode})
        config_path = tmp_path / "proj" / "project.json"
        session_path = tmp_path / "proj" / "session.json"
        before = (config_path.read_bytes(), session_path.read_bytes())

        result = client.post(
            "/api/chat", json={"message": message}, headers=_headers(token)
        ).json()

        assert result.get("via", "tool") == expected_via
        if expected_via == "blocked":
            assert result["blocked"] is True
            assert result["tool_mode"] == mode
        assert (config_path.read_bytes(), session_path.read_bytes()) == before

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

    @pytest.mark.parametrize(
        "server",
        [
            {"host": "-oProxyCommand=calc", "user": "alice", "port": 22},
            {"host": "good.example", "user": "-Fbad", "port": 22},
            {"host": "bad host", "user": "alice", "port": 22},
            {"host": " good.example", "user": "alice", "port": 22},
            {"host": "alice@evil", "user": "alice", "port": 22},
            {"host": "good.example", "user": "alice;id", "port": 22},
            {"host": "good.example", "user": "alice ", "port": 22},
            {"host": "good.example", "user": "alice", "port": 0},
            {"host": "good.example", "user": "alice", "port": 65536},
            {"host": "good.example", "user": "alice", "port": True},
            {"host": "good.example", "user": "alice", "port": 22.0},
        ],
    )
    def test_config_endpoint_rejects_invalid_connection_before_persistence(
        self, client, monkeypatch, server: dict
    ) -> None:
        import rnaseq_agent.webapp as webapp_module
        from unittest.mock import MagicMock

        token = _token(client)
        save_connection = MagicMock()
        set_credential = MagicMock()
        monkeypatch.setattr(webapp_module, "save_connection", save_connection)
        monkeypatch.setattr(webapp_module, "set_ssh_credential", set_credential)

        result = client.post(
            "/api/config",
            json={
                "server": {
                    **server,
                    "auth_mode": "password",
                    "password": "test-only-secret",
                }
            },
            headers=_headers(token),
        ).json()

        assert "error" in result
        assert save_connection.call_count == 0
        assert set_credential.call_count == 0

    def test_invalid_connection_is_rejected_before_restoring_stored_credentials(
        self, client, monkeypatch
    ) -> None:
        import rnaseq_agent.webapp as webapp_module
        from rnaseq_agent.connection_store import save_connection as persist_connection
        from unittest.mock import MagicMock

        token = _token(client)
        persist_connection(
            {
                "host": "stored.example",
                "user": "stored_user",
                "port": 22,
                "auth_mode": "key",
            }
        )
        save_connection = MagicMock()
        set_credential = MagicMock()
        monkeypatch.setattr(webapp_module, "save_connection", save_connection)
        monkeypatch.setattr(webapp_module, "set_ssh_credential", set_credential)

        result = client.post(
            "/api/config",
            json={"server": {"host": "-oProxyCommand=calc"}},
            headers=_headers(token),
        ).json()

        assert "error" in result
        save_connection.assert_not_called()
        set_credential.assert_not_called()

    @pytest.mark.parametrize(
        "server",
        [
            {"host": "hpc.example.edu", "user": "alice", "port": 22},
            {"host": "192.0.2.10", "user": "alice_1", "port": "2222"},
            {"host": "[2001:db8::1]", "user": "alice.dev", "port": None},
            {"host": "2001:db8::1", "user": "alice-dev", "port": ""},
        ],
    )
    def test_config_endpoint_accepts_valid_connection_identity(
        self, client, server: dict
    ) -> None:
        token = _token(client)

        result = client.post(
            "/api/config",
            json={"server": server},
            headers=_headers(token),
        ).json()

        assert "error" not in result

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

    def test_config_endpoint_persists_and_returns_the_llm_tool_mode(self, client) -> None:
        from rnaseq_agent.agent_tools import TOOL_MODE_READ_ONLY
        from rnaseq_agent.connection_store import load_llm

        token = _token(client)
        _new_project(client, token)
        saved = client.post(
            "/api/config",
            json={"llm": {"tool_mode": TOOL_MODE_READ_ONLY}},
            headers=_headers(token),
        ).json()

        assert saved["config"]["llm"]["tool_mode"] == TOOL_MODE_READ_ONLY
        assert load_llm()["tool_mode"] == TOOL_MODE_READ_ONLY
        assert client.get("/api/config", headers=_headers(token)).json()["config"]["llm"][
            "tool_mode"
        ] == TOOL_MODE_READ_ONLY

    def test_tool_mode_can_be_changed_as_a_global_setting_without_creating_a_project(
        self, client, tmp_path: Path
    ) -> None:
        from rnaseq_agent.agent_tools import TOOL_MODE_DISABLED

        token = _token(client)
        result = client.post(
            "/api/config",
            json={"llm": {"tool_mode": TOOL_MODE_DISABLED}},
            headers=_headers(token),
        ).json()

        assert "error" not in result
        assert result["config"]["llm"]["tool_mode"] == TOOL_MODE_DISABLED
        assert not (tmp_path / "proj" / "project.json").exists()

    @pytest.mark.parametrize("invalid", ["unlimited", False, 0, [], {}, None])
    def test_config_endpoint_rejects_an_invalid_llm_tool_mode(
        self, client, invalid: object
    ) -> None:
        from rnaseq_agent.agent_tools import TOOL_MODE_APPROVED_EXECUTE
        from rnaseq_agent.connection_store import load_llm, save_llm

        token = _token(client)
        _new_project(client, token)
        save_llm({"tool_mode": TOOL_MODE_APPROVED_EXECUTE})
        result = client.post(
            "/api/config",
            json={"llm": {"tool_mode": invalid}},
            headers=_headers(token),
        ).json()

        assert "error" in result
        assert "tool_mode" in result["error"]
        assert load_llm()["tool_mode"] == TOOL_MODE_APPROVED_EXECUTE

    def test_settings_page_exposes_all_four_llm_tool_modes(self, client) -> None:
        page = client.get("/settings").text

        assert 'id="llmToolMode"' in page
        for mode in ("disabled", "read_only", "approved_write", "approved_execute"):
            assert f'value="{mode}"' in page
        assert '<option value="approved_execute" selected>' in page

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

    def test_restart_restores_password_on_any_session_entry(self, client) -> None:
        """重启后只要进任何会话入口就该恢复密码，不必先去设置页点一下。

        用户诉求（2026-09-15）：「我不想每次重新进去都要重新写一遍我的服务器
        账号密码……就算我关了重新运行也不需要重新配置」。此前恢复只发生在
        ``GET /api/config``，用户直接打开工作台 / 对话页时运行时凭据仍为空，
        ``create_remote_transport`` 于是报「本次程序中没有临时密码」。
        """
        from rnaseq_agent.ssh_auth import clear_ssh_credential, get_ssh_credential

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={"server": {"host": "10.30.24.1", "user": "yeyulin", "auth_mode": "password", "password": "keep-me"}},
            headers=_headers(token),
        )

        # 模拟重启：进程内凭据清空。
        clear_ssh_credential("10.30.24.1", "yeyulin")
        assert get_ssh_credential("10.30.24.1", "yeyulin").password == ""

        # 只读一次项目状态（工作台/对话页打开时就会调用），不碰 /api/config。
        assert client.get("/api/state", headers=_headers(token)).status_code == 200

        credential = get_ssh_credential("10.30.24.1", "yeyulin")
        assert credential.mode == "password"
        assert credential.password == "keep-me"

    def test_connection_as_config_restores_password_for_transport(self, client) -> None:
        """``_connection_as_config()`` 取回连接后必须能立刻建 transport。

        这条路径是「项目还没有 project.json 就浏览服务器目录」时用的；
        如果它只返回字段而不注入凭据，密码用户会直接撞上
        「本次程序中没有临时密码」。
        """
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.ssh_auth import clear_ssh_credential, get_ssh_credential

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={"server": {"host": "10.30.24.1", "user": "yeyulin", "auth_mode": "password", "password": "pw-123456789"}},
            headers=_headers(token),
        )
        clear_ssh_credential("10.30.24.1", "yeyulin")

        resolved = webapp._connection_as_config()
        assert resolved is not None
        assert resolved["server"]["host"] == "10.30.24.1"

        credential = get_ssh_credential("10.30.24.1", "yeyulin")
        assert credential.mode == "password"
        assert credential.password == "pw-123456789"

    def test_runtime_credential_is_not_clobbered_by_stale_shared_config(self, client) -> None:
        """当前进程里刚填的密码优先于磁盘上的旧值，避免被还原逻辑覆盖。"""
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.ssh_auth import set_ssh_credential, get_ssh_credential

        token = _token(client)
        _new_project(client, token)
        client.post(
            "/api/config",
            json={"server": {"host": "10.30.24.1", "user": "yeyulin", "auth_mode": "password", "password": "on-disk"}},
            headers=_headers(token),
        )
        # 用户在本次进程里改成了新密码。
        set_ssh_credential("10.30.24.1", "yeyulin", mode="password", password="in-memory")

        webapp._restore_runtime_credential()
        assert get_ssh_credential("10.30.24.1", "yeyulin").password == "in-memory"

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
        session_path = tmp_path / "proj" / "session.json"
        before = session_path.read_text(encoding="utf-8")
        resp = client.post(
            "/api/chat",
            json={"message": "生成执行计划"},
            headers=_headers(token),
        ).json()
        assert resp["state"] == "drafting"
        assert resp["via"] == "confirmation_required"
        assert resp["confirmation_required"] is True
        assert resp["action"] == "plan"
        assert session_path.read_text(encoding="utf-8") == before


class TestSystemPromptForbidsFakeWrites:
    """大模型不得声称自己「保存了配置」——这条通道里它没有工具可调。

    用户实测（2026-09-15）：粘贴 FASTQ 路径与参考基因组后，AI 回了一句
    「已识别为配置保存操作……配置已保存 ✓」，但项目里根本没有 project.json，
    紧接着「生成执行计划」就报状态错误。那条「配置已保存」是模型编造的：
    真正写盘的只有确定性代码。因此系统提示词必须明确禁止这类声明。

    2026-09-16 起：**正常**对话路径（``/api/chat/stream``）的模型是真有工具的
    （见 ``chat_graph.SYSTEM_PROMPT``），写盘由它的 ``write_project_config``
    加人工确认完成。本提示词只服务这条「对话图不可用」的纯文本兜底通道，
    所以措辞改成如实描述「这条通道里没有可调用的工具」。
    """

    def test_prompt_states_no_tool_capability(self) -> None:
        import rnaseq_agent.webapp as webapp

        system = next(m["content"] for m in webapp._llm_messages("测试") if m["role"] == "system")
        # 兜底通道必须如实说明：这里没有工具，写盘只能由确定性代码做。
        assert "没有可调用的工具" in system
        assert "绝对不要声称" in system

    @pytest.mark.parametrize(
        "claim",
        ["保存了配置", "写入了文件", "修改了参数", "完成了分析"],
    )
    def test_prompt_lists_the_forbidden_claims(self, claim: str) -> None:
        import rnaseq_agent.webapp as webapp

        system = next(m["content"] for m in webapp._llm_messages("测试") if m["role"] == "system")
        assert claim in system, f"提示词未禁止声称「{claim}」"

    def test_prompt_tells_the_model_to_point_at_real_buttons(self) -> None:
        import rnaseq_agent.webapp as webapp

        system = next(m["content"] for m in webapp._llm_messages("测试") if m["role"] == "system")
        assert "生成执行计划" in system
        assert "确认并冻结契约" in system

    def test_prompt_points_at_the_execute_phrase_that_now_writes(self) -> None:
        """写入能力已经接通对话，提示词不能再让用户去「点界面按钮」。

        用户实测（2026-09-16）：AI 回「我无法直接执行，只能帮你核对配置」，
        用户于是不知道该点哪，再问一次还是同一句。既然「你帮我执行」现在会
        真正写盘，模型应当把用户引到这句话上。
        """
        import rnaseq_agent.webapp as webapp

        system = next(m["content"] for m in webapp._llm_messages("测试") if m["role"] == "system")
        assert "你帮我执行" in system, system
        assert "需要点击界面按钮" not in system, "写入已可由对话触发，不必再让用户找按钮"


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
