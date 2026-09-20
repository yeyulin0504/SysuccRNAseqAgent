from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import requests

from .model_disclosure import (
    MODEL_CONTEXT_SECRET_DETECTED,
    MODEL_EXACT_TOOL_CALL_REJECTED,
    MODEL_PROVIDER_REQUEST_FAILED,
    PreparedModelRequest,
    ProviderConfig,
    ProviderCredentials,
    ProviderEvent,
    ProviderIdentity,
    ProviderReply,
    ProviderRequestError,
)


MAX_EVENTS = 10_000
MAX_EVENT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TOOL_IDS = 32
MAX_TOOL_FRAGMENT_BYTES = 256 * 1024


@dataclass(frozen=True)
class SecretDetection:
    category: str
    section: str
    occurrence_id: str


def _secret_error(category: str) -> ProviderRequestError:
    occurrence_id = "evt_" + secrets.token_hex(16)
    return ProviderRequestError(
        f"模型请求包含禁止发送的凭据类别：{category}（事件 {occurrence_id}）。",
        code=MODEL_CONTEXT_SECRET_DETECTED,
        transmission_started=False,
        category=category,
        occurrence_id=occurrence_id,
    )


def scan_outbound_payload(serialized_payload: bytes, credentials: ProviderCredentials) -> None:
    """Fail closed when a serialized provider request contains credentials/secrets."""
    if not isinstance(serialized_payload, (bytes, bytearray)) or not serialized_payload:
        return
    text = bytes(serialized_payload).decode("utf-8", errors="replace")
    values: list[tuple[str, str]] = []
    if credentials.api_key:
        values.append(("known_secret", credentials.api_key))
    values.extend(("known_secret", value) for value in credentials.known_secrets if value)
    values.extend(("protected_value", value) for value in credentials.protected_values if value)
    if re.search(r"-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----", text):
        raise _secret_error("private_key_marker")
    if re.search(r"https?://[^\s/@]+:[^\s/@]+@", text, flags=re.IGNORECASE):
        raise _secret_error("url_userinfo")
    if re.search(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", text, flags=re.IGNORECASE):
        raise _secret_error("application_token")
    seen: set[tuple[str, str]] = set()
    for category, value in values:
        if (category, value) in seen:
            continue
        seen.add((category, value))
        if value in text:
            raise _secret_error(category)


def _request_error(message: str, *, started: bool, cause: Exception | None = None) -> ProviderRequestError:
    return ProviderRequestError(
        message,
        code=MODEL_PROVIDER_REQUEST_FAILED,
        transmission_started=started,
    )


def _bounded_json(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("provider response is not an object")
    return value


class ModelProviderGateway:
    """Single boundary for all generative provider transports."""

    def _prepare(self, request: PreparedModelRequest, *, exact: bool = False) -> tuple[bytes, dict[str, str]]:
        if request.identity != provider_identity(request.provider):
            raise ProviderRequestError("模型 provider 身份已变化。", code=MODEL_PROVIDER_REQUEST_FAILED, transmission_started=False)
        payload = dict(request.payload)
        if exact:
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            payload["stream"] = request.stream or request.api_mode == "chat_completions"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        scan_outbound_payload(body, request.credentials)
        headers = {"Content-Type": "application/json"}
        if request.credentials.api_key:
            headers["Authorization"] = f"Bearer {request.credentials.api_key}"
        return body, headers

    def complete(self, request: PreparedModelRequest) -> ProviderReply:
        body, headers = self._prepare(request)
        endpoint = request.provider.api_base.rstrip("/") + "/chat/completions"
        started = False
        try:
            started = True
            response = requests.post(endpoint, headers=headers, data=body, timeout=request.timeout_seconds)
            if getattr(response, "status_code", 0) < 200 or getattr(response, "status_code", 0) >= 300:
                raise _request_error(f"模型接口返回 HTTP {getattr(response, 'status_code', 0)}。", started=started)
            data = _bounded_json(response.json())
            choices = data.get("choices") or []
            message = choices[0].get("message") if choices and isinstance(choices[0], dict) else {}
            text = str((message or {}).get("content") or "")
            return ProviderReply(text=text, raw={"text": text})
        except ProviderRequestError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _request_error("模型请求失败。", started=started, cause=exc) from exc

    def stream(self, request: PreparedModelRequest):
        body, headers = self._prepare(request)
        endpoint = request.provider.api_base.rstrip("/") + "/chat/completions"
        started = False
        try:
            started = True
            response = requests.post(endpoint, headers=headers, data=body, timeout=request.timeout_seconds, stream=True)
            if getattr(response, "status_code", 0) < 200 or getattr(response, "status_code", 0) >= 300:
                raise _request_error(f"模型接口返回 HTTP {getattr(response, 'status_code', 0)}。", started=started)
        except ProviderRequestError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _request_error("模型请求失败。", started=started, cause=exc) from exc

        def iterator():
            sequence = 0
            total = 0
            content: list[str] = []
            try:
                for raw in response.iter_lines(decode_unicode=True):
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                    if len(line.encode("utf-8")) > MAX_EVENT_BYTES:
                        raise _request_error("模型响应帧超过大小限制。", started=True)
                    if not line.strip().startswith("data:"):
                        continue
                    data_text = line.split(":", 1)[1].strip()
                    if data_text == "[DONE]":
                        break
                    total += len(data_text.encode("utf-8"))
                    sequence += 1
                    if sequence > MAX_EVENTS or total > MAX_RESPONSE_BYTES:
                        raise _request_error("模型响应超过大小限制。", started=True)
                    try:
                        chunk = _bounded_json(json.loads(data_text))
                    except Exception as exc:  # noqa: BLE001
                        raise _request_error("模型响应格式无效。", started=True, cause=exc) from exc
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0] if isinstance(choices[0], dict) else {}
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        value = str(piece)
                        content.append(value)
                        yield ProviderEvent("delta", value, sequence)
                    for tool in delta.get("tool_calls") or []:
                        function = tool.get("function") if isinstance(tool, dict) else {}
                        yield ProviderEvent("tool_call_fragment", {"id": str((tool or {}).get("id") or ""), "name": str((function or {}).get("name") or ""), "arguments": str((function or {}).get("arguments") or "")}, sequence)
                yield ProviderEvent("message", {"content": "".join(content)}, sequence + 1)
            except ProviderRequestError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise _request_error("模型响应读取失败。", started=True, cause=exc) from exc
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        return iterator()

    def responses(self, request: PreparedModelRequest) -> ProviderReply:
        body, headers = self._prepare(request)
        endpoint = request.provider.api_base.rstrip("/") + "/responses"
        started = False
        try:
            started = True
            response = requests.post(endpoint, headers=headers, data=body, timeout=request.timeout_seconds)
            if getattr(response, "status_code", 0) < 200 or getattr(response, "status_code", 0) >= 300:
                raise _request_error(f"模型接口返回 HTTP {getattr(response, 'status_code', 0)}。", started=started)
            data = _bounded_json(response.json())
            text = str(data.get("output_text") or data.get("text") or "")
            return ProviderReply(text=text, raw={"text": text})
        except ProviderRequestError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _request_error("模型请求失败。", started=started, cause=exc) from exc

    def codex_exec(self, request: PreparedModelRequest) -> ProviderReply:
        prompt = request.prompt or str(request.payload.get("prompt") or "")
        scan_outbound_payload(prompt.encode("utf-8"), request.credentials)
        command = str(request.codex_executable or "codex")
        started = False
        try:
            started = True
            result = subprocess.run([command, "exec", "--model", request.provider.model], input=prompt, text=True, capture_output=True, timeout=request.timeout_seconds, check=False, cwd=str(request.codex_home) if request.codex_home else None)
            if result.returncode != 0:
                raise _request_error("Codex 请求失败。", started=started)
            output = str(result.stdout or "")
            if len(output.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise _request_error("模型响应超过大小限制。", started=started)
            try:
                parsed = json.loads(output)
                text = str(parsed.get("output_text") or parsed.get("text") or "") if isinstance(parsed, dict) else output
            except Exception:
                text = output
            return ProviderReply(text=text, raw={"text": text})
        except ProviderRequestError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _request_error("Codex 请求失败。", started=started, cause=exc) from exc

    def dispatch_exact(self, request: PreparedModelRequest):
        if not request.exact_attempt:
            raise ProviderRequestError("精确数据请求必须由已批准的一次性授权触发。", code=MODEL_EXACT_TOOL_CALL_REJECTED, transmission_started=False)
        exact = replace(request, payload={**dict(request.payload), "tools": None, "tool_choice": None}, exact_attempt=True)
        if exact.api_mode == "chat_completions":
            return self.stream(replace(exact, stream=True))
        if exact.api_mode == "responses":
            reply = self.responses(exact)
        elif exact.api_mode == "codex_cli":
            reply = self.codex_exec(exact)
        else:
            raise ProviderRequestError("不支持的精确请求模式。", code=MODEL_PROVIDER_REQUEST_FAILED, transmission_started=False)
        return iter((ProviderEvent("message", {"content": reply.text}, 1),))

    def list_models(self, config: ProviderConfig, credentials: ProviderCredentials, timeout_seconds: float) -> list[str]:
        endpoint = config.api_base.rstrip("/") + "/models"
        request = urllib.request.Request(endpoint, headers={"Authorization": f"Bearer {credentials.api_key}"} if credentials.api_key else {})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = _bounded_json(json.loads(response.read().decode("utf-8")))
            rows = data.get("data") or []
            return sorted(str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id"))
        except Exception as exc:  # noqa: BLE001
            raise _request_error("模型列表请求失败。", started=True, cause=exc) from exc


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _normalize_api_base(value: str, *, backend: str) -> str:
    raw = value.strip()
    if backend == "codex_cli":
        return "codex-cli"
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError("api_base must be an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError("api_base must not contain userinfo, query, or fragment")
    try:
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("api_base has an invalid host or port") from exc
    scheme = parts.scheme.lower()
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        authority = host
    else:
        authority = f"{host}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, authority, path, "", ""))


def normalize_provider_config(raw: Mapping[str, Any]) -> ProviderConfig:
    if not isinstance(raw, Mapping):
        raise ValueError("provider configuration must be a mapping")
    backend_raw = str(raw.get("backend") or "").strip().lower()
    if backend_raw in {"api", "openai", "openai_compatible", ""}:
        backend = "openai_compatible"
    elif backend_raw in {"codex", "codex_cli"}:
        backend = "codex_cli"
    else:
        raise ValueError("unsupported provider backend")
    provider = str(raw.get("provider") or ("codex" if backend == "codex_cli" else "openai")).strip().casefold()
    model = str(raw.get("model") or "").strip()
    if not provider or not model:
        raise ValueError("provider and model are required")
    api_base = _normalize_api_base(str(raw.get("api_base") or ("codex-cli" if backend == "codex_cli" else "")), backend=backend)
    mode_raw = str(raw.get("api_mode") or ("codex_cli" if backend == "codex_cli" else "chat_completions")).strip().lower()
    if backend == "codex_cli":
        mode = "codex_cli"
    elif mode_raw not in {"chat_completions", "responses"}:
        raise ValueError("unsupported provider API mode")
    else:
        mode = mode_raw
    return ProviderConfig(backend=backend, provider=provider, api_base=api_base, model=model, api_mode=mode)


def _origin(config: ProviderConfig) -> str:
    if config.backend == "codex_cli":
        return "codex-cli"
    parts = urlsplit(config.api_base)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def provider_identity(config: ProviderConfig) -> ProviderIdentity:
    origin = _origin(config)
    values = {"provider": config.provider, "origin": origin, "model": config.model}
    return ProviderIdentity(digest=_digest(values), provider=config.provider, origin=origin, model=config.model)


def provider_config_revision(config: ProviderConfig) -> str:
    return _digest({
        "backend": config.backend,
        "provider": config.provider,
        "api_base": config.api_base,
        "model": config.model,
        "api_mode": config.api_mode,
    })
