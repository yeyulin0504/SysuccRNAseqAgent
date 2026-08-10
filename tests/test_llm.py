from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rnaseq_agent.llm import (
    CodexCLIClient,
    LLMError,
    OpenAICompatibleClient,
    _extract_responses_text,
    _friendly_api_error,
    codex_home_path,
    codex_login_status,
    parse_decision,
    parse_onboarding_decision,
)


class ParseDecisionTests(unittest.TestCase):
    def test_parses_answer(self) -> None:
        decision = parse_decision(json.dumps({"action": "answer", "message": "可先检查 reads 质量。"}))
        self.assertEqual(decision.action, "answer")
        self.assertEqual(decision.message, "可先检查 reads 质量。")
        self.assertEqual(decision.proposals, ())

    def test_parses_proposal_after_local_validation(self) -> None:
        decision = parse_decision(
            json.dumps(
                {
                    "action": "config_patch_proposal",
                    "message": "建议关闭 Arriba。",
                    "proposals": [{"op": "replace", "path": "/pipeline/arriba/enabled", "value": False}],
                }
            )
        )
        self.assertEqual(decision.action, "config_patch_proposal")
        self.assertFalse(decision.proposals[0].value)

    def test_parses_agent_plan_after_local_validation(self) -> None:
        decision = parse_decision(
            json.dumps(
                {
                    "action": "agent_plan",
                    "message": "先校验，再等待确认运行。",
                    "steps": [{"kind": "validate"}, {"kind": "run", "wait": True}],
                }
            )
        )

        self.assertEqual(decision.action, "agent_plan")
        self.assertEqual(decision.steps[0].kind, "validate")
        self.assertTrue(decision.steps[1].wait)

    def test_rejects_agent_plan_with_unsafe_or_extra_fields(self) -> None:
        for payload in (
            {"action": "agent_plan", "message": "x", "steps": [{"kind": "run", "wait": False, "command": "x"}]},
            {"action": "agent_plan", "message": "x", "steps": [{"kind": "status"}], "proposals": []},
            {"action": "agent_plan", "message": "x", "steps": []},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(LLMError):
                    parse_decision(json.dumps(payload))

    def test_rejects_legacy_or_unsafe_action(self) -> None:
        for action in ("open", "run", "shell"):
            with self.subTest(action=action):
                with self.assertRaises(LLMError):
                    parse_decision(json.dumps({"action": action, "message": "不要执行"}))

    def test_rejects_answer_with_proposals(self) -> None:
        with self.assertRaises(LLMError):
            parse_decision(
                json.dumps(
                    {
                        "action": "answer",
                        "message": "说明",
                        "proposals": [{"op": "replace", "path": "/sequencing/layout", "value": "single"}],
                    }
                )
            )

    def test_rejects_non_json(self) -> None:
        with self.assertRaises(LLMError):
            parse_decision("run the project")

    def test_contract_submission_request_must_be_parameter_free(self) -> None:
        decision = parse_decision(
            json.dumps(
                {
                    "action": "contract_submission_request",
                    "message": "请在本地审阅并确认后提交当前绑定合同。",
                }
            )
        )
        self.assertEqual(decision.action, "contract_submission_request")
        self.assertEqual(decision.proposals, ())

        for key, value in (
            ("path", "/tmp/project.json"),
            ("contract_id", "sha256:x"),
            ("wait", False),
            ("proposals", []),
        ):
            with self.subTest(key=key):
                with self.assertRaises(LLMError):
                    parse_decision(
                        json.dumps(
                            {
                                "action": "contract_submission_request",
                                "message": "submit",
                                key: value,
                            }
                        )
                    )


class OnboardingDecisionTests(unittest.TestCase):
    def test_parses_safe_onboarding_proposal(self) -> None:
        decision = parse_onboarding_decision(
            json.dumps({"message": "已识别。", "proposals": [{"field": "server.host", "value": "hpc.example.edu"}]})
        )
        self.assertEqual(decision.proposals[0].field, "server.host")

    def test_rejects_onboarding_actions_and_sensitive_fields(self) -> None:
        with self.assertRaises(LLMError):
            parse_onboarding_decision(json.dumps({"action": "run", "message": "x", "proposals": []}))
        with self.assertRaises(LLMError):
            parse_onboarding_decision(json.dumps({"message": "x", "proposals": [{"field": "server.password", "value": "secret"}]}))


class ModelDiscoveryTests(unittest.TestCase):
    @patch("rnaseq_agent.llm.urllib.request.urlopen")
    def test_lists_and_sorts_provider_models(self, urlopen: MagicMock) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"data": [{"id": "model-z"}, {"id": "model-a"}, {"id": "model-a"}]}).encode("utf-8")
        urlopen.return_value = response
        client = OpenAICompatibleClient(base_url="https://api.example.com/v1/", model="", api_key="secret")

        self.assertEqual(client.list_models(), ["model-a", "model-z"])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.example.com/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

    def test_explains_insufficient_quota_in_plain_language(self) -> None:
        detail = json.dumps({"error": {"type": "insufficient_quota", "code": "insufficient_quota", "message": "You exceeded your current quota."}})
        message = _friendly_api_error(429, detail)
        self.assertIn("ChatGPT Plus/Codex", message)
        self.assertIn("Base URL", message)


