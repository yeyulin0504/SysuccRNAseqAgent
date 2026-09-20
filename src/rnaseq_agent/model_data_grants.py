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
from typing import Any, Callable, Iterator, Mapping, Sequence

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
)
from .model_context import canonical_json_sha256, read_model_data_revisions
from .model_provider import normalize_provider_config, provider_config_revision, provider_identity
from .storage import project_state_lock

_FIELDS = {"sample_ids", "fastq_filenames", "remote_paths", "report_excerpt"}
_TERMINAL = {"rejected", "consumed_success", "consumed_ambiguous", "consumed_failed"}


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
    return {
        "grant_id": grant.grant_id, "project_id": grant.project_id, "thread_id": grant.thread_id,
        "provider_identity": grant.provider_identity, "tool_mode": grant.tool_mode,
        "fields": list(grant.fields), "purpose_hash": grant.purpose_hash,
        "issued_at": grant.issued_at, "expires_at": grant.expires_at,
        "policy_version": grant.policy_version, "revisions": {
            "project_revision": revisions.project_revision, "sample_revision": revisions.sample_revision,
            "remote_scan_revision": revisions.remote_scan_revision, "report_revision": revisions.report_revision,
            "provider_config_revision": revisions.provider_config_revision, "policy_version": revisions.policy_version,
        }, "record_counts": dict(grant.record_counts), "status": grant.status,
        "remote_scan_ref_hash": grant.remote_scan_ref_hash, "decided_at": grant.decided_at,
        "claimed_at": grant.claimed_at, "consumed_at": grant.consumed_at,
        "error_code": grant.error_code, "manifest": dict(grant.manifest),
        "claim_token_hash": grant.manifest.get("claim_token_hash"),
    }


def _from_payload(value: Mapping[str, Any], grant_id: str) -> DataDisclosureGrant:
    try:
        r = value["revisions"]
        revisions = ModelDataRevisions(r["project_revision"], r["sample_revision"], r.get("remote_scan_revision"), r.get("report_revision"), r["provider_config_revision"], int(r.get("policy_version", value["policy_version"])))
        fields = tuple(value["fields"])
        if not fields or any(field not in _FIELDS for field in fields) or len(set(fields)) != len(fields):
            raise ValueError
        if str(value["grant_id"]) != grant_id or value["status"] not in {"pending", "approved", "rejected", "transmitting", "consumed_success", "consumed_ambiguous", "consumed_failed"}:
            raise ValueError
        return DataDisclosureGrant(
            grant_id=str(value["grant_id"]), project_id=str(value["project_id"]), thread_id=str(value["thread_id"]),
            provider_identity=str(value["provider_identity"]), tool_mode=str(value["tool_mode"]), fields=fields,
            purpose_hash=str(value["purpose_hash"]), issued_at=str(value["issued_at"]), expires_at=str(value["expires_at"]),
            policy_version=int(value["policy_version"]), revisions=revisions,
            record_counts={str(k): int(v) for k, v in dict(value["record_counts"]).items()}, status=value["status"],
            remote_scan_ref_hash=value.get("remote_scan_ref_hash"), decided_at=value.get("decided_at"),
            claimed_at=value.get("claimed_at"), consumed_at=value.get("consumed_at"), error_code=value.get("error_code"),
            manifest=dict(value.get("manifest") or {}),
        )
    except Exception as exc:
        raise DataGrantError("invalid model data grant", code=MODEL_DATA_GRANT_INVALID, grant_id=grant_id) from exc


def load_grant(project_dir: Path, grant_id: str) -> DataDisclosureGrant:
    with project_state_lock(_lock_path(Path(project_dir))):
        return _from_payload(_read(grant_record_path(project_dir, grant_id), grant_id), grant_id)


