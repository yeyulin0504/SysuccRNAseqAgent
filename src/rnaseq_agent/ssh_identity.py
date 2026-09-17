from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


_HOST_RE = re.compile(r"[A-Za-z0-9.:\[\]-]+")
_USER_RE = re.compile(r"[A-Za-z0-9._-]+")


class SSHIdentityError(ValueError):
    """Raised when an SSH destination cannot be represented safely."""


@dataclass(frozen=True)
class SSHIdentity:
    host: str
    user: str
    port: int


def _host_error(value: Any) -> str | None:
    host = str(value or "")
    if not host:
        return "SSH 主机不能为空。"
    if host.startswith("-") or _HOST_RE.fullmatch(host) is None:
        return "SSH 主机格式无效。"
    return None


def _user_error(value: Any) -> str | None:
    user = str(value or "")
    if not user:
        return "SSH 用户名不能为空。"
    if user.startswith("-") or _USER_RE.fullmatch(user) is None:
        return "SSH 用户名格式无效。"
    return None


def _normalized_port(value: Any, *, default_missing: bool) -> tuple[int | None, str | None]:
    if value in (None, "") and not isinstance(value, bool):
        return (22, None) if default_missing else (None, None)
    if isinstance(value, bool):
        return None, "SSH 端口必须是 1 到 65535 的整数。"
    if isinstance(value, str):
        text = value.strip()
        if not text or not text.isascii() or not text.isdigit():
            return None, "SSH 端口必须是 1 到 65535 的整数。"
        port = int(text)
    elif isinstance(value, int):
        port = value
    else:
        return None, "SSH 端口必须是 1 到 65535 的整数。"
    if not 1 <= port <= 65535:
        return None, "SSH 端口必须在 1 到 65535 之间。"
    return port, None


def validate_ssh_patch(values: Mapping[str, Any]) -> list[str]:
    """Validate only SSH identity fields present in a partial settings patch."""
    problems: list[str] = []
    if "host" in values:
        error = _host_error(values.get("host"))
        if error:
            problems.append(error)
    if "user" in values:
        error = _user_error(values.get("user"))
        if error:
            problems.append(error)
    if "port" in values:
        _, error = _normalized_port(values.get("port"), default_missing=False)
        if error:
            problems.append(error)
    return problems


def normalize_ssh_identity(server: Mapping[str, Any]) -> SSHIdentity:
    """Return a strict, process-safe SSH destination identity."""
    problems = validate_ssh_patch(
        {
            "host": server.get("host"),
            "user": server.get("user"),
            "port": server.get("port"),
        }
    )
    port, port_error = _normalized_port(server.get("port"), default_missing=True)
    if port_error and port_error not in problems:
        problems.append(port_error)
    if problems:
        raise SSHIdentityError("；".join(problems))
    assert port is not None
    return SSHIdentity(
        host=str(server.get("host") or ""),
        user=str(server.get("user") or ""),
        port=port,
    )
