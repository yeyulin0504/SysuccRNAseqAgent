"""对话控制平面图的测试：工具循环、风险守卫、interrupt/resume。

用户诉求（2026-09-16）：

> 我要修复的是大语言模型应该要有自己的执行工具啊

这套测试锁的是「模型真的能调工具」这件事，以及「高风险操作真的会拦」。

LLM 全部 fake（不联网）：本项目的图设计要求 LLM 调用与图逻辑分离，所以可以在
没有 API key 的情况下测完整条链路。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rnaseq_agent import chat_graph as cg
from rnaseq_agent.agent_tools import (
    TOOL_MODE_APPROVED_EXECUTE,
    TOOL_MODE_APPROVED_WRITE,
    TOOL_MODE_DISABLED,
    TOOL_MODE_READ_ONLY,
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
    tool_allowed,
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


def _graph(
    monkeypatch,
    turns,
    *,
    executor=None,
    calls=None,
    max_iterations=8,
    checkpointer=None,
    config_reader=None,
    approval_context_reader=None,
    clock=None,
    approval_ttl_seconds=900,
    tool_mode_reader=None,
):
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
        config_reader=config_reader,
        approval_context_reader=approval_context_reader,
        clock=clock,
        approval_ttl_seconds=approval_ttl_seconds,
        tool_mode_reader=tool_mode_reader,
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


def _approval_decision(
    interrupted_result: dict,
    *,
    approved: bool,
    note: str = "",
    approval_id: str | None = None,
) -> dict:
    card = interrupted_result["__interrupt__"][0].value
    return {
        "approved": approved,
        "note": note,
        "approval_id": approval_id or card["approval_id"],
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
    def test_tool_schema_exposure_follows_the_permission_mode(self) -> None:
        def names(mode: str) -> set[str]:
            return {item["function"]["name"] for item in tool_schemas(mode)}

        assert names(TOOL_MODE_DISABLED) == set()
        assert names(TOOL_MODE_READ_ONLY) == {
            name for name, spec in TOOL_SPECS.items() if spec.risk == RISK_READ
        }
        assert names(TOOL_MODE_READ_ONLY) == {
            "read_project_state",
            "browse_remote_samples",
        }
        assert names(TOOL_MODE_APPROVED_WRITE) == {
            name for name, spec in TOOL_SPECS.items() if spec.risk in {RISK_READ, RISK_WRITE}
        }
        assert names(TOOL_MODE_APPROVED_EXECUTE) == set(TOOL_SPECS)

    def test_unknown_tools_need_the_highest_permission_mode(self) -> None:
        assert not tool_allowed("invent_a_tool", TOOL_MODE_DISABLED)
        assert not tool_allowed("invent_a_tool", TOOL_MODE_READ_ONLY)
        assert not tool_allowed("invent_a_tool", TOOL_MODE_APPROVED_WRITE)
        assert tool_allowed("invent_a_tool", TOOL_MODE_APPROVED_EXECUTE)

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
        assert {
            "password",
            "private_key",
            "api_key",
            "auth_mode",
            "shell",
            "tool_mode",
        }.isdisjoint(
            properties
        )

    def test_qc_decision_is_not_exposed_as_a_general_llm_tool(self) -> None:
        names = {schema["function"]["name"] for schema in tool_schemas()}
        assert "record_qc_decision" not in names


def test_provider_messages_use_summary_context_without_exact_project_values(tmp_path: Path) -> None:
    (tmp_path / "project.json").write_text(
        json.dumps(
            {
                "route": {"id": "bulk_rna"},
                "samples": [
                    {
                        "sample_id": "PATIENT_SENTINEL_73",
                        "condition": "control",
                        "fastq_1": "/restricted/SENTINEL_73/TUMOR_SENTINEL_R1.fastq.gz",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    messages = [
        {"role": "system", "content": cg.SYSTEM_PROMPT},
        {"role": "user", "content": "请查看项目状态"},
    ]

    provider_messages, _config = cg._provider_messages_for_model(
        {"llm": {"api_base": "http://provider.test", "model": "m", "provider": "openai"}},
        messages,
        project_dir=tmp_path,
        project_id="p1",
        thread_id="t1",
    )

    serialized = json.dumps(provider_messages, ensure_ascii=False)
    assert "PATIENT_SENTINEL_73" not in serialized
    assert "TUMOR_SENTINEL_R1.fastq.gz" not in serialized
    assert "/restricted/SENTINEL_73" not in serialized
    assert any(item.get("role") == "user" and item.get("content") == "请查看项目状态" for item in provider_messages)


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

    @pytest.mark.parametrize(
        "arguments",
        [
            {"host": "-oProxyCommand=calc"},
            {"host": "bad host"},
            {"host": " good.example"},
            {"host": "alice@evil"},
            {"user": "-Fbad"},
            {"user": "alice;id"},
            {"user": "alice "},
            {"port": 0},
            {"port": 65536},
            {"port": True},
            {"port": 22.0},
        ],
    )
    def test_edit_connection_rejects_invalid_ssh_identity_patch(
        self, arguments: dict
    ) -> None:
        problems = validate_call("edit_connection", arguments)

        assert problems, arguments

    @pytest.mark.parametrize(
        "arguments",
        [
            {"host": "hpc.example.edu"},
            {"host": "192.0.2.10", "user": "alice_1", "port": 2222},
            {"host": "[2001:db8::1]", "user": "alice.dev"},
            {"host": "2001:db8::1", "user": "alice-dev"},
        ],
    )
    def test_edit_connection_accepts_valid_ssh_identity_patch(
        self, arguments: dict
    ) -> None:
        assert validate_call("edit_connection", arguments) == []


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

    def test_refresh_and_report_cards_state_their_persistent_side_effects(self) -> None:
        refresh = describe_call("refresh_project_status", {})
        report = describe_call("get_project_report", {})

        assert "项目运行状态" in refresh
        assert "本地会话" in refresh
        assert "只读" not in refresh
        assert "生成或覆盖" in report and "报告文件" in report
        assert "只读" not in report


# -- 图：工具循环 -----------------------------------------------------------


class TestChatGraphToolLoop:
    @pytest.mark.parametrize(
        ("mode", "tool_name", "arguments", "executed", "interrupts"),
        [
            (TOOL_MODE_DISABLED, "read_project_state", {}, False, False),
            (TOOL_MODE_READ_ONLY, "read_project_state", {}, True, False),
            (TOOL_MODE_READ_ONLY, "set_run_resources", {"threads": 16}, False, False),
            (TOOL_MODE_APPROVED_WRITE, "set_run_resources", {"threads": 16}, False, True),
            (TOOL_MODE_APPROVED_WRITE, "run_analysis", {}, False, False),
            (TOOL_MODE_APPROVED_EXECUTE, "run_analysis", {}, False, True),
        ],
    )
    def test_guardrail_enforces_the_live_permission_matrix(
        self,
        monkeypatch,
        tmp_path,
        mode,
        tool_name,
        arguments,
        executed,
        interrupts,
    ) -> None:
        turns = [{"tool_calls": [_tool_call("c1", tool_name, arguments)]}]
        if not interrupts:
            turns.append({"content": "已处理。"})
        graph, recorded, fake = _graph(
            monkeypatch,
            turns,
            checkpointer=_memory_checkpointer() if interrupts else None,
            tool_mode_reader=lambda: mode,
        )

        result = graph.invoke(
            _initial(tmp_path, "处理"),
            config={"configurable": {"thread_id": f"matrix-{mode}-{tool_name}"}},
        )

        assert bool(recorded) is executed
        assert bool(result.get("__interrupt__")) is interrupts
        if not executed and not interrupts:
            tool_messages = [m for m in fake.seen_messages[-1] if m.get("role") == "tool"]
            assert tool_messages
            assert mode in tool_messages[0]["content"]

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

    def test_legacy_dict_result_is_projected_before_provider_and_log(self, monkeypatch, tmp_path) -> None:
        secret_path = "/secret/SAMPLE_R1.fastq.gz"

        def executor(name, arguments, project_dir, approved):
            return {
                "ok": True,
                "reply": f"已读取 {secret_path}",
                "samples": [{"sample_id": "SECRET_SAMPLE", "fastq_1": secret_path}],
                "report_path": secret_path,
            }

        graph, _recorded, fake = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "read_project_state", {})]},
                {"content": "已完成。"},
            ],
            executor=executor,
        )
        result = graph.invoke(_initial(tmp_path, "状态"), config={"configurable": {"thread_id": "projection"}})

        serialized = json.dumps(fake.seen_messages[1:], ensure_ascii=False)
        assert "SECRET_SAMPLE" not in serialized
        assert secret_path not in serialized
        assert "SECRET_SAMPLE" not in json.dumps(result.get("tool_log") or {}, ensure_ascii=False)
        assert secret_path not in json.dumps(result.get("tool_log") or {}, ensure_ascii=False)

    def test_legacy_free_text_fields_use_fixed_categories(self, monkeypatch, tmp_path) -> None:
        secret = "PATIENT_SENTINEL_LEGACY /restricted/SENTINEL_LEGACY/report.md"

        def executor(name, arguments, project_dir, approved):
            return {"ok": False, "reply": secret, "message": secret, "error": secret,
                    "error_code": "TOOL_MODE_DISABLED"}

        graph, _recorded, fake = _graph(
            monkeypatch,
            [{"tool_calls": [_tool_call("c1", "read_project_state", {})]},
             {"content": "好的。"}],
            executor=executor,
        )
        result = graph.invoke(_initial(tmp_path, "用户自己的 PATIENT_SENTINEL_USER"),
                              config={"configurable": {"thread_id": "legacy-free-text"}})
        assert secret not in json.dumps(fake.seen_messages, ensure_ascii=False)
        assert secret not in json.dumps(result.get("messages") or [], ensure_ascii=False)
        assert "PATIENT_SENTINEL_USER" in json.dumps(fake.seen_messages[0], ensure_ascii=False)
        tool_messages = [m for m in fake.seen_messages[1] if m.get("role") == "tool"]
        assert tool_messages[0]["model_projection_version"] == 1

    def test_provider_assistant_content_is_not_persisted_or_replayed(self, monkeypatch, tmp_path) -> None:
        secret = "REPORT_SENTINEL_PROVIDER /restricted/SENTINEL_PROVIDER/report.md"
        graph, _recorded, fake = _graph(
            monkeypatch,
            [{"content": secret}],
        )
        result = graph.invoke(_initial(tmp_path, "请解释"),
                              config={"configurable": {"thread_id": "provider-content"}})
        assert result["reply"] == "模型回复已生成。"
        assert secret not in json.dumps(result.get("messages") or [], ensure_ascii=False)


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

    def test_switching_to_a_stricter_mode_invalidates_a_waiting_card(
        self, monkeypatch, tmp_path
    ) -> None:
        from langgraph.types import Command

        live = {"mode": TOOL_MODE_APPROVED_WRITE}
        graph, recorded, fake = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": 16})]},
                {"content": "没有执行。"},
            ],
            checkpointer=_memory_checkpointer(),
            tool_mode_reader=lambda: live["mode"],
        )
        config = {"configurable": {"thread_id": "tighten-on-resume"}}
        first = graph.invoke(_initial(tmp_path, "改成 16 线程"), config=config)

        live["mode"] = TOOL_MODE_READ_ONLY
        graph.invoke(Command(resume=_approval_decision(first, approved=True)), config=config)

        assert recorded == []
        tool_messages = [m for m in fake.seen_messages[-1] if m.get("role") == "tool"]
        assert TOOL_MODE_READ_ONLY in tool_messages[0]["content"]

    def test_execute_rechecks_mode_after_confirmation(self, monkeypatch, tmp_path) -> None:
        from langgraph.types import Command

        reads = iter(
            [
                TOOL_MODE_APPROVED_WRITE,
                TOOL_MODE_APPROVED_WRITE,
                TOOL_MODE_APPROVED_WRITE,
                TOOL_MODE_READ_ONLY,
            ]
        )
        graph, recorded, fake = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": 16})]},
                {"content": "没有执行。"},
            ],
            checkpointer=_memory_checkpointer(),
            tool_mode_reader=lambda: next(reads, TOOL_MODE_READ_ONLY),
        )
        config = {"configurable": {"thread_id": "tighten-at-execute"}}
        first = graph.invoke(_initial(tmp_path, "改成 16 线程"), config=config)
        graph.invoke(Command(resume=_approval_decision(first, approved=True)), config=config)

        assert recorded == []
        tool_messages = [m for m in fake.seen_messages[-1] if m.get("role") == "tool"]
        assert TOOL_MODE_READ_ONLY in tool_messages[0]["content"]

    def test_confirmation_card_binds_the_exact_server_context_without_secrets(
        self, monkeypatch, tmp_path
    ) -> None:
        context = {
            "config_revision": "project:7/shared:4",
            "config_hash": "sha256:config-snapshot",
            "contract_id": "sha256:contract-1",
            "run_id": "run-9",
        }
        graph, recorded, _ = _graph(
            monkeypatch,
            [{"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": 16})]}],
            checkpointer=_memory_checkpointer(),
            config_reader=lambda _path: {
                "server": {"threads": 8, "password": "never-show-this"},
                "llm": {"api_key": "sk-never-show-this"},
            },
            approval_context_reader=lambda _path: dict(context),
        )

        result = graph.invoke(
            _initial(tmp_path, "线程改为 16"),
            config={"configurable": {"thread_id": "bound-card"}},
        )
        card = result["__interrupt__"][0].value

        assert recorded == []
        assert card["approval_id"].startswith("appr_")
        assert card["project_id"] == "p1"
        assert card["thread_id"] == "t1"
        assert card["policy"] == POLICY_BATCH
        assert card["confirmation_policy_version"]
        assert card["config_revision"] == context["config_revision"]
        assert card["config_hash"] == context["config_hash"]
        assert card["contract_id"] == context["contract_id"]
        assert card["run_id"] == context["run_id"]
        assert card["issued_at"]
        assert card["expires_at"]
        assert set(card["calls"][0]) == {
            "call_id",
            "name",
            "arguments_hash",
            "risk",
            "label",
            "description",
        }
        assert "arguments" not in card["calls"][0]
        assert card["calls"][0]["arguments_hash"].startswith("sha256:")
        encoded = json.dumps(card, ensure_ascii=False)
        assert "never-show-this" not in encoded
        assert "sk-never-show-this" not in encoded

    def test_unavailable_bound_file_refuses_to_issue_a_card(
        self, monkeypatch, tmp_path
    ) -> None:
        graph, recorded, _ = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": 16})]},
                {"content": "当前项目配置不可读，没有执行。"},
            ],
            checkpointer=_memory_checkpointer(),
            approval_context_reader=lambda _path: {
                "config_revision": "project:unavailable|shared:abcd",
                "config_hash": "sha256:composite",
                "contract_id": "",
                "run_id": "",
            },
        )

        result = graph.invoke(
            _initial(tmp_path, "线程改为 16"),
            config={"configurable": {"thread_id": "unavailable-context"}},
        )

        assert "__interrupt__" not in result
        assert recorded == []
        assert result["via"] == "rejected"

    def test_card_hashes_normalized_write_arguments_and_displays_fastq_fields(
        self, monkeypatch, tmp_path
    ) -> None:
        from langgraph.types import Command

        args = {
            **SAMPLE_WRITE_ARGS,
            "samples": [
                {**SAMPLE_WRITE_ARGS["samples"][0], "condition": "control"},
                {**SAMPLE_WRITE_ARGS["samples"][1], "condition": ""},
            ],
        }
        graph, recorded, _ = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "write_project_config", args)]},
                {"content": "已写入。"},
            ],
            checkpointer=_memory_checkpointer(),
        )

        config = {"configurable": {"thread_id": "normalized-write"}}
        first = graph.invoke(
            _initial(tmp_path, "写入配置"),
            config=config,
        )
        call = first["__interrupt__"][0].value["calls"][0]
        normalized = normalize_write_arguments(args)

        assert call["arguments_hash"] == cg._canonical_hash(normalized)
        assert "S1_1.fastq.gz" in call["description"]
        assert "S1_2.fastq.gz" in call["description"]

        graph.invoke(
            Command(resume=_approval_decision(first, approved=True)), config=config
        )
        assert recorded[0]["arguments"] == normalized

    def test_card_displays_every_edit_sample_and_cms_execution_field(
        self, monkeypatch, tmp_path
    ) -> None:
        graph, _, _ = _graph(
            monkeypatch,
            [
                {
                    "tool_calls": [
                        _tool_call(
                            "samples",
                            "edit_samples",
                            {
                                "samples": [
                                    {
                                        "sample_id": "S1",
                                        "condition": "treat",
                                        "fastq_1": "replacement_R1.fastq.gz",
                                        "fastq_2": "replacement_R2.fastq.gz",
                                    }
                                ]
                            },
                        ),
                        _tool_call(
                            "cms",
                            "set_cms_options",
                            {"enabled": True, "n_perm": 2000, "fdr": 0.1, "run_mode": "counts"},
                        ),
                    ]
                }
            ],
            checkpointer=_memory_checkpointer(),
            config_reader=lambda _path: {
                "samples": {"items": SAMPLE_WRITE_ARGS["samples"]},
                "pipeline": {"cms": {"enabled": False}},
                "cms": {"n_perm": 1000, "fdr": 0.05, "run_mode": "pipeline"},
            },
        )

        first = graph.invoke(
            _initial(tmp_path, "改样本并配置 CMS"),
            config={"configurable": {"thread_id": "full-card-semantics"}},
        )
        descriptions = {
            item["name"]: item["description"]
            for item in first["__interrupt__"][0].value["calls"]
        }

        assert "replacement_R1.fastq.gz" in descriptions["edit_samples"]
        assert "replacement_R2.fastq.gz" in descriptions["edit_samples"]
        assert "counts" in descriptions["set_cms_options"]
        assert "pipeline" in descriptions["set_cms_options"]

    def test_approval_id_is_stable_when_the_interrupt_is_replayed(
        self, monkeypatch, tmp_path
    ) -> None:
        graph, _, _ = _graph(
            monkeypatch,
            [{"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": 16})]}],
            checkpointer=_memory_checkpointer(),
        )
        config = {"configurable": {"thread_id": "stable-card"}}
        first = graph.invoke(_initial(tmp_path, "线程改为 16"), config=config)
        replayed = graph.invoke(None, config=config)

        assert (
            first["__interrupt__"][0].value["approval_id"]
            == replayed["__interrupt__"][0].value["approval_id"]
        )

    @pytest.mark.parametrize("supplied", [None, "", "appr_forged"])
    def test_missing_or_forged_approval_id_never_executes(
        self, monkeypatch, tmp_path, supplied
    ) -> None:
        from langgraph.types import Command

        graph, recorded, _ = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": 16})]},
                {"content": "没有执行失效的确认。"},
            ],
            checkpointer=_memory_checkpointer(),
        )
        config = {"configurable": {"thread_id": f"forged-{supplied}"}}
        graph.invoke(_initial(tmp_path, "线程改为 16"), config=config)
        decision = {"approved": True}
        if supplied is not None:
            decision["approval_id"] = supplied

        result = graph.invoke(Command(resume=decision), config=config)

        assert recorded == []
        assert result["via"] == "rejected"

    def test_approval_fails_closed_when_config_changes_while_waiting(
        self, monkeypatch, tmp_path
    ) -> None:
        from langgraph.types import Command

        context = {
            "config_revision": "rev-1",
            "config_hash": "sha256:one",
            "contract_id": "",
            "run_id": "",
        }
        graph, recorded, _ = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": 16})]},
                {"content": "配置已变化，请重新确认。"},
            ],
            checkpointer=_memory_checkpointer(),
            approval_context_reader=lambda _path: dict(context),
        )
        config = {"configurable": {"thread_id": "drift"}}
        first = graph.invoke(_initial(tmp_path, "线程改为 16"), config=config)
        context.update(config_revision="rev-2", config_hash="sha256:two")

        result = graph.invoke(
            Command(resume=_approval_decision(first, approved=True)), config=config
        )

        assert recorded == []
        assert result["via"] == "rejected"

    def test_expired_approval_never_executes(self, monkeypatch, tmp_path) -> None:
        from langgraph.types import Command

        now = [datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)]
        graph, recorded, _ = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": 16})]},
                {"content": "确认已过期。"},
            ],
            checkpointer=_memory_checkpointer(),
            clock=lambda: now[0],
            approval_ttl_seconds=60,
        )
        config = {"configurable": {"thread_id": "expired"}}
        first = graph.invoke(_initial(tmp_path, "线程改为 16"), config=config)
        now[0] += timedelta(seconds=61)

        result = graph.invoke(
            Command(resume=_approval_decision(first, approved=True)), config=config
        )

        assert recorded == []
        assert result["via"] == "rejected"

    def test_execute_revalidates_policy_after_approval(
        self, monkeypatch, tmp_path
    ) -> None:
        from langgraph.types import Command

        graph, recorded, _ = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": 16})]},
                {"content": "策略已变化，请重新确认。"},
            ],
            checkpointer=_memory_checkpointer(),
        )
        config = {"configurable": {"thread_id": "policy-drift"}}
        first = graph.invoke(_initial(tmp_path, "线程改为 16"), config=config)
        original_policy = cg.confirmation_policy
        monkeypatch.setattr(
            cg,
            "confirmation_policy",
            lambda name: POLICY_SOLO if name == "set_run_resources" else original_policy(name),
        )

        result = graph.invoke(
            Command(resume=_approval_decision(first, approved=True)), config=config
        )

        assert recorded == []
        assert result["via"] == "rejected"

    def test_execute_revalidates_arguments_after_approval(
        self, monkeypatch, tmp_path
    ) -> None:
        from langgraph.types import Command

        graph, recorded, _ = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": 16})]},
                {"content": "参数校验规则已变化，请重新确认。"},
            ],
            checkpointer=_memory_checkpointer(),
        )
        config = {"configurable": {"thread_id": "validation-drift"}}
        first = graph.invoke(_initial(tmp_path, "线程改为 16"), config=config)
        monkeypatch.setattr(cg, "validate_call", lambda _name, _arguments: ["新规则拒绝"])

        result = graph.invoke(
            Command(resume=_approval_decision(first, approved=True)), config=config
        )

        assert recorded == []
        assert result["via"] == "rejected"

    def test_approval_cannot_cross_chat_threads(self, monkeypatch, tmp_path) -> None:
        from langgraph.types import Command

        graph, recorded, _ = _graph(
            monkeypatch,
            [
                {"tool_calls": [_tool_call("a", "set_run_resources", {"threads": 16})]},
                {"tool_calls": [_tool_call("b", "set_run_resources", {"threads": 32})]},
                {"content": "确认不属于当前对话。"},
            ],
            checkpointer=_memory_checkpointer(),
        )
        config_a = {"configurable": {"thread_id": "thread-a"}}
        config_b = {"configurable": {"thread_id": "thread-b"}}
        first_a = graph.invoke(_initial(tmp_path, "16"), config=config_a)
        first_b = graph.invoke(
            {**_initial(tmp_path, "32"), "thread_id": "t2"}, config=config_b
        )

        result = graph.invoke(
            Command(
                resume=_approval_decision(
                    first_b,
                    approved=True,
                    approval_id=first_a["__interrupt__"][0].value["approval_id"],
                )
            ),
            config=config_b,
        )

        assert recorded == []
        assert result["via"] == "rejected"

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
        first = graph.invoke(_initial(tmp_path, "你帮我执行"), config=config)
        result = graph.invoke(
            Command(resume=_approval_decision(first, approved=True)), config=config
        )

        assert [item["name"] for item in recorded] == ["write_project_config"]
        assert recorded[0]["approved"] is True
        assert result["reply"] == "已写入。"
        claims = list((tmp_path / ".approval_claims").glob("*.json"))
        assert len(claims) == 1
        claim = json.loads(claims[0].read_text(encoding="utf-8"))
        assert claim["status"] == "consumed"
        assert claim["calls"] == [
            {
                "call_id": "c1",
                "name": "write_project_config",
                "arguments_hash": first["__interrupt__"][0].value["calls"][0][
                    "arguments_hash"
                ],
            }
        ]
        assert claim["results"] == [
            {
                "call_id": "c1",
                "name": "write_project_config",
                "ok": True,
                "has_reply": True,
            }
        ]
        assert set(claim["calls"][0]) == {"call_id", "name", "arguments_hash"}
        assert "S1_1.fastq.gz" not in json.dumps(claim, ensure_ascii=False)

    def test_crash_after_side_effect_claim_fails_closed_on_replay(
        self, monkeypatch, tmp_path
    ) -> None:
        """A crash after the executor starts must not run the same approval twice."""
        from langgraph.types import Command

        attempts: list[str] = []

        def crashing_executor(name, arguments, project_dir, approved):
            attempts.append(name)
            raise KeyboardInterrupt("simulated process loss after side effect")

        graph, _, _ = _graph(
            monkeypatch,
            [{"tool_calls": [_tool_call("c1", "set_run_resources", {"threads": 16})]}],
            executor=crashing_executor,
            checkpointer=_memory_checkpointer(),
        )
        config = {"configurable": {"thread_id": "crash-replay"}}
        first = graph.invoke(_initial(tmp_path, "线程改为 16"), config=config)

        with pytest.raises(KeyboardInterrupt):
            graph.invoke(
                Command(resume=_approval_decision(first, approved=True)), config=config
            )
        claims = list((tmp_path / ".approval_claims").glob("*.json"))
        assert len(claims) == 1
        assert json.loads(claims[0].read_text(encoding="utf-8"))["status"] == "started"
        replayed = graph.invoke(None, config=config)

        assert attempts == ["set_run_resources"]
        assert replayed["via"] == "rejected"
        assert json.loads(claims[0].read_text(encoding="utf-8"))["status"] == "started"

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
        first = graph.invoke(_initial(tmp_path, "你帮我执行"), config=config)
        result = graph.invoke(
            Command(
                resume=_approval_decision(first, approved=False, note="分组写错了")
            ),
            config=config,
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
        first = graph.invoke(_initial(tmp_path, "改资源并生成计划"), config=config)
        graph.invoke(
            Command(resume=_approval_decision(first, approved=False, note="先别改")),
            config=config,
        )

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
        first = graph.invoke(_initial(tmp_path, "线程改成 16"), config=config)
        if isinstance(decision, dict) and decision.get("approved") is True:
            decision = {**decision, "approval_id": first["__interrupt__"][0].value["approval_id"]}
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
        first = graph.invoke(_initial(tmp_path, "改两项配置"), config=config)
        graph.invoke(
            Command(resume=_approval_decision(first, approved=True)), config=config
        )

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

        graph.invoke(
            Command(resume=_approval_decision(first, approved=True)), config=config
        )

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
