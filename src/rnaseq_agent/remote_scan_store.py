"""Private, expiring storage for bounded remote browse results.

The store is deliberately independent from the provider/chat layers.  A scan
is bound to the project and exact browse context; callers receive only an
opaque reference and must revalidate that binding before consuming it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from .connection_store import BrowsePolicy
from .private_files import create_private_temp, ensure_private_directory, verify_private_path
from .remote_browse import (
    BrowseContext,
    BrowseDirectoryGroup,
    BrowseResult,
    BrowseSampleRow,
    REMOTE_SCAN_REFERENCE_EXPIRED,
    REMOTE_SCAN_REFERENCE_INVALID,
    REMOTE_SCAN_REFERENCE_USED,
    REMOTE_SCAN_STORE_FAILED,
)
from .storage import shared_file_lock

REMOTE_SCAN_TTL_SECONDS = 15 * 60
REMOTE_SCAN_TERMINAL_TTL_SECONDS = 24 * 60 * 60
MAX_PENDING_REMOTE_SCANS_PER_PROJECT = 8
_ID_RE = re.compile(r"^(scan|src)_[0-9a-f]{32}$")


@dataclass(frozen=True)
class RemoteScanReference:
    scan_id: str
    source_ref: str
    result_revision: str
    project_id: str
    thread_id: str | None
    source: str
    expires_at: str


@dataclass(frozen=True)
class StoredRemoteScan:
    scan_id: str
    source_ref: str
    result_revision: str
    project_id: str
    thread_id: str | None
    source: str
    identity_digest: str
    root_id: str
    browse_policy_revision: str
    canonical_target: str
    groups: tuple[BrowseDirectoryGroup, ...]
    truncated: bool
    created_at: str
    expires_at: str
    apply_state: Literal["pending", "applying", "consumed", "expired"]
    apply_claim_id: str | None
    apply_group_id: str | None
    claimed_at: str | None
    consumed_at: str | None
    tombstone_expires_at: str | None


def _store_dir(project_dir: Path) -> Path:
    return ensure_private_directory(Path(project_dir) / ".remote-scans")


def _lock_path(directory: Path) -> Path:
    return directory / ".store.lock"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _validate_identifier(identifier: str, prefix: str) -> str:
    if not isinstance(identifier, str) or not _ID_RE.fullmatch(identifier) or not identifier.startswith(prefix + "_"):
        raise ValueError(REMOTE_SCAN_REFERENCE_INVALID)
    return identifier


def remote_scan_lookup_path(store_dir: Path, identifier: str, *, kind: Literal["scan", "source"]) -> Path:
    prefix = "scan" if kind == "scan" else "src"
    identifier = _validate_identifier(identifier, prefix)
    digest = hashlib.sha256(identifier.encode("ascii")).hexdigest()
    return Path(store_dir) / (f"scan-{digest}.record" if kind == "scan" else f"source-{digest}.ref")


def _group_to_json(group: BrowseDirectoryGroup) -> dict[str, object]:
    return {
        "group_id": group.group_id,
        "canonical_directory": group.canonical_directory,
        "samples": [asdict(item) for item in group.samples],
        "unmatched_basenames": list(group.unmatched_basenames),
    }


def _group_from_json(value: object) -> BrowseDirectoryGroup:
    if not isinstance(value, dict):
        raise ValueError(REMOTE_SCAN_REFERENCE_INVALID)
    samples = tuple(BrowseSampleRow(**item) for item in value.get("samples", []) if isinstance(item, dict))
    return BrowseDirectoryGroup(
        str(value["group_id"]), str(value["canonical_directory"]), samples,
        tuple(str(item) for item in value.get("unmatched_basenames", [])),
    )


def _record_payload(scan: StoredRemoteScan) -> dict[str, object]:
    return {
        "scan_id": scan.scan_id, "source_ref": scan.source_ref,
        "result_revision": scan.result_revision, "project_id": scan.project_id,
        "thread_id": scan.thread_id, "source": scan.source,
        "identity_digest": scan.identity_digest, "root_id": scan.root_id,
        "browse_policy_revision": scan.browse_policy_revision,
        "canonical_target": scan.canonical_target,
        "groups": [_group_to_json(group) for group in scan.groups],
        "truncated": scan.truncated, "created_at": scan.created_at,
        "expires_at": scan.expires_at, "apply_state": scan.apply_state,
        "apply_claim_id": scan.apply_claim_id, "apply_group_id": scan.apply_group_id,
        "claimed_at": scan.claimed_at, "consumed_at": scan.consumed_at,
        "tombstone_expires_at": scan.tombstone_expires_at,
    }


def _record_from_json(value: object) -> StoredRemoteScan:
    if not isinstance(value, dict):
        raise ValueError(REMOTE_SCAN_REFERENCE_INVALID)
    return StoredRemoteScan(
        scan_id=_validate_identifier(str(value["scan_id"]), "scan"),
        source_ref=_validate_identifier(str(value["source_ref"]), "src"),
        result_revision=str(value["result_revision"]), project_id=str(value["project_id"]),
        thread_id=None if value.get("thread_id") is None else str(value["thread_id"]),
        source=str(value["source"]), identity_digest=str(value["identity_digest"]),
        root_id=str(value["root_id"]), browse_policy_revision=str(value["browse_policy_revision"]),
        canonical_target=str(value["canonical_target"]),
        groups=tuple(_group_from_json(item) for item in value.get("groups", [])),
        truncated=bool(value["truncated"]), created_at=str(value["created_at"]),
        expires_at=str(value["expires_at"]), apply_state=str(value.get("apply_state", "pending")),
        apply_claim_id=value.get("apply_claim_id"), apply_group_id=value.get("apply_group_id"),
        claimed_at=value.get("claimed_at"), consumed_at=value.get("consumed_at"),
        tombstone_expires_at=value.get("tombstone_expires_at"),
    )


def _revision(*, result: BrowseResult, groups: tuple[BrowseDirectoryGroup, ...]) -> str:
    value = {
        "project_id": result.audit.project_id, "thread_id": result.audit.thread_id,
        "source": result.audit.source, "identity_digest": result.audit.identity_digest,
        "root_id": result.audit.root_id, "policy": result.audit.browse_policy_revision,
        "canonical_target": result.audit.canonical_target, "groups": [_group_to_json(g) for g in groups],
        "truncated": result.truncated,
    }
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    fd, temp = create_private_temp(path.parent, prefix=f".{path.name}.")
    try:
        raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(raw)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
        if os.name != "nt":
            dfd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temp is not None:
            try: Path(temp).unlink()
            except FileNotFoundError: pass


def _load(path: Path) -> StoredRemoteScan:
    verify_private_path(path)
    return _record_from_json(json.loads(path.read_text(encoding="utf-8")))


def _cleanup(directory: Path, now: datetime) -> None:
    for path in sorted(directory.glob("scan-*.record")):
        try:
            scan = _load(path)
        except Exception:
            continue
        if scan.apply_state in {"pending", "applying"} and _parse(scan.expires_at) <= now:
            expired = StoredRemoteScan(**{**asdict(scan), "apply_state": "expired", "tombstone_expires_at": _iso(now + timedelta(seconds=REMOTE_SCAN_TERMINAL_TTL_SECONDS))})
            _write_atomic(path, _record_payload(expired))
        elif scan.tombstone_expires_at and _parse(scan.tombstone_expires_at) <= now:
            try: path.unlink()
            except FileNotFoundError: pass


def store_remote_scan(project_dir: Path, result: BrowseResult) -> RemoteScanReference:
    if not result.ok:
        raise ValueError(REMOTE_SCAN_STORE_FAILED)
    directory = _store_dir(project_dir)
    now = _now()
    with shared_file_lock(_lock_path(directory)):
        _cleanup(directory, now)
        pending = []
        for path in directory.glob("scan-*.record"):
            try:
                scan = _load(path)
                if scan.apply_state in {"pending", "applying"} and scan.project_id == result.audit.project_id:
                    pending.append(scan)
            except Exception:
                continue
        if len(pending) >= MAX_PENDING_REMOTE_SCANS_PER_PROJECT:
            raise RuntimeError("REMOTE_SCAN_STORE_FULL")
        scan_id = "scan_" + secrets.token_hex(16)
        source_ref = "src_" + secrets.token_hex(16)
        groups = tuple(result.groups)
        created = _iso(now); expires = _iso(now + timedelta(seconds=REMOTE_SCAN_TTL_SECONDS))
        scan = StoredRemoteScan(
            scan_id, source_ref, _revision(result=result, groups=groups), result.audit.project_id,
            result.audit.thread_id, result.audit.source, result.audit.identity_digest,
            str(result.audit.root_id or ""), str(result.audit.browse_policy_revision or ""),
            str(result.audit.canonical_target or ""), groups, result.truncated, created, expires,
            "pending", None, None, None, None, None,
        )
        record_path = remote_scan_lookup_path(directory, scan_id, kind="scan")
        alias = remote_scan_lookup_path(directory, source_ref, kind="source")
        try:
            _write_atomic(record_path, _record_payload(scan))
            _write_atomic(alias, {"source_ref": source_ref, "scan_id": scan_id})
        except Exception as exc:
            # The exact result has not crossed the adapter boundary yet.  Remove
            # both halves when publication of the alias fails; a later recovery
            # pass must never expose a record that cannot be found by source_ref.
            for artifact in (record_path, alias):
                try:
                    artifact.unlink()
                except FileNotFoundError:
                    pass
            raise RuntimeError(REMOTE_SCAN_STORE_FAILED) from exc
    return RemoteScanReference(scan_id, source_ref, scan.result_revision, scan.project_id, scan.thread_id, scan.source, scan.expires_at)


def load_remote_scan(project_dir: Path, *, scan_id: str | None = None, source_ref: str | None = None) -> StoredRemoteScan:
    directory = _store_dir(project_dir)
    with shared_file_lock(_lock_path(directory)):
        _cleanup(directory, _now())
        if source_ref is not None:
            alias = remote_scan_lookup_path(directory, source_ref, kind="source")
            data = json.loads(alias.read_text(encoding="utf-8")); scan_id = str(data["scan_id"])
        if scan_id is None:
            raise ValueError(REMOTE_SCAN_REFERENCE_INVALID)
        scan = _load(remote_scan_lookup_path(directory, scan_id, kind="scan"))
        if scan.apply_state == "expired": raise ValueError(REMOTE_SCAN_REFERENCE_EXPIRED)
        if scan.apply_state == "consumed": raise ValueError(REMOTE_SCAN_REFERENCE_USED)
        return scan


def discard_remote_scan(project_dir: Path, reference: RemoteScanReference) -> None:
    directory = _store_dir(project_dir)
    with shared_file_lock(_lock_path(directory)):
        path = remote_scan_lookup_path(directory, reference.scan_id, kind="scan")
        try: scan = _load(path)
        except FileNotFoundError: return
        if scan.source_ref != reference.source_ref or scan.result_revision != reference.result_revision or scan.project_id != reference.project_id or scan.apply_state != "pending":
            return
        try: path.unlink()
        except FileNotFoundError: pass
        try: remote_scan_lookup_path(directory, reference.source_ref, kind="source").unlink()
        except FileNotFoundError: pass


def consume_remote_scan(project_dir: Path, *, scan_id: str, result_revision: str, group_id: str, expected_policy: BrowsePolicy, expected_context: BrowseContext, apply: Callable[[BrowseDirectoryGroup, str], None]) -> BrowseDirectoryGroup:
    raise NotImplementedError
