"""Authoritative bounded audit sink for remote browse attempts."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from .connection_store import connection_home
from .private_files import create_private_temp, ensure_private_directory, verify_private_path
from .remote_browse import BrowseAudit
from .storage import shared_file_lock


class AuditCommitUncertainError(RuntimeError):
    pass


_EVENT_RE = re.compile(r"^audit_[0-9a-f]{32}$")


def _audit_dir() -> Path:
    return ensure_private_directory(connection_home() / "security" / "remote-browse-audit")


def _canonical(event: BrowseAudit) -> bytes:
    if not _EVENT_RE.fullmatch(event.event_id):
        raise ValueError("invalid audit event id")
    payload = {
        "event_id": event.event_id, "project_id": event.project_id, "thread_id": event.thread_id,
        "source": event.source, "identity_digest": event.identity_digest,
        "requested_path_digest": event.requested_path_digest, "canonical_target": event.canonical_target,
        "root_id": event.root_id, "browse_policy_revision": event.browse_policy_revision,
        "directory_count": event.directory_count, "sample_count": event.sample_count,
        "unmatched_count": event.unmatched_count, "truncated": event.truncated,
        "outcome": event.outcome, "error_code": event.error_code,
        "started_at": event.started_at, "completed_at": event.completed_at,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > 32 * 1024:
        raise ValueError("audit record exceeds bounded size")
    return raw


def _path(directory: Path, event: BrowseAudit) -> Path:
    digest = hashlib.sha256(event.event_id.encode("ascii")).hexdigest()
    return directory / f"event-{digest}.record"


def _write(path: Path, raw: bytes) -> None:
    fd, tmp = create_private_temp(path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            view = memoryview(raw)
            while view:
                written = handle.write(view)
                if not written:
                    raise OSError("short audit write")
                view = view[written:]
            handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, path); tmp = None
        if os.name != "nt":
            dfd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
    finally:
        if fd >= 0: os.close(fd)
        if tmp is not None:
            try: Path(tmp).unlink()
            except FileNotFoundError: pass


def _parse(path: Path) -> bytes:
    verify_private_path(path)
    raw = path.read_bytes()
    json.loads(raw)
    return raw


def _record_browse_audit(event: BrowseAudit) -> str:
    directory = _audit_dir()
    path = _path(directory, event)
    raw = _canonical(event)
    with shared_file_lock(directory / ".audit.lock"):
        if path.exists():
            existing = _parse(path)
            if existing != raw:
                raise RuntimeError("AUDIT_EVENT_CONFLICT")
            return event.event_id
        try:
            _write(path, raw)
        except OSError as exc:
            # os.replace may already have published the destination before a
            # containing-directory fsync fails.  Preserve that record and make
            # the durability state explicit to the finalizer/reconciler.
            if path.exists():
                raise AuditCommitUncertainError("audit publication durability is uncertain") from exc
            raise
    return event.event_id


def reconcile_browse_audit(event: BrowseAudit) -> Literal["committed", "absent", "uncertain"]:
    directory = _audit_dir()
    path = _path(directory, event)
    try:
        if _parse(path) == _canonical(event):
            return "committed"
        return "uncertain"
    except FileNotFoundError:
        return "absent"
    except Exception:
        return "uncertain"


def browse_history_projection(event: BrowseAudit) -> dict[str, object]:
    return {
        "event_id": event.event_id,
        "source": event.source,
        "directory_count": event.directory_count,
        "sample_count": event.sample_count,
        "unmatched_count": event.unmatched_count,
        "truncated": event.truncated,
        "outcome": event.outcome,
        "error_code": event.error_code,
        "started_at": event.started_at,
        "completed_at": event.completed_at,
    }
