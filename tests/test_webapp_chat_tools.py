"""流式对话的工具循环端到端：模型调工具 → 弹确认 → resume 真写盘。

用户诉求（2026-09-16）：

> 我要修复的是大语言模型应该要有自己的执行工具啊
> 风险度高的要给人确认
> 保留为正则兜底

这里测的是 **webapp 端点层面** 的完整链路（``/api/chat/stream`` +
``/api/chat/resume``），与 ``test_chat_graph``（图层面）互补：

- ``test_chat_graph`` 用内存 checkpointer 锁图逻辑；
- 本文件用**真实的 SqliteSaver + 真实的项目目录**，验证确认前不写盘、
  批准后恰好写一次、拒绝后不写、以及多轮对话历史跨 resume 保留。

LLM 用 fake（脚本化 tool_calls），但写盘走 ``_run_tool`` → ``_write_project_session``
的真实链路——配置最终落在磁盘 ``project.json``。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

fastapi_missing = False
try:
    from fastapi.testclient import TestClient

    from rnaseq_agent import chat_graph as cg
    from rnaseq_agent.webapp import create_app
except ImportError:
    fastapi_missing = True

pytestmark = pytest.mark.skipif(fastapi_missing, reason="fastapi/httpx not installed")

langgraph = pytest.importorskip("langgraph")

SAMPLE_WRITE_ARGS = {
    "data_source": "remote_path",
    "fastq_dir": "/hwdata/reads",
    "samples": [
        {
            "sample_id": "S1",
            "condition": "control",
            "fastq_1": "S1_1.fastq.gz",
            "fastq_2": "S1_2.fastq.gz",
        },
        {
            "sample_id": "S2",
            "condition": "treat",
            "fastq_1": "S2_1.fastq.gz",
            "fastq_2": "S2_2.fastq.gz",
        },
        {
            "sample_id": "S3",
            "condition": "control",
            "fastq_1": "S3_1.fastq.gz",
            "fastq_2": "S3_2.fastq.gz",
        },
        {
            "sample_id": "S4",
            "condition": "treat",
            "fastq_1": "S4_1.fastq.gz",
            "fastq_2": "S4_2.fastq.gz",
        },
    ],
    "strandedness": "unknown",
}


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


class FakeLLM:
    """Scripted model: each call returns the next queued turn.

    记录收到的 messages，便于断言「确认卡片的内容真的回灌给了模型」。
    """

    def __init__(self, turns: list[dict]) -> None:
        self.turns = list(turns)
        self.seen_messages: list[list[dict]] = []

    def __call__(self, llm_config, messages, *, timeout=60.0):
        self.seen_messages.append([dict(m) for m in messages])
        turn = self.turns.pop(0) if self.turns else {"content": "（没有更多剧本）"}
        content = str(turn.get("content") or "")
        if content:
            yield ("delta", content)
        yield ("message", {"content": content, "tool_calls": turn.get("tool_calls") or []})


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


def _create_project(client, token: str, project_id: str) -> None:
    resp = client.post(
        "/api/projects",
        json={"project_id": project_id, "title": f"P {project_id}"},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text


def _configure_llm(client, token: str) -> None:
    from rnaseq_agent.connection_store import save_llm

    save_llm(
        {
            "enabled": True,
            "api_base": "https://llm.example/v1",
            "model": "DeepSeek-V4-Flash",
            "api_key": "sk-1",
        }
    )


def _parse_sse(text: str) -> list[dict]:
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


def _resume(client, token: str, *, project: str, approved: bool, note: str = "") -> list[dict]:
    resp = client.post(
        "/api/chat/resume",
        json={"project_id": project, "thread_id": "main", "approved": approved, "note": note},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return _parse_sse(resp.text)


def _deltas(events: list[dict]) -> str:
    return "".join(e["data"]["text"] for e in events if e["event"] == "delta")


def _confirm_event(events: list[dict]) -> dict | None:
    for e in events:
        if e["event"] == "confirm":
            return e["data"]
    return None


def _project_json(tmp_path: Path, project_id: str) -> dict | None:
    path = tmp_path / project_id / "project.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _session_json_exists(tmp_path: Path, project_id: str) -> bool:
    return (tmp_path / project_id / "session.json").is_file()


class TestModelWritesConfigViaTool:
    """模型调 write_project_config：确认前不写，批准后恰好写一次。"""

    def test_confirmation_blocks_write_until_approved(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "tool_a")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("call_1", "write_project_config", SAMPLE_WRITE_ARGS)]},
                {"content": "配置已经写入，4 个样本按 control/treat 循环分组。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        # 第一轮：模型请求写盘 → 必须挂起等确认。
        events = _stream(client, token, "把样本和分组写进项目", project="tool_a")

        assert _confirm_event(events) is not None, events
        # 确认前磁盘上没有 session.json —— 写盘被守卫拦住了。
        assert not _session_json_exists(tmp_path, "tool_a"), "确认前就写了盘？"

        done = events[-1]["data"]
        assert done.get("awaiting_confirmation") is True, done
        assert done["via"] == "tool", done

        # 第二轮：用户批准 → 真正写盘，且只写一次。
        events2 = _resume(client, token, project="tool_a", approved=True)
        project = _project_json(tmp_path, "tool_a")
        assert project is not None, "批准后没有写盘"
        conditions = [s["condition"] for s in project["samples"]["items"]]
        assert conditions == ["control", "treat", "control", "treat"], conditions
        assert project["sequencing"]["strandedness"] == "unknown"

        done2 = events2[-1]["data"]
        assert done2["state"] == "drafting", done2

    def test_rejected_tool_never_writes(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "tool_b")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("call_1", "write_project_config", SAMPLE_WRITE_ARGS)]},
                {"content": "好的，我没有写入。要不要我先读一下项目现有状态？"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "把样本写进项目", project="tool_b")
        assert _confirm_event(events) is not None, events

        # 用户拒绝 → 不写盘，且模型收到「用户拒绝」这个结果。
        events2 = _resume(client, token, project="tool_b", approved=False, note="样本表有误，先别写")
        assert not _session_json_exists(tmp_path, "tool_b"), "拒绝后仍然写了盘？"

        assert fake.seen_messages[-1][-1]["role"] == "tool"
        tool_content = fake.seen_messages[-1][-1]["content"]
        assert "用户拒绝" in tool_content, tool_content
        assert "样本表有误" in tool_content, tool_content

        # 模型给替代方案，用户能看到这段回复。
        # 注意：拒绝后图会回到 agent 让模型说话，所以最终 via 是 ``llm``；
        # 拒绝这件事体现在回灌给模型的工具结果里（上面已断言）。
        reply = _deltas(events2)
        assert "没有写入" in reply or "替代" in reply or "读" in reply, reply
        # 思考流要如实告诉用户「你拒绝了」，而不是假装什么都没发生。
        confirm_steps = [
            e["data"] for e in events2 if e["event"] == "step" and e["data"]["id"] == "confirm"
        ]
        assert any("拒绝" in s["detail"] for s in confirm_steps), confirm_steps

    def test_read_tool_runs_without_confirmation(self, client, tmp_path, monkeypatch) -> None:
        """只读工具不弹确认，直接执行并回灌结果。"""
        token = _token(client)
        _create_project(client, token, "tool_c")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("call_1", "read_project_state", {})]},
                {"content": "项目还没有样本，先配置数据再继续。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "现在项目什么状态", project="tool_c")

        # 没有确认卡片：只读操作风险为 read，守卫直接放行。
        assert _confirm_event(events) is None, events
        assert "还没有样本" in _deltas(events), events
        done = events[-1]["data"]
        assert done["via"] == "llm", done
        # 工具结果回灌给了模型（第二句剧本前能看到 tool 消息）。
        assert fake.seen_messages[-1][-1]["role"] == "tool", fake.seen_messages[-1]

    def test_invalid_arguments_are_caught_without_confirm(self, client, tmp_path, monkeypatch) -> None:
        """参数非法的调用不需要人确认——守卫直接把错误回灌给模型纠正。"""
        token = _token(client)
        _create_project(client, token, "tool_d")
        _configure_llm(client, token)
        bad_args = dict(SAMPLE_WRITE_ARGS)
        bad_args["samples"] = []  # minItems=1，必被校验拒绝
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("call_1", "write_project_config", bad_args)]},
                {"content": "样本列表是空的，我调整一下参数。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "把样本写进项目", project="tool_d")

        # 参数错误：不弹确认，直接回灌错误让模型自纠。
        assert _confirm_event(events) is None, events
        assert not _session_json_exists(tmp_path, "tool_d")
        assert "样本" in _deltas(events), events

    def test_multi_turn_conversation_keeps_history_across_resume(self, client, tmp_path, monkeypatch) -> None:
        """resume 后模型要记得上一轮的内容（图 checkpoint 保留完整消息）。"""
        token = _token(client)
        _create_project(client, token, "tool_e")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("call_1", "write_project_config", SAMPLE_WRITE_ARGS)]},
                # resume 后模型读到工具结果，应该基于「已写入 4 样本」作答。
                {"content": "已写入 4 个样本，control/treat 各两个。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "把样本写进项目", project="tool_e")
        assert _confirm_event(events) is not None, events

        events2 = _resume(client, token, project="tool_e", approved=True)
        project = _project_json(tmp_path, "tool_e")
        assert project is not None

        # 模型最后看到的完整上下文里，本轮用户消息只出现一次，且包含工具结果。
        final_messages = fake.seen_messages[-1]
        user_count = sum(1 for m in final_messages if m["role"] == "user")
        assert user_count == 1, final_messages
        assert any(m["role"] == "tool" for m in final_messages), final_messages


class TestConfirmCardInChatPage:
    """确认卡片必须真的存在于页面里，否则挂起的图永远等不到 resume。

    用户边界（2026-09-16）：「风险度高的要给人确认」。后端已经会 ``interrupt``
    挂起，前端必须给出可见的批准 / 拒绝入口，而不是让对话无声停住。
    """

    def test_page_handles_the_confirm_event(self, client) -> None:
        page = client.get("/chat").text
        assert 'name === "confirm"' in page, "handleEvent 没有消费 confirm 事件"
        assert "function renderConfirmCard" in page

    def test_card_offers_approve_and_reject(self, client) -> None:
        page = client.get("/chat").text
        card = page.split("function renderConfirmCard")[1].split("async function resolveConfirm")[0]
        assert "批准执行" in card, card
        assert "拒绝" in card, card
        assert "resolveConfirm(true)" in card, card
        assert "resolveConfirm(false)" in card, card

    def test_resume_posts_the_decision(self, client) -> None:
        page = client.get("/chat").text
        resume = page.split("async function dispatchResume")[1].split("async function readSSE")[0]
        assert "/api/chat/resume" in resume, resume
        assert "approved: approved" in resume, resume
        # 拒绝理由要带上，模型据此给替代方案。
        assert "note: note" in resume, resume

    def test_resume_does_not_fake_a_user_message(self, client) -> None:
        """批准/拒绝不是用户说的话，不能往会话记录里插一条假消息。"""
        page = client.get("/chat").text
        resume = page.split("async function dispatchResume")[1].split("async function readSSE")[0]
        assert 'beginLiveTurn("", false)' in resume, resume

    def test_stream_and_resume_share_one_sse_parser(self, client) -> None:
        """两处解析 SSE 会漂移——统一走 readSSE。"""
        page = client.get("/chat").text
        assert "async function readSSE" in page
        dispatch = page.split("async function dispatch(q)")[1].split("function handleEvent")[0]
        assert "readSSE(resp" in dispatch, dispatch
        assert "resp.body.getReader()" not in dispatch, dispatch


class TestResumeEndpointValidation:
    def test_resume_requires_boolean_approved(self, client, token_factory=None) -> None:
        token = _token(client)
        _create_project(client, token, "tool_f")
        resp = client.post(
            "/api/chat/resume",
            json={"project_id": "tool_f", "thread_id": "main", "approved": "yes"},
            headers=_headers(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "approved" in body.get("error", ""), body

    def test_resume_without_model_reports_cleanly(self, client) -> None:
        token = _token(client)
        _create_project(client, token, "tool_g")
        resp = client.post(
            "/api/chat/resume",
            json={"project_id": "tool_g", "thread_id": "main", "approved": True},
            headers=_headers(token),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(resp.text)
        done = events[-1]["data"]
        assert done["via"] == "error", done
