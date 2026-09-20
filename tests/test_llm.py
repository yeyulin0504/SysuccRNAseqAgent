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
from rnaseq_agent.model_disclosure import ProviderReply


class FakeGateway:
    def __init__(self, reply: ProviderReply | None = None, models=None) -> None:
        self.reply = reply
        self.models = list(models or [])
        self.requests = []

    def list_models(self, config, credentials, timeout_seconds):
        self.requests.append((config, credentials, timeout_seconds))
        return self.models

    def complete(self, request):
        self.requests.append(request)
        return self.reply

    def responses(self, request):
        self.requests.append(request)
        return self.reply

    def codex_exec(self, request):
        self.requests.append(request)
        return self.reply


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
    def test_lists_and_sorts_provider_models(self) -> None:
        gateway = FakeGateway(models=["model-z", "model-a", "model-a"])
        client = OpenAICompatibleClient(
            base_url="https://api.example.com/v1/",
            model="",
            api_key="secret",
            gateway=gateway,
        )

        models = client.list_models()

        self.assertEqual(models, ["model-a", "model-z"])
        config, credentials, _timeout = gateway.requests[0]
        self.assertEqual(config.api_base, "https://api.example.com/v1")
        self.assertEqual(credentials.api_key, "secret")

    def test_rejects_invalid_model_list_shape(self) -> None:
        gateway = FakeGateway(models=[])
        client = OpenAICompatibleClient(
            base_url="https://api.example.com/v1",
            model="",
            gateway=gateway,
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
    def test_decide_uses_injected_gateway_for_responses(self) -> None:
        gateway = FakeGateway(ProviderReply(
            text='{"action":"chat","message":"ok","path":""}', raw={}
        ))
        client = OpenAICompatibleClient(
            base_url="https://llm.example/v1", model="gpt-test",
            api_key="API_KEY_SENTINEL_73", api_mode="responses", gateway=gateway,
        )
        self.assertEqual(client.decide("你好", has_project=False).message, "ok")
        request = gateway.requests[0]
        self.assertEqual(request.api_mode, "responses")
        self.assertFalse(request.payload["store"])
        self.assertEqual(request.credentials.api_key, "API_KEY_SENTINEL_73")
        self.assertNotIn("API_KEY_SENTINEL_73", json.dumps(request.payload))

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
    def test_decide_uses_injected_gateway_for_codex(self) -> None:
        gateway = FakeGateway(ProviderReply(
            text='{"action":"summary","message":"ok","path":""}', raw={}
        ))
        client = CodexCLIClient(
            model="gpt-test", executable=Path("codex"), gateway=gateway,
        )
        self.assertEqual(client.decide("总结项目", has_project=True).action, "summary")
        self.assertEqual(gateway.requests[0].api_mode, "codex_cli")
        self.assertIn("总结项目", gateway.requests[0].prompt)

    def test_uses_gateway_for_codex_exec(self) -> None:
        gateway = FakeGateway(ProviderReply(
            text='{"action":"summary","message":"","path":""}', raw={}
        ))
        client = CodexCLIClient(
            model="gpt-5.5",
            executable=Path("codex.exe"),
            codex_home=Path("isolated-codex-home"),
            gateway=gateway,
        )

        decision = client.decide("总结项目", has_project=True)

        self.assertEqual(decision.action, "summary")
        request = gateway.requests[0]
        self.assertEqual(request.api_mode, "codex_cli")
        self.assertEqual(request.codex_executable, Path("codex.exe"))
        self.assertEqual(request.codex_home, Path("isolated-codex-home"))
        self.assertEqual(request.provider.model, "gpt-5.5")

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
