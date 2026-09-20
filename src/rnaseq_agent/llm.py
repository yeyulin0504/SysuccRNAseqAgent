from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model_disclosure import PreparedModelRequest, ProviderCredentials
from .model_provider import MAX_RESPONSE_BYTES, ModelProviderGateway, normalize_provider_config, provider_identity


ALLOWED_ACTIONS = {
    "new",
    "open",
    "validate",
    "run",
    "run-nowait",
    "status",
    "report",
    "explain",
    "summary",
    "where",
    "help",
    "quit",
    "chat",
}
API_MODES = {"auto", "chat_completions", "responses"}
LLM_BACKENDS = {"api", "codex_cli"}


@dataclass(frozen=True)
class LLMDecision:
    action: str
    message: str = ""
    path: str = ""


class LLMError(RuntimeError):
    pass


class OpenAICompatibleClient:
    """Minimal OpenAI-compatible client using only the Python standard library."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: int = 45,
        api_mode: str = "auto",
        gateway: ModelProviderGateway | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        normalized_mode = api_mode.strip().lower()
        self.api_mode = normalized_mode if normalized_mode in API_MODES else "auto"
        self.gateway = gateway or ModelProviderGateway()

    @property
    def provider_config(self):
        return self._provider_config()

    @classmethod
    def from_env(cls) -> OpenAICompatibleClient | None:
        base_url = os.environ.get("RNASEQ_AGENT_LLM_BASE_URL", "").strip()
        model = os.environ.get("RNASEQ_AGENT_LLM_MODEL", "").strip()
        api_key = os.environ.get("RNASEQ_AGENT_LLM_API_KEY", "").strip()
        api_mode = os.environ.get("RNASEQ_AGENT_LLM_API_MODE", "auto").strip()
        if not base_url or not model:
            return None

        timeout_text = os.environ.get("RNASEQ_AGENT_LLM_TIMEOUT", "45").strip()
        try:
            timeout_seconds = max(int(timeout_text), 1)
        except ValueError:
            timeout_seconds = 45
        return cls(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            api_mode=api_mode,
        )

    @property
    def resolved_api_mode(self) -> str:
        if self.api_mode != "auto":
            return self.api_mode
        if self._is_cc_switch_local():
            return "responses"
        return "chat_completions"

    def list_models(self) -> list[str]:
        """Return model IDs exposed by the provider's /models endpoint."""
        response = self._get_json("/models")
        items = response.get("data")
        if items is None:
            items = response.get("models")
        if not isinstance(items, list):
            raise LLMError("模型列表接口返回格式不正确：缺少 data 或 models 数组。")

        model_ids: set[str] = set()
        for item in items:
            if isinstance(item, str) and item.strip():
                model_ids.add(item.strip())
            elif isinstance(item, dict) and str(item.get("id", "")).strip():
                model_ids.add(str(item["id"]).strip())
        if not model_ids:
            if self._is_cc_switch_local():
                raise LLMError(
                    "CC Switch 当前返回空模型列表，请手动填写 Codex 中显示的模型名称。"
                )
            raise LLMError("模型列表接口没有返回可用模型（缺少 data 或 models 数组）。")
        return sorted(model_ids, key=str.casefold)

    def decide(self, user_text: str, *, has_project: bool) -> LLMDecision:
        system_prompt = _router_system_prompt(has_project)
        if self.resolved_api_mode == "responses":
            payload = {
                "model": self.model,
                "instructions": system_prompt,
                "input": user_text,
                "store": False,
            }
            response = self._post_json("/responses", payload)
            content = _extract_responses_text(response)
        else:
            payload = {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
            }
            response = self._post_json("/chat/completions", payload)
            try:
                content = response["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError("模型响应缺少 choices[0].message.content。") from exc
        return parse_decision(content)

    def _get_json(self, endpoint: str) -> dict[str, Any]:
        if endpoint != "/models":
            raise LLMError("不支持的模型 GET 接口。")
        provider = self._provider_config()
        try:
            models = self.gateway.list_models(
                provider,
                ProviderCredentials(api_key=self.api_key),
                self.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, LLMError):
                raise
            raise LLMError("模型列表接口请求失败。") from exc
        return {"data": models}

    def _post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if endpoint not in {"/responses", "/chat/completions"}:
            raise LLMError("不支持的模型 POST 接口。")
        mode = "responses" if endpoint == "/responses" else "chat_completions"
        provider = self._provider_config(api_mode=mode)
        request = PreparedModelRequest(
            provider=provider,
            identity=provider_identity(provider),
            api_mode=mode,
            payload=payload,
            credentials=ProviderCredentials(api_key=self.api_key),
            timeout_seconds=self.timeout_seconds,
        )
        try:
            reply = (self.gateway.responses(request) if mode == "responses" else self.gateway.complete(request))
        except Exception as exc:  # noqa: BLE001
            raise LLMError("模型接口请求失败。") from exc
        if mode == "responses":
            return {"output_text": reply.text}
        return {"choices": [{"message": {"content": reply.text}}]}

    def _provider_config(self, *, api_mode: str | None = None):
        return normalize_provider_config({
            "backend": "openai_compatible",
            "provider": "openai",
            "api_base": self.base_url,
            "model": self.model or "model-list",
            "api_mode": api_mode or self.resolved_api_mode,
        })

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _is_cc_switch_local(self) -> bool:
        parsed = urllib.parse.urlparse(self.base_url)
        return parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == 15721

class CodexCLIClient:
    """Use an already signed-in Codex CLI without reading its OAuth credentials."""

    base_url = "Codex CLI（使用现有 ChatGPT/Codex 登录）"
    resolved_api_mode = "codex_cli"

    def __init__(
        self,
        *,
        model: str,
        executable: Path | None = None,
        codex_home: Path | None = None,
        timeout_seconds: int = 90,
        gateway: ModelProviderGateway | None = None,
    ) -> None:
        self.model = model
        self.executable = executable or find_codex_executable()
        self.codex_home = codex_home or codex_home_path()
        self.timeout_seconds = timeout_seconds
        self.gateway = gateway or ModelProviderGateway()

    @property
    def provider_config(self):
        return normalize_provider_config({
            "backend": "codex_cli",
            "provider": "codex",
            "model": self.model or "codex-default",
            "api_mode": "codex_cli",
        })

    def decide(self, user_text: str, *, has_project: bool) -> LLMDecision:
        if self.executable is None:
            raise LLMError("没有找到 Codex CLI。请先安装 Codex。")
        self.codex_home.mkdir(parents=True, exist_ok=True)

        schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": sorted(ALLOWED_ACTIONS)},
                "message": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["action", "message", "path"],
            "additionalProperties": False,
        }
        prompt = (
            f"{_router_system_prompt(has_project)}\n"
            "Do not call tools, inspect files, or run commands. Answer immediately.\n"
            f"User input: {user_text}"
        )

        request = PreparedModelRequest(
            provider=self.provider_config,
            identity=provider_identity(self.provider_config),
            api_mode="codex_cli",
            payload={},
            credentials=ProviderCredentials(),
            timeout_seconds=self.timeout_seconds,
            prompt=prompt,
            response_schema=schema,
            codex_executable=self.executable,
            codex_home=self.codex_home,
        )
        try:
            reply = self.gateway.codex_exec(request)
        except Exception as exc:  # noqa: BLE001 - preserve the stable CLI error surface
            raise LLMError("Codex CLI 调用失败。") from exc
        return parse_decision(reply.text)