class ProviderPayloadTests(unittest.TestCase):
    @patch.object(OpenAICompatibleClient, "_post_json")
    def test_responses_payload_minimizes_project_context(self, post_json: MagicMock) -> None:
        post_json.return_value = {"output_text": '{"action":"answer","message":"说明"}'}
        client = OpenAICompatibleClient(base_url="http://127.0.0.1:15721/v1", model="gpt-5.5", api_mode="auto")
        secret_config = {
            "server": {"host": "secret-hpc", "user": "private-user"},
            "samples": {"items": [{"sample_id": "patient-001", "fastq_1": "raw.fastq.gz"}]},
            "sequencing": {"layout": "paired", "strandedness": "auto", "reads_per_sample_million": 40},
            "polling": {"interval_seconds": 300, "timeout_hours": 24},
            "pipeline": {"fastp": {"enabled": True}},
        }

        decision = client.decide("解释流程", has_project=True, config=secret_config)

        self.assertEqual(decision.action, "answer")
        endpoint, payload = post_json.call_args.args
        self.assertEqual(endpoint, "/responses")
        self.assertNotIn("temperature", payload)
        serialized = payload["input"]
        for secret in ("secret-hpc", "private-user", "patient-001", "raw.fastq.gz"):
            self.assertNotIn(secret, serialized)
        self.assertIn('"enabled_steps": ["fastp"]', serialized)

    def test_extracts_direct_responses_output_text(self) -> None:
        self.assertEqual(_extract_responses_text({"output_text": "hello"}), "hello")


class CodexCLIClientTests(unittest.TestCase):
    @patch("rnaseq_agent.llm.subprocess.run")
    def test_uses_ephemeral_read_only_codex_exec(self, run: MagicMock) -> None:
        def fake_run(command: list[str], **_: object) -> SimpleNamespace:
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text('{"action":"answer","message":"说明"}', encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        run.side_effect = fake_run
        client = CodexCLIClient(model="gpt-5.5", executable=Path("codex.exe"), codex_home=Path("isolated-codex-home"))

        decision = client.decide("总结项目", has_project=True)

        self.assertEqual(decision.action, "answer")
        command = run.call_args.args[0]
        for expected in ("--ephemeral", "--ignore-user-config", "read-only", "--ignore-rules", "mcp_servers={}", "gpt-5.5"):
            self.assertIn(expected, command)
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], "isolated-codex-home")

    @patch.dict("os.environ", {"RNASEQ_AGENT_CODEX_HOME": "custom-codex-home"}, clear=False)
    def test_codex_home_can_be_configured(self) -> None:
        self.assertEqual(codex_home_path(), Path("custom-codex-home"))

    @patch("rnaseq_agent.llm.subprocess.run")
    def test_login_status_uses_isolated_home(self, run: MagicMock) -> None:
        run.return_value = SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr="")
        status = codex_login_status(executable=Path("codex.exe"), codex_home=Path("isolated-home"))
        self.assertEqual(status, "Logged in using ChatGPT")
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], "isolated-home")
