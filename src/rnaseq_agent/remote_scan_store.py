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
from dataclasses import asdict, dataclass, replace
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
    _identity_digest,
    contains_posix_path,
    validate_remote_browse_path,
)
from .storage import shared_file_lock

REMOTE_SCAN_TTL_SECONDS = 15 * 60
REMOTE_SCAN_TERMINAL_TTL_SECONDS = 24 * 60 * 60
MAX_PENDING_REMOTE_SCANS_PER_PROJECT = 8
_ID_RE = re.compile(r"^(scan|src)_[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECORD_FIELDS = frozenset(
    {
        "scan_id", "source_ref", "result_revision", "project_id", "thread_id",
        "source", "identity_digest", "root_id", "browse_policy_revision",
        "canonical_target", "groups", "truncated", "created_at", "expires_at",
        "apply_state", "apply_claim_id", "apply_group_id", "claimed_at",
        "consumed_at", "tombstone_expires_at",
    }
)
_GROUP_FIELDS = frozenset({"group_id", "canonical_directory", "samples", "unmatched_basenames"})
_SAMPLE_FIELDS = frozenset({"sample_id", "fastq_1", "fastq_2"})
_SOURCES = frozenset({"workbench", "project_command", "rule_chat", "llm_tool"})
_APPLY_STATES = frozenset({"pending", "applying", "consumed", "expired"})
_MAX_TEXT = 4096
_MAX_GROUPS = 1024
_MAX_GROUP_SAMPLES = 2500
_MAX_UNMATCHED = 500


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


def _scan_error(code: str) -> ValueError:
    """Return the stable public error used by the HTTP adapters.

    The store intentionally does not depend on FastAPI.  Adapters can inspect
    ``str(exc)`` (or ``exc.args[0]``) and preserve the fixed error contract.
    """
    return ValueError(code)


def _validate_identifier(identifier: str, prefix: str) -> str:
    if not isinstance(identifier, str) or not _ID_RE.fullmatch(identifier) or not identifier.startswith(prefix + "_"):
        raise ValueError(REMOTE_SCAN_REFERENCE_INVALID)
    return identifier


