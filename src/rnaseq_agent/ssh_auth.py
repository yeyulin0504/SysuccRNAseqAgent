"""SSH 认证方式的进程内凭据存储。

注意：WEB 前端 settings.html 的认证分段按钮用 ``pass`` 作为内部值，
而这里规范化的模式是 ``password``。``normalize_auth_mode`` 负责把 ``pass``
别名映射为 ``password``，避免静默降级成密钥模式导致密码被丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from .ssh_identity import normalize_ssh_host


AUTH_MODES = {"key", "password", "system"}
AUTH_MODE_ALIASES = {"pass": "password"}


def normalize_auth_mode(mode: str | None) -> str:
    """把前端别名（``pass``）与未知值归一化为规范认证模式。

    未知/空值回退为 ``key``，保证 ``SSHCredential.mode`` 始终是可判别值。
    """
    if mode is None:
        return "key"
    if mode in AUTH_MODE_ALIASES:
        return AUTH_MODE_ALIASES[mode]
    return mode if mode in AUTH_MODES else "key"


@dataclass(frozen=True)
class SSHCredential:
    mode: str = "key"
    password: str = ""
    key_path: str = ""


_credentials: dict[tuple[str, str], SSHCredential] = {}
_lock = Lock()


def _credential_key(host: str, user: str) -> tuple[str, str]:
    host_text = host.strip()
    canonical_host = normalize_ssh_host(host_text) if host_text else ""
    return canonical_host, user.strip()


def set_ssh_credential(
    host: str,
    user: str,
    *,
    mode: str,
    password: str = "",
    key_path: str = "",
) -> None:
    normalized_mode = normalize_auth_mode(mode)
    credential = SSHCredential(
        mode=normalized_mode,
        password=password if normalized_mode == "password" else "",
        key_path=str(Path(key_path).expanduser()) if key_path else "",
    )
    with _lock:
        _credentials[_credential_key(host, user)] = credential


def get_ssh_credential(host: str, user: str) -> SSHCredential:
    with _lock:
        return _credentials.get(
            _credential_key(host, user),
            SSHCredential(),
        )


def clear_ssh_credential(host: str, user: str) -> None:
    with _lock:
        _credentials.pop(_credential_key(host, user), None)
