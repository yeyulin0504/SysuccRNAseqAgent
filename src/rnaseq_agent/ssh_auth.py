from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock


AUTH_MODES = {"key", "password", "system"}


@dataclass(frozen=True)
class SSHCredential:
    mode: str = "key"
    password: str = ""
    key_path: str = ""


_credentials: dict[tuple[str, str], SSHCredential] = {}
_lock = Lock()


def set_ssh_credential(
    host: str,
    user: str,
    *,
    mode: str,
    password: str = "",
    key_path: str = "",
) -> None:
    normalized_mode = mode if mode in AUTH_MODES else "key"
    credential = SSHCredential(
        mode=normalized_mode,
        password=password if normalized_mode == "password" else "",
        key_path=str(Path(key_path).expanduser()) if key_path else "",
    )
    with _lock:
        _credentials[(host.strip(), user.strip())] = credential


def get_ssh_credential(host: str, user: str) -> SSHCredential:
    with _lock:
        return _credentials.get(
            (host.strip(), user.strip()),
            SSHCredential(),
        )


def clear_ssh_credential(host: str, user: str) -> None:
    with _lock:
        _credentials.pop((host.strip(), user.strip()), None)
