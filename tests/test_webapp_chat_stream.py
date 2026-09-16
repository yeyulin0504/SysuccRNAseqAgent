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


class TestPlanWithoutProjectIsActionable:
    """没有 project.json 的项目里问「生成执行计划」，要给下一步而不是裸错误。

    用户实测（2026-09-15）：AI 先谎称「配置已保存 ✓」，随后「生成执行计划」
    返回「操作未完成：当前状态 idle 不允许该操作；允许的状态：
    drafting/planned/confirmed」。根因是项目根本没有分析会话，
    ``session.config is None`` 使状态恒为 idle。
    """

    def test_stream_reply_is_not_a_bare_state_error(self, client) -> None:
        token = _token(client)
        # 只注册项目，不建分析会话（等价于用户当时的状态）。
        _create_project(client, token, "no_session")

        events = _stream(client, token, "生成执行计划", project="no_session")
        done = events[-1]["data"]

        assert done["state"] == "idle"
        reply = done["reply"]
        assert "不允许该操作" not in reply, reply
        assert "当前状态" not in reply, reply
        # 必须指向下一步。
        assert "工作台" in reply, reply
        assert "project.json" in reply, reply

    def test_state_endpoint_confirms_no_config(self, client) -> None:
        """守住前提：这个场景确实是缺 project.json，而不是判定逻辑出错。"""
        token = _token(client)
        _create_project(client, token, "no_session2")
        state = client.get("/api/state?project=no_session2", headers=_headers(token)).json()
        assert state["state"] == "idle"
        assert state["config"] is None

    def test_project_with_session_still_plans(self, client) -> None:
        """守卫不能误伤有配置的项目。"""
        token = _token(client)
        _create_project(client, token, "has_session")
        _with_session(client, token, "has_session")

        events = _stream(client, token, "生成执行计划", project="has_session")
        assert events[-1]["data"]["state"] == "planned"


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


class TestChatPageScrollsToLatest:
    """发消息后要停在最新结果，而不是弹回顶部。

    用户实测（2026-09-15）：「发送对话后这个界面自动滚到最顶部了，而不是
    展示下面的对话结果」。根因是 ``.stream`` 作为 flex 子项没有
    ``min-height: 0``，被内容撑高后自身不滚动，``scrollTop`` 写它无效；
    再加上内容刚 append 时高度未结算，同步赋值会落在旧高度上。
    """

    def test_stream_is_a_bounded_scroll_container(self, client) -> None:
        page = client.get("/chat").text
        # flex 子项默认 min-height:auto，必须显式归零才能成为滚动容器。
        assert re.search(r"\.main\s*\{[^}]*min-height:\s*0", page), "`.main` 缺少 min-height:0"
        assert re.search(r"\.stream\s*\{[^}]*min-height:\s*0", page), "`.stream` 缺少 min-height:0"
        assert re.search(r"\.stream\s*\{[^}]*overflow-y:\s*auto", page), "`.stream` 未成为滚动容器"

    def test_scroll_to_bottom_waits_for_layout(self, client) -> None:
        page = client.get("/chat").text
        # 内容 append 后高度未结算，需在下一帧再滚一次。
        assert "function scrollToBottom()" in page
        assert "requestAnimationFrame" in page, "滚动未等待布局完成"
        assert "el.scrollTop = el.scrollHeight" in page

    def test_manual_scroll_up_is_not_interrupted(self, client) -> None:
        """用户往上翻时不能被流式输出拽回底部。"""
        page = client.get("/chat").text
        assert "function isNearBottom" in page
        # appendDelta 必须条件跟随，而不是无条件滚动。
        delta = page.split("function appendDelta(text)")[1].split("function ")[0]
        assert "isNearBottom()" in delta, "appendDelta 未做「贴近底部才跟随」判断"


