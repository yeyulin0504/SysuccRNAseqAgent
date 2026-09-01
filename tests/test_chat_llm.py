from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rnaseq_agent.chat import ChatSession
from rnaseq_agent.llm import LLMDecision


class FakeLLM:
    model = "fake-model"
    base_url = "http://localhost/v1"

    def __init__(self, decision: LLMDecision) -> None:
        self.decision = decision
        self.calls: list[tuple[str, bool]] = []

    def decide(self, user_text: str, *, has_project: bool) -> LLMDecision:
        self.calls.append((user_text, has_project))
        return self.decision


class CapturingSession(ChatSession):
    def __init__(self, output_dir: Path, llm: FakeLLM) -> None:
        super().__init__(output_dir, llm=llm)  # type: ignore[arg-type]
        self.messages: list[str] = []

    def say(self, message: str) -> None:
        self.messages.append(message)


class ChatLLMTests(unittest.TestCase):
    def test_llm_general_answer_is_displayed(self) -> None:
        llm = FakeLLM(
            LLMDecision(
                action="chat",
                message="gene count 矩阵可用于后续差异表达分析。",
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            session = CapturingSession(Path(temp_dir), llm)
            handled = session.dispatch_llm_command("gene count 能做什么？")

        self.assertTrue(handled)
        self.assertEqual(llm.calls, [("gene count 能做什么？", False)])
        self.assertIn("gene count 矩阵可用于后续差异表达分析。", session.messages)

    def test_llm_cannot_dispatch_unknown_action(self) -> None:
        llm = FakeLLM(LLMDecision(action="chat", message="该动作不在允许列表中。"))
        with tempfile.TemporaryDirectory() as temp_dir:
            session = CapturingSession(Path(temp_dir), llm)
            handled = session.dispatch_llm_command("执行任意 shell")

        self.assertTrue(handled)
        self.assertIn("该动作不在允许列表中。", session.messages)


if __name__ == "__main__":
    unittest.main()
