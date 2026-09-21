from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Callable, Literal

from .connection_store import ApprovedDataRoot, BrowsePolicy
from .execution import (
    CommandOutputLimitError,
    CommandResult,
    CommandTimeoutError,
)
from .ssh_identity import SSHIdentity
from .remote_transport import DeadlineAwareRemoteTransport

REMOTE_PATH_INVALID = "REMOTE_PATH_INVALID"
REMOTE_ROOT_NOT_APPROVED = "REMOTE_ROOT_NOT_APPROVED"
REMOTE_ROOT_STALE = "REMOTE_ROOT_STALE"
REMOTE_PATH_NOT_FOUND = "REMOTE_PATH_NOT_FOUND"
REMOTE_PATH_NOT_DIRECTORY = "REMOTE_PATH_NOT_DIRECTORY"
REMOTE_PATH_ESCAPE = "REMOTE_PATH_ESCAPE"
REMOTE_POLICY_CHANGED = "REMOTE_POLICY_CHANGED"
REMOTE_SCAN_TIMEOUT = "REMOTE_SCAN_TIMEOUT"
REMOTE_SCAN_OUTPUT_LIMIT = "REMOTE_SCAN_OUTPUT_LIMIT"
REMOTE_SCAN_INVALID_OUTPUT = "REMOTE_SCAN_INVALID_OUTPUT"
REMOTE_SCAN_FAILED = "REMOTE_SCAN_FAILED"
REMOTE_SCAN_PYTHON3_REQUIRED = "REMOTE_SCAN_PYTHON3_REQUIRED"
REMOTE_ROOT_LIMIT = "REMOTE_ROOT_LIMIT"
REMOTE_ROOT_REVISION_CONFLICT = "REMOTE_ROOT_REVISION_CONFLICT"
REMOTE_SCAN_REFERENCE_INVALID = "REMOTE_SCAN_REFERENCE_INVALID"
REMOTE_SCAN_REFERENCE_EXPIRED = "REMOTE_SCAN_REFERENCE_EXPIRED"
REMOTE_SCAN_REFERENCE_USED = "REMOTE_SCAN_REFERENCE_USED"
REMOTE_SCAN_STORE_FULL = "REMOTE_SCAN_STORE_FULL"
REMOTE_SCAN_STORE_FAILED = "REMOTE_SCAN_STORE_FAILED"
REMOTE_SECURITY_AUDIT_FAILED = "REMOTE_SECURITY_AUDIT_FAILED"
PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"

MAX_ACTIVE_ROOTS = 32
MAX_SCAN_DEPTH = 2
MAX_SCAN_CANDIDATES = 5_000
SCAN_TIMEOUT_SECONDS = 60.0
MAX_SCAN_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_RETURNED_SAMPLES = 2_500
MAX_RETURNED_UNMATCHED = 500


@dataclass
class BrowseExecutionBudget:
    absolute_deadline: float
    remaining_output_bytes: int

    @classmethod
    def start(cls, *, timeout_seconds: float, max_output_bytes: int) -> "BrowseExecutionBudget":
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        return cls(
            absolute_deadline=time.monotonic() + timeout_seconds,
            remaining_output_bytes=max_output_bytes,
        )

    def execute(self, transport: DeadlineAwareRemoteTransport, remote_command: str) -> CommandResult:
        if self.remaining_output_bytes <= 0:
            raise CommandOutputLimitError("browse output budget exhausted")
        if time.monotonic() >= self.absolute_deadline:
            raise CommandTimeoutError("browse execution deadline exceeded")
        result = transport.execute_bounded(
            remote_command,
            absolute_deadline=self.absolute_deadline,
            max_capture_bytes=self.remaining_output_bytes,
        )
        captured = int(getattr(result, "captured_bytes", 0))
        if captured < 0 or captured > self.remaining_output_bytes:
            raise CommandOutputLimitError("invalid browse output accounting")
        self.remaining_output_bytes -= captured
        return result


