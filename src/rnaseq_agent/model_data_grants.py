"""Durable, metadata-only model data disclosure grants.

The grant ledger deliberately stores only authorization metadata. Exact values
are reconstructed in memory after a successful claim and are never serialized.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence, runtime_checkable

from .model_disclosure import (
    DataDisclosureGrant,
    DataField,
    DisclosureManifest,
    GrantBindings,
    GrantOutcome,
    ModelDataRevisions,
    ClaimedDataGrant,
    MODEL_DATA_GRANT_CONSUMED,
    MODEL_DATA_GRANT_EXPIRED,
    MODEL_DATA_GRANT_INVALID,
    MODEL_DATA_GRANT_REJECTED,
    MODEL_DATA_REVISION_CHANGED,
    MODEL_PROVIDER_CHANGED,
    MODEL_DATA_SCOPE_UNSUPPORTED,
    MODEL_CONTEXT_SECRET_DETECTED,
    MODEL_PROVIDER_REQUEST_FAILED,
    GRANT_TTL_SECONDS,
    DISCLOSURE_PURPOSE_CATEGORIES,
    normalize_disclosure_purpose,
)
from .model_context import canonical_json_sha256, read_model_data_revisions
from .model_provider import normalize_provider_config, provider_config_revision, provider_identity
from .storage import project_state_lock

_FIELDS = {"sample_ids", "fastq_filenames", "remote_paths", "report_excerpt"}
_ENABLED_FIELDS = frozenset({"sample_ids"})
_TERMINAL = {"rejected", "consumed_success", "consumed_ambiguous", "consumed_failed"}
_MARKED_PREPARE_IDS: set[int] = set()
_MARKED_STREAM_IDS: set[int] = set()


class DataGrantError(RuntimeError):
    def __init__(self, message: str, *, code: str, grant_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.grant_id_hash = hashlib.sha256(grant_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True, repr=False)
class ModelDisclosureConnectionSnapshot:
    provider: Any
    provider_identity: Any
    provider_config_revision: str
    tool_mode: str
    credentials: Any = None
    connection_revision: str = ""
    browse_policy: Any = None


@dataclass(frozen=True, repr=False)
class ExactClaimInputs:
    project_id: str
    thread_id: str
    source_ref: str | None = None
    connection_store_dir: Path | None = None
    scan_store_dir: Path | None = None
    runtime_secrets: tuple[str, ...] = ()


@dataclass(frozen=True, repr=False)
class PreparedExactClaim:
    claim: ClaimedDataGrant
    connection_snapshot: ModelDisclosureConnectionSnapshot | None = None
    context: Any = None
    request: Any = None
    events: Iterator[Any] = iter(())
    transmission_started: bool = False
    terminal_guard: Callable[..., Any] | None = None


@runtime_checkable
class ExactPrepareCallback(Protocol):
    __model_data_exact_prepare__: bool

    def __call__(self, claim: ClaimedDataGrant, snapshot: ModelDisclosureConnectionSnapshot) -> tuple[Any, Any]: ...


@runtime_checkable
class ExactOpenStreamCallback(Protocol):
    __model_data_exact_open_stream__: bool

    def __call__(self, request: Any) -> Iterator[Any]: ...


def mark_exact_prepare(callback: Callable[..., Any]) -> Callable[..., Any]:
    """Mark the sole exact-request preparation callback."""
    setattr(callback, "__model_data_exact_prepare__", True)
    _MARKED_PREPARE_IDS.add(id(callback))
    return callback


def mark_exact_open_stream(callback: Callable[..., Any]) -> Callable[..., Any]:
    """Mark the sole exact dispatcher callback."""
    setattr(callback, "__model_data_exact_open_stream__", True)
    _MARKED_STREAM_IDS.add(id(callback))
    return callback


def _marked(callback: Any, marker: str) -> bool:
    if not callable(callback):
        return False
    if id(callback) in (_MARKED_PREPARE_IDS if marker == "__model_data_exact_prepare__" else _MARKED_STREAM_IDS):
        return True
    if marker == "__model_data_exact_open_stream__":
        try:
            from .model_provider import ModelProviderGateway
            return getattr(callback, "__func__", None) is ModelProviderGateway.dispatch_exact
        except (ImportError, AttributeError):
            return False
    return False


def _now(value: datetime | None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _grant_dir(project_dir: Path) -> Path:
    path = Path(project_dir) / ".model_data_grants"
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def grant_record_path(project_dir: Path, grant_id: str) -> Path:
    digest = hashlib.sha256(str(grant_id).encode("utf-8")).hexdigest()
    return _grant_dir(Path(project_dir)) / f"{digest}.json"


def _lock_path(project_dir: Path) -> Path:
    return _grant_dir(Path(project_dir)) / ".store.lock"


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = ""
    finally:
        if temp:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _read(path: Path, grant_id: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_reject_duplicates)
    except Exception as exc:
        raise DataGrantError("invalid model data grant", code=MODEL_DATA_GRANT_INVALID, grant_id=grant_id) from exc
    if not isinstance(value, dict):
        raise DataGrantError("invalid model data grant", code=MODEL_DATA_GRANT_INVALID, grant_id=grant_id)
    return value


def _provider_snapshot(store_dir: Path | None) -> ModelDisclosureConnectionSnapshot:
    from .connection_store import load_llm
    llm = load_llm(store_dir=store_dir)
    raw = {
        "backend": llm.get("backend", "openai_compatible"),
        "provider": llm.get("provider", "openai"),
        "api_base": llm.get("api_base", "https://localhost/v1"),
        "model": llm.get("model", "unknown"),
        "api_mode": llm.get("api_mode", "chat_completions"),
    }
    try:
        config = normalize_provider_config(raw)
    except ValueError:
        config = normalize_provider_config({"provider": "openai", "api_base": "https://localhost/v1", "model": "unknown"})
    identity = provider_identity(config)
    mode = str(llm.get("tool_mode") or "disabled")
    return ModelDisclosureConnectionSnapshot(config, identity, provider_config_revision(config), mode)


def _connection_revision_payload(payload: Mapping[str, Any], *, browse_policy: Any = None) -> dict[str, Any]:
    """Return the non-secret connection state used for snapshot revisioning.

    The on-disk connection document contains protected credential ciphertext
    (and may contain other private extension fields).  Hashing the raw
    document would make a harmless credential rotation look like a provider
    configuration change, while also making the revision depend on secret
    material.  Keep the revision deliberately allowlisted: provider settings,
    tool mode, server connection metadata, and the browse-policy revision are
    all authorization-relevant; credential values and unknown extensions are
    not.
    """
    from . import connection_store

    llm = payload.get(connection_store.LLM_BLOCK_KEY)
    if not isinstance(llm, Mapping):
        llm = {}
    server = payload.get("server")
    if not isinstance(server, Mapping):
        server = {}
    api_key_token = llm.get(connection_store._LLM_API_KEY_KEY)
    password_token = payload.get(connection_store._PASSWORD_KEY)
    if not password_token:
        password_token = server.get("password_protected")
    return {
        "llm": {
            key: llm[key]
            for key in connection_store.LLM_FIELDS
            if key in llm and llm[key] is not None
        },
        "server": {
            key: server[key]
            for key in connection_store.SHARED_FIELDS
            if key in server and server[key] is not None
        },
        # Record only credential presence, never protected ciphertext or a
        # decrypted value.  Rotation keeps the revision stable; adding or
        # removing a usable credential invalidates a bound snapshot.
        "credentials": {
            "api_key_present": isinstance(api_key_token, str) and bool(api_key_token),
            "password_present": isinstance(password_token, str) and bool(password_token),
        },
        "browse_policy_revision": getattr(browse_policy, "revision", None),
    }


@contextmanager
def locked_model_disclosure_connection(
    *, store_dir: Path | None = None, runtime_secrets: Sequence[str] = ()
) -> Iterator[ModelDisclosureConnectionSnapshot]:
    """Yield one provider/tool/credential snapshot from one locked store read.

    The connection lock is deliberately acquired by this boundary.  Callers
    that need to bind a grant must enter this context before the project/grant
    lock (the fixed order is connection -> project).  No unlocked ``load_llm``
    reader is used, so provider identity, mode, credentials and revision all
    describe the same bytes.
    """
    from . import connection_store
    from .agent_tools import TOOL_MODE_APPROVED_EXECUTE, TOOL_MODE_DISABLED, normalize_tool_mode
    from .storage import project_state_lock

    directory = connection_store._resolve_store_dir(store_dir)
    path = directory / connection_store.CONNECTION_FILE_NAME
    with project_state_lock(path):
        payload, _exists, valid = connection_store._read_payload_state(directory, secure=True)
        if not valid:
            payload = {}
        raw_llm = payload.get(connection_store.LLM_BLOCK_KEY)
        if not isinstance(raw_llm, dict):
            raw_llm = {}
        decrypted_llm = {
            key: raw_llm[key]
            for key in connection_store.LLM_FIELDS
            if raw_llm.get(key) is not None
        }
        token = raw_llm.get(connection_store._LLM_API_KEY_KEY)
        if isinstance(token, str) and token:
            secret = connection_store._unprotect(token)
            if secret:
                decrypted_llm["api_key"] = secret
        raw = {
            "backend": decrypted_llm.get("backend", "openai_compatible"),
            "provider": decrypted_llm.get("provider", "openai"),
            "api_base": decrypted_llm.get("api_base", "https://localhost/v1"),
            "model": decrypted_llm.get("model", "unknown"),
            "api_mode": decrypted_llm.get("api_mode", "chat_completions"),
        }
        try:
            config = normalize_provider_config(raw)
        except ValueError:
            config = normalize_provider_config({"provider": "openai", "api_base": "https://localhost/v1", "model": "unknown"})
        identity = provider_identity(config)
        mode_value = decrypted_llm.get(
            "tool_mode",
            TOOL_MODE_APPROVED_EXECUTE if connection_store.LLM_BLOCK_KEY in payload else TOOL_MODE_DISABLED,
        )
        if mode_value is None:
            mode = TOOL_MODE_DISABLED
        else:
            try:
                mode = normalize_tool_mode(mode_value)
            except ValueError:
                mode = TOOL_MODE_DISABLED
        credentials = connection_store.provider_credentials_from_connection(
            decrypted_llm, payload, runtime_secrets=runtime_secrets
        )
        browse_policy = connection_store._policy_from_payload(payload) if valid else None
        revision = canonical_json_sha256(_connection_revision_payload(payload, browse_policy=browse_policy))
        yield ModelDisclosureConnectionSnapshot(
            config,
            identity,
            provider_config_revision(config),
            mode,
            credentials=credentials,
            connection_revision=revision,
            browse_policy=browse_policy,
        )


def _project_counts(project_dir: Path, fields: Sequence[str]) -> dict[str, int]:
    try:
        value = json.loads((Path(project_dir) / "project.json").read_text(encoding="utf-8"))
    except Exception as exc:
        raise DataGrantError("invalid project data", code=MODEL_DATA_GRANT_INVALID, grant_id="") from exc
    samples = value.get("samples", {}).get("items", []) if isinstance(value, dict) else []
    if not isinstance(samples, list):
        samples = []
    result: dict[str, int] = {}
    if "sample_ids" in fields:
        result["sample_ids"] = sum(1 for row in samples if isinstance(row, dict) and isinstance(row.get("sample_id"), str) and row["sample_id"])
    if "fastq_filenames" in fields:
        result["fastq_filenames"] = sum(1 for row in samples if isinstance(row, dict) for key in ("fastq_1", "fastq_2") if isinstance(row.get(key), str) and row[key])
    if "report_excerpt" in fields:
        try:
            result["report_excerpt"] = len((Path(project_dir) / "report.md").read_text(encoding="utf-8")[:4000])
        except OSError:
            result["report_excerpt"] = 0
    return result


def _stored_project_id(project_dir: Path, *, grant_id: str = "") -> str:
    try:
        value = json.loads((Path(project_dir) / "project.json").read_text(encoding="utf-8"))
        project = value.get("project") if isinstance(value, dict) else None
        stored = project.get("id") if isinstance(project, dict) else None
    except Exception as exc:
        raise DataGrantError("invalid project data", code=MODEL_DATA_GRANT_INVALID, grant_id=grant_id) from exc
    if not isinstance(stored, str) or not stored.strip():
        raise DataGrantError("invalid model data binding", code=MODEL_DATA_GRANT_INVALID, grant_id=grant_id)
    return stored


def _assert_stored_project_id(project_dir: Path, project_id: str, *, grant_id: str = "") -> None:
    if _stored_project_id(project_dir, grant_id=grant_id) != project_id:
        raise DataGrantError("invalid model data binding", code=MODEL_DATA_GRANT_INVALID, grant_id=grant_id)


def _extract(project_dir: Path, fields: Sequence[str]) -> dict[DataField, tuple[str, ...]]:
    value = json.loads((Path(project_dir) / "project.json").read_text(encoding="utf-8"))
    samples = value.get("samples", {}).get("items", []) if isinstance(value, dict) else []
    result: dict[DataField, tuple[str, ...]] = {}
    if "sample_ids" in fields:
        result["sample_ids"] = tuple(row["sample_id"] for row in samples if isinstance(row, dict) and isinstance(row.get("sample_id"), str) and row["sample_id"])
    if "fastq_filenames" in fields:
        result["fastq_filenames"] = tuple(os.path.basename(row[key]) for row in samples if isinstance(row, dict) for key in ("fastq_1", "fastq_2") if isinstance(row.get(key), str) and row[key])
    if "report_excerpt" in fields:
        try:
            result["report_excerpt"] = ((Path(project_dir) / "report.md").read_text(encoding="utf-8")[:4000],)
        except OSError:
            result["report_excerpt"] = ()
    return result


def _to_payload(grant: DataDisclosureGrant) -> dict[str, Any]:
    revisions = grant.revisions
    manifest = dict(grant.manifest)
    if grant.status in _TERMINAL - {"rejected"} and not all(key in manifest for key in ("fields", "record_counts", "byte_length", "revisions")):
        manifest = {
            "fields": list(grant.fields),
            "record_counts": dict(grant.record_counts),
            "byte_length": 0,
            "revisions": revisions.__dict__,
            **({"claim_token_hash": manifest["claim_token_hash"]} if "claim_token_hash" in manifest else {}),
        }
    return {
        "grant_id": grant.grant_id, "project_id": grant.project_id, "thread_id": grant.thread_id,
        "provider_identity": grant.provider_identity, "tool_mode": grant.tool_mode,
        "fields": list(grant.fields), "purpose_hash": grant.purpose_hash,
        "purpose_category": grant.purpose_category,
        "issued_at": grant.issued_at, "expires_at": grant.expires_at,
        "policy_version": grant.policy_version, "revisions": {
            "project_revision": revisions.project_revision, "sample_revision": revisions.sample_revision,
            "remote_scan_revision": revisions.remote_scan_revision, "report_revision": revisions.report_revision,
            "provider_config_revision": revisions.provider_config_revision, "policy_version": revisions.policy_version,
        }, "record_counts": dict(grant.record_counts), "status": grant.status,
        "remote_scan_ref_hash": grant.remote_scan_ref_hash, "decided_at": grant.decided_at,
        "claimed_at": grant.claimed_at, "consumed_at": grant.consumed_at,
        "error_code": grant.error_code, "manifest": manifest,
        "claim_token_hash": manifest.get("claim_token_hash"),
    }


def _from_payload(value: Mapping[str, Any], grant_id: str) -> DataDisclosureGrant:
    try:
        if not isinstance(value, Mapping):
            raise ValueError
        for key in ("grant_id", "project_id", "thread_id", "provider_identity", "tool_mode", "purpose_hash", "issued_at", "expires_at", "policy_version", "revisions", "record_counts", "status"):
            if key not in value:
                raise ValueError
        r = value["revisions"]
        if not isinstance(r, Mapping):
            raise ValueError
        if not isinstance(value["policy_version"], int) or isinstance(value["policy_version"], bool):
            raise ValueError
        policy_version = value["policy_version"]
        if policy_version <= 0 or int(r.get("policy_version", policy_version)) != policy_version:
            raise ValueError
        revisions = ModelDataRevisions(r["project_revision"], r["sample_revision"], r.get("remote_scan_revision"), r.get("report_revision"), r["provider_config_revision"], int(r.get("policy_version", value["policy_version"])))
        if any(not isinstance(item, str) or not item.strip() for item in (revisions.project_revision, revisions.sample_revision, revisions.provider_config_revision)):
            raise ValueError
        if revisions.remote_scan_revision is not None and (not isinstance(revisions.remote_scan_revision, str) or not revisions.remote_scan_revision.strip()):
            raise ValueError
        if revisions.report_revision is not None and (not isinstance(revisions.report_revision, str) or not revisions.report_revision.strip()):
            raise ValueError
        if not isinstance(value["fields"], (list, tuple)):
            raise ValueError
        fields = tuple(value["fields"])
        if not fields or any(not isinstance(field, str) or field not in _FIELDS for field in fields) or len(set(fields)) != len(fields):
            raise ValueError
        if any(field not in _ENABLED_FIELDS for field in fields):
            raise DataGrantError("unsupported model data scope", code=MODEL_DATA_SCOPE_UNSUPPORTED, grant_id=grant_id)
        if not isinstance(value["project_id"], str) or not isinstance(value["thread_id"], str):
            raise ValueError
        project_id, thread_id = value["project_id"], value["thread_id"]
        if not project_id.strip() or not thread_id.strip():
            raise ValueError
        status = value["status"]
        if str(value["grant_id"]) != grant_id or status not in {"pending", "approved", "rejected", "transmitting", "consumed_success", "consumed_ambiguous", "consumed_failed"}:
            raise ValueError
        if not isinstance(value["issued_at"], str) or not isinstance(value["expires_at"], str):
            raise ValueError
        issued_at, expires_at = value["issued_at"], value["expires_at"]
        issued, expires = _parse(issued_at), _parse(expires_at)
        if "purpose_category" not in value:
            raise ValueError
        purpose_category = str(value.get("purpose_category") or "")
        if purpose_category not in DISCLOSURE_PURPOSE_CATEGORIES:
            raise ValueError
        if expires <= issued or not str(value["provider_identity"]).strip() or not str(value["tool_mode"]).strip() or not str(value["purpose_hash"]).strip():
            raise ValueError
        if not isinstance(value["record_counts"], dict):
            raise ValueError
        counts = value["record_counts"]
        if set(counts) != set(fields) or any(not isinstance(k, str) or not isinstance(v, int) or isinstance(v, bool) or v < 0 for k, v in counts.items()):
            raise ValueError
        if "manifest" not in value or not isinstance(value["manifest"], dict):
            raise ValueError
        manifest = value["manifest"]
        if status in _TERMINAL - {"rejected"} and not all(key in manifest for key in ("fields", "record_counts", "byte_length", "revisions")):
            raise ValueError
        for timestamp_key in ("decided_at", "claimed_at", "consumed_at"):
            if value.get(timestamp_key) is not None:
                if not isinstance(value[timestamp_key], str):
                    raise ValueError
                _parse(value[timestamp_key])
        if status in {"approved", "rejected"} and value.get("decided_at") is None:
            raise ValueError
        if status in {"pending", "transmitting"} and value.get("decided_at") is None and status == "transmitting":
            raise ValueError
        if status in _TERMINAL - {"rejected"} and value.get("consumed_at") is None:
            raise ValueError
        if status == "transmitting" and value.get("claimed_at") is None:
            raise ValueError
        if status == "transmitting" and not isinstance(manifest.get("claim_token_hash"), str):
            raise ValueError
        if "claim_token_hash" in value and value["claim_token_hash"] != manifest.get("claim_token_hash"):
            raise ValueError
        if status in {"pending", "approved", "rejected"} and (value.get("claimed_at") is not None or value.get("consumed_at") is not None or "claim_token_hash" in manifest):
            raise ValueError
        if status == "pending" and value.get("decided_at") is not None:
            raise ValueError
        if status in _TERMINAL - {"rejected"}:
            mf = manifest
            mf_fields = tuple(mf["fields"])
            if not isinstance(mf["record_counts"], dict):
                raise ValueError
            mf_counts = mf["record_counts"]
            mf_revisions = mf["revisions"]
            if mf_fields != fields or mf_counts != counts or isinstance(mf["byte_length"], bool) or not isinstance(mf["byte_length"], int) or mf["byte_length"] < 0 or not isinstance(mf_revisions, Mapping):
                raise ValueError
            if any(mf_revisions.get(key) != r.get(key) for key in ("project_revision", "sample_revision", "remote_scan_revision", "report_revision", "provider_config_revision", "policy_version")):
                raise ValueError
        return DataDisclosureGrant(
            grant_id=str(value["grant_id"]), project_id=project_id, thread_id=thread_id,
            provider_identity=str(value["provider_identity"]), tool_mode=str(value["tool_mode"]), fields=fields,
            purpose_hash=str(value["purpose_hash"]), issued_at=issued_at, expires_at=expires_at,
            policy_version=policy_version, revisions=revisions,
            record_counts=counts, status=status,
            remote_scan_ref_hash=value.get("remote_scan_ref_hash"), decided_at=value.get("decided_at"),
            claimed_at=value.get("claimed_at"), consumed_at=value.get("consumed_at"), error_code=value.get("error_code"),
            manifest=dict(manifest), purpose_category=purpose_category,
        )
    except DataGrantError:
        raise
    except Exception as exc:
        raise DataGrantError("invalid model data grant", code=MODEL_DATA_GRANT_INVALID, grant_id=grant_id) from exc


def load_grant(project_dir: Path, grant_id: str) -> DataDisclosureGrant:
    with project_state_lock(_lock_path(Path(project_dir))):
        grant = _from_payload(_read(grant_record_path(project_dir, grant_id), grant_id), grant_id)
        _assert_stored_project_id(Path(project_dir), grant.project_id, grant_id=grant_id)
        return grant


def issue_grant_request(
    project_dir: Path, *, project_id: str, thread_id: str, fields: Sequence[DataField], purpose: str,
    source_ref: str | None = None, connection_store_dir: Path | None = None, scan_store_dir: Path | None = None,
    runtime_secrets: Sequence[str] = (), now: datetime | None = None,
) -> DataDisclosureGrant:
    del scan_store_dir, runtime_secrets
    if source_ref is not None or not fields or len(set(fields)) != len(fields) or any(field not in _ENABLED_FIELDS for field in fields):
        raise DataGrantError("unsupported model data scope", code=MODEL_DATA_SCOPE_UNSUPPORTED, grant_id="")
    try:
        purpose_category = normalize_disclosure_purpose(purpose)
    except ValueError:
        raise DataGrantError("invalid model data purpose", code=MODEL_DATA_GRANT_INVALID, grant_id="")
    if not isinstance(project_id, str) or not isinstance(thread_id, str) or not project_id.strip() or not thread_id.strip():
        raise DataGrantError("invalid model data binding", code=MODEL_DATA_GRANT_INVALID, grant_id="")
    project_dir = Path(project_dir)
    _assert_stored_project_id(project_dir, project_id)
    snapshot = _provider_snapshot(connection_store_dir)
    revisions = read_model_data_revisions(project_dir, snapshot.provider)
    counts = _project_counts(project_dir, fields)
    issued = _now(now)
    grant_id = secrets.token_urlsafe(24)
    grant = DataDisclosureGrant(
        grant_id=grant_id, project_id=project_id, thread_id=thread_id,
        provider_identity=snapshot.provider_identity.digest, tool_mode=snapshot.tool_mode,
        fields=tuple(fields), purpose_hash=canonical_json_sha256({"purpose_category": purpose_category}),
        issued_at=_iso(issued), expires_at=_iso(issued + timedelta(seconds=GRANT_TTL_SECONDS)),
        policy_version=revisions.policy_version, revisions=revisions, record_counts=counts, status="pending",
        purpose_category=purpose_category,
    )
    with project_state_lock(_lock_path(project_dir)):
        _write(grant_record_path(project_dir, grant_id), _to_payload(grant))
    return grant


def decide_grant(project_dir: Path, grant_id: str, *, approved: bool, note: str = "", now: datetime | None = None) -> DataDisclosureGrant:
    del note
    project_dir = Path(project_dir)
    with project_state_lock(_lock_path(project_dir)):
        path = grant_record_path(project_dir, grant_id)
        grant = _from_payload(_read(path, grant_id), grant_id)
        _assert_stored_project_id(project_dir, grant.project_id, grant_id=grant_id)
        if grant.status == "rejected" and not approved:
            return grant
        if grant.status != "pending":
            raise DataGrantError("model data grant already consumed", code=MODEL_DATA_GRANT_CONSUMED, grant_id=grant_id)
        current = _now(now)
        if _parse(grant.expires_at) <= current:
            updated = DataDisclosureGrant(**{**grant.__dict__, "status": "consumed_failed", "error_code": MODEL_DATA_GRANT_EXPIRED, "consumed_at": _iso(current)})
        elif not approved:
            updated = DataDisclosureGrant(**{**grant.__dict__, "status": "rejected", "error_code": MODEL_DATA_GRANT_REJECTED, "decided_at": _iso(current)})
        else:
            updated = DataDisclosureGrant(**{**grant.__dict__, "status": "approved", "decided_at": _iso(current)})
        _write(path, _to_payload(updated))
        return updated


def _claim_error(grant: DataDisclosureGrant, code: str, now: datetime) -> DataGrantError:
    return DataGrantError("model data grant invalid", code=code, grant_id=grant.grant_id)


def _claim_grant_for_send_locked(
    project_dir: Path, grant_id: str, *, live_inputs: ExactClaimInputs | None = None,
    prepare: Callable[..., Any] | None = None, open_stream: Callable[..., Iterator[Any]] | None = None,
    now: datetime | None = None,
    _connection_snapshot: ModelDisclosureConnectionSnapshot | None = None,
) -> PreparedExactClaim:
    project_dir = Path(project_dir)
    current = _now(now)
    if not _marked(prepare, "__model_data_exact_prepare__") or not _marked(open_stream, "__model_data_exact_open_stream__"):
        raise DataGrantError("exact claim requires marked prepare and open_stream callbacks", code=MODEL_DATA_GRANT_INVALID, grant_id=grant_id)
    with project_state_lock(_lock_path(project_dir)):
        path = grant_record_path(project_dir, grant_id)
        grant = _from_payload(_read(path, grant_id), grant_id)
        _assert_stored_project_id(project_dir, grant.project_id, grant_id=grant_id)
        if grant.status == "transmitting":
            recovered = DataDisclosureGrant(**{**grant.__dict__, "status": "consumed_ambiguous", "error_code": MODEL_PROVIDER_REQUEST_FAILED, "consumed_at": _iso(current)})
            _write(path, _to_payload(recovered))
            raise _claim_error(recovered, MODEL_DATA_GRANT_CONSUMED, current)
        if grant.status in _TERMINAL or grant.status != "approved":
            raise _claim_error(grant, MODEL_DATA_GRANT_CONSUMED, current)
        if _parse(grant.expires_at) <= current:
            failed = DataDisclosureGrant(**{**grant.__dict__, "status": "consumed_failed", "error_code": MODEL_DATA_GRANT_EXPIRED, "consumed_at": _iso(current)})
            _write(path, _to_payload(failed))
            raise _claim_error(failed, MODEL_DATA_GRANT_EXPIRED, current)
        inputs = live_inputs or ExactClaimInputs(grant.project_id, grant.thread_id)
        if inputs.project_id != grant.project_id or inputs.thread_id != grant.thread_id:
            failed = DataDisclosureGrant(**{**grant.__dict__, "status": "consumed_failed", "error_code": MODEL_DATA_GRANT_INVALID, "consumed_at": _iso(current)})
            _write(path, _to_payload(failed))
            raise _claim_error(failed, MODEL_DATA_GRANT_INVALID, current)
        snapshot = _connection_snapshot or _provider_snapshot(inputs.connection_store_dir)
        if snapshot.provider_identity.digest != grant.provider_identity:
            failed = DataDisclosureGrant(**{**grant.__dict__, "status": "consumed_failed", "error_code": MODEL_PROVIDER_CHANGED, "consumed_at": _iso(current)})
            _write(path, _to_payload(failed))
            raise _claim_error(failed, MODEL_PROVIDER_CHANGED, current)
        if snapshot.provider_config_revision != grant.revisions.provider_config_revision:
            failed = DataDisclosureGrant(**{**grant.__dict__, "status": "consumed_failed", "error_code": MODEL_DATA_REVISION_CHANGED, "consumed_at": _iso(current)})
            _write(path, _to_payload(failed))
            raise _claim_error(failed, MODEL_DATA_REVISION_CHANGED, current)
        if snapshot.tool_mode != grant.tool_mode:
            failed = DataDisclosureGrant(**{**grant.__dict__, "status": "consumed_failed", "error_code": MODEL_DATA_REVISION_CHANGED, "consumed_at": _iso(current)})
            _write(path, _to_payload(failed))
            raise _claim_error(failed, MODEL_DATA_REVISION_CHANGED, current)
        # Project writers lock project.json. Hold the same lock across the
        # revision/count checks and exact extraction so a compliant writer
        # cannot replace the approved sample data between validation and use.
        with project_state_lock(project_dir / "project.json"):
            live_revisions = read_model_data_revisions(project_dir, snapshot.provider)
            if live_revisions.project_revision != grant.revisions.project_revision:
                failed = DataDisclosureGrant(**{**grant.__dict__, "status": "consumed_failed", "error_code": MODEL_DATA_REVISION_CHANGED, "consumed_at": _iso(current)})
                _write(path, _to_payload(failed))
                raise _claim_error(failed, MODEL_DATA_REVISION_CHANGED, current)
            for field in grant.fields:
                if field == "sample_ids" and live_revisions.sample_revision != grant.revisions.sample_revision:
                    code = MODEL_DATA_REVISION_CHANGED
                    failed = DataDisclosureGrant(**{**grant.__dict__, "status": "consumed_failed", "error_code": code, "consumed_at": _iso(current)})
                    _write(path, _to_payload(failed))
                    raise _claim_error(failed, code, current)
                if field == "fastq_filenames" and live_revisions.sample_revision != grant.revisions.sample_revision:
                    code = MODEL_DATA_REVISION_CHANGED
                    failed = DataDisclosureGrant(**{**grant.__dict__, "status": "consumed_failed", "error_code": code, "consumed_at": _iso(current)})
                    _write(path, _to_payload(failed))
                    raise _claim_error(failed, code, current)
                if field == "report_excerpt" and live_revisions.report_revision != grant.revisions.report_revision:
                    code = MODEL_DATA_REVISION_CHANGED
                    failed = DataDisclosureGrant(**{**grant.__dict__, "status": "consumed_failed", "error_code": code, "consumed_at": _iso(current)})
                    _write(path, _to_payload(failed))
                    raise _claim_error(failed, code, current)
            live_counts = _project_counts(project_dir, grant.fields)
            if live_counts != dict(grant.record_counts):
                failed = DataDisclosureGrant(**{**grant.__dict__, "status": "consumed_failed", "error_code": MODEL_DATA_REVISION_CHANGED, "consumed_at": _iso(current)})
                _write(path, _to_payload(failed))
                raise _claim_error(failed, MODEL_DATA_REVISION_CHANGED, current)
            token = secrets.token_urlsafe(24)
            claim = ClaimedDataGrant(grant.grant_id, token, GrantBindings(grant.project_id, grant.thread_id, grant.provider_identity, grant.tool_mode, grant.revisions), grant.fields, _extract(project_dir, grant.fields))
            manifest = dict(grant.manifest)
            manifest["claim_token_hash"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
            transmitting = DataDisclosureGrant(**{**grant.__dict__, "status": "transmitting", "claimed_at": _iso(current), "manifest": manifest})
            _write(path, _to_payload(transmitting))
    context = request = None
    events: Iterator[Any] = iter(())
    try:
        context, request = prepare(claim, snapshot)
        events = open_stream(request)
        if events is None:
            raise DataGrantError("exact dispatcher returned no transport", code=MODEL_DATA_GRANT_INVALID, grant_id=grant_id)
    except BaseException as exc:
        fallback = DisclosureManifest(None, claim.fields, grant.record_counts, 0, grant.revisions)
        started = bool(getattr(exc, "transmission_started", False))
        finish_grant_claim(project_dir, claim, outcome="consumed_ambiguous" if started else "consumed_failed", manifest=fallback, error_code=getattr(exc, "code", MODEL_CONTEXT_SECRET_DETECTED), now=current)
        raise

    @contextmanager
    def terminal_guard() -> Iterator[None]:
        try:
            yield
        except BaseException as exc:
            fallback = DisclosureManifest(None, claim.fields, grant.record_counts, 0, grant.revisions)
            started = bool(getattr(exc, "transmission_started", False))
            finish_grant_claim(project_dir, claim, outcome="consumed_ambiguous" if started else "consumed_failed", manifest=fallback, error_code=getattr(exc, "code", MODEL_PROVIDER_REQUEST_FAILED), now=_now(None))
            raise
        else:
            manifest = DisclosureManifest(None, claim.fields, grant.record_counts, 0, grant.revisions)
            finish_grant_claim(project_dir, claim, outcome="consumed_success", manifest=manifest, now=_now(None))

    return PreparedExactClaim(claim, snapshot, context, request, events, bool(getattr(events, "transmission_started", False)), terminal_guard)


def claim_grant_for_send(
    project_dir: Path, grant_id: str, *, live_inputs: ExactClaimInputs | None = None,
    prepare: Callable[..., Any] | None = None, open_stream: Callable[..., Iterator[Any]] | None = None,
    now: datetime | None = None,
) -> PreparedExactClaim:
    """Claim an exact grant while holding one live connection snapshot.

    The connection-store lock is acquired before the project/grant lock, and
    remains held through request preparation and transport creation.  This
    prevents provider or tool-mode drift between validation and transmission.
    """
    store_dir = live_inputs.connection_store_dir if live_inputs is not None else None
    runtime_secrets = live_inputs.runtime_secrets if live_inputs is not None else ()
    with locked_model_disclosure_connection(store_dir=store_dir, runtime_secrets=runtime_secrets) as snapshot:
        return _claim_grant_for_send_locked(
            project_dir,
            grant_id,
            live_inputs=live_inputs,
            prepare=prepare,
            open_stream=open_stream,
            now=now,
            _connection_snapshot=snapshot,
        )


def finish_grant_claim(
    project_dir: Path, claim: ClaimedDataGrant, *, outcome: GrantOutcome,
    manifest: DisclosureManifest, error_code: str | None = None, now: datetime | None = None,
) -> DataDisclosureGrant:
    project_dir = Path(project_dir)
    current = _now(now)
    with project_state_lock(_lock_path(project_dir)):
        path = grant_record_path(project_dir, claim.grant_id)
        grant = _from_payload(_read(path, claim.grant_id), claim.grant_id)
        if grant.status != "transmitting":
            if grant.status in _TERMINAL:
                return grant
            raise _claim_error(grant, MODEL_DATA_GRANT_INVALID, current)
        token_hash = grant.manifest.get("claim_token_hash")
        if token_hash != hashlib.sha256(claim.claim_token.encode("utf-8")).hexdigest():
            raise _claim_error(grant, MODEL_DATA_GRANT_INVALID, current)
        if outcome not in {"consumed_success", "consumed_ambiguous", "consumed_failed"}:
            raise _claim_error(grant, MODEL_DATA_GRANT_INVALID, current)
        valid_manifest = (
            tuple(manifest.fields) == tuple(grant.fields)
            and dict(manifest.record_counts) == dict(grant.record_counts)
            and isinstance(manifest.byte_length, int) and manifest.byte_length >= 0
            and manifest.revisions == grant.revisions
        )
        if not valid_manifest:
            outcome, error_code = "consumed_failed", MODEL_DATA_GRANT_INVALID
            manifest = DisclosureManifest(None, grant.fields, grant.record_counts, 0, grant.revisions)
        updated = DataDisclosureGrant(**{**grant.__dict__, "status": outcome, "consumed_at": _iso(current), "error_code": error_code, "manifest": {"fields": list(manifest.fields), "record_counts": dict(manifest.record_counts), "byte_length": manifest.byte_length, "revisions": manifest.revisions.__dict__}})
        _write(path, _to_payload(updated))
        return updated


claim_grant = claim_grant_for_send


def count_exact_project_data(project_dir: Path, fields: Sequence[DataField]) -> dict[str, int]:
    return _project_counts(Path(project_dir), fields)


def extract_exact_project_data(project_dir: Path, fields: Sequence[DataField]) -> Mapping[DataField, tuple[str, ...]]:
    return _extract(Path(project_dir), fields)