class TestComposerSendsOnEnter:
    """Enter 发送、Shift+Enter 换行。

    用户实测（2026-09-15）：「点击键盘 enter 并不能发送，只能点击发送按钮」。
    ``workbench.html`` 里根本没有为 ``#chatInput`` 绑过 ``keydown``——用户当时
    就在工作台对话；``chat.html`` 虽绑了，但直接绑到元素上，脚本顺序一变就
    整体中断。两个页面统一改成 document 级委托。
    """

    @pytest.mark.parametrize("path", ["/chat", "/workbench"])
    def test_composer_binds_enter_via_delegation(self, client, path: str) -> None:
        page = client.get(path).text
        assert 'document.addEventListener("keydown"' in page, f"{path} 没有键盘监听"
        handler = page.split('document.addEventListener("keydown"')[1].split("});")[0]
        assert "chatInput" in handler, f"{path} 的键盘监听未覆盖 #chatInput"
        assert 'e.key === "Enter"' in handler, f"{path} 未处理 Enter"

    @pytest.mark.parametrize("path", ["/chat", "/workbench"])
    def test_shift_enter_and_ime_are_respected(self, client, path: str) -> None:
        page = client.get(path).text
        handler = page.split('document.addEventListener("keydown"')[1].split("});")[0]
        # Shift+Enter 换行：不得在按下 Shift 时发送。
        assert "!e.shiftKey" in handler, f"{path} 会吞掉 Shift+Enter 的换行"
        # 中文输入法组合态按 Enter 是选词，不能当发送。
        assert "e.isComposing" in handler, f"{path} 未排除输入法组合态"

    @pytest.mark.parametrize(
        "path,submit",
        [("/chat", "sendMessage()"), ("/workbench", "sendChat()")],
    )
    def test_enter_calls_the_page_submit_function(self, client, path: str, submit: str) -> None:
        page = client.get(path).text
        handler = page.split('document.addEventListener("keydown"')[1].split("});")[0]
        assert submit in handler, f"{path} 的 Enter 未调用 {submit}"


