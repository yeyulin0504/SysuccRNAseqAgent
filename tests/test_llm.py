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
    codex_home_path,
    codex_login_status,
    _extract_responses_text,
    _friendly_api_error,
    parse_decision,
)


class ParseDecisionTests(unittest.TestCase):
    def test_parses_allowed_action(self) -> None:
        decision = parse_decision(
            json.dumps(
                {
                    "action": "open",
                    "message": "打开已有项目",
                    "path": "runs/demo/project.json",
                }
            )
        )
        self.assertEqual(decision.action, "open")
        self.assertEqual(decision.path, "runs/demo/project.json")

    def test_strips_markdown_fence(self) -> None:
        decision = parse_decision('```json\n{"action":"status","message":"","path":""}\n```')
        self.assertEqual(decision.action, "status")

    def test_unknown_action_falls_back_to_chat(self) -> None:
        decision = parse_decision('{"action":"shell","message":"no","path":""}')
        self.assertEqual(decision.action, "chat")

    def test_rejects_non_json(self) -> None:
        with self.assertRaises(LLMError):
            parse_decision("run the project")


class ModelDiscoveryTests(unittest.TestCase):
    @patch("rnaseq_agent.llm.urllib.request.urlopen")
    def test_lists_and_sorts_provider_models(self, urlopen: MagicMock) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "data": [
                    {"id": "model-z"},
                    {"id": "model-a"},
                    {"id": "model-a"},
                ]
            }
        ).encode("utf-8")
        urlopen.return_value = response
        client = OpenAICompatibleClient(
            base_url="https://api.example.com/v1/",
            model="",
            api_key="secret",
        )

        models = client.list_models()

        self.assertEqual(models, ["model-a", "model-z"])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.example.com/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

    @patch("rnaseq_agent.llm.urllib.request.urlopen")
    def test_rejects_invalid_model_list_shape(self, urlopen: MagicMock) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"wrong":[]}'
        urlopen.return_value = response
        client = OpenAICompatibleClient(
            base_url="https://api.example.com/v1",
            model="",
        )

        with self.assertRaisesRegex(LLMError, "data"):
            client.list_models()

    def test_explains_insufficient_quota_in_plain_language(self) -> None:
        detail = json.dumps(
            {
                "error": {
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                    "message": "You exceeded your current quota.",
                }
            }
        )

        message = _friendly_api_error(429, detail)

        self.assertIn("ChatGPT Plus/Codex", message)
        self.assertIn("Base URL", message)


class ResponsesAPITests(unittest.TestCase):
    def test_cc_switch_auto_selects_responses(self) -> None:
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:15721/v1",
            model="gpt-5.5",
            api_key="provider-key",
            api_mode="auto",
        )

        self.assertEqual(client.resolved_api_mode, "responses")
        self.assertEqual(
            client._headers()["Authorization"],
            "Bearer provider-key",
        )

    @patch.object(OpenAICompatibleClient, "_post_json")
    def test_decide_uses_responses_endpoint(self, post_json: MagicMock) -> None:
        post_json.return_value = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"action":"help","message":"","path":""}',
                        }
                    ],
                }
            ]
        }
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:15721/v1",
            model="gpt-5.5",
            api_mode="auto",
        )

        decision = client.decide("你能做什么？", has_project=False)

        self.assertEqual(decision.action, "help")
        endpoint, payload = post_json.call_args.args
        self.assertEqual(endpoint, "/responses")
        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertNotIn("temperature", payload)

    def test_extracts_direct_responses_output_text(self) -> None:
        self.assertEqual(
            _extract_responses_text({"output_text": "hello"}),
            "hello",
        )


class CodexCLIClientTests(unittest.TestCase):
    @patch("rnaseq_agent.llm.subprocess.run")
    def test_uses_ephemeral_read_only_codex_exec(self, run: MagicMock) -> None:
        def fake_run(command: list[str], **_: object) -> SimpleNamespace:
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(
                '{"action":"summary","message":"","path":""}',
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        run.side_effect = fake_run
        client = CodexCLIClient(
            model="gpt-5.5",
            executable=Path("codex.exe"),
            codex_home=Path("isolated-codex-home"),
        )

        decision = client.decide("总结项目", has_project=True)

        self.assertEqual(decision.action, "summary")
        command = run.call_args.args[0]
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("read-only", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("mcp_servers={}", command)
        self.assertIn("gpt-5.5", command)
        self.assertEqual(
            run.call_args.kwargs["env"]["CODEX_HOME"],
            "isolated-codex-home",
        )

    @patch.dict(
        "os.environ",
        {"RNASEQ_AGENT_CODEX_HOME": "custom-codex-home"},
        clear=False,
    )
    def test_codex_home_can_be_configured(self) -> None:
        self.assertEqual(codex_home_path(), Path("custom-codex-home"))

    @patch("rnaseq_agent.llm.subprocess.run")
    def test_login_status_uses_isolated_home(self, run: MagicMock) -> None:
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout="Logged in using ChatGPT",
            stderr="",
        )
        status = codex_login_status(
            executable=Path("codex.exe"),
            codex_home=Path("isolated-home"),
        )
        self.assertEqual(status, "Logged in using ChatGPT")
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], "isolated-home")


if __name__ == "__main__":
    unittest.main()