def issue_grant_request(
    project_dir: Path, *, project_id: str, thread_id: str, fields: Sequence[DataField], purpose: str,
    source_ref: str | None = None, connection_store_dir: Path | None = None, scan_store_dir: Path | None = None,
    runtime_secrets: Sequence[str] = (), now: datetime | None = None,
) -> DataDisclosureGrant:
    del scan_store_dir, runtime_secrets
    if source_ref is not None or not fields or len(set(fields)) != len(fields) or any(field not in _FIELDS for field in fields):
        raise DataGrantError("unsupported model data scope", code=MODEL_DATA_SCOPE_UNSUPPORTED, grant_id="")
    if not isinstance(purpose, str) or not 1 <= len(purpose) <= 240:
        raise DataGrantError("invalid model data purpose", code=MODEL_DATA_GRANT_INVALID, grant_id="")
    if "remote_paths" in fields:
        raise DataGrantError("unsupported model data scope", code=MODEL_DATA_SCOPE_UNSUPPORTED, grant_id="")
    project_dir = Path(project_dir)
    try:
        project_payload = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        stored_project_id = project_payload.get("project", {}).get("id") if isinstance(project_payload, dict) else None
    except Exception as exc:
        raise DataGrantError("invalid project data", code=MODEL_DATA_GRANT_INVALID, grant_id="") from exc
    if stored_project_id is not None and stored_project_id != project_id:
        raise DataGrantError("invalid model data binding", code=MODEL_DATA_GRANT_INVALID, grant_id="")
    snapshot = _provider_snapshot(connection_store_dir)
    revisions = read_model_data_revisions(project_dir, snapshot.provider)
    counts = _project_counts(project_dir, fields)
    issued = _now(now)
    grant_id = secrets.token_urlsafe(24)
    grant = DataDisclosureGrant(
        grant_id=grant_id, project_id=project_id, thread_id=thread_id,
        provider_identity=snapshot.provider_identity.digest, tool_mode=snapshot.tool_mode,
        fields=tuple(fields), purpose_hash=canonical_json_sha256({"purpose": purpose}),
        issued_at=_iso(issued), expires_at=_iso(issued + timedelta(seconds=GRANT_TTL_SECONDS)),
        policy_version=revisions.policy_version, revisions=revisions, record_counts=counts, status="pending",
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


def claim_grant_for_send(
    project_dir: Path, grant_id: str, *, live_inputs: ExactClaimInputs | None = None,
    prepare: Callable[..., Any] | None = None, open_stream: Callable[..., Iterator[Any]] | None = None,
    now: datetime | None = None,
) -> PreparedExactClaim:
    project_dir = Path(project_dir)
    current = _now(now)
    with project_state_lock(_lock_path(project_dir)):
        path = grant_record_path(project_dir, grant_id)
        grant = _from_payload(_read(path, grant_id), grant_id)
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
        snapshot = _provider_snapshot(inputs.connection_store_dir)
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
        token = secrets.token_urlsafe(24)
        claim = ClaimedDataGrant(grant.grant_id, token, GrantBindings(grant.project_id, grant.thread_id, grant.provider_identity, grant.tool_mode, grant.revisions), grant.fields, _extract(project_dir, grant.fields))
        manifest = dict(grant.manifest)
        manifest["claim_token_hash"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
        transmitting = DataDisclosureGrant(**{**grant.__dict__, "status": "transmitting", "claimed_at": _iso(current), "manifest": manifest})
        _write(path, _to_payload(transmitting))
    context = request = None
    events: Iterator[Any] = iter(())
    try:
        if prepare is not None:
            context, request = prepare(claim, snapshot)
        if open_stream is not None and request is not None:
            events = open_stream(request)
    except BaseException as exc:
        fallback = DisclosureManifest(None, claim.fields, grant.record_counts, 0, grant.revisions)
        finish_grant_claim(project_dir, claim, outcome="consumed_failed", manifest=fallback, error_code=MODEL_CONTEXT_SECRET_DETECTED, now=current)
        raise

    @contextmanager
    def terminal_guard() -> Iterator[None]:
        try:
            yield
        except BaseException:
            fallback = DisclosureManifest(None, claim.fields, grant.record_counts, 0, grant.revisions)
            finish_grant_claim(project_dir, claim, outcome="consumed_ambiguous", manifest=fallback, error_code=MODEL_PROVIDER_REQUEST_FAILED, now=_now(None))
            raise
        else:
            manifest = DisclosureManifest(None, claim.fields, grant.record_counts, 0, grant.revisions)
            finish_grant_claim(project_dir, claim, outcome="consumed_success", manifest=manifest, now=_now(None))

    return PreparedExactClaim(claim, snapshot, context, request, events, False, terminal_guard)


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
        updated = DataDisclosureGrant(**{**grant.__dict__, "status": outcome, "consumed_at": _iso(current), "error_code": error_code, "manifest": {"fields": list(manifest.fields), "record_counts": dict(manifest.record_counts), "byte_length": manifest.byte_length, "revisions": manifest.revisions.__dict__}})
        _write(path, _to_payload(updated))
        return updated


claim_grant = claim_grant_for_send


def count_exact_project_data(project_dir: Path, fields: Sequence[DataField]) -> dict[str, int]:
    return _project_counts(Path(project_dir), fields)


def extract_exact_project_data(project_dir: Path, fields: Sequence[DataField]) -> Mapping[DataField, tuple[str, ...]]:
    return _extract(Path(project_dir), fields)