@dataclass(frozen=True)
class BrowseContext:
    project_id: str
    thread_id: str | None
    source: Literal["workbench", "project_command", "rule_chat", "llm_tool"]


@dataclass(frozen=True)
class BrowseSampleRow:
    sample_id: str
    fastq_1: str
    fastq_2: str | None


@dataclass(frozen=True)
class BrowseDirectoryGroup:
    group_id: str
    canonical_directory: str
    samples: tuple[BrowseSampleRow, ...]
    unmatched_basenames: tuple[str, ...]


@dataclass(frozen=True)
class BrowseScanPayload:
    groups: tuple[BrowseDirectoryGroup, ...]
    sample_count: int
    unmatched_count: int
    truncated: bool


@dataclass(frozen=True)
class ScanCommand:
    argv: tuple[str, ...]
    helper_source: str
    remote_command: str
    max_depth: int
    max_candidates: int


class ScanProtocolError(ValueError):
    """The remote scanner returned a document outside its bounded protocol."""


class ScanPathEscape(ValueError):
    """A scanner candidate escaped the authorized target/root."""


class ScanPython3Required(ScanProtocolError):
    """The remote scanner host does not provide the required Python 3 runtime."""


BrowseScanner = Callable[[str, str, BrowseExecutionBudget, DeadlineAwareRemoteTransport], BrowseScanPayload]


_SCAN_HELPER_SOURCE = r'''import json, os, sys
arguments = sys.argv[1:]
if arguments and arguments[0] == '--':
    arguments = arguments[1:]
root, depth_limit, item_limit, deadline, target = arguments
depth_limit = int(depth_limit)
item_limit = int(item_limit)
root = os.path.realpath(root)
target = os.path.realpath(target)
def inside(parent, child):
    try:
        return os.path.commonpath((parent, child)) == parent
    except ValueError:
        return False
if not os.path.isdir(root) or not os.path.isdir(target) or not inside(root, target):
    raise SystemExit(44)
files = []
truncated = False
stack = [(target, 0)]
while stack:
    directory, depth = stack.pop()
    try:
        entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError:
        continue
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            if depth < depth_limit:
                stack.append((entry.path, depth + 1))
            continue
        if not entry.is_file(follow_symlinks=False):
            continue
        name = entry.name.lower()
        if not (name.endswith('.fastq') or name.endswith('.fq') or name.endswith('.fastq.gz') or name.endswith('.fq.gz')):
            continue
        candidate = os.path.realpath(entry.path)
        if not inside(root, candidate) or not inside(target, candidate):
            raise SystemExit(45)
        if len(files) >= item_limit:
            truncated = True
            break
        files.append(candidate)
    if truncated:
        break
print(json.dumps({'files': files, 'truncated': truncated}, ensure_ascii=False, separators=(',', ':')))
'''


@dataclass(frozen=True)
class BrowseAudit:
    event_id: str
    project_id: str
    thread_id: str | None
    source: str
    identity_digest: str
    requested_path_digest: str
    canonical_target: str | None
    root_id: str | None
    browse_policy_revision: str | None
    directory_count: int
    sample_count: int
    unmatched_count: int
    truncated: bool
    outcome: Literal["allowed", "denied", "failed"]
    error_code: str | None
    started_at: str
    completed_at: str


@dataclass(frozen=True)
class BrowseResult:
    ok: bool
    blocked: bool
    error_code: str | None
    message: str
    groups: tuple[BrowseDirectoryGroup, ...]
    sample_count: int
    unmatched_count: int
    truncated: bool
    authorization: dict[str, str] | None
    audit: BrowseAudit


def validate_remote_browse_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(REMOTE_PATH_INVALID)
    if value != value.strip():
        raise ValueError(REMOTE_PATH_INVALID)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(REMOTE_PATH_INVALID)
    if not value.startswith("/") or value.startswith("//"):
        raise ValueError(REMOTE_PATH_INVALID)
    if value != "/" and value.endswith("/"):
        raise ValueError(REMOTE_PATH_INVALID)
    pieces = value.split("/")
    if any(piece in {"", ".", ".."} for piece in pieces[1:]):
        raise ValueError(REMOTE_PATH_INVALID)
    return value


