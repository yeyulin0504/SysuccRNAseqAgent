"""流式对话：像 Codex 一样展示思考过程并逐字输出。

用户需求（2026-09-15）：

- 对话记录要**有显示记录**、要有大片对话框；
- 要能像 Codex 一样**展现思考动态过程**。

因此新增 ``POST /api/chat/stream``：以 SSE 逐步推送

1. ``step``   —— 思考步骤（理解问题 / 读取项目上下文 / 调用大模型 / 执行操作）；
2. ``delta``  —— 逐段输出回复正文；
3. ``done``   —— 收尾，带最终 state / via / thread_id / message_id。

同时把用户提问与 Agent 答复落盘到当前对话（thread），刷新或切换对话
后仍可回看完整记录。
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
    monkeypatch.setenv("RNASEQ_AGENT_HOME", str(tmp_path / "agent_home"))
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


def _with_session(client, token: str, project_id: str) -> None:
    """Give the project a real analysis session (so plan/confirm can run)."""
    resp = client.post(
        f"/api/new?project={project_id}",
        json={
            "project_id": project_id,
            "title": f"P {project_id}",
            "samples": [
                {"sample_id": "a", "condition": "ctrl", "fastq_1": "a_R1.fastq.gz", "fastq_2": "a_R2.fastq.gz"},
                {"sample_id": "b", "condition": "trt", "fastq_1": "b_R1.fastq.gz", "fastq_2": "b_R2.fastq.gz"},
            ],
        },
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "drafting", resp.text


def _parse_sse(text: str) -> list[dict]:
    """Parse an SSE body into ``[{"event": ..., "data": {...}}, ...]``."""
    events: list[dict] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        name = ""
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if not name:
            continue
        payload = "\n".join(data_lines)
        try:
            data = json.loads(payload) if payload else {}
        except ValueError:
            data = {"_raw": payload}
        events.append({"event": name, "data": data})
    return events


def _stream(client, token: str, message: str, *, project: str = "", **extra) -> list[dict]:
    path = "/api/chat/stream"
    if project:
        path += "?project=" + project
    resp = client.post(path, json={"message": message, **extra}, headers=_headers(token))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return _parse_sse(resp.text)


def _deltas(events: list[dict]) -> str:
    return "".join(e["data"]["text"] for e in events if e["event"] == "delta")


class TestStreamFraming:
    def test_stream_returns_event_stream_content_type(self, client) -> None:
        token = _token(client)
        resp = client.post(
            "/api/chat/stream",
            json={"message": "你好"},
            headers=_headers(token),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

    def test_stream_requires_token(self, client) -> None:
        resp = client.post("/api/chat/stream", json={"message": "hi"})
        assert resp.status_code == 403

    def test_empty_message_is_rejected(self, client) -> None:
        token = _token(client)
        events = _stream(client, token, "   ")
        assert any(e["event"] == "error" for e in events)

    def test_stream_ends_with_done_event(self, client) -> None:
        token = _token(client)
        _create_project(client, token, "stream_b")
        events = _stream(client, token, "你好", project="stream_b")
        assert events[-1]["event"] == "done"
        assert "state" in events[-1]["data"]


class TestThinkingSteps:
    def test_emits_step_events_before_answer(self, client) -> None:
        """思考步骤必须出现在正文之前，才能形成「动态过程」。"""
        token = _token(client)
        _create_project(client, token, "stream_c")
        events = _stream(client, token, "你好", project="stream_c")

        kinds = [e["event"] for e in events]
        assert "step" in kinds, events
        first_delta = kinds.index("delta") if "delta" in kinds else len(kinds)
        assert kinds.index("step") < first_delta

    def test_step_has_label_and_status(self, client) -> None:
        token = _token(client)
        _create_project(client, token, "stream_d")
        events = _stream(client, token, "你好", project="stream_d")
        steps = [e["data"] for e in events if e["event"] == "step"]

        assert steps, events
        for step in steps:
            assert step.get("label"), step
            assert step.get("status") in {"running", "done", "failed"}, step

    def test_understand_step_reports_detected_intent(self, client) -> None:
        """用户要能看到「系统理解成了什么」。"""
        token = _token(client)
        _create_project(client, token, "stream_e")
        events = _stream(client, token, "你好", project="stream_e")
        steps = [e["data"] for e in events if e["event"] == "step"]
        understand = [s for s in steps if s.get("id") == "understand"]

        assert understand, steps
        assert understand[-1]["status"] == "done"
        assert understand[-1].get("detail")

    def test_llm_step_present_when_model_configured(self, client, monkeypatch) -> None:
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.connection_store import save_llm

        token = _token(client)
        _create_project(client, token, "stream_f")
        save_llm({"enabled": True, "api_base": "https://llm.example/v1", "model": "m", "api_key": "sk-1"})
        monkeypatch.setattr(webapp, "_llm_stream_chunks", lambda config, text: iter(["模型", "答复"]))

        events = _stream(client, token, "这个流程大概要跑多久", project="stream_f")
        steps = [e["data"] for e in events if e["event"] == "step"]
        assert any(s.get("id") == "llm" for s in steps), steps
        assert _deltas(events) == "模型答复"

    def test_tool_step_present_for_actionable_message(self, client) -> None:
        """可执行动作要显示成「正在执行操作」，而不是偷偷跑完。"""
        token = _token(client)
        _create_project(client, token, "stream_tool")
        _with_session(client, token, "stream_tool")

        events = _stream(client, token, "生成执行计划", project="stream_tool")
        steps = [e["data"] for e in events if e["event"] == "step"]

        assert any(s.get("id") == "tool" for s in steps), steps

    def test_context_step_names_the_real_model(self, client, monkeypatch) -> None:
        """思考过程要报出真实模型名，而不是「未指定模型」。

        模型名在 ``config["llm"]["model"]``，此前直接对顶层取 ``model``
        永远取空，用户看到的思考流里模型一栏始终是「未指定模型」。
        """
        from rnaseq_agent.connection_store import save_llm

        token = _token(client)
        _create_project(client, token, "stream_model")
        save_llm(
            {
                "enabled": True,
                "api_base": "https://llm.example/v1",
                "model": "DeepSeek-V4-Flash",
                "api_key": "sk-1",
            }
        )

        events = _stream(client, token, "这个流程大概要跑多久", project="stream_model")
        steps = [e["data"] for e in events if e["event"] == "step"]
        context = [s for s in steps if s.get("id") == "context"][-1]

        assert "DeepSeek-V4-Flash" in context.get("detail", ""), context


class TestStreamingDeltas:
    def test_deltas_concatenate_to_full_reply(self, client, monkeypatch) -> None:
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.connection_store import save_llm

        token = _token(client)
        _create_project(client, token, "stream_g")
        save_llm({"enabled": True, "api_base": "https://llm.example/v1", "model": "m", "api_key": "sk-1"})
        monkeypatch.setattr(webapp, "_llm_stream_chunks", lambda config, text: iter(["每", "组", "至少", "3 个"]))
        events = _stream(client, token, "为什么要三个重复", project="stream_g")
        deltas = [e["data"]["text"] for e in events if e["event"] == "delta"]

        assert len(deltas) > 1, "必须逐段推送，而不是一次性给出"
        assert _deltas(events) == "每组至少3 个"

    def test_done_reports_via_and_reply(self, client, monkeypatch) -> None:
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.connection_store import save_llm

        token = _token(client)
        _create_project(client, token, "stream_h")
        save_llm({"enabled": True, "api_base": "https://llm.example/v1", "model": "m", "api_key": "sk-1"})
        monkeypatch.setattr(webapp, "_llm_stream_chunks", lambda config, text: iter(["好的"]))
        events = _stream(client, token, "你好", project="stream_h")
        done = events[-1]["data"]

        assert done["via"] == "llm"
        assert done["reply"] == "好的"

    def test_rule_path_still_streams_one_delta(self, client) -> None:
        """未配模型时也要有正文，规则答复整段推一次即可。"""
        token = _token(client)
        _create_project(client, token, "stream_i")
        events = _stream(client, token, "你好", project="stream_i")

        assert _deltas(events)
        assert events[-1]["data"]["via"] == "rule"

    def test_llm_failure_falls_back_to_rule_with_delta(self, client, monkeypatch) -> None:
        import rnaseq_agent.webapp as webapp
        from rnaseq_agent.connection_store import save_llm

        token = _token(client)
        _create_project(client, token, "stream_j")
        save_llm({"enabled": True, "api_base": "https://llm.example/v1", "model": "m", "api_key": "sk-1"})
        # 模型返回空流 → 视为失败，回落规则路由。
        monkeypatch.setattr(webapp, "_llm_stream_chunks", lambda config, text: iter([]))

        events = _stream(client, token, "这个流程大概要跑多久", project="stream_j")

        assert _deltas(events)
        assert events[-1]["data"]["via"] == "rule"


class TestThreadPersistence:
    def test_streamed_turn_is_persisted_to_thread(self, client) -> None:
        """刷新/切换对话后要能回看，因此必须落盘。"""
        token = _token(client)
        _create_project(client, token, "stream_k")

        events = _stream(client, token, "你好", project="stream_k")
        done = events[-1]["data"]
        thread_id = done["thread_id"]

        saved = client.get(
            f"/api/projects/stream_k/threads/{thread_id}/messages",
            headers=_headers(token),
        ).json()["messages"]

        roles = [m["role"] for m in saved]
        assert roles == ["user", "agent"], saved
        assert saved[0]["content"] == "你好"
        assert saved[1]["content"] == done["reply"]

    def test_agent_message_records_thinking_steps(self, client) -> None:
        """思考过程也要留存，回看时能展开。"""
        token = _token(client)
        _create_project(client, token, "stream_l")
        events = _stream(client, token, "你好", project="stream_l")
        done = events[-1]["data"]

        saved = client.get(
            f"/api/projects/stream_l/threads/{done['thread_id']}/messages",
            headers=_headers(token),
        ).json()["messages"]
        agent = saved[-1]

        assert agent["references"].get("via") == "rule"
        assert agent["references"].get("steps"), agent

    def test_stream_targets_named_thread(self, client) -> None:
        token = _token(client)
        _create_project(client, token, "stream_m")
        client.post(
            "/api/projects/stream_m/threads",
            json={"thread_id": "second", "title": "第二对话"},
            headers=_headers(token),
        )

        events = _stream(client, token, "你好", project="stream_m", thread_id="second")
        assert events[-1]["data"]["thread_id"] == "second"

        saved = client.get(
            "/api/projects/stream_m/threads/second/messages",
            headers=_headers(token),
        ).json()["messages"]
        assert len(saved) == 2

    def test_tool_intent_runs_and_reports_state(self, client) -> None:
        """工具动作（生成计划）在流式路径下同样要真正执行。"""
        token = _token(client)
        _create_project(client, token, "stream_n")
        _with_session(client, token, "stream_n")

        events = _stream(client, token, "生成执行计划", project="stream_n")
        done = events[-1]["data"]

        assert done["state"] == "planned", done
        state = client.get("/api/state?project=stream_n", headers=_headers(token)).json()
        assert state["state"] == "planned"


class TestChatPage:
    def test_chat_page_renders(self, client) -> None:
        resp = client.get("/chat")
        assert resp.status_code == 200
        assert "SYSU" in resp.text
        assert "const TOKEN" in resp.text

    def test_chat_page_renders_with_project(self, client) -> None:
        token = _token(client)
        _create_project(client, token, "chat_p")
        resp = client.get("/chat?project=chat_p")
        assert resp.status_code == 200

    def test_chat_page_has_message_area_and_composer(self, client) -> None:
        page = client.get("/chat").text
        for control_id in ("chatStream", "chatComposer", "chatSend", "threadList", "newChat"):
            assert f'id="{control_id}"' in page, control_id

    def test_chat_page_subscribes_to_stream_endpoint(self, client) -> None:
        page = client.get("/chat").text
        assert "/api/chat/stream" in page

    def test_workbench_links_to_chat_page(self, client) -> None:
        page = client.get("/workbench").text
        assert 'href="/chat' in page
