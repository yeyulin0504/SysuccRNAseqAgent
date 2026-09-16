"""对话控制平面图的测试：工具循环、风险守卫、interrupt/resume。

用户诉求（2026-09-16）：

> 我要修复的是大语言模型应该要有自己的执行工具啊

这套测试锁的是「模型真的能调工具」这件事，以及「高风险操作真的会拦」。

LLM 全部 fake（不联网）：本项目的图设计要求 LLM 调用与图逻辑分离，所以可以在
没有 API key 的情况下测完整条链路。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rnaseq_agent import chat_graph as cg
from rnaseq_agent.agent_tools import (
    POLICY_BATCH,
    POLICY_NEVER,
    POLICY_SOLO,
    RISK_EXECUTE,
    RISK_READ,
    RISK_WRITE,
    TOOL_SPECS,
    ToolCallAccumulator,
    ToolSpec,
    confirmation_policy,
    describe_call,
    normalize_write_arguments,
    parse_message_tool_calls,
    requires_confirmation,
    risk_of,
    split_calls_for_round,
    tool_schemas,
    validate_call,
)

langgraph = pytest.importorskip("langgraph")

SAMPLE_WRITE_ARGS = {
    "data_source": "remote_path",
    "fastq_dir": "/hwdata/reads",
    "samples": [
        {"sample_id": "S1", "condition": "control", "fastq_1": "S1_1.fastq.gz", "fastq_2": "S1_2.fastq.gz"},
        {"sample_id": "S2", "condition": "treat", "fastq_1": "S2_1.fastq.gz", "fastq_2": "S2_2.fastq.gz"},
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

    记录收到的 messages，便于断言「工具结果确实回灌给了模型」。
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


def _graph(monkeypatch, turns, *, executor=None, calls=None, max_iterations=8, checkpointer=None):
    """Build the chat graph with a scripted model and a recording executor.

    ``checkpointer`` 在需要 ``Command(resume=...)`` 的场景必须给：没有 checkpointer
    时 LangGraph 无法跨调用保存挂起状态，会直接抛
    ``Cannot use Command(resume=...) without checkpointer``。
    """
    fake = FakeLLM(turns)
    monkeypatch.setattr(cg, "_stream_chat_completion", fake)

    recorded = calls if calls is not None else []

    def default_executor(name, arguments, project_dir, approved):
        recorded.append(
            {"name": name, "arguments": arguments, "approved": approved, "dir": str(project_dir)}
        )
        return {"ok": True, "reply": f"已执行 {name}"}

    graph = cg.build_chat_graph(
        executor=executor or default_executor,
        llm_config={"llm": {"enabled": True, "api_base": "http://x", "api_key": "k", "model": "m"}},
        checkpointer=checkpointer,
        max_iterations=max_iterations,
    )
    return graph, recorded, fake


def _memory_checkpointer():
    """In-memory checkpointer for tests that need interrupt/resume."""
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()


def _initial(project_dir: Path, text: str) -> dict:
    return {
        "project_dir": str(project_dir),
        "project_id": "p1",
        "thread_id": "t1",
        "messages": [{"role": "user", "content": text}],
    }


def _assert_all_declared_tool_calls_are_answered(messages: list[dict]) -> None:
    """OpenAI protocol invariant: every declared call must receive one tool reply."""
    declared = {
        str(call.get("id") or "")
        for message in messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    }
    answered = {
        str(message.get("tool_call_id") or "")
        for message in messages
        if message.get("role") == "tool"
    }
    assert declared == answered


# -- 工具契约 ---------------------------------------------------------------


class TestToolContract:
    def test_every_tool_requiring_confirmation_is_write_or_execute(self) -> None:
        """守卫的唯一真源：写盘与执行必须确认，只读必须放行。"""
        for name, spec in TOOL_SPECS.items():
            if spec.risk == RISK_READ:
                assert not requires_confirmation(name), name
            else:
                assert requires_confirmation(name), name

    def test_unknown_tool_is_treated_as_the_most_dangerous(self) -> None:
        """模型幻觉出来的工具名不能悄悄放行。"""
        assert risk_of("invent_a_tool") == RISK_EXECUTE
        assert requires_confirmation("invent_a_tool") is True

    def test_schemas_are_valid_openai_function_definitions(self) -> None:
        for schema in tool_schemas():
            assert schema["type"] == "function"
            function = schema["function"]
            assert function["name"]
            assert function["description"]
            assert function["parameters"]["type"] == "object"

    def test_unknown_strandedness_is_a_legal_value(self) -> None:
        """用户要求「允许特异性未知的选项先写着」。"""
        schema = next(s for s in tool_schemas() if s["function"]["name"] == "write_project_config")
        enum = schema["function"]["parameters"]["properties"]["strandedness"]["enum"]
        assert "unknown" in enum

    def test_connection_tool_never_exposes_credentials_or_auth_controls(self) -> None:
        schema = next(s for s in tool_schemas() if s["function"]["name"] == "edit_connection")
        properties = schema["function"]["parameters"]["properties"]
        assert {"password", "private_key", "api_key", "auth_mode", "shell"}.isdisjoint(
            properties
        )


class TestConfirmationPolicy:
    """用户 2026-09-16 定的确认边界：配置合并、执行单独。"""

    def test_read_is_auto_and_write_execute_need_a_card(self) -> None:
        for name, spec in TOOL_SPECS.items():
            if spec.risk == RISK_READ:
                assert confirmation_policy(name) == POLICY_NEVER, name
            elif spec.risk == RISK_WRITE:
                # 允许 spec 用 policy 字段钉成更严的 solo（见 edit_connection：
                # 用户要求连接配置「每次必确认」，不许被合并卡片夹带）。
                assert confirmation_policy(name) == (spec.policy or POLICY_BATCH), name
            else:
                assert confirmation_policy(name) == POLICY_SOLO, name

    def test_a_spec_may_override_the_default_policy(self) -> None:
        """连接改动比改样本表更重，允许用 policy 字段钉成 solo。"""
        spec = ToolSpec(
            name="t", label="t", description="t", parameters={}, risk=RISK_WRITE, policy=POLICY_SOLO
        )
        TOOL_SPECS[spec.name] = spec
        try:
            assert confirmation_policy("t") == POLICY_SOLO
        finally:
            del TOOL_SPECS[spec.name]

    def test_unknown_tool_defaults_to_solo(self) -> None:
        """幻觉工具名不能跟着别的调用被顺手批准。"""
        assert confirmation_policy("invent_a_tool") == POLICY_SOLO

    def test_lone_read_call_is_grouped_without_a_card(self) -> None:
        batches = split_calls_for_round([{"call_id": "1", "name": "read_project_state"}])
        assert len(batches) == 1
        assert batches[0].policy == POLICY_NEVER
        assert [c["call_id"] for c in batches[0].calls] == ["1"]

    def test_writes_merge_into_one_card(self) -> None:
        calls = [
            {"call_id": "1", "name": "write_project_config"},
            {"call_id": "2", "name": "set_run_resources"},
            {"call_id": "3", "name": "configure_pipeline"},
        ]
        batches = split_calls_for_round(calls)
        assert [b.policy for b in batches] == [POLICY_BATCH]
        assert len(batches[0].calls) == 3

    def test_each_execute_call_gets_its_own_card(self) -> None:
        calls = [
            {"call_id": "1", "name": "generate_plan"},
            {"call_id": "2", "name": "run_analysis"},
        ]
        batches = split_calls_for_round(calls)
        assert [b.policy for b in batches] == [POLICY_SOLO, POLICY_SOLO]
        assert [c["call_id"] for c in batches[0].calls] == ["1"]
        assert [c["call_id"] for c in batches[1].calls] == ["2"]

    def test_a_mixed_turn_is_ordered_read_then_write_then_execute(self) -> None:
        """一轮里三种都出现时，卡片顺序要读得出「先落配置、再动执行」。"""
        calls = [
            {"call_id": "exec", "name": "run_analysis"},
            {"call_id": "write", "name": "write_project_config"},
            {"call_id": "read", "name": "read_project_state"},
        ]
        batches = split_calls_for_round(calls)
        assert [b.policy for b in batches] == [POLICY_NEVER, POLICY_BATCH, POLICY_SOLO]
        assert [c["call_id"] for b in batches for c in b.calls] == ["read", "write", "exec"]

    def test_empty_round_yields_no_batches(self) -> None:
        assert split_calls_for_round([]) == []


class TestValidateCall:
    def test_accepts_a_well_formed_write(self) -> None:
        assert validate_call("write_project_config", SAMPLE_WRITE_ARGS) == []

    def test_rejects_relative_remote_dir(self) -> None:
        args = {**SAMPLE_WRITE_ARGS, "fastq_dir": "reads/fastq"}
        problems = validate_call("write_project_config", args)
        assert any("绝对路径" in item for item in problems), problems

    def test_rejects_a_directory_in_the_filename_field(self) -> None:
        """模型常把完整路径塞进 fastq_1；必须挡住并告诉它只写文件名。"""
        args = {
            **SAMPLE_WRITE_ARGS,
            "samples": [
                {
                    "sample_id": "S1",
                    "condition": "control",
                    "fastq_1": "/hwdata/reads/S1_1.fastq.gz",
                }
            ],
        }
        problems = validate_call("write_project_config", args)
        assert any("只写文件名" in item for item in problems), problems

    def test_rejects_duplicate_sample_ids(self) -> None:
        args = {
            **SAMPLE_WRITE_ARGS,
            "samples": [
                {"sample_id": "S1", "condition": "control", "fastq_1": "a_1.fq.gz"},
                {"sample_id": "S1", "condition": "treat", "fastq_1": "b_1.fq.gz"},
            ],
        }
        problems = validate_call("write_project_config", args)
        assert any("重复" in item for item in problems), problems

    def test_rejects_unknown_arguments(self) -> None:
        problems = validate_call("read_project_state", {"sneaky": 1})
        assert any("不接受参数" in item for item in problems), problems

    def test_rejects_unknown_pipeline_step(self) -> None:
        """enum 由 schema 驱动校验，措辞里带上合法取值方便模型自纠。"""
        problems = validate_call("configure_pipeline", {"step": "bwa", "enabled": True})
        assert any("fastp" in item and "bwa" in item for item in problems), problems

    def test_rejects_unknown_tool_name(self) -> None:
        problems = validate_call("rm_rf", {})
        assert any("不存在" in item for item in problems), problems


class TestNormalizeWriteArguments:
    def test_cycles_partial_grouping(self) -> None:
        """用户原话场景：4 个样本、2 个分组名 → 循环展开。"""
        args = {
            "samples": [
                {"sample_id": "S1", "condition": "control", "fastq_1": "1.fq.gz"},
                {"sample_id": "S2", "condition": "treat", "fastq_1": "2.fq.gz"},
                {"sample_id": "S3", "condition": "", "fastq_1": "3.fq.gz"},
                {"sample_id": "S4", "condition": "", "fastq_1": "4.fq.gz"},
            ]
        }
        result = normalize_write_arguments(args)
        assert [s["condition"] for s in result["samples"]] == [
            "control", "treat", "control", "treat",
        ]

    def test_leaves_a_complete_grouping_alone(self) -> None:
        args = {
            "samples": [
                {"sample_id": "S1", "condition": "a", "fastq_1": "1.fq.gz"},
                {"sample_id": "S2", "condition": "b", "fastq_1": "2.fq.gz"},
            ]
        }
        assert normalize_write_arguments(args) == args

    def test_does_not_invent_conditions_from_nothing(self) -> None:
        """一个分组名都没给时不许猜——让门禁如实报缺字段。"""
        args = {"samples": [{"sample_id": "S1", "condition": "", "fastq_1": "1.fq.gz"}]}
        result = normalize_write_arguments(args)
        assert result["samples"][0]["condition"] == ""


class TestToolCallParsing:
    def test_parses_a_non_streaming_message(self) -> None:
        message = {"tool_calls": [_tool_call("c1", "read_project_state", {})]}
        calls = parse_message_tool_calls(message)
        assert len(calls) == 1
        assert calls[0].name == "read_project_state"
        assert calls[0].call_id == "c1"

    def test_reports_broken_json_instead_of_crashing(self) -> None:
        message = {
            "tool_calls": [
                {"id": "c1", "function": {"name": "read_project_state", "arguments": "{oops"}}
            ]
        }
        calls = parse_message_tool_calls(message)
        assert calls[0].parse_error
        assert calls[0].arguments == {}

    def test_reassembles_arguments_split_across_deltas(self) -> None:
        """流式协议会把一个 JSON 切成几十段，必须增量拼接。"""
        accumulator = ToolCallAccumulator()
        accumulator.add_delta([{"index": 0, "id": "c1", "function": {"name": "write_"}}])
        accumulator.add_delta([{"index": 0, "function": {"name": "project_config"}}])
        accumulator.add_delta([{"index": 0, "function": {"arguments": '{"data_source":'}}])
        accumulator.add_delta([{"index": 0, "function": {"arguments": '"remote_path"}'}}])

        calls = accumulator.finish()
        assert len(calls) == 1
        assert calls[0].name == "write_project_config"
        assert calls[0].arguments == {"data_source": "remote_path"}

    def test_keeps_parallel_calls_separate(self) -> None:
        accumulator = ToolCallAccumulator()
        accumulator.add_delta(
            [
                {"index": 0, "id": "a", "function": {"name": "read_project_state", "arguments": "{}"}},
                {"index": 1, "id": "b", "function": {"name": "rollback_changes", "arguments": "{}"}},
            ]
        )
        calls = accumulator.finish()
        assert [c.name for c in calls] == ["read_project_state", "rollback_changes"]


class TestDescribeCall:
    def test_write_card_lists_every_sample_and_condition(self) -> None:
        card = describe_call("write_project_config", SAMPLE_WRITE_ARGS)
        assert "S1" in card and "control" in card
        assert "S2" in card and "treat" in card
        assert "unknown" in card

    def test_pipeline_card_states_the_verb(self) -> None:
        assert "关闭" in describe_call("configure_pipeline", {"step": "rsem", "enabled": False})
        assert "启用" in describe_call("configure_pipeline", {"step": "rsem", "enabled": True})


# -- 图：工具循环 -----------------------------------------------------------


class TestChatGraphToolLoop:
    def test_read_only_tool_runs_without_confirmation(self, monkeypatch, tmp_path) -> None:
        graph, recorded, _ = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "read_project_state", {})]},
                {"content": "项目现在是 drafting 状态。"},
            ],
        )
        result = graph.invoke(_initial(tmp_path, "现在什么情况"), config={"configurable": {"thread_id": "x"}})

        assert [item["name"] for item in recorded] == ["read_project_state"]
        assert recorded[0]["approved"] is False, "只读工具不该被标记为已确认"
        assert result["reply"] == "项目现在是 drafting 状态。"

    def test_tool_result_is_fed_back_to_the_model(self, monkeypatch, tmp_path) -> None:
        """工具结果必须回灌，否则模型下一轮还在猜。"""
        graph, _, fake = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "read_project_state", {})]},
                {"content": "好的。"},
            ],
        )
        graph.invoke(_initial(tmp_path, "状态"), config={"configurable": {"thread_id": "x"}})

        second_turn = fake.seen_messages[1]
        tool_messages = [m for m in second_turn if m.get("role") == "tool"]
        assert tool_messages, second_turn
        assert tool_messages[0]["tool_call_id"] == "c1"

    def test_invalid_arguments_go_back_to_the_model_for_correction(
        self, monkeypatch, tmp_path
    ) -> None:
        """参数不合法时不执行，把原因告诉模型让它自己改。"""
        graph, recorded, fake = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "browse_remote_samples", {"path": "relative"})]},
                {"content": "抱歉，路径需要是绝对路径。"},
            ],
        )
        graph.invoke(_initial(tmp_path, "看看"), config={"configurable": {"thread_id": "x"}})

        assert recorded == [], "非法参数不该被执行"
        second_turn = fake.seen_messages[1]
        tool_messages = [m for m in second_turn if m.get("role") == "tool"]
        assert "绝对路径" in tool_messages[0]["content"]

    def test_multiple_tool_rounds_are_allowed(self, monkeypatch, tmp_path) -> None:
        graph, recorded, _ = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "read_project_state", {})]},
                {"tool_calls": [_tool_call("c2", "browse_remote_samples", {"path": "/data"})]},
                {"content": "都看完了。"},
            ],
        )
        graph.invoke(_initial(tmp_path, "看看"), config={"configurable": {"thread_id": "x"}})
        assert [item["name"] for item in recorded] == [
            "read_project_state", "browse_remote_samples",
        ]

    def test_runaway_loop_is_capped(self, monkeypatch, tmp_path) -> None:
        """模型反复调工具时必须停下，不能无限烧 token。"""
        turns = [{"tool_calls": [_tool_call(f"c{i}", "read_project_state", {})]} for i in range(20)]
        graph, recorded, _ = _graph(monkeypatch, turns, max_iterations=3)
        result = graph.invoke(_initial(tmp_path, "循环"), config={"configurable": {"thread_id": "x"}})

        assert len(recorded) == 3
        assert "太多次" in result["reply"]


# -- 图：人工确认 -----------------------------------------------------------


class TestChatGraphConfirmation:
    """写盘/执行前必须人工确认。

    这些用例都用 checkpointer——生产中对话图固定挂 SqliteSaver，且
    ``Command(resume=...)`` 没有 checkpointer 会直接报错。
    """

    def test_write_pauses_for_confirmation_and_does_not_execute(self, monkeypatch, tmp_path) -> None:
        """写盘必须挂起等人确认——这是「风险度高的要给人确认」。"""
        graph, recorded, _ = _graph(
            monkeypatch,
            [{"tool_calls": [_tool_call("c1", "write_project_config", SAMPLE_WRITE_ARGS)]}],
            checkpointer=_memory_checkpointer(),
        )
        result = graph.invoke(
            _initial(tmp_path, "你帮我执行"), config={"configurable": {"thread_id": "approve"}}
        )

        assert recorded == [], "确认之前不得写盘"
        interrupts = result.get("__interrupt__")
        assert interrupts, result
        payload = interrupts[0].value
        assert payload["type"] == "tool_confirmation"
        assert payload["calls"][0]["name"] == "write_project_config"
        assert payload["calls"][0]["risk"] == RISK_WRITE
        assert "S1" in payload["calls"][0]["description"]

    def test_approval_executes_exactly_once(self, monkeypatch, tmp_path) -> None:
        """resume 会重跑 guardrail，所以确认后必须只执行一次、不重复写。"""
        from langgraph.types import Command

        graph, recorded, _ = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "write_project_config", SAMPLE_WRITE_ARGS)]},
                {"content": "已写入。"},
            ],
            checkpointer=_memory_checkpointer(),
        )
        config = {"configurable": {"thread_id": "once"}}
        graph.invoke(_initial(tmp_path, "你帮我执行"), config=config)
        result = graph.invoke(Command(resume={"approved": True}), config=config)

        assert [item["name"] for item in recorded] == ["write_project_config"]
        assert recorded[0]["approved"] is True
        assert result["reply"] == "已写入。"

    def test_rejection_does_not_execute_and_tells_the_model(self, monkeypatch, tmp_path) -> None:
        from langgraph.types import Command

        graph, recorded, fake = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "write_project_config", SAMPLE_WRITE_ARGS)]},
                {"content": "好的，我没有写入，需要的话告诉我改哪里。"},
            ],
            checkpointer=_memory_checkpointer(),
        )
        config = {"configurable": {"thread_id": "reject"}}
        graph.invoke(_initial(tmp_path, "你帮我执行"), config=config)
        result = graph.invoke(
            Command(resume={"approved": False, "note": "分组写错了"}), config=config
        )

        assert recorded == [], "用户拒绝后不得执行"
        tool_messages = [m for m in fake.seen_messages[-1] if m.get("role") == "tool"]
        assert "用户拒绝" in tool_messages[0]["content"]
        assert "分组写错了" in tool_messages[0]["content"]
        assert "没有写入" in result["reply"]

    def test_rejection_answers_calls_in_the_discarded_queue(self, monkeypatch, tmp_path) -> None:
        """拒绝第一组时，顺延组也必须收到取消回复，不能留下悬空 tool_call。"""
        from langgraph.types import Command

        graph, recorded, fake = _graph(
            monkeypatch,
            [
                {
                    "tool_calls": [
                        _tool_call("call_a", "set_run_resources", {"threads": 16}),
                        _tool_call("call_b", "generate_plan", {}),
                    ]
                },
                {"content": "好的，这一批动作都不执行。"},
            ],
            checkpointer=_memory_checkpointer(),
        )
        config = {"configurable": {"thread_id": "reject-queue"}}
        graph.invoke(_initial(tmp_path, "改资源并生成计划"), config=config)
        graph.invoke(Command(resume={"approved": False, "note": "先别改"}), config=config)

        assert recorded == []
        final_messages = fake.seen_messages[-1]
        _assert_all_declared_tool_calls_are_answered(final_messages)
        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in final_messages
            if message.get("role") == "tool"
        }
        assert "用户拒绝" in tool_messages["call_a"]
        assert "一并放弃" in tool_messages["call_b"]

    @pytest.mark.parametrize(
        ("decision", "should_execute"),
        [
            ({"approved": True}, True),
            ({"approved": False, "note": "不要"}, False),
            ({}, False),
            ({"note": "我只是随手写了句备注"}, False),
            ({"approved": "yes"}, False),
            ({"approved": 0}, False),
            ("yes", False),
            (True, False),
            ([], False),
            ([1], False),
        ],
    )
    def test_confirmation_is_fail_closed(
        self, monkeypatch, tmp_path, decision, should_execute
    ) -> None:
        """只有字面量 True 才是批准；缺失、字符串和数字都按拒绝处理。"""
        from langgraph.types import Command

        graph, recorded, fake = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": 16})]},
                {"content": "本轮处理完毕。"},
            ],
            checkpointer=_memory_checkpointer(),
        )
        config = {"configurable": {"thread_id": f"fail-closed-{decision!r}"}}
        graph.invoke(_initial(tmp_path, "线程改成 16"), config=config)
        graph.invoke(Command(resume=decision), config=config)

        assert bool(recorded) is should_execute
        _assert_all_declared_tool_calls_are_answered(fake.seen_messages[-1])

    def test_approved_batch_answers_every_declared_call(self, monkeypatch, tmp_path) -> None:
        """批准路径同样锁住 tool_call 协议不变量。"""
        from langgraph.types import Command

        graph, recorded, fake = _graph(
            monkeypatch,
            [
                {
                    "tool_calls": [
                        _tool_call("c1", "set_run_resources", {"threads": 16}),
                        _tool_call(
                            "c2", "configure_pipeline", {"step": "rsem", "enabled": False}
                        ),
                    ]
                },
                {"content": "两项配置已处理。"},
            ],
            checkpointer=_memory_checkpointer(),
        )
        config = {"configurable": {"thread_id": "approve-protocol"}}
        graph.invoke(_initial(tmp_path, "改两项配置"), config=config)
        graph.invoke(Command(resume={"approved": True}), config=config)

        assert [item["name"] for item in recorded] == [
            "set_run_resources",
            "configure_pipeline",
        ]
        _assert_all_declared_tool_calls_are_answered(fake.seen_messages[-1])

    def test_invalid_arguments_answer_every_declared_call(self, monkeypatch, tmp_path) -> None:
        """参数校验失败也必须回一个 tool 消息给模型。"""
        graph, recorded, fake = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("bad", "set_run_resources", {"threads": -5})]},
                {"content": "线程数不合法。"},
            ],
        )
        graph.invoke(
            _initial(tmp_path, "线程改成 -5"),
            config={"configurable": {"thread_id": "invalid-protocol"}},
        )

        assert recorded == []
        _assert_all_declared_tool_calls_are_answered(fake.seen_messages[-1])

    def test_partly_invalid_batch_keeps_the_error_across_resume(
        self, monkeypatch, tmp_path
    ) -> None:
        """同组部分非法时，确认合法调用后仍要保留非法调用的 tool 回复。"""
        from langgraph.types import Command

        graph, recorded, fake = _graph(
            monkeypatch,
            [
                {
                    "tool_calls": [
                        _tool_call("bad", "set_run_resources", {"threads": -5}),
                        _tool_call(
                            "good",
                            "configure_pipeline",
                            {"step": "rsem", "enabled": False},
                        ),
                    ]
                },
                {"content": "合法的配置已处理，非法参数没有执行。"},
            ],
            checkpointer=_memory_checkpointer(),
        )
        config = {"configurable": {"thread_id": "partial-validation"}}
        first = graph.invoke(_initial(tmp_path, "线程 -5，关闭 rsem"), config=config)
        card = first["__interrupt__"][0].value
        assert [call["call_id"] for call in card["calls"]] == ["good"]

        graph.invoke(Command(resume={"approved": True}), config=config)

        assert [item["name"] for item in recorded] == ["configure_pipeline"]
        _assert_all_declared_tool_calls_are_answered(fake.seen_messages[-1])
        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in fake.seen_messages[-1]
            if message.get("role") == "tool"
        }
        assert "不能小于" in tool_messages["bad"]
        assert '"ok": true' in tool_messages["good"]

    def test_execute_risk_also_requires_confirmation(self, monkeypatch, tmp_path) -> None:
        graph, recorded, _ = _graph(
            monkeypatch,
            [{"tool_calls": [_tool_call("c1", "run_analysis", {})]}],
            checkpointer=_memory_checkpointer(),
        )
        result = graph.invoke(
            _initial(tmp_path, "开始跑"), config={"configurable": {"thread_id": "run"}}
        )

        assert recorded == []
        payload = result["__interrupt__"][0].value
        assert payload["calls"][0]["risk"] == RISK_EXECUTE
        assert payload["calls"][0]["name"] == "run_analysis"

    def test_read_only_tool_is_not_gated(self, monkeypatch, tmp_path) -> None:
        """只读不该弹确认，否则对话会变得很啰嗦。"""
        graph, recorded, _ = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "read_project_state", {})]},
                {"content": "读完了。"},
            ],
        )
        result = graph.invoke(
            _initial(tmp_path, "状态"), config={"configurable": {"thread_id": "ro"}}
        )
        assert "__interrupt__" not in result, result
        assert len(recorded) == 1


class TestChatThreadIsolation:
    def test_chat_threads_are_prefixed_away_from_the_pipeline_graph(self) -> None:
        """对话与分析流水线共用 checkpoint 文件，thread 必须隔离。"""
        config = cg.chat_thread_config("proj", "main")
        assert config["configurable"]["thread_id"] == "chat:proj:main"
        assert not config["configurable"]["thread_id"] == "proj"


class TestStatePersistence:
    def test_durable_checkpointer_keeps_the_conversation(self, monkeypatch, tmp_path) -> None:
        """对话要能跨重启恢复（SqliteSaver 按项目落盘）。"""
        from rnaseq_agent.agent_graph import sqlite_checkpointer_for

        turns = [{"content": "第一次回答。"}]
        fake = FakeLLM(turns)
        monkeypatch.setattr(cg, "_stream_chat_completion", fake)

        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        config = cg.chat_thread_config("p", "main")

        def executor(name, arguments, directory, approved):
            return {"ok": True, "reply": "ok"}

        with sqlite_checkpointer_for(project_dir) as checkpointer:
            graph = cg.build_chat_graph(
                executor=executor,
                llm_config={"llm": {"enabled": True, "api_base": "http://x", "api_key": "k", "model": "m"}},
                checkpointer=checkpointer,
            )
            graph.invoke(_initial(project_dir, "你好"), config=config)

        with sqlite_checkpointer_for(project_dir) as checkpointer:
            graph = cg.build_chat_graph(
                executor=executor,
                llm_config={"llm": {"enabled": True, "api_base": "http://x", "api_key": "k", "model": "m"}},
                checkpointer=checkpointer,
            )
            snapshot = graph.get_state(config)
            contents = [m.get("content") for m in snapshot.values.get("messages", [])]
            assert "你好" in contents, contents
            assert "第一次回答。" in contents, contents