def _strict_text(value: object, *, field: str, max_length: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    return value


def _strict_timestamp(value: object) -> str:
    text = _strict_text(value, field="timestamp", max_length=64)
    try:
        _parse(text)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID) from exc
    return text


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
        result[key] = value
    return result


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
    if not isinstance(value, dict) or set(value) != _GROUP_FIELDS:
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    group_id = _strict_text(value["group_id"], field="group_id", max_length=256)
    canonical_directory = _strict_text(value["canonical_directory"], field="canonical_directory")
    try:
        validate_remote_browse_path(canonical_directory)
    except ValueError as exc:
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID) from exc
    raw_samples = value["samples"]
    raw_unmatched = value["unmatched_basenames"]
    if not isinstance(raw_samples, list) or len(raw_samples) > _MAX_GROUP_SAMPLES:
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    if not isinstance(raw_unmatched, list) or len(raw_unmatched) > _MAX_UNMATCHED:
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    samples_list: list[BrowseSampleRow] = []
    for item in raw_samples:
        if not isinstance(item, dict) or set(item) != _SAMPLE_FIELDS:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
        sample_id = _strict_text(item["sample_id"], field="sample_id", max_length=512)
        fastq_1 = _strict_text(item["fastq_1"], field="fastq_1", max_length=512)
        fastq_2_value = item["fastq_2"]
        if fastq_2_value is not None:
            fastq_2_value = _strict_text(fastq_2_value, field="fastq_2", max_length=512)
        samples_list.append(BrowseSampleRow(sample_id, fastq_1, fastq_2_value))
    unmatched = tuple(_strict_text(item, field="unmatched", max_length=512) for item in raw_unmatched)
    return BrowseDirectoryGroup(
        group_id, canonical_directory, tuple(samples_list), unmatched,
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


def _replace_scan(scan: StoredRemoteScan, **changes: object) -> StoredRemoteScan:
    """Dataclass replace that preserves tuple[BrowseDirectoryGroup, ...]."""
    return replace(scan, **changes)


def _record_from_json(value: object) -> StoredRemoteScan:
    if not isinstance(value, dict) or set(value) != _RECORD_FIELDS:
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    scan_id = _validate_identifier(value["scan_id"], "scan")
    source_ref = _validate_identifier(value["source_ref"], "src")
    result_revision = _strict_text(value["result_revision"], field="result_revision", max_length=80)
    if not _DIGEST_RE.fullmatch(result_revision):
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    project_id = _strict_text(value["project_id"], field="project_id")
    thread_id = value["thread_id"]
    if thread_id is not None:
        thread_id = _strict_text(thread_id, field="thread_id")
    source = _strict_text(value["source"], field="source", max_length=32)
    if source not in _SOURCES:
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    identity_digest = _strict_text(value["identity_digest"], field="identity_digest", max_length=80)
    if not _DIGEST_RE.fullmatch(identity_digest):
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    root_id = _strict_text(value["root_id"], field="root_id", max_length=256)
    browse_policy_revision = _strict_text(value["browse_policy_revision"], field="browse_policy_revision", max_length=80)
    if not _DIGEST_RE.fullmatch(browse_policy_revision):
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    canonical_target = _strict_text(value["canonical_target"], field="canonical_target")
    try:
        validate_remote_browse_path(canonical_target)
    except ValueError as exc:
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID) from exc
    groups_raw = value["groups"]
    if not isinstance(groups_raw, list) or len(groups_raw) > _MAX_GROUPS:
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    groups = tuple(_group_from_json(item) for item in groups_raw)
    if type(value["truncated"]) is not bool:
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    created_at = _strict_timestamp(value["created_at"])
    expires_at = _strict_timestamp(value["expires_at"])
    apply_state = value["apply_state"]
    if not isinstance(apply_state, str) or apply_state not in _APPLY_STATES:
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    optional_ids: dict[str, str | None] = {}
    for field in ("apply_claim_id", "apply_group_id"):
        raw = value[field]
        if raw is not None:
            raw = _strict_text(raw, field=field, max_length=256)
        optional_ids[field] = raw
    optional_times: dict[str, str | None] = {}
    for field in ("claimed_at", "consumed_at", "tombstone_expires_at"):
        raw = value[field]
        optional_times[field] = None if raw is None else _strict_timestamp(raw)
    return StoredRemoteScan(
        scan_id=scan_id, source_ref=source_ref, result_revision=result_revision,
        project_id=project_id, thread_id=thread_id, source=source,
        identity_digest=identity_digest, root_id=root_id,
        browse_policy_revision=browse_policy_revision, canonical_target=canonical_target,
        groups=groups, truncated=value["truncated"], created_at=created_at,
        expires_at=expires_at, apply_state=apply_state,
        apply_claim_id=optional_ids["apply_claim_id"], apply_group_id=optional_ids["apply_group_id"],
        claimed_at=optional_times["claimed_at"], consumed_at=optional_times["consumed_at"],
        tombstone_expires_at=optional_times["tombstone_expires_at"],
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
            view = memoryview(raw)
            while view:
                written = handle.write(view)
                if not written:
                    raise OSError("short remote scan write")
                view = view[written:]
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
    return _record_from_json(
        json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    )


def _root_for_scan(scan: StoredRemoteScan, policy: BrowsePolicy):
    if not policy.available or policy.identity is None:
        return None
    if scan.identity_digest != _identity_digest(policy.identity):
        return None
    if scan.browse_policy_revision != policy.revision:
        return None
    return next(
        (
            root
            for root in policy.roots
            if root.root_id == scan.root_id
            and root.host == policy.identity.host
            and root.user == policy.identity.user
            and root.port == policy.identity.port
            and root.revoked_at is None
            and contains_posix_path(root.canonical_path, scan.canonical_target)
        ),
        None,
    )


def _validate_group_for_apply(group: BrowseDirectoryGroup) -> None:
    """Reject malformed/ambiguous server records before invoking a writer."""
    from .safety import identifier_error, relative_filename_error

    if not group.group_id or not group.canonical_directory.startswith("/"):
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    for row in group.samples:
        if not isinstance(row.sample_id, str) or not row.sample_id.strip():
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
        if identifier_error(row.sample_id, "sample_id"):
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
        if row.sample_id in seen_ids:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
        seen_ids.add(row.sample_id)
        for filename in (row.fastq_1, row.fastq_2):
            if filename is None:
                continue
            if not isinstance(filename, str) or relative_filename_error(filename, "fastq"):
                raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
            if filename in seen_files:
                raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
            seen_files.add(filename)


def _cleanup(directory: Path, now: datetime) -> None:
    for path in sorted(directory.glob("scan-*.record")):
        try:
            scan = _load(path)
        except Exception:
            continue
        if scan.apply_state in {"pending", "applying"} and _parse(scan.expires_at) <= now:
            expired = _replace_scan(
                scan,
                apply_state="expired",
                tombstone_expires_at=_iso(now + timedelta(seconds=REMOTE_SCAN_TERMINAL_TTL_SECONDS)),
            )
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
        if source_ref is not None:
            alias = remote_scan_lookup_path(directory, source_ref, kind="source")
            try:
                verify_private_path(alias)
                data = json.loads(
                    alias.read_text(encoding="utf-8"),
                    object_pairs_hook=_reject_duplicate_json_keys,
                )
                if (
                    not isinstance(data, dict)
                    or set(data) != {"source_ref", "scan_id"}
                    or data.get("source_ref") != source_ref
                ):
                    raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
                scan_id = _validate_identifier(data.get("scan_id"), "scan")
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID) from exc
        if scan_id is None:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
        try:
            scan = _load(remote_scan_lookup_path(directory, scan_id, kind="scan"))
        except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc) in {
                REMOTE_SCAN_REFERENCE_EXPIRED,
                REMOTE_SCAN_REFERENCE_USED,
            }:
                raise
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID) from exc
        if source_ref is not None and scan.source_ref != source_ref:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
        if scan.apply_state == "expired": raise _scan_error(REMOTE_SCAN_REFERENCE_EXPIRED)
        if scan.apply_state == "consumed": raise _scan_error(REMOTE_SCAN_REFERENCE_USED)
        return scan


