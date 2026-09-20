"""跨项目共享的服务器连接与大模型配置（配一次，所有项目复用）。

设计目标（用户需求 2026-09-14）：

- **配一次**：服务器连接与大模型接入只填一次，之后任何项目自动继承；
- **永久保存**：进程重启后仍然生效，不需要重新输入密码 / API Key。

落点是一个用户级配置文件，默认 ``~/.rnaseq_agent/connection.json``；
可用环境变量 ``RNASEQ_AGENT_HOME`` 覆盖其所在目录（测试与便携部署用）。
服务器连接字段平铺在顶层，大模型配置放在 ``llm`` 子对象里。

密码与 API Key 的处理：在 Windows 上用 DPAPI（``CryptProtectData``）加密，
密文只能被当前 Windows 用户解密，把文件拷到别的机器也无法还原明文；其他
平台在没有系统密钥环时退化为「仅存非敏感字段、不存秘密」。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterator, Sequence
from typing import Any
from pathlib import PurePosixPath

from .storage import ConnectionStoreCorruptError, locked_json_transaction
from .ssh_identity import SSHIdentity, SSHIdentityError, normalize_ssh_identity

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

# 大模型接入（OpenAI 兼容端点）同样是用户级共享配置：存在同一个文件的
# ``llm`` 子对象里，API Key 与密码用同一套 DPAPI 加密。
LLM_BLOCK_KEY = "llm"
LLM_FIELDS = ("enabled", "provider", "api_base", "model", "tool_mode")
_LLM_API_KEY_KEY = "api_key_protected"


class BrowsePolicyConflictError(RuntimeError):
    """Raised when a root mutation uses a stale browse-policy revision."""

    error_code = "REMOTE_ROOT_REVISION_CONFLICT"


@dataclass(frozen=True)
class ApprovedDataRoot:
    root_id: str
    host: str
    user: str
    port: int
    requested_path: str
    canonical_path: str
    created_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class BrowsePolicy:
    identity: SSHIdentity | None
    roots: tuple[ApprovedDataRoot, ...]
    revision: str
    available: bool


def browse_policy_revision(
    identity: SSHIdentity | None,
    active_roots: Sequence[ApprovedDataRoot],
) -> str:
    payload = {
        "identity": None if identity is None else {
            "host": identity.host, "user": identity.user, "port": identity.port,
        },
        "roots": [asdict(root) for root in sorted(active_roots, key=lambda item: item.root_id)],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


MAX_ACTIVE_ROOTS = 32
_ROOT_FIELDS = frozenset(
    {
        "root_id",
        "host",
        "user",
        "port",
        "requested_path",
        "canonical_path",
        "created_at",
        "revoked_at",
    }
)


def _invalid_policy() -> BrowsePolicy:
    return BrowsePolicy(
        identity=None,
        roots=(),
        revision=browse_policy_revision(None, ()),
        available=False,
    )


def _validate_timestamp(value: Any, *, allow_none: bool) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("root timestamp must be a non-empty string")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("root timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("root timestamp must include timezone")
    return value


def _validate_root_path(value: Any, field: str, *, reject_root: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field} must be an absolute POSIX path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must be an absolute POSIX path")
    try:
        path = PurePosixPath(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an absolute POSIX path") from exc
    if not path.is_absolute() or "\\" in value or any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"{field} must be an absolute POSIX path")
    if reject_root and path == PurePosixPath("/"):
        raise ValueError("root path '/' is not approvable")
    return value


def _parse_root_record(value: Any) -> ApprovedDataRoot:
    if not isinstance(value, dict) or set(value) != _ROOT_FIELDS:
        raise ValueError("approved_data_roots contains an invalid record")
    root_id = value.get("root_id")
    host = value.get("host")
    user = value.get("user")
    port = value.get("port")
    if not isinstance(root_id, str) or not root_id.strip():
        raise ValueError("root_id must be a non-empty string")
    if not isinstance(host, str) or not host:
        raise ValueError("root host must be a non-empty string")
    if not isinstance(user, str) or not user:
        raise ValueError("root user must be a non-empty string")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("root port must be an integer from 1 to 65535")
    try:
        normalized = normalize_ssh_identity({"host": host, "user": user, "port": port})
    except SSHIdentityError as exc:
        raise ValueError("approved_data_roots contains an invalid SSH identity") from exc
    if (normalized.host, normalized.user, normalized.port) != (host, user, port):
        raise ValueError("approved_data_roots contains a non-normalized SSH identity")
    requested_path = _validate_root_path(value.get("requested_path"), "requested_path", reject_root=True)
    canonical_path = _validate_root_path(value.get("canonical_path"), "canonical_path", reject_root=True)
    created_at = _validate_timestamp(value.get("created_at"), allow_none=False)
    revoked_at = _validate_timestamp(value.get("revoked_at"), allow_none=True)
    assert created_at is not None
    return ApprovedDataRoot(
        root_id=root_id,
        host=host,
        user=user,
        port=port,
        requested_path=requested_path,
        canonical_path=canonical_path,
        created_at=created_at,
        revoked_at=revoked_at,
    )


def _parse_roots(payload: dict[str, Any]) -> tuple[ApprovedDataRoot, ...]:
    raw = payload.get("approved_data_roots", [])
    if raw is None or not isinstance(raw, list):
        raise ValueError("approved_data_roots must be a list")
    roots: list[ApprovedDataRoot] = []
    seen_ids: set[str] = set()
    for item in raw:
        root = _parse_root_record(item)
        if root.root_id in seen_ids:
            raise ValueError("approved_data_roots contains duplicate root_id")
        seen_ids.add(root.root_id)
        roots.append(root)
    return tuple(roots)


def _active_roots_for_identity(
    roots: Sequence[ApprovedDataRoot], identity: SSHIdentity | None,
) -> tuple[ApprovedDataRoot, ...]:
    if identity is None:
        return ()
    return tuple(
        root
        for root in roots
        if root.revoked_at is None
        and (root.host, root.user, root.port) == (identity.host, identity.user, identity.port)
    )


def _policy_from_payload(payload: dict[str, Any]) -> BrowsePolicy:
    try:
        identity = normalize_ssh_identity(payload)
        roots = _parse_roots(payload)
        if sum(root.revoked_at is None for root in roots) > MAX_ACTIVE_ROOTS:
            raise ValueError("too many active roots")
        active = _active_roots_for_identity(roots, identity)
        return BrowsePolicy(
            identity=identity,
            roots=tuple(sorted(active, key=lambda item: item.root_id)),
            revision=browse_policy_revision(identity, active),
            available=True,
        )
    except (SSHIdentityError, TypeError, ValueError, KeyError):
        return _invalid_policy()


def _read_browse_payload(directory: Path) -> tuple[dict[str, Any], bool, bool]:
    try:
        from .private_files import verify_private_path

        if directory.exists() or directory.is_symlink():
            verify_private_path(directory)
        payload, _exists, valid_payload = _read_payload_state(directory)
        path = directory / CONNECTION_FILE_NAME
        if path.exists() or path.is_symlink():
            verify_private_path(path)
    except (OSError, PermissionError):
        return {}, True, False
    return payload, _exists, valid_payload


def load_browse_policy(*, store_dir: Path | None = None) -> BrowsePolicy:
    directory = _resolve_store_dir(store_dir)
    payload, _exists, valid_payload = _read_browse_payload(directory)
    if not valid_payload:
        return _invalid_policy()
    return _policy_from_payload(payload)


@contextmanager
def locked_browse_policy(*, store_dir: Path | None = None) -> Iterator[BrowsePolicy]:
    directory = _resolve_store_dir(store_dir)
    path = directory / CONNECTION_FILE_NAME
    from .storage import project_state_lock

    with project_state_lock(path):
        payload, _exists, valid_payload = _read_browse_payload(directory)
        yield _policy_from_payload(payload) if valid_payload else _invalid_policy()


def list_approved_data_roots(*, store_dir: Path | None = None) -> tuple[ApprovedDataRoot, ...]:
    policy = load_browse_policy(store_dir=store_dir)
    return policy.roots if policy.available else ()


def _mutation_conflict() -> BrowsePolicyConflictError:
    return BrowsePolicyConflictError("REMOTE_ROOT_REVISION_CONFLICT")


def _validate_approval_root(root: ApprovedDataRoot) -> None:
    if type(root) is not ApprovedDataRoot:
        raise TypeError("root must be an ApprovedDataRoot")
    if not isinstance(root.root_id, str) or not root.root_id.strip():
        raise ValueError("root_id must be a non-empty string")
    if not isinstance(root.host, str) or not root.host:
        raise ValueError("root host must be a non-empty string")
    if not isinstance(root.user, str) or not root.user:
        raise ValueError("root user must be a non-empty string")
    if isinstance(root.port, bool) or not isinstance(root.port, int) or not 1 <= root.port <= 65535:
        raise ValueError("root port must be an integer from 1 to 65535")
    _validate_root_path(root.requested_path, "requested_path", reject_root=True)
    _validate_root_path(root.canonical_path, "canonical_path", reject_root=True)
    _validate_timestamp(root.created_at, allow_none=False)
    _validate_timestamp(root.revoked_at, allow_none=True)


def approve_data_root(
    root: ApprovedDataRoot,
    *,
    expected_revision: str,
    store_dir: Path | None = None,
) -> BrowsePolicy:
    _validate_approval_root(root)
    directory = _resolve_store_dir(store_dir)
    path = directory / CONNECTION_FILE_NAME
    observed: dict[str, Any] = {}

    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            identity = normalize_ssh_identity(payload)
            roots = _parse_roots(payload)
            active = _active_roots_for_identity(roots, identity)
            current_revision = browse_policy_revision(identity, active)
        except (SSHIdentityError, TypeError, ValueError, KeyError) as exc:
            raise ConnectionStoreCorruptError("invalid browse policy") from exc
        if not isinstance(expected_revision, str) or not hmac.compare_digest(
            current_revision, expected_revision
        ):
            raise _mutation_conflict()
        if (root.host, root.user, root.port) != (identity.host, identity.user, identity.port):
            raise _mutation_conflict()
        for existing in roots:
            if existing.root_id == root.root_id:
                if (
                    existing.revoked_at is None
                    and (existing.host, existing.user, existing.port)
                    == (root.host, root.user, root.port)
                    and existing.canonical_path == root.canonical_path
                ):
                    observed["payload"] = payload
                    return payload
                raise _mutation_conflict()
            if (
                existing.revoked_at is None
                and (existing.host, existing.user, existing.port) == (identity.host, identity.user, identity.port)
                and existing.canonical_path == root.canonical_path
            ):
                observed["payload"] = payload
                return payload
        if len(active) >= MAX_ACTIVE_ROOTS:
            raise _mutation_conflict()
        payload["approved_data_roots"] = [asdict(item) for item in roots] + [asdict(root)]
        observed["payload"] = payload
        return payload

    updated = locked_json_transaction(path, mutate)
    return _policy_from_payload(updated)


def revoke_data_root(
    root_id: str,
    *,
    expected_revision: str,
    store_dir: Path | None = None,
) -> BrowsePolicy:
    if not isinstance(root_id, str) or not root_id.strip():
        raise ValueError("root_id must be a non-empty string")
    directory = _resolve_store_dir(store_dir)
    path = directory / CONNECTION_FILE_NAME

    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            identity = normalize_ssh_identity(payload)
            roots = _parse_roots(payload)
            active = _active_roots_for_identity(roots, identity)
            current_revision = browse_policy_revision(identity, active)
        except (SSHIdentityError, TypeError, ValueError, KeyError) as exc:
            raise ConnectionStoreCorruptError("invalid browse policy") from exc
        if not isinstance(expected_revision, str) or not hmac.compare_digest(
            current_revision, expected_revision
        ):
            raise _mutation_conflict()
        found = False
        updated_roots: list[ApprovedDataRoot] = []
        for item in roots:
            if item.root_id == root_id:
                if item.revoked_at is not None or (item.host, item.user, item.port) != (identity.host, identity.user, identity.port):
                    raise _mutation_conflict()
                found = True
                updated_roots.append(
                    ApprovedDataRoot(
                        root_id=item.root_id,
                        host=item.host,
                        user=item.user,
                        port=item.port,
                        requested_path=item.requested_path,
                        canonical_path=item.canonical_path,
                        created_at=item.created_at,
                        revoked_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    )
                )
            else:
                updated_roots.append(item)
        if not found:
            raise _mutation_conflict()
        payload["approved_data_roots"] = [asdict(item) for item in updated_roots]
        return payload

    updated = locked_json_transaction(path, mutate)
    return _policy_from_payload(updated)


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


def _read_payload_state(directory: Path) -> tuple[dict[str, Any], bool, bool]:
    """Return ``(payload, exists, valid_object)`` for the shared settings file.

    Callers that make security decisions must distinguish a missing legacy
    file from an existing file that cannot be trusted. The former may use
    compatibility defaults; the latter must fail closed.
    """
    path = directory / CONNECTION_FILE_NAME
    if not path.is_file():
        return {}, False, True
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, True, False
    if not isinstance(raw, dict):
        return {}, True, False
    return raw, True, True


def _read_payload(directory: Path) -> dict[str, Any]:
    """Read the object payload, retaining legacy tolerance for non-policy callers."""
    payload, _exists, _valid = _read_payload_state(directory)
    return payload


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_connection(*, store_dir: Path | None = None) -> dict[str, Any]:
    """Return the stored connection, with the password decrypted when possible."""
    payload = _read_payload(_resolve_store_dir(store_dir))
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
    path = directory / CONNECTION_FILE_NAME

    def mutate(merged: dict[str, Any]) -> dict[str, Any]:
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
        return merged

    locked_json_transaction(path, mutate)
    return load_connection(store_dir=store_dir)


def clear_password(*, store_dir: Path | None = None) -> None:
    """Forget the stored password while keeping the rest of the connection."""
    directory = _resolve_store_dir(store_dir)
    path = directory / CONNECTION_FILE_NAME
    if not path.is_file():
        return
    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        payload.pop(_PASSWORD_KEY, None)
        return payload

    locked_json_transaction(path, mutate)


def load_llm(*, store_dir: Path | None = None) -> dict[str, Any]:
    """Return the shared LLM settings, with the API key decrypted when possible."""
    payload, _exists, valid_payload = _read_payload_state(
        _resolve_store_dir(store_dir)
    )
    from .agent_tools import (
        TOOL_MODE_APPROVED_EXECUTE,
        TOOL_MODE_DISABLED,
        normalize_tool_mode,
    )

    if not valid_payload:
        return {"tool_mode": TOOL_MODE_DISABLED}
    block = payload.get(LLM_BLOCK_KEY)
    if LLM_BLOCK_KEY not in payload:
        return {}
    if not isinstance(block, dict):
        return {"tool_mode": TOOL_MODE_DISABLED}
    result = {key: block[key] for key in LLM_FIELDS if block.get(key) is not None}
    if "tool_mode" not in block:
        result["tool_mode"] = TOOL_MODE_APPROVED_EXECUTE
    elif block["tool_mode"] is None:
        result["tool_mode"] = TOOL_MODE_DISABLED
    else:
        try:
            result["tool_mode"] = normalize_tool_mode(block["tool_mode"])
        except ValueError:
            # A manually corrupted value must fail closed instead of broadening
            # privileges through the compatibility default.
            result["tool_mode"] = TOOL_MODE_DISABLED
    token = block.get(_LLM_API_KEY_KEY)
    if isinstance(token, str) and token:
        secret = _unprotect(token)
        if secret:
            result["api_key"] = secret
    return result


def save_llm(values: dict[str, Any], *, store_dir: Path | None = None) -> dict[str, Any]:
    """Merge LLM ``values`` into the user-level store and persist them.

    Secret handling mirrors ``save_connection``: a non-empty ``api_key`` is
    encrypted, omitting the key keeps whatever was stored, and an explicit
    empty string clears it.
    """
    from .agent_tools import normalize_tool_mode

    normalized_values = dict(values)
    if "tool_mode" in normalized_values:
        if normalized_values["tool_mode"] is None:
            raise ValueError("tool_mode 不能是 null；请提供明确的权限模式。")
        normalized_values["tool_mode"] = normalize_tool_mode(
            normalized_values["tool_mode"]
        )

    directory = _resolve_store_dir(store_dir)
    path = directory / CONNECTION_FILE_NAME

    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        block = payload.get(LLM_BLOCK_KEY)
        if not isinstance(block, dict):
            block = {}
        for key in LLM_FIELDS:
            if key in normalized_values and normalized_values[key] is not None:
                block[key] = normalized_values[key]
        if "api_key" in normalized_values:
            new_key = str(normalized_values.get("api_key") or "")
            if new_key:
                block[_LLM_API_KEY_KEY] = _protect(new_key)
            else:
                block.pop(_LLM_API_KEY_KEY, None)
        payload[LLM_BLOCK_KEY] = block
        return payload

    locked_json_transaction(path, mutate)
    return load_llm(store_dir=store_dir)


def apply_llm_to_config(config: dict[str, Any], shared: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``config`` whose ``llm`` block inherits ``shared``.

    Project-level values win when they are already set; the shared block only
    fills the gaps. ``enabled`` is the exception: it is an OR, because enabling
    the model is a user-level decision (「配一次，所有项目共用」) and a project
    that merely defaulted to ``False`` must not cancel it.
    """
    from copy import deepcopy

    merged = deepcopy(config)
    if not shared:
        return merged
    block = merged.get("llm")
    if not isinstance(block, dict):
        block = {}
    for key in LLM_FIELDS:
        value = shared.get(key)
        if value is None or value == "":
            continue
        if key == "tool_mode":
            # The user-level mode is an emergency control and always wins over
            # stale project data. It must never follow the normal inheritance
            # rule where project-local values take precedence.
            block[key] = value
            continue
        if key == "enabled":
            block[key] = bool(block.get(key)) or bool(value)
            continue
        if block.get(key) not in (None, ""):
            continue
        block[key] = value
    if not block.get("api_key") and shared.get("api_key"):
        block["api_key"] = shared["api_key"]
    merged["llm"] = block
    return merged


def llm_model_name(config: dict[str, Any] | None) -> str:
    """Return the configured model name from a runtime config, or ``""``.

    模型名嵌在 ``config["llm"]["model"]``。状态行、思考过程等展示位经常直接
    对顶层取 ``model`` 而永远取空（界面遂显示「未指定模型」），统一收口到
    这里，调用方不必再记得层级。
    """
    if not isinstance(config, dict):
        return ""
    block = config.get("llm")
    if not isinstance(block, dict):
        return ""
    return str(block.get("model") or "").strip()


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