def contains_posix_path(root: str, candidate: str) -> bool:
    root_parts = PurePosixPath(root).parts
    candidate_parts = PurePosixPath(candidate).parts
    return candidate_parts[: len(root_parts)] == root_parts


def resolve_remote_directory(
    requested_path: str,
    runner: BrowseExecutionBudget,
    transport: DeadlineAwareRemoteTransport,
) -> str:
    quoted = shlex.quote(requested_path)
    command = (
        f"resolved=$(realpath -e -- {quoted}) || exit 44; "
        f"[ -d \"$resolved\" ] || exit 45; "
        "printf '%s\\n' \"$resolved\""
    )
    result = runner.execute(transport, command)
    if result.returncode == 44:
        raise _BrowseResolutionError(REMOTE_PATH_NOT_FOUND)
    if result.returncode == 45:
        raise _BrowseResolutionError(REMOTE_PATH_NOT_DIRECTORY)
    if result.returncode != 0:
        diagnostics = f"{result.stdout}\n{result.stderr}".lower()
        if "not a directory" in diagnostics or "not_directory" in diagnostics:
            raise _BrowseResolutionError(REMOTE_PATH_NOT_DIRECTORY)
        if "no such" in diagnostics or "not found" in diagnostics or "missing" in diagnostics:
            raise _BrowseResolutionError(REMOTE_PATH_NOT_FOUND)
        raise _BrowseResolutionError(REMOTE_SCAN_FAILED)
    canonical = result.stdout.strip("\r\n")
    try:
        canonical = validate_remote_browse_path(canonical)
    except ValueError as exc:
        raise _BrowseResolutionError(REMOTE_PATH_ESCAPE) from exc
    return canonical


def provider_browse_summary(result: BrowseResult, _source_ref: str | None) -> dict[str, object]:
    return {
        "ok": result.ok,
        "blocked": result.blocked,
        "error_code": result.error_code,
        "directory_count": len(result.groups),
        "sample_count": result.sample_count,
        "unmatched_count": result.unmatched_count,
        "truncated": result.truncated,
    }


def browse_log_projection(event: BrowseAudit) -> dict[str, object]:
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


def scan_remote_fastqs(
    canonical_target: str,
    canonical_root: str,
    runner: BrowseExecutionBudget,
    transport: DeadlineAwareRemoteTransport,
) -> BrowseScanPayload:
    command = build_scan_command(canonical_root, canonical_target, absolute_deadline=runner.absolute_deadline)
    result = runner.execute(transport, command.remote_command)
    if result.returncode == 46:
        raise ScanPython3Required(REMOTE_SCAN_PYTHON3_REQUIRED)
    if result.returncode == 44:
        raise ScanPathEscape(REMOTE_PATH_ESCAPE)
    if result.returncode == 45:
        raise ScanPathEscape(REMOTE_PATH_ESCAPE)
    if result.returncode != 0:
        raise ScanProtocolError(REMOTE_SCAN_INVALID_OUTPUT)
    try:
        document = json.loads(result.stdout, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, ScanProtocolError) as exc:
        raise ScanProtocolError(REMOTE_SCAN_INVALID_OUTPUT) from exc
    return _scan_payload_from_document(document, canonical_target, canonical_root)


def build_scan_command(
    canonical_root: str,
    canonical_target: str,
    *,
    absolute_deadline: float | None = None,
) -> ScanCommand:
    deadline_value = (
        max(0.0, absolute_deadline - time.monotonic())
        if absolute_deadline is not None
        else SCAN_TIMEOUT_SECONDS
    )
    deadline = str(deadline_value)
    # Keep all user-controlled values outside the fixed helper source. The
    # target is deliberately the final positional value for easy framing audits.
    argv = (
        "python3",
        "-c",
        _SCAN_HELPER_SOURCE,
        "--",
        canonical_root,
        str(MAX_SCAN_DEPTH),
        str(MAX_SCAN_CANDIDATES),
        deadline,
        canonical_target,
    )
    quoted = " ".join(shlex.quote(value) for value in argv)
    remote_command = "command -v python3 >/dev/null 2>&1 || exit 46; exec " + quoted
    return ScanCommand(
        argv=argv,
        helper_source=_SCAN_HELPER_SOURCE,
        remote_command=remote_command,
        max_depth=MAX_SCAN_DEPTH,
        max_candidates=MAX_SCAN_CANDIDATES,
    )


