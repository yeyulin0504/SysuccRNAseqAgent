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


def _seed_project(tmp_path: Path, monkeypatch, project_id: str) -> None:
    """Write a real session so the *edit* tools have something to modify."""
    from rnaseq_agent.session import ProjectSession
    from rnaseq_agent.webapp import _default_config

    project_dir = tmp_path / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    config = _default_config(project_dir, {"project_id": project_id, "samples": SAMPLE_WRITE_ARGS["samples"]})
    config["samples"]["items"] = [dict(sample) for sample in SAMPLE_WRITE_ARGS["samples"]]
    config["samples"]["source"] = "remote_path"
    config["samples"]["remote_data_dir"] = "/hwdata/reads"
    config["server"].update({"threads": 8, "memory_gb": 32, "host": "old.example", "user": "old"})
    session = ProjectSession(project_dir)
    session.new_project(config)


class TestConfirmationGrouping:
    """用户定的边界：配置合并成一张卡、执行各自单独一张卡。"""

    def test_write_calls_merge_into_one_card(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "grp_a")
        _seed_project(tmp_path, monkeypatch, "grp_a")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {
                    "tool_calls": [
                        _tool_call("c1", "set_run_resources", {"threads": 16}),
                        _tool_call("c2", "configure_pipeline", {"step": "rsem", "enabled": False}),
                    ]
                },
                {"content": "两处配置都改好了。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "线程改16，关掉 rsem", project="grp_a")
        card = _confirm_event(events)
        assert card is not None, events
        assert card["policy"] == "batch", card
        assert [c["name"] for c in card["calls"]] == ["set_run_resources", "configure_pipeline"]

        # 一次批准，两条配置一起落地。
        _resume(client, token, project="grp_a", approved=True)
        project = _project_json(tmp_path, "grp_a")
        assert project["server"]["threads"] == 16
        assert project["pipeline"]["rsem"]["enabled"] is False

    def test_execute_calls_each_get_their_own_card(self, client, tmp_path, monkeypatch) -> None:
        """执行类不许被批量批准夹带：一张卡只签一个动作。"""
        token = _token(client)
        _create_project(client, token, "grp_b")
        _seed_project(tmp_path, monkeypatch, "grp_b")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {
                    "tool_calls": [
                        _tool_call("c1", "generate_plan", {}),
                        _tool_call("c2", "confirm_contract", {}),
                    ]
                },
                {"content": "计划已生成、契约已冻结。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "生成计划并冻结", project="grp_b")
        card = _confirm_event(events)
        assert card is not None, events
        assert card["policy"] == "solo", card
        assert [c["name"] for c in card["calls"]] == ["generate_plan"], card

        # 批准后还有第二张卡（confirm_contract 顺延到下一组）。
        events2 = _resume(client, token, project="grp_b", approved=True)
        card2 = _confirm_event(events2)
        assert card2 is not None, events2
        assert [c["name"] for c in card2["calls"]] == ["confirm_contract"], card2

    def test_rejection_drops_the_whole_remaining_queue(self, client, tmp_path, monkeypatch) -> None:
        """用户说「不」之后不再弹下一张卡：不逼他对同一批动作重复表态。"""
        token = _token(client)
        _create_project(client, token, "grp_c")
        _seed_project(tmp_path, monkeypatch, "grp_c")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {
                    "tool_calls": [
                        _tool_call("c1", "generate_plan", {}),
                        _tool_call("c2", "confirm_contract", {}),
                    ]
                },
                {"content": "好的，先不执行。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "生成计划并冻结", project="grp_c")
        assert _confirm_event(events) is not None

        events2 = _resume(client, token, project="grp_c", approved=False, note="先看看")
        assert _confirm_event(events2) is None, events2
        # 两项都没有执行：契约没被冻结。
        project = _project_json(tmp_path, "grp_c")
        assert not (tmp_path / "grp_c" / "contract.json").is_file(), "拒绝后仍然冻结了契约"
        assert project is not None


class TestConnectionEditIsAlwaysSolo:
    """用户要求：连接配置「含，但每次必确认」。"""

    def test_connection_card_shows_old_to_new(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "conn_a")
        _seed_project(tmp_path, monkeypatch, "conn_a")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {
                    "tool_calls": [
                        _tool_call("c1", "edit_connection", {"host": "new.example", "threads": 32}),
                    ]
                },
                {"content": "已切到新服务器。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "换到 new.example，32 线程", project="conn_a")
        card = _confirm_event(events)
        assert card is not None, events
        # 连接配置永远是 solo：不许和别的配置合并。
        assert card["policy"] == "solo", card
        description = card["calls"][0]["description"]
        assert "old.example" in description, description
        assert "new.example" in description, description
        assert "→" in description, description
        # 线程数的旧值也要写出来。
        assert "8" in description and "32" in description, description

    def test_connection_edit_is_not_merged_with_other_writes(self, client, tmp_path, monkeypatch) -> None:
        """一轮里同时改连接和别的配置：连接必须独占一张卡。"""
        token = _token(client)
        _create_project(client, token, "conn_b")
        _seed_project(tmp_path, monkeypatch, "conn_b")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {
                    "tool_calls": [
                        _tool_call("c1", "edit_connection", {"host": "new.example"}),
                        _tool_call("c2", "configure_pipeline", {"step": "arriba", "enabled": False}),
                    ]
                },
                {"content": "都改好了。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "换服务器并关掉 arriba", project="conn_b")
        cards = [e["data"] for e in events if e["event"] == "confirm"]
        assert len(cards) == 1, cards
        # 顺序上 batch 组先弹（先落配置），连接（solo）随后单独弹。
        assert cards[0]["policy"] == "batch", cards[0]
        assert [c["name"] for c in cards[0]["calls"]] == ["configure_pipeline"], cards[0]

        events2 = _resume(client, token, project="conn_b", approved=True)
        card2 = _confirm_event(events2)
        assert card2 is not None, events2
        assert card2["policy"] == "solo", card2
        assert [c["name"] for c in card2["calls"]] == ["edit_connection"], card2


class TestRangeValidationBlocksBadValues:
    """schema 里写的 minimum/maximum 必须真的被读（此前的真实漏洞）。"""

    def test_out_of_range_threads_never_reaches_the_card(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "range_a")
        _seed_project(tmp_path, monkeypatch, "range_a")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": -5})]},
                {"content": "线程数不能是负数，我改成 16。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "线程改成 -5", project="range_a")
        assert _confirm_event(events) is None, events
        project = _project_json(tmp_path, "range_a")
        assert project["server"]["threads"] == 8, project["server"]
        # 错误如实回灌给模型。
        tool_content = fake.seen_messages[-1][-1]["content"]
        assert "不能小于" in tool_content, tool_content

    def test_out_of_range_memory_gb_is_rejected(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "range_b")
        _seed_project(tmp_path, monkeypatch, "range_b")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("c1", "set_run_resources", {"memory_gb": 999999})]},
                {"content": "内存上限超了，我调小一点。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "内存给我 999999", project="range_b")
        assert _confirm_event(events) is None, events
        project = _project_json(tmp_path, "range_b")
        assert project["server"]["memory_gb"] == 32, project["server"]


class TestEditSamplesOnExistingSession:
    """已有会话里改样本表——此前模型完全没有这条路。"""

    def test_retarget_one_sample_keeps_the_others(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "edit_a")
        _seed_project(tmp_path, monkeypatch, "edit_a")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {
                    "tool_calls": [
                        _tool_call("c1", "edit_samples", {"samples": [{"sample_id": "S1", "condition": "treat"}]})
                    ]
                },
                {"content": "S1 已经改到 treat 组。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "把 S1 分到 treat", project="edit_a")
        card = _confirm_event(events)
        assert card is not None, events
        # 卡片上要看得见这一条是从哪个组改到哪个组。
        assert "control" in card["calls"][0]["description"], card
        assert "treat" in card["calls"][0]["description"], card

        _resume(client, token, project="edit_a", approved=True)
        project = _project_json(tmp_path, "edit_a")
        conditions = {s["sample_id"]: s["condition"] for s in project["samples"]["items"]}
        assert conditions["S1"] == "treat", conditions
        # 未提到的样本原样保留（没被冲掉）。
        assert conditions["S2"] == "treat", conditions
        assert conditions["S3"] == "control", conditions
        assert conditions["S4"] == "treat", conditions
        assert len(conditions) == 4, conditions

    def test_removing_a_sample_shrinks_the_table(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "edit_b")
        _seed_project(tmp_path, monkeypatch, "edit_b")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("c1", "edit_samples", {"remove": ["S4"]})]},
                {"content": "S4 已删除。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "去掉 S4", project="edit_b")
        assert _confirm_event(events) is not None, events
        _resume(client, token, project="edit_b", approved=True)
        project = _project_json(tmp_path, "edit_b")
        ids = [s["sample_id"] for s in project["samples"]["items"]]
        assert ids == ["S1", "S2", "S3"], ids


class TestEditReference:
    def test_gtf_change_lands_in_the_reference_block(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "ref_a")
        _seed_project(tmp_path, monkeypatch, "ref_a")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {
                    "tool_calls": [
                        _tool_call("c1", "edit_reference", {"gtf": "/ref/gencode.v99.gtf"})
                    ]
                },
                {"content": "GTF 已换成 v99。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "GTF 换成 /ref/gencode.v99.gtf", project="ref_a")
        card = _confirm_event(events)
        assert card is not None, events
        # 模型用的短参数名 gtf，卡片上要显示成 config 里那一位的旧值 → 新值。
        assert "/ref/gencode.v99.gtf" in card["calls"][0]["description"], card

        _resume(client, token, project="ref_a", approved=True)
        project = _project_json(tmp_path, "ref_a")
        assert project["reference"]["remote_gtf_path"] == "/ref/gencode.v99.gtf", project["reference"]

    def test_relative_path_is_rejected_before_the_card(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "ref_b")
        _seed_project(tmp_path, monkeypatch, "ref_b")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("c1", "edit_reference", {"gtf": "ref/local.gtf"})]},
                {"content": "路径要写绝对路径，我改一下。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "GTF 换成 ref/local.gtf", project="ref_b")
        assert _confirm_event(events) is None, events
        project = _project_json(tmp_path, "ref_b")
        assert project["reference"]["remote_gtf_path"] != "ref/local.gtf"


class TestReadOnlyStatusTools:
    def test_refresh_status_runs_without_a_card(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "ro_a")
        _seed_project(tmp_path, monkeypatch, "ro_a")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("c1", "refresh_project_status", {})]},
                {"content": "当前还没有开始跑。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "跑到哪了", project="ro_a")
        assert _confirm_event(events) is None, events
        assert fake.seen_messages[-1][-1]["role"] == "tool", fake.seen_messages[-1]

    def test_get_project_report_runs_without_a_card(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "ro_b")
        _seed_project(tmp_path, monkeypatch, "ro_b")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("c1", "get_project_report", {})]},
                {"content": "报告已经生成好了。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "给我看看报告", project="ro_b")
        assert _confirm_event(events) is None, events
        tool_content = fake.seen_messages[-1][-1]["content"]
        assert "report" in tool_content.lower(), tool_content


class TestStageAwareRunAnalysis:
    def test_stage_shows_up_on_the_card(self, client, tmp_path, monkeypatch) -> None:
        """分阶段执行的卡片必须写明只跑哪一段，不能只说「启动分析」。"""
        token = _token(client)
        _create_project(client, token, "stage_a")
        _seed_project(tmp_path, monkeypatch, "stage_a")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("c1", "run_analysis", {"stage": "de"})]},
                {"content": "好的，先只跑差异表达。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "只跑差异表达", project="stage_a")
        card = _confirm_event(events)
        assert card is not None, events
        assert card["policy"] == "solo", card
        description = card["calls"][0]["description"]
        assert "de" in description, description

    def test_unknown_stage_is_rejected_without_a_card(self, client, tmp_path, monkeypatch) -> None:
        token = _token(client)
        _create_project(client, token, "stage_b")
        _seed_project(tmp_path, monkeypatch, "stage_b")
        _configure_llm(client, token)
        fake = FakeLLM(
            [
                {"tool_calls": [_tool_call("c1", "run_analysis", {"stage": "nope"})]},
                {"content": "没有这个阶段，可选的是 qc/quant/de/cms/counts。"},
            ]
        )
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        events = _stream(client, token, "跑 nope 阶段", project="stage_b")
        assert _confirm_event(events) is None, events
        tool_content = fake.seen_messages[-1][-1]["content"]
        assert "qc" in tool_content, tool_content

