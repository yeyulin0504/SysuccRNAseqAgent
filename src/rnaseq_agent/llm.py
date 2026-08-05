from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        normalized_mode = api_mode.strip().lower()
        self.api_mode = normalized_mode if normalized_mode in API_MODES else "auto"

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
            raise LLMError("模型列表接口没有返回可用模型。")
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
        request = urllib.request.Request(
            f"{self.base_url}{endpoint}",
            headers=self._headers(),
            method="GET",
        )
        return self._open_json(request, operation="模型列表接口")

    def _post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{endpoint}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        return self._open_json(request, operation="模型接口")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _is_cc_switch_local(self) -> bool:
        parsed = urllib.parse.urlparse(self.base_url)
        return parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == 15721

    def _open_json(
        self,
        request: urllib.request.Request,
        *,
        operation: str,
    ) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            content_type = exc.headers.get("Content-Type", "")
            if exc.code == 401 and self._is_cc_switch_local():
                detail = (
                    "CC Switch 的 Codex 官方路由需要 Codex 客户端内部的 OAuth 凭证。"
                    "普通 API Key、ChatGPT Plus 订阅或空 Key 都不能代替该凭证；"
                    "请不要从 auth.json 复制登录令牌。可改用独立 API 服务或本地模型。"
                )
            elif "text/html" in content_type.lower() or detail.lstrip().lower().startswith("<html"):
                detail = (
                    "服务器返回了网页而不是 API JSON，请检查 Base URL 是否为 API 地址"
                    "（OpenAI 官方地址为 https://api.openai.com/v1）。"
                )
            else:
                detail = _friendly_api_error(exc.code, detail)
            raise LLMError(f"{operation}返回 HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(f"无法连接{operation}：{exc}") from exc

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{operation}返回的内容不是合法 JSON。") from exc
        if not isinstance(decoded, dict):
            raise LLMError(f"{operation}返回格式不正确。")
        return decoded


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
    ) -> None:
        self.model = model
        self.executable = executable or find_codex_executable()
        self.codex_home = codex_home or codex_home_path()
        self.timeout_seconds = timeout_seconds

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

        with tempfile.TemporaryDirectory(prefix="rnaseq-agent-codex-") as temp_name:
            temp_dir = Path(temp_name)
            schema_path = temp_dir / "decision.schema.json"
            output_path = temp_dir / "decision.json"
            schema_path.write_text(
                json.dumps(schema, ensure_ascii=False),
                encoding="utf-8",
            )
            command = [
                str(self.executable),
                "exec",
                "--ignore-user-config",
                "--ephemeral",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--cd",
                str(temp_dir),
                "--config",
                "mcp_servers={}",
            ]
            if self.model:
                command.extend(["--model", self.model])
            command.append(prompt)
            try:
                process_env = os.environ.copy()
                process_env["CODEX_HOME"] = str(self.codex_home)
                result = subprocess.run(
                    command,
                    cwd=temp_dir,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    check=False,
                    env=process_env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired as exc:
                raise LLMError(
                    f"Codex CLI 在 {self.timeout_seconds} 秒内没有返回。"
                ) from exc
            except OSError as exc:
                raise LLMError(f"无法启动 Codex CLI：{exc}") from exc

            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-1200:]
                if "401" in detail or "not logged in" in detail.lower():
                    raise LLMError(
                        "RNA-seq Agent 尚未完成独立的 ChatGPT 登录。"
                        "请在“模型”页点击“登录 ChatGPT”，完成浏览器/设备授权后再测试。"
                    )
                raise LLMError(
                    f"Codex CLI 调用失败（退出码 {result.returncode}）：{detail}"
                )
            if not output_path.exists():
                raise LLMError("Codex CLI 没有生成决策结果。")
            return parse_decision(output_path.read_text(encoding="utf-8"))


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
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    output = response.get("output")
    if isinstance(output, list):
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if part.get("type") == "output_text" and isinstance(text, str):
                    texts.append(text)
        if texts:
            return "\n".join(texts).strip()

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