class TestChatWritesConfiguration:
    """对话落地配置：用户说「你帮我执行」必须真的写盘。

    用户实测（2026-09-16）：把 FASTQ 目录 / 文件名 / 参考基因组贴进对话，再说
    「你帮我执行，链特异性未知，1是control2是treat」，得到的是
    「我无法直接执行，只能帮你核对配置」；再点「生成执行计划」，又回到
    「这个项目还没有分析会话」。用户在「AI 说做不到 ↔ 不知道该点哪」之间死循环。

    根因：写盘能力本身存在（``POST /api/projects/{id}/fastq/session``），只是
    对话够不着——``route_intent`` 没有任何能写配置的动作。这里补上。
    """

    CONFIG_MESSAGE = (
        "你帮我填这些信息：原始 FASTQ 数据\n"
        "/hwdata/home/yeyulin/SysuccRNAseqAgent-master/runs/fastq_pair_test/fastq\n"
        "包含 8 个文件（4 个样本的双端测序）：\n"
        "SRR28119110_1.fastq.gz, SRR28119110_2.fastq.gz\n"
        "SRR28119111_1.fastq.gz, SRR28119111_2.fastq.gz\n"
        "SRR28119112_1.fastq.gz, SRR28119112_2.fastq.gz\n"
        "SRR28119113_1.fastq.gz, SRR28119113_2.fastq.gz\n"
        "参考基因组和注释文件\n"
        "GTF 注释：\n"
        "/hwdata/home/yeyulin/project/GSE259357_mm10_analysis/references/"
        "gencode_m25_grcm38p6/gencode.vM25.annotation.gtf\n"
        "基因组 FASTA：\n"
        "/hwdata/home/yeyulin/project/GSE259357_mm10_analysis/references/"
        "gencode_m25_grcm38p6/GRCm38.primary_assembly.genome.fa\n"
        "STAR 索引目录：\n"
        "/hwdata/home/yeyulin/project/GSE259357_mm10_analysis/references/"
        "gencode_m25_grcm38p6/star_index\n"
    )

    EXECUTE_MESSAGE = "你帮我执行，链特异性未知，1是control2是treat"

    def _send_config_then_execute(self, client, token: str, project: str) -> list[dict]:
        """两条消息模拟真实对话：先贴配置，再说「你帮我执行」。"""
        _stream(client, token, self.CONFIG_MESSAGE, project=project)
        return _stream(client, token, self.EXECUTE_MESSAGE, project=project)

    def test_execute_creates_a_real_session(self, client, tmp_path) -> None:
        token = _token(client)
        _create_project(client, token, "cfg_a")
        self._send_config_then_execute(client, token, "cfg_a")

        project_dir = tmp_path / "cfg_a"
        assert (project_dir / "project.json").is_file(), "对话没有真正写盘"

    def test_execute_populates_all_four_samples(self, client, tmp_path) -> None:
        token = _token(client)
        _create_project(client, token, "cfg_b")
        self._send_config_then_execute(client, token, "cfg_b")

        project_json = json.loads(
            (tmp_path / "cfg_b" / "project.json").read_text(encoding="utf-8")
        )
        items = project_json["samples"]["items"]
        assert [item["sample_id"] for item in items] == [
            "SRR28119110", "SRR28119111", "SRR28119112", "SRR28119113",
        ]

    def test_two_group_names_cycle_over_four_samples(self, client, tmp_path) -> None:
        """用户原话：4 个样本、2 个分组名，按 ctrl/treat 顺序排列，不要反问。"""
        token = _token(client)
        _create_project(client, token, "cfg_c")
        self._send_config_then_execute(client, token, "cfg_c")

        project_json = json.loads(
            (tmp_path / "cfg_c" / "project.json").read_text(encoding="utf-8")
        )
        assert [item["condition"] for item in project_json["samples"]["items"]] == [
            "control", "treat", "control", "treat",
        ]

    def test_unknown_strandedness_is_persisted(self, client, tmp_path) -> None:
        """「要允许特异性未知的选项先写着」——不能静默被 auto 覆盖。"""
        token = _token(client)
        _create_project(client, token, "cfg_d")
        self._send_config_then_execute(client, token, "cfg_d")

        project_json = json.loads(
            (tmp_path / "cfg_d" / "project.json").read_text(encoding="utf-8")
        )
        assert project_json["sequencing"]["strandedness"] == "unknown"

    def test_reply_reports_the_real_write(self, client, tmp_path) -> None:
        token = _token(client)
        _create_project(client, token, "cfg_e")
        events = self._send_config_then_execute(client, token, "cfg_e")

        reply = _deltas(events)
        assert "已写入" in reply, reply
        assert "SRR28119110" in reply, reply
        # 不能再出现「我无法直接执行」这类把活推回给用户的措辞。
        assert "无法直接执行" not in reply, reply

    def test_reply_does_not_claim_a_write_that_did_not_happen(self, client, tmp_path) -> None:
        """信息不足时必须如实说缺什么，不得谎称已保存。"""
        token = _token(client)
        _create_project(client, token, "cfg_f")
        events = _stream(client, token, "你帮我执行", project="cfg_f")

        reply = _deltas(events)
        assert "已写入" not in reply, reply
        assert not (tmp_path / "cfg_f" / "project.json").is_file()

    def test_existing_session_is_not_overwritten(self, client, tmp_path) -> None:
        """已有会话时不得把用户确认过的样本设计冲掉。"""
        token = _token(client)
        _create_project(client, token, "cfg_g")
        _with_session(client, token, "cfg_g")
        before = (tmp_path / "cfg_g" / "project.json").read_text(encoding="utf-8")

        events = self._send_config_then_execute(client, token, "cfg_g")

        after = (tmp_path / "cfg_g" / "project.json").read_text(encoding="utf-8")
        assert after == before, "对话写入覆盖了已有会话"
        assert "已经有分析会话" in _deltas(events)

    def test_plan_after_chat_write_succeeds(self, client, tmp_path) -> None:
        """死循环的收尾：写盘之后「生成执行计划」不能再回「还没有分析会话」。"""
        token = _token(client)
        _create_project(client, token, "cfg_h")
        self._send_config_then_execute(client, token, "cfg_h")

        events = _stream(client, token, "生成执行计划", project="cfg_h")
        reply = _deltas(events)

        assert "还没有分析会话" not in reply, reply
        assert any(e["event"] == "done" and e["data"].get("steps") for e in events), events

    def test_stream_reports_the_write_as_a_tool_step(self, client) -> None:
        """思考过程里要能看到「读取对话中的配置 → 写入项目配置」。"""
        token = _token(client)
        _create_project(client, token, "cfg_i")
        events = self._send_config_then_execute(client, token, "cfg_i")

        labels = [e["data"]["label"] for e in events if e["event"] == "step"]
        assert any("写入项目配置" in label for label in labels), labels
        write_steps = [
            e["data"]
            for e in events
            if e["event"] == "step" and "写入项目配置" in e["data"]["label"]
        ]
        assert write_steps[-1]["status"] == "done", write_steps

    def test_done_event_carries_the_new_state(self, client) -> None:
        """写盘后状态要从 idle 变成 drafting，前端才跟得上。"""
        token = _token(client)
        _create_project(client, token, "cfg_j")
        events = self._send_config_then_execute(client, token, "cfg_j")

        done = events[-1]["data"]
        assert done["state"] == "drafting", done
        assert done["via"] == "tool", done

    def test_non_streaming_endpoint_writes_too(self, client, tmp_path) -> None:
        """``/api/chat``（工作台走的非流式端点）同样能落地配置。

        该端点不落盘会话消息，跨轮上下文拿不到，因此这里发**一条自包含**的
        消息（配置 + 执行指令一起给），验证它同样走确定性写盘。
        """
        token = _token(client)
        _create_project(client, token, "cfg_k")
        body = client.post(
            "/api/chat?project=cfg_k",
            json={"message": self.CONFIG_MESSAGE + "\n" + self.EXECUTE_MESSAGE},
            headers=_headers(token),
        ).json()

        assert "已写入" in body.get("reply", ""), body
        project_json = json.loads(
            (tmp_path / "cfg_k" / "project.json").read_text(encoding="utf-8")
        )
        assert [item["condition"] for item in project_json["samples"]["items"]] == [
            "control", "treat", "control", "treat",
        ]
        assert project_json["sequencing"]["strandedness"] == "unknown"

    def test_remote_reads_are_not_reported_as_missing(self, client, tmp_path) -> None:
        """写盘回复不得夹带虚假「缺少输入文件」。

        用户贴的是服务器目录，reads 本来就不在本地。早期实现拿本地
        ``local_data_dir`` 去拼样本文件名，于是每个样本都被报成缺少文件，
        把「已写入」这条好消息淹没了（2026-09-16 实测）。
        """
        token = _token(client)
        _create_project(client, token, "cfg_m")
        events = self._send_config_then_execute(client, token, "cfg_m")

        reply = _deltas(events)
        assert "缺少输入文件" not in reply, reply
        assert "mvp_demo_data" not in reply, reply

        done = events[-1]["data"]
        assert done["state"] == "drafting", done
        # 门禁应给出「通过」，而不是一屏 NOT_EVALUABLE 的「不适用」。
        gate = done.get("gate") or []
        assert gate, done
        assert "通过" in " ".join(gate), gate
        assert "不适用" not in " ".join(gate), gate


class TestConfigureIntentDoesNotStealOtherActions:
    """「落地配置」的识别必须精确，不能把别的动作抢走。"""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("帮我执行差异表达", "edit"),
            ("帮我跑差异表达，以control为对照", "edit"),
            ("把线程改成 16", "edit"),
            ("生成执行计划", "plan"),
            ("解释一下差异表达的原理", None),
            ("帮我做单细胞聚类", None),
        ],
    )
    def test_other_intents_are_untouched(self, text: str, expected) -> None:
        from rnaseq_agent.webchat import route_intent

        intent = route_intent(text)
        assert (intent.action if intent else None) == expected

    @pytest.mark.parametrize(
        "text",
        ["你帮我执行", "帮我执行", "帮我填这些信息", "帮我把这些路径保存下来", "把这些信息写入项目"],
    )
    def test_execution_phrases_route_to_configure(self, text: str) -> None:
        from rnaseq_agent.webchat import route_intent

        intent = route_intent(text)
        assert intent is not None and intent.action == "configure_project"