def scan_result_from_canonical_files(paths: list[str]) -> BrowseScanPayload:
    return _group_canonical_files(paths)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ScanProtocolError(REMOTE_SCAN_INVALID_OUTPUT)
        result[key] = value
    return result


def _scan_payload_from_document(
    document: object,
    canonical_target: str,
    canonical_root: str,
) -> BrowseScanPayload:
    if not isinstance(document, dict) or set(document) != {"files", "truncated"}:
        raise ScanProtocolError(REMOTE_SCAN_INVALID_OUTPUT)
    files = document["files"]
    producer_truncated = document["truncated"]
    if not isinstance(files, list) or not isinstance(producer_truncated, bool):
        raise ScanProtocolError(REMOTE_SCAN_INVALID_OUTPUT)
    if len(files) > MAX_SCAN_CANDIDATES:
        raise ScanProtocolError(REMOTE_SCAN_INVALID_OUTPUT)
    if len(files) == MAX_SCAN_CANDIDATES and not producer_truncated:
        raise ScanProtocolError(REMOTE_SCAN_INVALID_OUTPUT)
    filtered: list[str] = []
    for candidate in files:
        if not isinstance(candidate, str) or not candidate.startswith("/"):
            raise ScanProtocolError(REMOTE_SCAN_INVALID_OUTPUT)
        try:
            validate_remote_browse_path(candidate)
        except ValueError as exc:
            raise ScanProtocolError(REMOTE_SCAN_INVALID_OUTPUT) from exc
        if not contains_posix_path(canonical_root, candidate) or not contains_posix_path(canonical_target, candidate):
            raise ScanPathEscape(REMOTE_PATH_ESCAPE)
        filtered.append(candidate)
    payload = _group_canonical_files(filtered)
    if producer_truncated:
        return BrowseScanPayload(payload.groups, payload.sample_count, payload.unmatched_count, True)
    return payload


_FASTQ_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq")
_PAIR_RE = re.compile(
    r"^(?P<sample>.+?)(?:[_\.](?:R)?(?P<read>[12]))(?P<suffix>\.f(?:ast)?q(?:\.gz)?)$",
    re.IGNORECASE,
)


def _fastq_parts(basename: str) -> tuple[str, int] | None:
    match = _PAIR_RE.match(basename)
    if match is None:
        return None
    return match.group("sample"), int(match.group("read"))