def find_codex_executable() -> Path | None:
    configured = os.environ.get("RNASEQ_AGENT_CODEX_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        bin_dir = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        candidates = list(bin_dir.glob("*/codex.exe"))
        if candidates:
            return max(candidates, key=lambda item: item.stat().st_mtime)

    discovered = shutil.which("codex")
    if discovered:
        return Path(discovered)
    return None


def codex_home_path() -> Path:
    """Return the isolated Codex home used only by this application."""
    configured = os.environ.get("RNASEQ_AGENT_CODEX_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".sysu_rnaseq_agent" / "codex_home"


def codex_login_status(
    *,
    executable: Path | None = None,
    codex_home: Path | None = None,
) -> str:
    """Return Codex login status without reading or exposing credentials."""
    resolved_executable = executable or find_codex_executable()
    if resolved_executable is None:
        raise LLMError("没有找到 Codex CLI。")
    resolved_home = codex_home or codex_home_path()
    resolved_home.mkdir(parents=True, exist_ok=True)
    process_env = os.environ.copy()
    process_env["CODEX_HOME"] = str(resolved_home)
    result = subprocess.run(
        [str(resolved_executable), "login", "status"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
        env=process_env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    message = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        return "未登录"
    if "api key" in message.lower():
        return "已使用 API Key 登录（不等同于 ChatGPT Plus）"
    return message or "已登录"


def launch_codex_device_login(
    *,
    executable: Path | None = None,
    codex_home: Path | None = None,
) -> subprocess.Popen[str]:
    """Open an isolated official Codex device-login flow in a new console."""
    resolved_executable = executable or find_codex_executable()
    if resolved_executable is None:
        raise LLMError("没有找到 Codex CLI。")
    resolved_home = codex_home or codex_home_path()
    resolved_home.mkdir(parents=True, exist_ok=True)
    process_env = os.environ.copy()
    process_env["CODEX_HOME"] = str(resolved_home)
    return subprocess.Popen(
        [str(resolved_executable), "login", "--device-auth"],
        cwd=resolved_home,
        env=process_env,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )


def load_llm_client_from_env() -> OpenAICompatibleClient | CodexCLIClient | None:
    backend = os.environ.get("RNASEQ_AGENT_LLM_BACKEND", "api").strip().lower()
    if backend == "codex_cli":
        model = os.environ.get("RNASEQ_AGENT_LLM_MODEL", "").strip()
        timeout_text = os.environ.get("RNASEQ_AGENT_LLM_TIMEOUT", "90").strip()
        try:
            timeout_seconds = max(int(timeout_text), 1)
        except ValueError:
            timeout_seconds = 90
        return CodexCLIClient(model=model, timeout_seconds=timeout_seconds)
    return OpenAICompatibleClient.from_env()


def _router_system_prompt(has_project: bool) -> str:
    return (
        "You are the intent router for a controlled RNA-seq analysis agent. "
        "Choose exactly one action from: new, open, validate, run, run-nowait, "
        "status, report, explain, summary, where, help, quit, chat. "
        "Never produce shell commands and never invent file paths. "
        "Use chat for general RNA-seq questions or when no safe action is appropriate. "
        "For chat, put a concise, cautious Chinese answer in message. Clearly distinguish "
        "artifact explanation from biological or clinical conclusions. "
        "If the user asks to open a project, copy the explicit project.json path "
        "into path; otherwise leave path empty. "
        "Return only JSON with keys action, message, path. "
        f"A project is currently bound: {has_project}."
    )


def _extract_responses_text(response: dict[str, Any]) -> str:
    if any(isinstance(response.get(key), list) and response.get(key) for key in ("tool_calls", "function_calls")):
        raise LLMError("Responses API 返回了不支持的工具调用。")
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        if len(direct.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise LLMError("Responses API 响应超过大小限制。")
        return direct.strip()

    output = response.get("output")
    if isinstance(output, list):
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            if item_type in {"function_call", "tool_call", "computer_call"}:
                raise LLMError("Responses API 返回了不支持的工具调用。")
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if str(part.get("type") or "").strip().lower() in {"function_call", "tool_call", "function_call_output"}:
                    raise LLMError("Responses API 返回了不支持的工具调用。")
                text = part.get("text")
                if part.get("type") == "output_text" and isinstance(text, str):
                    texts.append(text)
        if texts:
            result = "\n".join(texts).strip()
            if len(result.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise LLMError("Responses API 响应超过大小限制。")
            return result

    # Some compatible gateways translate Responses requests back to chat shape.
    try:
        translated = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        translated = None
    if isinstance(translated, str) and translated.strip():
        return translated.strip()
    raise LLMError("Responses API 响应中没有找到 output_text 内容。")


def _friendly_api_error(status_code: int, detail: str) -> str:
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        return detail[:300]

    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        return detail[:300]
    error_type = str(error.get("type", "")).strip()
    error_code = str(error.get("code", "")).strip()
    message = str(error.get("message", "")).strip()

    if status_code == 429 and (
        error_type == "insufficient_quota" or error_code == "insufficient_quota"
    ):
        return (
            "API 项目没有可用额度。ChatGPT Plus/Codex 订阅与 OpenAI Platform API "
            "额度分开；如果此 Key 来自 CC Switch 的代理服务，请改用该代理配置中的 "
            "Base URL，而不是 https://api.openai.com/v1。"
        )
    if status_code == 401:
        return (
            "API Key 无效或不属于当前 Base URL。请确认 Key 与服务商地址成对使用。"
        )
    return message[:300] or detail[:300]


def parse_decision(content: str) -> LLMDecision:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError("模型没有返回合法的动作 JSON。") from exc
    if not isinstance(payload, dict):
        raise LLMError("模型动作响应必须是 JSON 对象。")

    action = str(payload.get("action", "chat")).strip().lower()
    if action not in ALLOWED_ACTIONS:
        action = "chat"
    return LLMDecision(
        action=action,
        message=str(payload.get("message", "")).strip(),
        path=str(payload.get("path", "")).strip(),
    )
