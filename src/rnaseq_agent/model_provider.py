from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .model_disclosure import ProviderConfig, ProviderIdentity


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