def _group_canonical_files(paths: list[str]) -> BrowseScanPayload:
    grouped: dict[str, list[str]] = {}
    for path in paths:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ScanProtocolError(REMOTE_SCAN_INVALID_OUTPUT)
        try:
            validate_remote_browse_path(path)
        except ValueError as exc:
            raise ScanProtocolError(REMOTE_SCAN_INVALID_OUTPUT) from exc
        basename = PurePosixPath(path).name
        if not basename.lower().endswith(_FASTQ_SUFFIXES):
            continue
        parent = str(PurePosixPath(path).parent)
        grouped.setdefault(parent, []).append(path)

    groups: list[BrowseDirectoryGroup] = []
    unmatched_total = 0
    sample_total = 0
    truncated = False
    for parent in sorted(grouped):
        by_sample: dict[str, dict[int, str]] = {}
        unmatched: list[str] = []
        for path in sorted(set(grouped[parent])):
            basename = PurePosixPath(path).name
            parts = _fastq_parts(basename)
            if parts is None:
                unmatched.append(basename)
                continue
            sample, read = parts
            by_sample.setdefault(sample, {})[read] = basename
        rows: list[BrowseSampleRow] = []
        for sample in sorted(by_sample):
            reads = by_sample[sample]
            if 1 not in reads:
                unmatched.append(reads.get(2, sample))
                continue
            rows.append(BrowseSampleRow(sample, reads[1], reads.get(2)))
        rows.sort(key=lambda row: (row.sample_id, row.fastq_1, row.fastq_2 or ""))
        unmatched = sorted(set(unmatched))
        if sample_total + len(rows) > MAX_RETURNED_SAMPLES:
            keep = max(0, MAX_RETURNED_SAMPLES - sample_total)
            rows = rows[:keep]
            truncated = True
        if unmatched_total + len(unmatched) > MAX_RETURNED_UNMATCHED:
            keep = max(0, MAX_RETURNED_UNMATCHED - unmatched_total)
            unmatched = unmatched[:keep]
            truncated = True
        group_id = _group_digest("", "", parent, "")
        groups.append(BrowseDirectoryGroup(group_id, parent, tuple(rows), tuple(unmatched)))
        sample_total += len(rows)
        unmatched_total += len(unmatched)
    return BrowseScanPayload(tuple(groups), sample_total, unmatched_total, truncated)


def _group_digest(
    authorization_revision: str,
    selected_root_id: str,
    canonical_parent: str,
    result_identity: str,
) -> str:
    value = "\x00".join((authorization_revision, selected_root_id, canonical_parent, result_identity))
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rebind_group_ids(
    payload: BrowseScanPayload,
    authorization_revision: str,
    selected_root_id: str,
    result_identity: str,
) -> BrowseScanPayload:
    groups = tuple(
        BrowseDirectoryGroup(
            _group_digest(authorization_revision, selected_root_id, group.canonical_directory, result_identity),
            group.canonical_directory,
            group.samples,
            group.unmatched_basenames,
        )
        for group in payload.groups
    )
    return BrowseScanPayload(groups, payload.sample_count, payload.unmatched_count, payload.truncated)


