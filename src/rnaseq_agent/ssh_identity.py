from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Mapping


_DNS_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_USER_RE = re.compile(r"[A-Za-z0-9._-]+")


class SSHIdentityError(ValueError):
    """Raised when an SSH destination cannot be represented safely."""


@dataclass(frozen=True)
class SSHIdentity:
    host: str
    user: str
    port: int


def _normalized_host(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        if value in (None, ""):
            return None, "SSH 主机不能为空。"
        return None, "SSH 主机格式无效。"
    host = value
    if not host:
        return None, "SSH 主机不能为空。"

    if host.startswith("[") or host.endswith("]"):
        if not (host.startswith("[") and host.endswith("]")):
            return None, "SSH 主机格式无效。"
        try:
            return str(ipaddress.IPv6Address(host[1:-1])), None
        except ipaddress.AddressValueError:
            return None, "SSH 主机格式无效。"

    if ":" in host:
        try:
            return str(ipaddress.IPv6Address(host)), None
        except ipaddress.AddressValueError:
            return None, "SSH 主机格式无效。"

    if all(character.isdigit() or character == "." for character in host):
        if len(host.split(".")) != 4:
            return None, "SSH 主机格式无效。"
        try:
            return str(ipaddress.IPv4Address(host)), None
        except ipaddress.AddressValueError:
            return None, "SSH 主机格式无效。"

    if len(host) > 253 or not host.isascii():
        return None, "SSH 主机格式无效。"
    labels = host.split(".")
    if any(_DNS_LABEL_RE.fullmatch(label) is None for label in labels):
        return None, "SSH 主机格式无效。"
    return host, None


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
        _, error = _normalized_host(values.get("host"))
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
    host, host_error = _normalized_host(server.get("host"))
    user_error = _user_error(server.get("user"))
    port, port_error = _normalized_port(server.get("port"), default_missing=True)
    problems = [
        error for error in (host_error, user_error, port_error) if error is not None
    ]
    if problems:
        raise SSHIdentityError("；".join(problems))
    assert host is not None
    assert port is not None
    return SSHIdentity(
        host=host,
        user=str(server.get("user") or ""),
        port=port,
    )