def discard_remote_scan(project_dir: Path, reference: RemoteScanReference) -> None:
    directory = _store_dir(project_dir)
    with shared_file_lock(_lock_path(directory)):
        path = remote_scan_lookup_path(directory, reference.scan_id, kind="scan")
        try: scan = _load(path)
        except FileNotFoundError: return
        if (
            scan.source_ref != reference.source_ref
            or scan.result_revision != reference.result_revision
            or scan.project_id != reference.project_id
            or scan.thread_id != reference.thread_id
            or scan.source != reference.source
            or scan.expires_at != reference.expires_at
            or scan.apply_state != "pending"
        ):
            return
        try: path.unlink()
        except FileNotFoundError: pass
        try: remote_scan_lookup_path(directory, reference.source_ref, kind="source").unlink()
        except FileNotFoundError: pass


def consume_remote_scan(
    project_dir: Path,
    *,
    scan_id: str,
    result_revision: str,
    group_id: str,
    expected_policy: BrowsePolicy,
    expected_context: BrowseContext,
    apply: Callable[[BrowseDirectoryGroup, str], None],
    accept_truncated: bool = False,
) -> BrowseDirectoryGroup:
    """Claim and consume one server-produced browse group exactly once.

    The selected directory and FASTQ names come exclusively from the private
    scan record.  A durable ``applying`` state is written before the callback,
    and the same opaque claim id is reused on retries after a process crash.
    The callback owns its own idempotent project receipt; once it returns, the
    record is durably marked ``consumed``.
    """
    _validate_identifier(scan_id, "scan")
    if not isinstance(result_revision, str) or not result_revision.startswith("sha256:"):
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    if not isinstance(group_id, str) or not group_id or any(ch in group_id for ch in "\\/\x00"):
        raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
    directory = _store_dir(project_dir)
    path = remote_scan_lookup_path(directory, scan_id, kind="scan")
    now = _now()
    with shared_file_lock(_lock_path(directory)):
        # Classify the presented target before housekeeping can remove an
        # unrelated terminal record.  This keeps replay/expiry deterministic.
        try:
            scan = _load(path)
        except FileNotFoundError as exc:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID) from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID) from exc

        if scan.apply_state == "expired":
            raise _scan_error(REMOTE_SCAN_REFERENCE_EXPIRED)
        if scan.apply_state == "consumed":
            raise _scan_error(REMOTE_SCAN_REFERENCE_USED)
        if scan.project_id != expected_context.project_id or scan.thread_id != expected_context.thread_id or scan.source != expected_context.source:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
        if expected_context.source != "workbench" or expected_context.thread_id is not None:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
        if scan.result_revision != result_revision:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
        if _parse(scan.expires_at) <= now:
            expired = _replace_scan(
                scan,
                apply_state="expired",
                tombstone_expires_at=_iso(now + timedelta(seconds=REMOTE_SCAN_TERMINAL_TTL_SECONDS)),
            )
            _write_atomic(path, _record_payload(expired))
            raise _scan_error(REMOTE_SCAN_REFERENCE_EXPIRED)
        root = _root_for_scan(scan, expected_policy)
        if root is None:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
        groups = {group.group_id: group for group in scan.groups}
        if group_id not in groups:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
        group = groups[group_id]
        _validate_group_for_apply(group)
        if scan.truncated and not accept_truncated:
            # There is no stable dedicated truncation code in the fixed
            # contract; a conflict is the HTTP-safe refusal for missing
            # explicit acknowledgement.
            raise _scan_error("REMOTE_SCAN_TRUNCATED_ACK_REQUIRED")

        claim_id = scan.apply_claim_id
        if scan.apply_state == "pending":
            claim_id = "claim_" + secrets.token_hex(16)
            scan = _replace_scan(
                scan,
                apply_state="applying",
                apply_claim_id=claim_id,
                apply_group_id=group.group_id,
                claimed_at=_iso(now),
            )
            _write_atomic(path, _record_payload(scan))
        elif scan.apply_state == "applying":
            if not scan.apply_claim_id or scan.apply_group_id != group.group_id:
                raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)
            claim_id = scan.apply_claim_id
        else:
            raise _scan_error(REMOTE_SCAN_REFERENCE_INVALID)

        assert claim_id is not None
        # Keep the scan-store lock while invoking the idempotent project
        # callback.  Competing consumers therefore cannot create a second
        # claim; a crash releases the OS lock and leaves ``applying`` durable.
        apply(group, claim_id)
        consumed = _replace_scan(
            scan,
            apply_state="consumed",
            consumed_at=_iso(_now()),
            tombstone_expires_at=_iso(_now() + timedelta(seconds=REMOTE_SCAN_TERMINAL_TTL_SECONDS)),
        )
        _write_atomic(path, _record_payload(consumed))
        return group