def browse_remote_fastqs(
    requested_path: str,
    context: BrowseContext,
    policy_reader: Callable[[], BrowsePolicy],
    transport_factory: Callable[[SSHIdentity], DeadlineAwareRemoteTransport],
    scanner: BrowseScanner | None = None,
) -> BrowseResult:
    scanner = scanner or scan_remote_fastqs
    started_at = _utc_now()
    # Authoritative audit records use a stable, self-describing opaque id.
    event_id = "audit_" + uuid.uuid4().hex
    requested_digest = _digest(requested_path)

    try:
        target = validate_remote_browse_path(requested_path)
    except ValueError:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=None,
            requested_path=requested_path,
            canonical_target=None,
            root_id=None,
            revision=None,
            error_code=REMOTE_PATH_INVALID,
            message="Remote browse path is invalid.",
        )

    try:
        initial = policy_reader()
    except Exception:
        initial = None
    if (
        initial is None
        or not initial.available
        or initial.identity is None
        or not initial.roots
    ):
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=None if initial is None else initial.identity,
            requested_path=target,
            canonical_target=None,
            root_id=None,
            revision=None if initial is None else initial.revision,
            error_code=REMOTE_ROOT_NOT_APPROVED,
            message="Remote browse root is not approved.",
        )

    identity = initial.identity
    assert identity is not None
    active_roots = tuple(
        root
        for root in initial.roots
        if (root.host, root.user, root.port) == (identity.host, identity.user, identity.port)
        and root.revoked_at is None
        and root.requested_path != "/"
        and root.canonical_path != "/"
    )
    if not active_roots:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=None,
            root_id=None,
            revision=initial.revision,
            error_code=REMOTE_ROOT_NOT_APPROVED,
            message="Remote browse root is not approved.",
        )
    if len(active_roots) > MAX_ACTIVE_ROOTS:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=None,
            root_id=None,
            revision=initial.revision,
            error_code=REMOTE_ROOT_LIMIT,
            message="Too many active remote browse roots.",
        )
    try:
        transport = transport_factory(identity)
    except Exception:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=None,
            root_id=None,
            revision=initial.revision,
            error_code=REMOTE_SCAN_FAILED,
            message="Remote browse failed.",
        )
    runner = BrowseExecutionBudget.start(
        timeout_seconds=SCAN_TIMEOUT_SECONDS,
        max_output_bytes=MAX_SCAN_OUTPUT_BYTES,
    )

    resolved_roots: list[tuple[ApprovedDataRoot, str]] = []
    for root in active_roots:
        try:
            canonical = resolve_remote_directory(root.requested_path, runner, transport)
        except _BrowseResolutionError:
            return _result(
                context=context,
                event_id=event_id,
                started_at=started_at,
                identity=identity,
                requested_path=target,
                canonical_target=None,
                root_id=root.root_id,
                revision=initial.revision,
                error_code=REMOTE_ROOT_STALE,
                message="An approved remote root is stale.",
            )
        except (CommandTimeoutError, CommandOutputLimitError):
            return _result(
                context=context,
                event_id=event_id,
                started_at=started_at,
                identity=identity,
                requested_path=target,
                canonical_target=None,
                root_id=None,
                revision=initial.revision,
                error_code=REMOTE_ROOT_STALE,
                message="An approved remote root is stale.",
            )
        except Exception:
            return _result(
                context=context,
                event_id=event_id,
                started_at=started_at,
                identity=identity,
                requested_path=target,
                canonical_target=None,
                root_id=None,
                revision=initial.revision,
                error_code=REMOTE_ROOT_STALE,
                message="An approved remote root is stale.",
            )
        if canonical != root.canonical_path:
            return _result(
                context=context,
                event_id=event_id,
                started_at=started_at,
                identity=identity,
                requested_path=target,
                canonical_target=None,
                root_id=root.root_id,
                revision=initial.revision,
                error_code=REMOTE_ROOT_STALE,
                message="An approved remote root is stale.",
            )
        resolved_roots.append((root, canonical))

    try:
        canonical_target = resolve_remote_directory(target, runner, transport)
    except _BrowseResolutionError as exc:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=None,
            root_id=None,
            revision=initial.revision,
            error_code=exc.code,
            message="Remote browse target could not be resolved.",
        )
    except (CommandTimeoutError, CommandOutputLimitError):
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=None,
            root_id=None,
            revision=initial.revision,
            error_code=REMOTE_PATH_ESCAPE,
            message="Remote browse target is outside the approved root.",
        )
    except Exception:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=None,
            root_id=None,
            revision=initial.revision,
            error_code=REMOTE_SCAN_FAILED,
            message="Remote browse failed.",
        )

    selected: tuple[ApprovedDataRoot, str] | None = None
    for item in resolved_roots:
        if contains_posix_path(item[1], canonical_target) and (
            selected is None
            or len(PurePosixPath(item[1]).parts) > len(PurePosixPath(selected[1]).parts)
        ):
            selected = item
    if selected is None:
        lexical_match = any(contains_posix_path(root.requested_path, target) for root, _ in resolved_roots)
        code = REMOTE_PATH_ESCAPE if lexical_match else REMOTE_ROOT_NOT_APPROVED
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=canonical_target,
            root_id=None,
            revision=initial.revision,
            error_code=code,
            message="Remote browse target is outside the approved root." if code == REMOTE_PATH_ESCAPE else "Remote browse root is not approved.",
        )

    try:
        current = policy_reader()
    except Exception:
        current = None
    if not _same_authorization_snapshot(current, initial, selected[0]):
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=canonical_target,
            root_id=selected[0].root_id,
            revision=initial.revision,
            error_code=REMOTE_POLICY_CHANGED,
            message="Remote browse policy changed; retry the request.",
        )

    try:
        payload = scanner(canonical_target, selected[1], runner, transport)
        # The remote scanner executes outside the local policy lock.  Re-read
        # the authorization snapshot after it returns so a root revocation or
        # policy revision that happens during the scan cannot be published as
        # a trusted result.
        try:
            final_policy = policy_reader()
        except Exception:
            final_policy = None
        if not _same_authorization_snapshot(final_policy, initial, selected[0]):
            return _result(
                context=context,
                event_id=event_id,
                started_at=started_at,
                identity=identity,
                requested_path=target,
                canonical_target=canonical_target,
                root_id=selected[0].root_id,
                revision=initial.revision,
                error_code=REMOTE_POLICY_CHANGED,
                message="Remote browse policy changed; retry the request.",
            )
        payload = _rebind_group_ids(payload, initial.revision, selected[0].root_id, event_id)
        _validate_scan_payload(payload)
    except CommandTimeoutError:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=canonical_target,
            root_id=selected[0].root_id,
            revision=initial.revision,
            error_code=REMOTE_SCAN_TIMEOUT,
            message="Remote browse timed out.",
        )
    except CommandOutputLimitError:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=canonical_target,
            root_id=selected[0].root_id,
            revision=initial.revision,
            error_code=REMOTE_SCAN_OUTPUT_LIMIT,
            message="Remote browse output exceeded its limit.",
        )
    except _InvalidScanPayload:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=canonical_target,
            root_id=selected[0].root_id,
            revision=initial.revision,
            error_code=REMOTE_SCAN_INVALID_OUTPUT,
            message="Remote browse returned invalid output.",
        )
    except ScanPathEscape:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=canonical_target,
            root_id=selected[0].root_id,
            revision=initial.revision,
            error_code=REMOTE_PATH_ESCAPE,
            message="Remote browse candidate escaped the approved root.",
        )
    except ScanPython3Required:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=canonical_target,
            root_id=selected[0].root_id,
            revision=initial.revision,
            error_code=REMOTE_SCAN_PYTHON3_REQUIRED,
            message="Remote browse requires Python 3 on the server.",
        )
    except ScanProtocolError:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=canonical_target,
            root_id=selected[0].root_id,
            revision=initial.revision,
            error_code=REMOTE_SCAN_INVALID_OUTPUT,
            message="Remote browse returned invalid output.",
        )
    except Exception:
        return _result(
            context=context,
            event_id=event_id,
            started_at=started_at,
            identity=identity,
            requested_path=target,
            canonical_target=canonical_target,
            root_id=selected[0].root_id,
            revision=initial.revision,
            error_code=REMOTE_SCAN_FAILED,
            message="Remote browse failed.",
        )

    authorization = {
        "root_id": selected[0].root_id,
        "canonical_root": selected[1],
        "canonical_target": canonical_target,
        "policy_revision": initial.revision,
    }
    return _result(
        context=context,
        event_id=event_id,
        started_at=started_at,
        identity=identity,
        requested_path=target,
        canonical_target=canonical_target,
        root_id=selected[0].root_id,
        revision=initial.revision,
        payload=payload,
        authorization=authorization,
    )


