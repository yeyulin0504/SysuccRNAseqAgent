"""跨项目共享的服务器连接配置（配一次，所有项目复用）。

设计目标（用户需求 2026-09-14）：

- **配一次**：连接信息只填一次，之后新建的任何项目自动继承；
- **永久保存**：进程重启后仍然生效，不需要重新输入密码。

落点是一个用户级配置文件，默认 ``~/.rnaseq_agent/connection.json``；
可用环境变量 ``RNASEQ_AGENT_HOME`` 覆盖其所在目录（测试与便携部署用）。

密码处理：在 Windows 上用 DPAPI（``CryptProtectData``）加密，密文只能被
当前 Windows 用户解密，把文件拷到别的机器也无法还原明文；其他平台在
没有系统密钥环时退化为「仅存连接信息、不存密码」，并在返回值里标记。
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

CONNECTION_FILE_NAME = "connection.json"
HOME_ENV_VAR = "RNASEQ_AGENT_HOME"
DEFAULT_HOME_DIR_NAME = ".rnaseq_agent"

# 可跨项目共享、允许落盘的连接字段（密码单独加密处理）。
SHARED_FIELDS = (
    "profile",
    "host",
    "user",
    "port",
    "scheduler",
    "threads",
    "memory_gb",
    "remote_base_dir",
    "remote_workdir",
    "shell",
    "auth_mode",
)

_PASSWORD_KEY = "password_protected"


def connection_home() -> Path:
    """Directory that holds the user-level connection file."""
    override = os.environ.get(HOME_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_HOME_DIR_NAME


def connection_file_path() -> Path:
    return connection_home() / CONNECTION_FILE_NAME


def _protect(secret: str) -> str:
    """Encrypt a secret for storage on this machine/user."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        def _blob(data: bytes) -> DATA_BLOB:
            buffer = ctypes.create_string_buffer(data, len(data))
            return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_blob = _blob(secret.encode("utf-8"))
        output_blob = DATA_BLOB()
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
        )
        if not ok:
            raise OSError("CryptProtectData failed")
        try:
            protected = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)
        return "dpapi:" + base64.b64encode(protected).decode("ascii")

    # 非 Windows：没有系统密钥环可用时，拒绝把明文密码写进文件。
    raise NotImplementedError("no OS keyring backend for this platform")


def _unprotect(token: str) -> str:
    if not token.startswith("dpapi:"):
        return ""
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes) -> DATA_BLOB:
        buffer = ctypes.create_string_buffer(data, len(data))
        return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    raw = base64.b64decode(token[len("dpapi:"):])
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    input_blob = _blob(raw)
    output_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
    )
    if not ok:
        return ""
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _resolve_store_dir(store_dir: Path | None) -> Path:
    return Path(store_dir) if store_dir is not None else connection_home()


def load_connection(*, store_dir: Path | None = None) -> dict[str, Any]:
    """Return the stored connection, with the password decrypted when possible."""
    path = _resolve_store_dir(store_dir) / CONNECTION_FILE_NAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result = {k: v for k, v in payload.items() if k in SHARED_FIELDS and v is not None}
    token = payload.get(_PASSWORD_KEY)
    if isinstance(token, str) and token:
        secret = _unprotect(token)
        if secret:
            result["password"] = secret
    return result


def save_connection(values: dict[str, Any], *, store_dir: Path | None = None) -> dict[str, Any]:
    """Merge ``values`` into the stored connection and persist it.

    Secret handling: a non-empty ``password`` is encrypted; omitting the key
    keeps whatever was stored; passing an empty string clears it.
    """
    directory = _resolve_store_dir(store_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / CONNECTION_FILE_NAME

    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing = raw
        except (OSError, ValueError):
            existing = {}

    merged = dict(existing)
    auth_mode = str(values.get("auth_mode") or merged.get("auth_mode") or "key")
    for key in SHARED_FIELDS:
        if key in values and values[key] is not None:
            merged[key] = values[key]

    if "password" in values:
        new_password = str(values.get("password") or "")
        if auth_mode == "password" and new_password:
            merged[_PASSWORD_KEY] = _protect(new_password)
        elif not new_password and auth_mode != "password":
            merged.pop(_PASSWORD_KEY, None)
    if auth_mode != "password":
        merged.pop(_PASSWORD_KEY, None)

    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return load_connection(store_dir=store_dir)


def clear_password(*, store_dir: Path | None = None) -> None:
    """Forget the stored password while keeping the rest of the connection."""
    directory = _resolve_store_dir(store_dir)
    path = directory / CONNECTION_FILE_NAME
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    payload.pop(_PASSWORD_KEY, None)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_connection_to_config(
    config: dict[str, Any], connection: dict[str, Any]
) -> dict[str, Any]:
    """Return a copy of ``config`` whose ``server`` block inherits ``connection``.

    The connection provides the shared infrastructure (host/user/scheduler/…);
    project-specific paths already in ``config`` are preserved when the
    connection does not define them. Secrets are never written into the config.
    """
    from copy import deepcopy

    merged = deepcopy(config)
    if not connection:
        return merged
    server = merged.setdefault("server", {})
    if not isinstance(server, dict):
        server = {}
        merged["server"] = server
    for key in SHARED_FIELDS:
        value = connection.get(key)
        if value is None or value == "":
            continue
        server[key] = value
    return merged
