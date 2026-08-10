from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rnaseq_agent.chat import ChatSession
from rnaseq_agent.llm import LLMDecision
from rnaseq_agent.llm_policy import AgentStep, ConfigPatch
from rnaseq_agent.storage import save_json


class FakeLLM:
    model = "fake-model"
    base_url = "http://localhost/v1"
    resolved_api_mode = "test"

    def __init__(self, decision: LLMDecision) -> None:
        self.decision = decision
        self.calls: list[tuple[str, bool, dict | None]] = []

    def decide(self, user_text: str, *, has_project: bool, config=None) -> LLMDecision:
        self.calls.append((user_text, has_project, config))
        return self.decision


class CapturingSession(ChatSession):
    def __init__(self, output_dir: Path, llm: FakeLLM) -> None:
        super().__init__(output_dir, llm=llm)  # type: ignore[arg-type]
        self.messages: list[str] = []

    def say(self, message: str) -> None:
        self.messages.append(message)


def _config() -> dict:
    return {
        "project": {"id": "llm_test", "title": "LLM test", "owner": "local"},
        "sequencing": {"layout": "paired", "strandedness": "auto", "reads_per_sample_million": 40},
        "pipeline": {"fastp": {"enabled": True}},
    }


class ChatLLMTests(unittest.TestCase):
    def test_llm_answer_is_displayed_without_side_effect(self) -> None:
        llm = FakeLLM(LLMDecision(action="answer", message="gene count 矩阵可用于后续差异表达分析。"))
        with tempfile.TemporaryDirectory() as temp_dir:
            session = CapturingSession(Path(temp_dir), llm)
            handled = session.dispatch_llm_command("gene count 能做什么？")

        self.assertTrue(handled)
        self.assertEqual(llm.calls, [("gene count 能做什么？", False, None)])
        self.assertIn("gene count 矩阵可用于后续差异表达分析。", session.messages)

    def test_proposal_is_review_only_until_explicit_apply(self) -> None:
        llm = FakeLLM(
            LLMDecision(
                action="config_patch_proposal",
                message="建议使用单端布局。",
                proposals=(ConfigPatch("/sequencing/layout", "single"),),
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "project.json"
            save_json(config_path, _config())
            session = CapturingSession(root, llm)
            session.current_config_path = config_path

            session.dispatch_llm_command("这是单端数据")
            self.assertEqual(session.pending_proposals[0].value, "single")
            self.assertIsNone(session.pending_config)

            with patch.object(session, "ask_yes_no", return_value=True):
                session.apply_pending_proposals()

        self.assertEqual(session.pending_config["sequencing"]["layout"], "single")
        self.assertIn("配置建议已应用到当前会话内存，尚未写入 project.json。", session.messages)

    def test_contract_request_requires_local_confirmation_before_submission(self) -> None:
        llm = FakeLLM(LLMDecision(action="contract_submission_request", message="请提交当前合同。"))
        with tempfile.TemporaryDirectory() as temp_dir:
            session = CapturingSession(Path(temp_dir), llm)
            config_path = Path(temp_dir) / "project.json"
            save_json(config_path, _config())
            session.current_config_path = config_path

            with patch("rnaseq_agent.chat.inspect_llm_submission") as inspect, patch(
                "rnaseq_agent.chat.submit_llm_contract"
            ) as submit, patch.object(session, "ask_yes_no", return_value=False):
                inspect.return_value = type("Preview", (), {"contract_id": "sha256:verified"})()
                session.dispatch_llm_command("提交当前合同")

        submit.assert_not_called()
        self.assertTrue(any("sha256:verified" in message for message in session.messages))
        self.assertIn("已取消合同提交。", session.messages)

    def test_agent_plan_executes_only_local_mapped_steps(self) -> None:
        llm = FakeLLM(
            LLMDecision(
                action="agent_plan",
                message="先检查并生成报告。",
                steps=(AgentStep("validate"), AgentStep("summary"), AgentStep("report")),
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            session = CapturingSession(Path(temp_dir), llm)
            config_path = Path(temp_dir) / "project.json"
            save_json(config_path, _config())
            session.current_config_path = config_path
            with patch.object(session, "validate_current_project") as validate, patch.object(
                session, "show_current_summary"
            ) as summary, patch.object(session, "generate_current_report") as report:
                session.dispatch_llm_command("检查并生成报告")

        validate.assert_called_once()
        summary.assert_called_once()
        report.assert_called_once()

    def test_agent_plan_without_project_does_not_execute_steps(self) -> None:
        llm = FakeLLM(LLMDecision(action="agent_plan", steps=(AgentStep("validate"),)))
        with tempfile.TemporaryDirectory() as temp_dir:
            session = CapturingSession(Path(temp_dir), llm)
            with patch.object(session, "validate_current_project") as validate:
                session.dispatch_llm_command("检查项目")

        validate.assert_not_called()
        self.assertTrue(any("当前会话还没有绑定项目" in message for message in session.messages))

    def test_agent_plan_stops_after_declined_remote_confirmation(self) -> None:
        llm = FakeLLM(
            LLMDecision(
                action="agent_plan",
                steps=(AgentStep("status"), AgentStep("report")),
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            session = CapturingSession(Path(temp_dir), llm)
            config_path = Path(temp_dir) / "project.json"
            save_json(config_path, _config())
            session.current_config_path = config_path
            with patch.object(session, "ask_yes_no", return_value=False), patch(
                "rnaseq_agent.chat.refresh_project_status_action"
            ) as refresh_status, patch.object(session, "generate_current_report") as report:
                session.dispatch_llm_command("刷新状态再生成报告")

        refresh_status.assert_not_called()
        report.assert_not_called()
        self.assertIn("已取消刷新状态。", session.messages)

    def test_unknown_decision_never_dispatches_action(self) -> None:
        llm = FakeLLM(LLMDecision(action="run", message="不应运行"))
        with tempfile.TemporaryDirectory() as temp_dir:
            session = CapturingSession(Path(temp_dir), llm)
            handled = session.dispatch_llm_command("执行任意 shell")

        self.assertTrue(handled)
        self.assertIn("模型响应不符合受限策略，未执行任何操作。", session.messages)

    def test_direct_run_requires_confirmation(self) -> None:
        llm = FakeLLM(LLMDecision(action="answer", message="unused"))
        with tempfile.TemporaryDirectory() as temp_dir:
            session = CapturingSession(Path(temp_dir), llm)
            config_path = Path(temp_dir) / "project.json"
            save_json(config_path, _config())
            session.current_config_path = config_path
            with patch.object(session, "ask_yes_no", return_value=False), patch(
                "rnaseq_agent.chat.run_project_action"
            ) as run_project:
                session.run_current_project(wait=False)

        run_project.assert_not_called()
        self.assertIn("已取消运行。", session.messages)

    def test_direct_status_requires_confirmation(self) -> None:
        llm = FakeLLM(LLMDecision(action="answer", message="unused"))
        with tempfile.TemporaryDirectory() as temp_dir:
            session = CapturingSession(Path(temp_dir), llm)
            config_path = Path(temp_dir) / "project.json"
            save_json(config_path, _config())
            session.current_config_path = config_path
            with patch.object(session, "ask_yes_no", return_value=False), patch(
                "rnaseq_agent.chat.refresh_project_status_action"
            ) as refresh_status:
                session.show_current_status()

        refresh_status.assert_not_called()
        self.assertIn("已取消刷新状态。", session.messages)


if __name__ == "__main__":
    unittest.main()