class _BrowseResolutionError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _InvalidScanPayload(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()


def _identity_digest(identity: SSHIdentity | None) -> str:
    if identity is None:
        return _digest("")
    return _digest(f"{identity.host}\x00{identity.user}\x00{identity.port}")


def _result(
    *,
    context: BrowseContext,
    event_id: str,
    started_at: str,
    identity: SSHIdentity | None,
    requested_path: object,
    canonical_target: str | None,
    root_id: str | None,
    revision: str | None,
    error_code: str | None = None,
    message: str = "",
    payload: BrowseScanPayload | None = None,
    authorization: dict[str, str] | None = None,
) -> BrowseResult:
    payload = payload or BrowseScanPayload((), 0, 0, False)
    ok = error_code is None
    if ok:
        outcome: Literal["allowed", "denied", "failed"] = "allowed"
    elif error_code in {
        REMOTE_SCAN_TIMEOUT,
        REMOTE_SCAN_OUTPUT_LIMIT,
        REMOTE_SCAN_INVALID_OUTPUT,
        REMOTE_SCAN_FAILED,
    }:
        outcome = "failed"
    else:
        outcome = "denied"
    audit = BrowseAudit(
        event_id=event_id,
        project_id=context.project_id,
        thread_id=context.thread_id,
        source=context.source,
        identity_digest=_identity_digest(identity),
        requested_path_digest=_digest(requested_path),
        canonical_target=canonical_target,
        root_id=root_id,
        browse_policy_revision=revision,
        directory_count=len(payload.groups),
        sample_count=payload.sample_count,
        unmatched_count=payload.unmatched_count,
        truncated=payload.truncated,
        outcome=outcome,
        error_code=error_code,
        started_at=started_at,
        completed_at=_utc_now(),
    )
    return BrowseResult(
        ok=ok,
        blocked=not ok,
        error_code=error_code,
        message=message,
        groups=payload.groups,
        sample_count=payload.sample_count,
        unmatched_count=payload.unmatched_count,
        truncated=payload.truncated,
        authorization=authorization,
        audit=audit,
    )


def _same_authorization_snapshot(
    current: BrowsePolicy | None,
    initial: BrowsePolicy,
    selected: ApprovedDataRoot,
) -> bool:
    if current is None or not current.available or current.identity != initial.identity:
        return False
    if current.revision != initial.revision:
        return False
    match = next((root for root in current.roots if root.root_id == selected.root_id), None)
    return match == selected


def _validate_scan_payload(payload: object) -> None:
    if not isinstance(payload, BrowseScanPayload):
        raise _InvalidScanPayload()
    if not isinstance(payload.groups, tuple):
        raise _InvalidScanPayload()
    if len(payload.groups) > MAX_SCAN_DEPTH * MAX_SCAN_CANDIDATES:
        raise _InvalidScanPayload()
    if not isinstance(payload.sample_count, int) or not 0 <= payload.sample_count <= MAX_RETURNED_SAMPLES:
        raise _InvalidScanPayload()
    if not isinstance(payload.unmatched_count, int) or not 0 <= payload.unmatched_count <= MAX_RETURNED_UNMATCHED:
        raise _InvalidScanPayload()
    if not isinstance(payload.truncated, bool):
        raise _InvalidScanPayload()
    for group in payload.groups:
        if not isinstance(group, BrowseDirectoryGroup):
            raise _InvalidScanPayload()
        if not isinstance(group.group_id, str) or not isinstance(group.canonical_directory, str):
            raise _InvalidScanPayload()
        try:
            validate_remote_browse_path(group.canonical_directory)
        except ValueError as exc:
            raise _InvalidScanPayload() from exc
        if not isinstance(group.samples, tuple) or not isinstance(group.unmatched_basenames, tuple):
            raise _InvalidScanPayload()
        if len(group.samples) > MAX_RETURNED_SAMPLES or len(group.unmatched_basenames) > MAX_RETURNED_UNMATCHED:
            raise _InvalidScanPayload()
        for sample in group.samples:
            if not isinstance(sample, BrowseSampleRow):
                raise _InvalidScanPayload()
            if not _is_safe_basename(sample.sample_id) or not _is_safe_basename(sample.fastq_1):
                raise _InvalidScanPayload()
            if sample.fastq_2 is not None and not _is_safe_basename(sample.fastq_2):
                raise _InvalidScanPayload()
        for basename in group.unmatched_basenames:
            if not _is_safe_basename(basename):
                raise _InvalidScanPayload()
    if sum(len(group.samples) for group in payload.groups) != payload.sample_count:
        raise _InvalidScanPayload()
    if sum(len(group.unmatched_basenames) for group in payload.groups) != payload.unmatched_count:
        raise _InvalidScanPayload()
    if payload.sample_count > MAX_RETURNED_SAMPLES or payload.unmatched_count > MAX_RETURNED_UNMATCHED:
        raise _InvalidScanPayload()


def _is_safe_basename(value: object) -> bool:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        return False
    if "/" in value or "\\" in value:
        return False
    return not any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
