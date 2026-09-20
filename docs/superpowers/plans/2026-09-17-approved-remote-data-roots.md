# Approved Remote Data Roots Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require every remote FASTQ discovery path to browse only canonical directories approved in Settings for the live SSH identity, with bounded scanning, stable errors, and secret-free audit records.

**Architecture:** Store typed, identity-bound root grants in the existing user connection document behind one atomic compare-and-swap transaction. A new `remote_browse.py` service owns lexical validation, canonical authorization, the bounded remote scanner, grouping, error projection, and audit projection; the workbench, project command, rule chat, and LLM tool become thin adapters. Local structured UI may use the exact authorized result, while provider-facing disclosure remains a separate policy layer.

**Tech Stack:** Python 3.11+, FastAPI, dataclasses and typing, pytest/TestClient, Jinja/vanilla JavaScript, existing SystemSSH/Paramiko transport abstractions, JSON connection store.

## Global Constraints

- Root lifecycle is Settings-only: project edits, model tools, generic connection payloads, and confirmation cards cannot create, widen, migrate, or revoke roots.
- `tool_mode`, approved remote roots, and LLM `data_scope` are independent permission axes. This plan does not add disclosure grants or a `ModelContextBuilder`.
- No active root fails closed before scanning; legacy connection files without `approved_data_roots` load as a valid empty policy.
- A corrupt root collection is unavailable for authorization and is never silently repaired or overwritten by a root mutation.
- Active roots must exactly match normalized `host`, `user`, and `port`; identity changes deactivate grants and never rebind them.
- `remote_base_dir`, `remote_workdir`, and `samples.remote_data_dir` never auto-create grants. Root `/` is never approvable.
- Authorization uses canonical, case-sensitive POSIX path components. Raw string-prefix checks are forbidden.
- Browse uses a maximum depth of `2`, at most `5_000` candidate files, one `60` second monotonic deadline, and a combined stdout/stderr ceiling of `4 MiB`.
- At most `32` active roots participate in one browse policy. Approval at the limit fails; a malformed store above the limit grants no browse authority.
- The 60-second deadline and 4-MiB ceiling are shared cumulatively by root re-resolution, target resolution, and scanning, including DNS/connect time; they are not reset per SSH command.
- Traversal does not follow directory or file symlinks. Every emitted candidate is canonicalized and checked against both target and selected root.
- Scan framing must be JSON-safe or NUL-safe. Newline-delimited filenames and `splitlines()` parsing are forbidden.
- Project drafts retain filename-only FASTQ fields plus a separately authorized canonical directory. Existing filename validation stays strict.
- Exact paths and filenames may be returned to the authorized local UI. Provider input defaults to a de-identified count/status projection until the separate disclosure plan grants exact data.
- Audit and History records are bounded and secret-free; they never store credentials, command lines, raw stdout/stderr, or unbounded file lists.
- Exact scan results cross requests only through an atomic private project scan store with a 15-minute TTL, eight pending records per project, revision binding, and crash-recoverable one-time consumption. POSIX uses directory `0o700`/file `0o600`; Windows uses a protected DACL granting full control only to the current user SID and LocalSystem.
- Exact LLM browse output remains request-local. Only a de-identified projection reaches provider messages, `ChatState`, checkpointers, generic tool logs, or History.
- Existing projects, counts workflows, and frozen contracts remain usable without roots when they do not invoke new discovery.
- Use the stable error codes defined below; no route may fabricate success or flatten them into `NOT_EVALUABLE`.

---

## Fixed Interfaces And Error Contract

These names and shapes are the cross-task contract. Change them only by updating this plan and every dependent task together.

```python
# src/rnaseq_agent/connection_store.py
from dataclasses import dataclass
from pathlib import Path

class ConnectionStoreCorruptError(RuntimeError): ...
class BrowsePolicyConflictError(RuntimeError): ...

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
) -> str: ...

def load_browse_policy(*, store_dir: Path | None = None) -> BrowsePolicy: ...
@contextmanager
def locked_browse_policy(
    *, store_dir: Path | None = None,
) -> Iterator[BrowsePolicy]: ...
def list_approved_data_roots(
    *, store_dir: Path | None = None,
) -> tuple[ApprovedDataRoot, ...]: ...
def approve_data_root(
    root: ApprovedDataRoot,
    *,
    expected_revision: str,
    store_dir: Path | None = None,
) -> BrowsePolicy: ...
def revoke_data_root(
    root_id: str,
    *,
    expected_revision: str,
    store_dir: Path | None = None,
) -> BrowsePolicy: ...
```

```python
# src/rnaseq_agent/remote_transport.py
class DeadlineAwareRemoteTransport(RemoteTransport, Protocol):
    def execute_bounded(
        self,
        remote_command: str,
        *,
        absolute_deadline: float,
        max_capture_bytes: int,
    ) -> CommandResult: ...
```

```python
# src/rnaseq_agent/remote_browse.py
from dataclasses import dataclass
from typing import Callable, Literal, Sequence

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
    def start(
        cls,
        *,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> "BrowseExecutionBudget": ...

    def execute(
        self,
        transport: DeadlineAwareRemoteTransport,
        remote_command: str,
    ) -> CommandResult: ...

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

BrowseScanner = Callable[
    [str, str, BrowseExecutionBudget, DeadlineAwareRemoteTransport],
    BrowseScanPayload,
]

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

def validate_remote_browse_path(value: object) -> str: ...
def contains_posix_path(root: str, candidate: str) -> bool: ...
def resolve_remote_directory(
    requested_path: str,
    runner: BrowseExecutionBudget,
    transport: DeadlineAwareRemoteTransport,
) -> str: ...
def provider_browse_summary(
    result: BrowseResult,
    source_ref: str | None,
) -> dict[str, object]: ...
def browse_log_projection(event: BrowseAudit) -> dict[str, object]: ...
def scan_remote_fastqs(
    canonical_target: str,
    canonical_root: str,
    runner: BrowseExecutionBudget,
    transport: DeadlineAwareRemoteTransport,
) -> BrowseScanPayload: ...
def browse_remote_fastqs(
    requested_path: str,
    context: BrowseContext,
    policy_reader: Callable[[], BrowsePolicy],
    transport_factory: Callable[[SSHIdentity], DeadlineAwareRemoteTransport],
    scanner: BrowseScanner,
) -> BrowseResult: ...
```

Task 3 extends `CommandResult` with `captured_bytes: int`, populated from the
raw stdout/stderr byte counters before UTF-8 replacement decoding.
`BrowseExecutionBudget.execute` passes the same absolute monotonic deadline to
every DNS/connect/command operation, passes only the remaining byte allowance,
then subtracts `result.captured_bytes`; it never estimates bytes by re-encoding
decoded text. It raises `CommandTimeoutError` or `CommandOutputLimitError`
before another remote call when either budget is exhausted. Canonicalizing
roots, resolving the target, Settings preview, and the final scan all use this
interface; generic `RemoteTransport.execute` is not a browse fallback.
Task 3 passes a scripted `BrowseScanner` to prove authorization success without
implementing file discovery. Task 4 implements `scan_remote_fastqs` and wires it
as the only production scanner. This seam is dependency injection for testing,
not a second browse implementation.

```python
# src/rnaseq_agent/remote_scan_store.py
REMOTE_SCAN_TTL_SECONDS = 15 * 60
REMOTE_SCAN_TERMINAL_TTL_SECONDS = 24 * 60 * 60
MAX_PENDING_REMOTE_SCANS_PER_PROJECT = 8

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

def store_remote_scan(
    project_dir: Path,
    result: BrowseResult,
) -> RemoteScanReference: ...

def discard_remote_scan(
    project_dir: Path,
    reference: RemoteScanReference,
) -> None: ...

def consume_remote_scan(
    project_dir: Path,
    *,
    scan_id: str,
    result_revision: str,
    group_id: str,
    expected_policy: BrowsePolicy,
    expected_context: BrowseContext,
    apply: Callable[[BrowseDirectoryGroup, str], None],
) -> BrowseDirectoryGroup: ...
```

The store is `<project_dir>/.remote-scans/`. `scan_id` and `source_ref` are
independent random 128-bit identifiers with strict, ASCII-only opaque forms:
`scan_id` is exactly `scan_` followed by 32 lower-case hexadecimal characters
(37 bytes total), and `source_ref` is exactly `src_` followed by 32 lower-case
hexadecimal characters (36 bytes total). Values are generated from 16 random
bytes and are never Unicode-normalized, decoded from a client-supplied alternate
encoding, or accepted with surrounding whitespace. Any other length, prefix,
character, case, separator, dot-segment, absolute-path spelling, drive/UNC/device
spelling, or NUL/control character is `REMOTE_SCAN_REFERENCE_INVALID` before a
filesystem operation.

All scan and source-reference lookups use one helper:

```python
def remote_scan_lookup_path(
    store_dir: Path,
    identifier: str,
    *,
    kind: Literal["scan", "source"],
) -> Path: ...
```

The helper validates the exact grammar above, requires the expected prefix for
`kind`, and derives the filename only from
`sha256(identifier.encode("ascii")).hexdigest()`: a scan key maps to
`scan-<digest>.record`, while a source key maps to
`source-<digest>.ref`. It never interpolates the identifier into a pathname.
The `.record` file is authoritative and contains the complete stored scan. The
`.ref` file contains only the validated `source_ref` and its corresponding
`scan_id`; it is an atomic alias, not a second mutable copy of the scan. Creating
or replacing a scan commits the record and alias under the scan-store lock, and
recovery rebuilds a missing alias from valid records before serving a source
lookup. A source lookup validates the alias, resolves its validated `scan_id`
through the same helper, and then re-checks the full record binding.

Before every read, replace, or delete, the helper verifies that the resolved
store parent is the configured private `.remote-scans` directory, every path
component from the configured store root to the target is a non-reparse,
non-symlink directory, and the target is in-store. The final component is
checked with no-follow semantics; on Windows this includes reparse-point and
device/UNC escape checks. A missing target is reported only after the parent
and store checks pass. Temporary files use names derived from the same helper,
are exclusively created inside the verified store, and are removed only through
the helper. Hostile identifiers (`""`, oversized values, `/`, `\\`, `.`, `..`,
absolute POSIX paths, drive paths, UNC/device paths, alternate separators, and
lookalike Unicode) must therefore be unable to read, write, replace, delete, or
touch a sentinel outside `.remote-scans`.

`source_ref` names the whole bounded stored result (all directory groups), not a
client-selected subset. It is bound to `project_id`, exact `thread_id` (including
`None`), `source`, identity, root, policy revision, and result revision. A
workbench apply endpoint accepts only a record whose exact context is
`BrowseContext(project_id, None, "workbench")`; project-command, rule-chat, and
LLM records cannot be submitted to that endpoint. A workbench record with
`thread_id=None` can be applied locally but cannot become an LLM disclosure
source; disclosure requires the stored non-null thread to match exactly. This
plan stores that binding but leaves data-grant creation to the separate
disclosure plan.

`result_revision` is SHA-256 over canonical JSON for every immutable
security-relevant stored field and every group in the whole bounded result. The
apply lifecycle fields are excluded so the revision remains stable through
claim and recovery. Create/read/consume holds one cross-process scan-store lock,
refuses a ninth pending/applying record with `REMOTE_SCAN_STORE_FULL`, and uses
the shared private-file writer. Pending capacity excludes terminal tombstones.

Apply is a recoverable two-file protocol under the global
`connection -> remote-scan -> project` lock order:

1. validate the target record before sweeping unrelated records, so an expired
   or consumed presented id yields `REMOTE_SCAN_REFERENCE_EXPIRED` or
   `REMOTE_SCAN_REFERENCE_USED`;
2. verify project, exact expected context, identity, root, policy revision,
   result revision, expiry, and group, then durably change `pending` to
   `applying` with a random `apply_claim_id` and selected group;
3. call `apply(group, apply_claim_id)` under the project lock. The callback is
   idempotent: one atomic project update writes the filename-only sample state
   and appends the opaque claim id to a bounded project-side receipt ledger; an
   existing matching receipt returns success without rewriting samples;
4. durably change the scan record to `consumed` only after the receipt exists.

After a process exit or scan-store replace failure, retry observes the durable
claim and calls the idempotent callback with the same claim id. A matching
project receipt completes consumption without applying twice; no receipt means
the project write never committed and the callback may safely retry. A callback
failure before its receipt remains retryable. Consumed and expired records
become bounded terminal tombstones for 24 hours; lookup classifies the presented
target before housekeeping deletes older unrelated tombstones. Thus a recent
replay/expiry cannot collapse into `REMOTE_SCAN_REFERENCE_INVALID` merely
because another request ran cleanup.

`discard_remote_scan` verifies the full reference binding and may remove only a
newly stored `pending` record which has never been exposed. It exists solely for
fail-closed rollback when the subsequent authoritative audit commit definitively
fails before publication or reconciliation returns `absent`; it is never used
while a published audit remains `uncertain`.

```python
# src/rnaseq_agent/security_audit.py
class AuditCommitUncertainError(RuntimeError): ...
def _record_browse_audit(event: BrowseAudit) -> str: ...
def reconcile_browse_audit(
    event: BrowseAudit,
) -> Literal["committed", "absent", "uncertain"]: ...
def browse_history_projection(event: BrowseAudit) -> dict[str, object]: ...
```

`_record_browse_audit` commits one fixed-schema bounded record to the protected
directory `$RNASEQ_AGENT_HOME/security/remote-browse-audit/`; the directory of
per-event records, rather than a JSONL file, is authoritative. Under the
cross-process audit commit lock, it performs this failure-atomic protocol:

1. Validate the internally generated `event_id`, which is exactly `audit_`
   followed by 32 lower-case hexadecimal characters (16 random bytes), encode
   the canonical sorted JSON record with a fixed maximum byte size, and derive
   the deterministic
   final name `event-<sha256(event_id.encode("ascii")).hexdigest()>.record`.
2. If that final name already exists, parse and validate it. Identical canonical
   bytes are an idempotent duplicate and return the existing event id; different
   bytes are an `AUDIT_EVENT_CONFLICT`/store-corruption failure and are never
   overwritten.
3. Create a private temporary file exclusively inside the verified audit
   directory. Write the complete bounded record with a short-write-safe loop;
   any prefix, write-after-prefix error, or size overflow leaves only a private
   temp artifact and does not publish an event. Flush and `fsync` the temp file.
4. Atomically publish the fully fsynced temp with `os.replace` while the commit
   lock is held, then fsync the containing directory where the platform
   supports directory handles. The protected directory is created with POSIX
   `0o700` or the Windows current-user plus LocalSystem protected DACL, and every
   existing artifact is re-verified before use.

If `os.replace` fails before publication, the exact private temp is removed
through the verified helper, the directory is flushed where supported, and the
attempt returns a definitive no-record failure. If a destination is nevertheless
visible after the exception, it is reconciled by the same rule below before any
rollback decision; the exception alone never authorizes deletion of that
published destination.

If the directory fsync raises after `os.replace`, `_record_browse_audit` does
not immediately report an ordinary failure. In the same lock scope it invokes
`reconcile_browse_audit(event)`, which re-reads the destination through the
verified helper, validates the exact canonical bytes/schema/digest, retries the
required file and directory flush once, and returns one of three outcomes:

- `committed`: the published record is now durably verified, so
  `_record_browse_audit` returns the event id and the finalizer may expose
  channels;
- `absent`: no valid published record remains (or a malformed record was
  explicitly quarantined through the verified helper and its directory flush
  completed), so the attempt has a definitive no-record failure;
- `uncertain`: a valid published record remains but durability cannot yet be
  proven. The record is retained for recovery, `_record_browse_audit` raises
  `AuditCommitUncertainError`, and no channel is exposed.

The same bounded reconciliation is used after a process exit observed on the
next startup/read: a valid post-publish record becomes `committed` only after
the required flush succeeds, while a pre-publish temporary is ignored or
removed. A valid published record is never discarded merely because `replace`
or directory fsync raised; only an explicit reconciliation result of `absent`
permits scan rollback. The finalizer therefore cannot delete an audit that may
already be durably published. Startup/read housekeeping may remove only private
temporary files whose names match the implementation's generated temp grammar,
whose age exceeds the bounded recovery window, and whose verified parent is the
audit directory; it is capped to a fixed number of entries per pass and never
deletes a published `.record`, a `.record`-derived quarantine, or a temp whose
event can still be reconciled. If the cap is reached, the sink remains
available and cleanup continues on the next bounded pass.

The record is considered committed only after the final record passes schema,
event-id, filename-digest, bounded-size, and byte-integrity checks. Recovery on
startup or before a read deterministically scans sorted `*.record` names,
ignores only private temporary names left by an interrupted pre-publish write,
and quarantines malformed published records as store corruption without
silently treating them as audits. A malformed or digest-mismatched record,
duplicate event id with different bytes, failed replace, or an unresolved
post-publish flush makes the audit sink unavailable until the record is repaired
or the artifact is explicitly quarantined; it never falls back to an in-place
append. The reader/index is deterministic by `(completed_at, event_id)` and
returns each event once. A JSONL file, if requested for export or compatibility,
is rebuilt from validated record files in that order and is a derived,
non-authoritative projection that may never be used for commit, recovery, or
exactly-once checks.

It is the authoritative audit for allowed, denied, and failed attempts. History
is a display projection containing `event_id`, source, counts, truncation,
outcome, error code, and timestamps; it contains no canonical path, requested
path, root record, filenames, command, stdout/stderr, or credential.

```python
# src/rnaseq_agent/chat_graph.py
@dataclass(frozen=True)
class ToolExecutionResult:
    local: dict[str, Any]
    model: dict[str, Any]
    log_projection: dict[str, Any]
    security_audit: BrowseAudit | None

@dataclass(frozen=True)
class ToolExecutionContext:
    project_id: str
    thread_id: str | None

ToolExecutor = Callable[
    [str, dict[str, Any], Path, bool, ToolExecutionContext],
    ToolExecutionResult,
]
ToolResultFinalizer = Callable[
    [ToolExecutionResult, Path | None],
    ToolExecutionResult,
]
```

For browse, `local` may contain the exact authorized result and scan reference
for the current HTTP/SSE response only; it is delivered through a request-local
callback and discarded. `model` is the de-identified provider projection and is
the only portion used for a tool message. `log_projection` is the bounded
de-identified payload allowed in generic `tool_log`. `security_audit` carries
the exact bounded `BrowseAudit` to the independent authoritative sink. It must
never enter provider input, `ChatState`, a checkpoint, History, or generic
`tool_log`.

There is one persistence owner: the web-supplied
`_finalize_tool_execution_result` callback. Every structured adapter and
`chat_graph.node_execute` must invoke that same callback exactly once before
delivering `local`, appending `model`, or persisting `log_projection`. The
finalizer is the only product function permitted to call
`_record_browse_audit`; it writes `security_audit`, appends only
`browse_history_projection(event)` when a project exists, then returns a copy
with `security_audit=None`. Callers never write an audit themselves. Search and
tests enforce this ownership rule.

If the audit commit fails definitively before publication, or reconciliation
returns `absent`, the finalizer calls `discard_remote_scan` for an unexposed
pending reference, emits no provider/log/local success, and returns a sanitized
`REMOTE_SECURITY_AUDIT_FAILED` result with all four channels safe and
`security_audit=None`. If `_record_browse_audit` raises
`AuditCommitUncertainError`, the finalizer does **not** discard the scan: it
retains the pending reference for the bounded recovery/reconciliation path,
emits no provider/log/local success, and returns the same sanitized failure.
Only a later `reconcile_browse_audit` result of `absent` permits rollback; a
`committed` result permits delivery only after the finalizer resumes and clears
`security_audit`. The commit was attempted exactly once and is not retried
implicitly by the request. A scan-store failure is converted before finalization into one
`BrowseAudit(outcome="failed", error_code="REMOTE_SCAN_STORE_FAILED")`; the
allowed event is replaced, not additionally written. The finalizer therefore
persists at most one terminal event per attempt and no allowed result can escape
without a successful authoritative commit.

The authoritative commit happens before the non-authoritative History update.
If History append fails after the audit succeeds, record a bounded operational
warning and continue with the already-audited result; never retry or duplicate
the security audit to make the display index succeed.

The `ToolExecutionResult` wrapper and its `local` member must never enter
`ChatState`, the LangGraph checkpointer, messages, generic tool logs, or
History. Only `model` and `log_projection` may enter their dedicated sinks after
the finalizer has cleared `security_audit`.

The provider projection is deliberately de-identified:

```python
def provider_browse_summary(
    result: BrowseResult,
    source_ref: str | None,
) -> dict[str, object]:
    return {
        "ok": result.ok,
        "blocked": result.blocked,
        "error_code": result.error_code,
        "source_ref": source_ref if result.ok else None,
        "directory_count": len(result.groups),
        "sample_count": result.sample_count,
        "unmatched_count": result.unmatched_count,
        "truncated": result.truncated,
    }
```

The generic browse log projection is independently fixed and contains no exact
authorization fields:

```python
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
```

The four route adapters retain `ok`, `blocked`, `error_code`, and `message`. Use these HTTP projections where an HTTP response is involved:

| Error code | HTTP | Remote activity |
|---|---:|---|
| `PROJECT_NOT_FOUND` | 404 | none |
| `REMOTE_PATH_INVALID` | 400 | none |
| `REMOTE_ROOT_NOT_APPROVED` | 403 | no scan |
| `REMOTE_ROOT_STALE` | 409 | root resolution only |
| `REMOTE_PATH_NOT_FOUND` | 404 | target resolution only |
| `REMOTE_PATH_NOT_DIRECTORY` | 422 | target resolution only |
| `REMOTE_PATH_ESCAPE` | 403 | resolution or aborted scan |
| `REMOTE_POLICY_CHANGED` | 409 | no scan after recheck |
| `REMOTE_SCAN_TIMEOUT` | 504 | scan terminated |
| `REMOTE_SCAN_OUTPUT_LIMIT` | 413 | scan terminated |
| `REMOTE_SCAN_INVALID_OUTPUT` | 502 | scan terminated |
| `REMOTE_SCAN_FAILED` | 502 | scan terminated |
| `REMOTE_ROOT_REVISION_CONFLICT` | 409 | Settings mutation only |
| `REMOTE_SCAN_REFERENCE_INVALID` | 409 | apply only; no remote activity |
| `REMOTE_SCAN_REFERENCE_EXPIRED` | 410 | apply only; no remote activity |
| `REMOTE_SCAN_REFERENCE_USED` | 409 | replayed apply; no remote activity |
| `REMOTE_SCAN_STORE_FULL` | 429 | too many unexpired pending scans |
| `REMOTE_SCAN_STORE_FAILED` | 500 | exact result was not exposed; failed audit event only |
| `REMOTE_SECURITY_AUDIT_FAILED` | 503 | exact result/reference rolled back and not exposed |

## File Ownership Map

- Create `src/rnaseq_agent/remote_browse.py`: all browse authorization, canonicalization, bounded scanning, grouping, errors, and audit projections.
- Create `tests/test_remote_browse.py`: scripted transport unit tests for audit batches 2 and 3.
- Optionally create `tests/integration/test_remote_browse_ssh.py`: opt-in real SSH parity tests; absence of SSH credentials skips only this file.
- Modify `src/rnaseq_agent/storage.py`: reusable per-path cross-process lock while preserving `project_state_lock`.
- Create `src/rnaseq_agent/private_files.py`: POSIX private modes and protected Windows DACLs for exact local state.
- Create `tests/test_private_files.py`: POSIX mode and Windows ACE/protected-DACL integration proof.
- Modify `src/rnaseq_agent/connection_store.py`: atomic transactions, typed roots, policy digest, identity filtering, and CAS mutations.
- Modify `src/rnaseq_agent/remote_transport.py`: browse-only absolute-deadline/cumulative-output execution for SystemSSH and Paramiko.
- Create `src/rnaseq_agent/remote_scan_store.py`: expiring, replay-safe, project-bound exact scan storage.
- Create `src/rnaseq_agent/security_audit.py`: authoritative bounded browse audit with a cross-process crash-atomic per-event record protocol and derived JSONL export.
- Modify `src/rnaseq_agent/webapp.py`: Settings APIs, strict project resolution, transport closure, four thin route adapters, bounded History, and group apply.
- Modify `src/rnaseq_agent/agent_tools.py`: central lexical validation, root-aware tool copy, and rejection of root fields from model edits.
- Modify `src/rnaseq_agent/chat_graph.py`: `ToolExecutionContext`, `ToolExecutionResult`, request-local exact result sink, and de-identified tool-log projection.
- Modify `src/rnaseq_agent/webtemplates/settings.html`: preview, explicit approval, active root list, revoke, and conflict refresh.
- Modify `src/rnaseq_agent/webtemplates/workbench.html`: grouped directory selection, truncation state, and server-verifiable group apply.
- Modify `src/rnaseq_agent/project_intake.py`: append only the de-identified audit History projection.
- Modify `src/rnaseq_agent/sample_detection.py` only if pairing cannot remain private to `remote_browse.py`.
- Do not relax `src/rnaseq_agent/validation.py` or `src/rnaseq_agent/safety.py`; extend their tests for filename-only persistence.
- Modify `docs/llm_tool_permission_model.md`: document root authorization as independent from tool mode and data disclosure.

## Primary Audit-Case Ownership Matrix

Each numbered case has exactly one primary implementation task. Other focused
assertions are unnumbered prerequisites or regressions and do not claim the case
again.

| Task | Primary audit cases | Primary proof |
|---|---|---|
| 0 | none | verify commit `5f4e0ca` transport prerequisite |
| 1 | none | unnumbered atomic/private-file prerequisite tests |
| 2 | 12-13, 35-36, 42 | identity filtering, root `/`, policy CAS/corruption, mutation boundary |
| 3 | 1-5, 7-11, 32-33 | lexical/canonical authorization and execute-time policy/identity recheck |
| 4 | 14-22, 26 | fixed scanner, cumulative bounds, transport parity, safe argv |
| 5 | none | unnumbered Settings lifecycle and resolver integration tests |
| 6 | 6, 23-25, 27-31, 34, 37-41, 43 | four-route convergence, boundary validation, audit, race, tool mode |
| 7 | 44-48 | grouped selection and crash-recoverable basename apply |
| 8 | 49-51 | compatibility regressions and release gate |

### Exact RED/Regression Test Nodes

These are the required node names. Feature tests must collect and fail on the
stated behavior before their implementation task; missing imports/404 alone do
not count except Settings/apply endpoints whose absent route is the behavior.
Cases 49-51 are intentionally pre-feature GREEN characterization tests and must
remain GREEN after every task; forcing them RED would manufacture a regression.

| Case | Required test node(s) | Pre-implementation evidence |
|---:|---|---|
| 1 | `tests/test_remote_browse.py::test_no_roots_denies_before_transport` | current compile-only service cannot return the denial |
| 2 | `tests/test_remote_browse.py::test_exact_canonical_root_is_authorized` | authorization service has no behavior |
| 3 | `tests/test_remote_browse.py::test_canonical_descendant_is_authorized` | authorization service has no behavior |
| 4 | `tests/test_remote_browse.py::test_component_boundary_rejects_prefix_sibling` | central component check has no behavior |
| 5 | `tests/test_remote_browse.py::test_relative_path_fails_before_policy_or_transport` | central validator has no behavior |
| 6 | `tests/test_webapp.py::test_all_four_routes_reject_dotdot_with_real_validator` | existing route implementations diverge |
| 7 | `tests/test_remote_browse.py::test_ambiguous_and_control_paths_fail_before_transport` | central validator has no behavior |
| 8 | `tests/test_remote_browse.py::test_target_symlink_escape_is_denied` | canonical containment is absent |
| 9 | `tests/test_remote_browse.py::test_stale_root_alias_is_denied` | root re-resolution is absent |
| 10 | `tests/test_remote_browse.py::test_posix_containment_is_case_sensitive` | central POSIX check has no behavior |
| 11 | `tests/test_remote_browse.py::test_longest_matching_root_is_selected_and_audited` | selection/audit is absent |
| 12 | `tests/test_connection_store.py::test_other_identity_roots_are_inactive_and_undisclosed` | typed identity filtering is absent |
| 13 | `tests/test_connection_store.py::test_root_slash_cannot_be_approved` | root approval is absent |
| 14 | `tests/test_remote_browse.py::test_scanner_does_not_follow_directory_symlink` | fixed scanner is absent |
| 15 | `tests/test_remote_browse.py::test_scanner_does_not_emit_symlink_file` | fixed scanner is absent |
| 16 | `tests/test_remote_browse.py::test_raced_candidate_escape_fails_without_partial_result` | per-file canonical filtering is absent |
| 17 | `tests/test_remote_browse.py::test_hostile_path_characters_remain_single_helper_arguments` | fixed helper framing is absent |
| 18 | `tests/test_remote_browse.py::test_newline_filename_cannot_create_extra_record` | JSON-safe parser is absent |
| 19 | `tests/test_remote_browse.py::test_candidate_limit_sets_truncated_and_bounds_rows` | producer limit is absent |
| 20 | `tests/test_remote_browse.py::test_cumulative_resolve_and_scan_bytes_share_one_budget` | existing transport resets per command |
| 21 | `tests/test_remote_browse.py::test_dns_connect_resolve_and_scan_share_absolute_deadline` | existing transport creates per-call deadlines |
| 22 | `tests/test_remote_transport.py::test_browse_bounded_errors_match_for_systemssh_and_paramiko` | browse-specific method is absent |
| 23 | `tests/test_webapp.py::test_option_like_identity_fails_before_settings_credential_lookup` | root Settings entry is absent |
| 24 | `tests/test_webapp_chat_tools.py::test_hostile_identity_fails_at_llm_and_settings_boundaries` | new entry boundaries are absent |
| 25 | `tests/test_webapp.py::test_invalid_ports_fail_at_settings_tool_and_transport_layers` and `tests/test_webapp_chat_tools.py::test_invalid_port_never_reaches_llm_browse_executor` | multi-layer sentinels are absent |
| 26 | `tests/test_remote_transport.py::test_browse_systemssh_argv_cannot_reinterpret_identity_as_option` | browse-specific argv path is absent |
| 27 | `tests/test_webapp_chat_tools.py::test_browse_secret_sentinels_absent_across_all_persisted_layers` | audit/model/state separation is absent |
| 28 | `tests/test_webapp.py::test_explicit_unknown_scan_project_never_falls_back` | current legacy resolver falls back |
| 29 | `tests/test_webapp.py::test_four_browse_entries_use_one_recording_adapter` | current routes call separate scanners |
| 30 | `tests/test_webapp_chat_tools.py::test_llm_browse_exact_metadata_only_in_security_audit` | generic tool log currently owns tool result |
| 31 | `tests/test_webapp_project_wizard.py::test_scan_history_is_deidentified_audit_projection` | current History stores route-specific data |
| 32 | `tests/test_remote_browse.py::test_revocation_between_resolve_and_scan_prevents_scan` | execute-time policy recheck is absent |
| 33 | `tests/test_remote_browse.py::test_identity_change_between_resolve_and_scan_prevents_scan` | execute-time identity recheck is absent |
| 34 | `tests/test_webapp.py::test_concurrent_connection_save_and_browse_never_mix_identity` | shared transaction/browse snapshot is absent |
| 35 | `tests/test_connection_store.py::test_stale_concurrent_approve_cannot_resurrect_revoked_root` | CAS lifecycle is absent |
| 36 | `tests/test_connection_store.py::test_corrupt_root_store_never_reaches_transport` | typed fail-closed policy is absent |
| 37 | `tests/test_webapp_chat_tools.py::test_in_root_read_only_browse_runs_without_confirmation` | approved-root LLM path is absent |
| 38 | `tests/test_webapp_chat_tools.py::test_disabled_mode_stops_before_policy_and_transport` | unified entry/audit behavior is absent |
| 39 | `tests/test_webapp_chat_tools.py::test_execute_mode_cannot_confirm_out_of_root_browse` | central denial behavior is absent |
| 40 | `tests/test_webapp_workbench.py::test_structured_in_root_scan_ignores_llm_tool_mode` | structured route has no root policy |
| 41 | `tests/test_webapp_workbench.py::test_structured_out_of_root_scan_denied_in_every_mode` | structured route has no root policy |
| 42 | `tests/test_webapp_chat_tools.py::test_model_and_generic_edits_cannot_mutate_root_collection` | typed root field does not yet exist |
| 43 | `tests/test_webapp_chat_tools.py::test_provider_projection_excludes_inactive_identity_roots` | provider projection is absent |
| 44 | `tests/test_webapp_project_wizard.py::test_scan_apply_persists_basenames_and_canonical_parent` | scan reference/apply endpoint is absent |
| 45 | `tests/test_webapp_project_wizard.py::test_applied_remote_scan_can_plan_and_confirm` | apply endpoint is absent |
| 46 | `tests/test_remote_browse.py::test_depth_two_parents_remain_separate_groups` | typed grouping is absent |
| 47 | `tests/test_webapp_project_wizard.py::test_duplicate_group_fails_before_project_write` | server-owned group apply is absent |
| 48 | `tests/test_webapp_workbench.py::test_truncated_scan_requires_visible_explicit_apply_action` | truncation apply contract is absent |
| 49 | `tests/test_webapp_project_wizard.py::test_rootless_existing_project_lifecycle_never_reads_browse_policy` | characterization GREEN before and after |
| 50 | `tests/test_webapp_project_wizard.py::test_counts_and_expression_workflows_ignore_browse_policy` | characterization GREEN before and after |
| 51 | `tests/test_validation.py::test_frozen_remote_contract_does_not_require_browse_root` | characterization GREEN before and after |

---

### Task 0: Verify The Transport Prerequisite

**Primary cases:** none; this is the transport prerequisite for Task 4 and
un-numbered route-boundary tests.

**Files:**
- Inspect: `src/rnaseq_agent/execution.py`
- Inspect: `src/rnaseq_agent/remote_transport.py`
- Inspect: `src/rnaseq_agent/ssh_auth.py`
- Inspect: `src/rnaseq_agent/ssh_identity.py`
- Test: `tests/test_execution.py`
- Test: `tests/test_remote_transport.py`

**Interfaces:**
- Consumes: corrected DNS/connect/host-key/worker-start/handshake transport baseline at commit `5f4e0ca` (`tests/test_execution.py` plus `tests/test_remote_transport.py`: 125 passing focused tests and 860 passing full-suite tests at planning time).
- Produces: verified timeout/output-limit exceptions, process-tree termination, normalized identity validation, safe argv construction, and sanitized errors for Task 3 onward.

- [ ] **Step 1: Verify the exact prerequisite commit and focused suite**

```powershell
git merge-base --is-ancestor 5f4e0ca HEAD
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_execution.py tests/test_remote_transport.py -q
```

Expected: the ancestry check exits 0 and pytest reports 125 passing tests. In
particular, timeout/output overflow terminate owned resources, unsafe identities
fail before launch, and exception text excludes credentials.

- [ ] **Step 2: Re-run the formerly open deadline and interruption regressions**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_remote_transport.py -q -k "host_key_loading_obeys_total_deadline or abandons_nonreturning_host_key_loader or prepare_start_interruption_abandons_worker or handshake_start_interruption_abandons_worker or handshake_wait_interruption_closes_owned_resources or handshake_publication_interruption_abandons_result"
```

Expected: PASS. Delayed and nonreturning host-key loading remain inside the
absolute deadline and never proceeds to socket creation after abandonment;
`KeyboardInterrupt`/`SystemExit` while starting, waiting for, or publishing a
worker/handshake abandon the worker, close owned Paramiko client/socket state,
and cannot publish late success.

- [ ] **Step 3: Inspect the corrected boundary and record baseline evidence**

```powershell
rg -n "_prepare_paramiko_client_with_deadline|_connect_paramiko_with_deadline|_remaining_remote_time|BaseException|close" src/rnaseq_agent/remote_transport.py tests/test_remote_transport.py
git show --stat --oneline 5f4e0ca
```

Expected: host-key preparation and handshake waiting share the caller's absolute
deadline, abandonment closes owned resources for `BaseException` interruption,
and commit `5f4e0ca` is the recorded prerequisite. Do not reopen or duplicate
this correction inside the approved-roots commits.

- [ ] **Step 4: Confirm the full planning baseline**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: 860 passing tests at the `5f4e0ca` planning baseline. Task 0 creates no
files and no commit; any failure blocks Task 1 rather than becoming conditional
work hidden inside later tasks.

---

### Task 1: Make The Shared Connection Store Atomic

**Primary cases:** none; atomic storage is a prerequisite for Tasks 2, 6, and 7.

**Files:**
- Modify: `src/rnaseq_agent/storage.py`
- Modify: `src/rnaseq_agent/connection_store.py`
- Create: `src/rnaseq_agent/private_files.py`
- Test: `tests/test_connection_store.py`
- Create: `tests/test_private_files.py`

**Interfaces:**
- Consumes: current connection file path and `project_state_lock` locking conventions.
- Produces: `ConnectionStoreCorruptError`, `locked_json_transaction(path: Path, mutate: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]`, and shared private-path helpers used by connection, scan, and audit storage.

- [ ] **Step 0: Add compile-only declarations for new storage symbols**

Declare `ConnectionStoreCorruptError`, `locked_json_transaction`,
`ensure_private_directory`, `create_private_temp`, and
`verify_private_path` with fixed signatures before importing them in
tests. Transaction/security bodies raise `NotImplementedError`; do not commit
this collection scaffold until the behavioral tests below are GREEN.

- [ ] **Step 1: Add failing tests for merge safety, reader atomicity, and corruption**

Use a real `multiprocessing.get_context("spawn").Barrier(3)` shared by the parent and two worker processes so `save_connection` and `save_llm` begin together; run the race repeatedly and assert both disjoint updates survive. Add a second process test where one worker holds `shared_file_lock`, the contender signals that it is ready, and an `Event` proves its mutation callback cannot enter until the owner releases. Add a reader loop which repeatedly parses the file during writes and never sees partial JSON. Assert a malformed existing JSON document raises `ConnectionStoreCorruptError` and remains byte-for-byte unchanged.

In `tests/test_private_files.py`, add platform integration tests. POSIX asserts
directory `0o700` and file `0o600`. Windows creates a real directory/file,
reads the resulting security descriptor through `GetNamedSecurityInfoW`, and
asserts the DACL is protected from inheritance and its allow ACEs name only the
current process user SID and LocalSystem. Explicitly reject `Everyone`,
`BUILTIN\\Users`, and `Authenticated Users`. A mocked call or an
`os.chmod(0o600)` assertion is not Windows privacy evidence.
Name the required Windows node
`test_windows_private_paths_have_protected_current_user_dacl`.
Also create
`tests/test_connection_store.py::test_windows_connection_file_has_protected_current_user_dacl`
through the real public save function; it must inspect the final connection
file rather than merely call the helper.

```python
def test_concurrent_connection_and_llm_saves_preserve_both_blocks(tmp_path, monkeypatch):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    first = context.Process(target=_save_connection_worker, args=(tmp_path, barrier))
    second = context.Process(target=_save_llm_worker, args=(tmp_path, barrier))
    first.start(); second.start(); barrier.wait(timeout=5)
    first.join(10); second.join(10)
    assert first.exitcode == second.exitcode == 0
    saved = json.loads((tmp_path / "connection.json").read_text("utf-8"))
    assert saved["host"] == "h"
    assert saved["llm"]["model"] == "m"
```

The worker functions are module-level (required by Windows `spawn`), set
`RNASEQ_AGENT_HOME` themselves, wait on the shared barrier, and call the real
public save functions. They do not patch a post-read hook or coordinate through
sleep timing.

- [ ] **Step 2: Run RED tests**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_connection_store.py -k "concurrent or atomic or corrupt" -vv
```

Expected: at least one lost update or corrupt-state overwrite assertion fails against the current read-modify-write implementation.

- [ ] **Step 3: Add one same-directory atomic transaction primitive**

Use the existing process lock pattern plus a cross-process lock keyed by the
resolved connection path. While holding both locks: read and validate the whole
JSON object, call `mutate`, create a same-directory private temporary with
exclusive semantics, write, flush and `os.fsync`, then `os.replace`, verify final
private security, then `fsync` the containing directory where supported. POSIX
uses `tempfile.mkstemp` (`O_CREAT|O_EXCL`, `0o600`). Windows uses
`CreateFileW(CREATE_NEW)` with a protected security descriptor supplied through
`SECURITY_ATTRIBUTES`, so there is no create-then-restrict window. Close the
handle and remove the exact temporary path on every pre-replace failure; never
follow or overwrite a pre-created temp symlink/reparse point and never replace a
corrupt source document.

```python
def locked_json_transaction(
    path: Path,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    with shared_file_lock(path):
        current = _read_json_object_or_raise(path)
        updated = mutate(copy.deepcopy(current))
        _atomic_replace_json(path, updated)
        return copy.deepcopy(updated)
```

Route `save_connection`, `save_llm`, and `clear_password` through this primitive. Their allowlists must preserve unknown top-level typed blocks while preventing callers from setting those blocks via extra payload fields.

Add failure-injection tests for `json.dump`, `fsync`, and `os.replace`; each
must preserve the old final bytes and leave no temp file. Assert the `0o600`
mode on temp and final files on POSIX. On Windows, `private_files.py` obtains the
current user SID with Win32 token APIs, builds a protected DACL with full-control
allow ACEs only for that SID and `S-1-5-18` (LocalSystem), and supplies that
descriptor to `CreateDirectoryW`/`CreateFileW` at creation. Existing paths must
have a protected matching DACL verified through `GetNamedSecurityInfoW` before
opening for write or replace; a mismatched/reparse path fails closed. SID lookup,
descriptor construction, creation, application, or verification failure is
fatal. Add a pre-existing candidate-name test proving exclusive creation chooses
a different temp and never overwrites the candidate.

- [ ] **Step 4: Run GREEN tests and all store regressions**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_connection_store.py tests/test_private_files.py -q
```

Expected: PASS across threads and at least one spawned-process contention test; malformed JSON remains unchanged.

- [ ] **Step 5: Commit the atomic store boundary**

```powershell
git add src/rnaseq_agent/storage.py src/rnaseq_agent/private_files.py src/rnaseq_agent/connection_store.py tests/test_private_files.py tests/test_connection_store.py
git commit -m "fix: make shared connection writes atomic"
```

---

### Task 2: Add Typed Identity-Bound Roots And Revision CAS

**Primary cases:** `12-13, 35-36, 42`.

**Files:**
- Modify: `src/rnaseq_agent/connection_store.py`
- Modify: `src/rnaseq_agent/agent_tools.py`
- Test: `tests/test_connection_store.py`
- Test: `tests/test_webapp_chat_tools.py`

**Interfaces:**
- Consumes: Task 1 `locked_json_transaction`/`ConnectionStoreCorruptError` and normalized `SSHIdentity` when connection identity is configured.
- Produces: `ApprovedDataRoot`, `BrowsePolicy`, `browse_policy_revision`, `load_browse_policy`, `locked_browse_policy`, `list_approved_data_roots`, `approve_data_root`, `revoke_data_root`, and `BrowsePolicyConflictError` with the exact fixed signatures above.

- [ ] **Step 0: Add a compile-only typed-policy skeleton**

Add the fixed dataclasses, exception, and function signatures before importing
them in tests. Function bodies raise `NotImplementedError`; this scaffold exists
only so RED comes from a runtime/behavior assertion and is committed only with
the completed GREEN implementation.

- [ ] **Step 1: Add failing typed-policy and lifecycle tests**

Add the exact Task 2 nodes named in the RED catalog plus focused tests named
`test_legacy_store_without_roots_is_available_empty_policy`,
`test_missing_or_invalid_identity_is_unavailable_without_fake_identity`,
`test_policy_revision_ignores_llm_and_unrelated_connection_fields`,
`test_policy_revision_changes_for_active_root_or_identity`,
`test_approve_rechecks_root_identity_against_locked_live_payload`,
`test_duplicate_active_canonical_root_is_idempotent`, and
`test_more_than_32_active_roots_fails_closed_without_transport`. Each test must
invoke the public API and assert the concrete `BrowsePolicy`/exception result;
no empty test body or mock-only assertion counts.

Use `created_at` and `revoked_at` exactly. Validate required strings, port range, absolute canonical/requested POSIX paths, unique `root_id`, and timestamps before producing authority.

- [ ] **Step 2: Run policy tests and confirm RED**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_connection_store.py -k "root or policy or revision or identity" -vv
```

Expected: tests collect successfully and fail against the compile-only policy
skeleton before any SSH or credential lookup.

- [ ] **Step 3: Implement deterministic policy loading and digesting**

Normalize active records into a canonical JSON array sorted by `root_id`. Hash only the normalized identity plus active root records, prefixing the lowercase SHA-256 hex digest with `sha256:`.

```python
def browse_policy_revision(identity, active_roots):
    payload = {
        "identity": None if identity is None else {
            "host": identity.host, "user": identity.user, "port": identity.port,
        },
        "roots": [asdict(root) for root in sorted(active_roots, key=lambda item: item.root_id)],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
```

`load_browse_policy` places only non-revoked roots for the live identity in `BrowsePolicy.roots`. A valid configured identity with no root collection yields `available=True`, that identity, and an empty tuple. A missing/invalid identity, unreadable/structurally invalid root collection, or more than 32 active roots yields `available=False`, `identity=None`, and an empty tuple; it must preserve a secret-free reason internally without leaking record details. `browse_policy_revision` hashes JSON `null` for the absent identity so callers never synthesize a placeholder host or user, but an unavailable policy can never authorize transport.

`locked_browse_policy` acquires the same connection-store lock and yields the
policy parsed from bytes read under that lock. It exists only for Task 7's
short, local apply transaction; callers must not perform DNS/SSH or provider
calls while holding it.

- [ ] **Step 4: Implement approval and revocation under CAS**

Inside the Task 1 transaction, normalize the live host/user/port from the payload read under that same lock, require it to equal `root.host/user/port`, recompute the live policy revision, compare it to `expected_revision` with `hmac.compare_digest`, validate the exact root type, and mutate only `approved_data_roots`. Never validate against an identity snapshot loaded before the lock. Revocation sets `revoked_at`; it does not delete or rewrite identity fields. Refuse a 33rd active root. If the live identity already has an active record with the same canonical path, return that existing record and unchanged revision idempotently, even when the requested alias differs; a reused `root_id` with different authority fields is corruption/conflict, not an update.

```python
if not hmac.compare_digest(current_revision, expected_revision):
    raise BrowsePolicyConflictError(REMOTE_ROOT_REVISION_CONFLICT)
```

Reject `/` before entering the mutation and generate `root_<random>` outside caller-controlled payloads in the Settings adapter later.

- [ ] **Step 5: Prove model and generic settings cannot mutate roots**

Keep `approved_data_roots` out of `SHARED_FIELDS`, `CONNECTION_FIELDS`, the `edit_connection` tool schema, and generic config payload allowlists.

```python
def test_edit_connection_schema_has_no_approved_root_fields():
    schema = get_tool("edit_connection").input_schema
    assert "approved_data_roots" not in schema.get("properties", {})
    assert "remote_data_root" not in schema.get("properties", {})
```

- [ ] **Step 6: Run GREEN policy and mutation-boundary tests**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_connection_store.py tests/test_webapp_chat_tools.py -k "root or policy or revision or edit_connection" -q
```

Expected: PASS; changing only the LLM model leaves the policy revision unchanged, and stale approve/revoke calls raise the typed conflict.

- [ ] **Step 7: Commit the typed policy model**

```powershell
git add src/rnaseq_agent/connection_store.py src/rnaseq_agent/agent_tools.py tests/test_connection_store.py tests/test_webapp_chat_tools.py
git commit -m "feat: add identity-bound approved data roots"
```

---

### Task 3: Build The Pure Browse Authorization Service

**Primary cases:** `1-5, 7-11, 32-33`.

**Files:**
- Create: `src/rnaseq_agent/remote_browse.py`
- Modify: `src/rnaseq_agent/remote_transport.py`
- Modify: `src/rnaseq_agent/execution.py`
- Create: `tests/test_remote_browse.py`
- Test: `tests/test_execution.py`

**Interfaces:**
- Consumes: Task 2 `BrowsePolicy`, `ApprovedDataRoot`, and normalized transport identity; Task 0 bounded transport exceptions.
- Produces: exact raw `CommandResult.captured_bytes`, the structural `DeadlineAwareRemoteTransport` protocol, and all constants/dataclasses, `BrowseExecutionBudget`, `BrowseScanner`, `validate_remote_browse_path`, `contains_posix_path`, `resolve_remote_directory`, `provider_browse_summary`, and `browse_remote_fastqs` from the fixed interface section. Task 3 proves authorization with a scripted transport and injected scripted scanner; Task 4 implements the production scanner and transport protocol.

- [ ] **Step 0: Add a compile-only interface skeleton**

Create `remote_browse.py` with the fixed constants/dataclasses/signatures and
function bodies that raise `NotImplementedError`. This is collection scaffolding,
not a behavior implementation, and is not committed until the behavioral tests
below are GREEN. It ensures RED is an assertion/runtime failure rather than a
missing-module import failure.

- [ ] **Step 1: Write the lexical validation table tests**

Use a parameter table that proves rejection happens before policy or transport access:

```python
@pytest.mark.parametrize("value", [
    None, "", "relative/path", "./data", "/data/../secret", "/data/./reads",
    "/data//reads", "/data/reads/", "/data\x00/reads", "/data\nreads",
    "//server/share", "C:/reads", " /data/reads", "/data/reads ",
])
def test_invalid_lexical_path_short_circuits(value, policy_spy, transport_spy, scanner_spy):
    result = browse_remote_fastqs(value, CONTEXT, policy_spy, transport_spy, scanner_spy)
    assert result.error_code == REMOTE_PATH_INVALID
    assert policy_spy.calls == 0
    assert transport_spy.calls == 0
    assert scanner_spy.calls == 0
```

Add accepted cases for `/data`, `/data/batch-1`, Unicode POSIX names, and shell metacharacters that are legal path characters. Validation checks shape; shell quoting remains the scanner's responsibility.

- [ ] **Step 2: Write failing component-containment and authorization tests**

Use a `ScriptedTransport` that records directory-resolution operations and a
separate `ScriptedScanner` which returns a bounded `BrowseScanPayload`. Cover
exact root, descendant, `/data/root2` boundary, case sensitivity, target
missing, target non-directory, target symlink escaping, stale requested-root
alias, longest matching root, other-identity roots, unavailable policy, and no
roots. Success tests pass the scripted scanner explicitly; Task 3 never builds a
remote file-discovery command.

```python
def test_component_containment_rejects_prefix_sibling():
    assert contains_posix_path("/data/root", "/data/root/sample") is True
    assert contains_posix_path("/data/root", "/data/root2/sample") is False

def test_no_active_root_fails_before_transport(empty_policy, transport_spy, scanner_spy):
    result = browse_remote_fastqs(
        "/data/reads", CONTEXT, lambda: empty_policy, transport_spy, scanner_spy,
    )
    assert result.error_code == REMOTE_ROOT_NOT_APPROVED
    assert transport_spy.calls == 0
    assert scanner_spy.calls == 0
```

- [ ] **Step 3: Write the execute-time policy recheck tests**

Provide a policy reader that returns the approved snapshot on its first call and
a changed revision, selected root, or SSH identity on its second call. Assert
`REMOTE_POLICY_CHANGED` and `scripted_scanner.calls == 0` in all three cases.

```python
def test_revocation_between_resolution_and_scan_prevents_scan(scripted_transport, scripted_scanner):
    policies = iter([approved_policy(), revoked_policy()])
    result = browse_remote_fastqs(
        "/data/root/batch", CONTEXT, lambda: next(policies),
        lambda identity: scripted_transport, scripted_scanner,
    )
    assert result.error_code == REMOTE_POLICY_CHANGED
    assert scripted_scanner.calls == 0
```

- [ ] **Step 4: Run the authorization tests to verify RED**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_remote_browse.py -k "lexical or contain or root or policy or identity" -vv
```

Expected: tests collect successfully and fail because the compile-only service
raises `NotImplementedError` or does not yet return the asserted stable code;
an import/collection failure does not count as RED.

- [ ] **Step 5: Implement lexical validation and POSIX component containment**

Parse with `PurePosixPath`, reject ambiguous textual forms before normalization, and compare `.parts`:

```python
def contains_posix_path(root: str, candidate: str) -> bool:
    root_parts = PurePosixPath(root).parts
    candidate_parts = PurePosixPath(candidate).parts
    return candidate_parts[:len(root_parts)] == root_parts
```

Do not use `startswith`, `normcase`, Windows path semantics, or case folding. Root `/` is invalid as a grant even though it is an absolute POSIX path.

Before implementing `BrowseExecutionBudget`, add `captured_bytes` to
`CommandResult` with a backward-compatible default. Set it from the exact raw
combined byte counter in `run_command_bounded`; add an execution test containing
invalid UTF-8 proving the field is not derived from replacement-decoded text.

- [ ] **Step 6: Implement the ordered authorization state machine**

The call order must be observable and fixed:

1. Validate the lexical target.
2. Read the initial policy and return `REMOTE_ROOT_NOT_APPROVED` before transport when unavailable, `identity is None`, or roots are empty.
3. After the non-null guard, construct transport for exactly `policy.identity`.
4. Re-resolve every active root's `requested_path`; flag canonical mismatch as `REMOTE_ROOT_STALE`.
5. Resolve target and require a directory.
6. Select the longest canonical root containing the canonical target.
7. Read policy again and compare identity, revision, and selected root.
8. Call the injected `BrowseScanner` only after the second policy check.
9. Validate its bounded typed payload and project audit metadata.

Task 3 success tests inject a scanner that returns deterministic typed groups;
denial/race tests assert it is not called. Task 4 supplies the only production
implementation, so the Task 3 GREEN checkpoint is reachable without premature
helper construction, parsing, pairing, or grouping behavior.

Return a `BrowseResult` for expected denials and failures; reserve exceptions for programmer errors. Construct the bounded `BrowseAudit` on every return path. Other-identity roots and their paths must never appear in `message`.

- [ ] **Step 7: Implement the de-identified provider hook**

`provider_browse_summary` returns only status, error code, counts, and truncation. Add a test that recursively serializes the summary and proves it excludes each sample basename, requested path, canonical path, root id, and identity.

- [ ] **Step 8: Run GREEN authorization tests**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_remote_browse.py -k "not scanner" -q
```

Expected: PASS for primary cases `1-5, 7-11, 32-33`. Unnumbered service
regressions also prove inactive roots remain undisclosed and root `/` cannot
authorize a scan; primary ownership of cases `12-13` remains in Task 2.

- [ ] **Step 9: Commit the central authorization service**

```powershell
git add src/rnaseq_agent/remote_browse.py src/rnaseq_agent/remote_transport.py src/rnaseq_agent/execution.py tests/test_remote_browse.py tests/test_execution.py
git commit -m "feat: add approved-root browse authorization service"
```

---

### Task 4: Add The Fixed Bounded Scanner And Directory Grouping

**Primary cases:** `14-22, 26`.

**Files:**
- Modify: `src/rnaseq_agent/remote_browse.py`
- Modify: `src/rnaseq_agent/remote_transport.py`
- Modify: `tests/test_remote_browse.py`
- Optional create: `tests/integration/test_remote_browse_ssh.py`
- Test: `tests/test_execution.py`
- Test: `tests/test_remote_transport.py`

**Interfaces:**
- Consumes: Task 3 authorization state machine and `BrowseScanner` seam, plus the corrected Task 0 transport baseline at commit `5f4e0ca`.
- Produces: production SystemSSH/Paramiko implementations of `DeadlineAwareRemoteTransport.execute_bounded`, `scan_remote_fastqs` as the only production `BrowseScanner`, one code-owned scan helper, safe parser, per-file containment checks, typed basename pairing, `BrowseDirectoryGroup`, and bound/error translation used by every route and Settings preview.

- [ ] **Step 1: Write failing helper-construction and framing tests**

Assert user paths occur only as separately quoted positional arguments after `--`; they never enter Python source, an option, a pipeline, a glob, or a command name. Test spaces, single and double quotes, semicolons, dollars, backticks, wildcard characters, leading dashes, tabs, and embedded newlines in remote filenames.

```python
def test_scan_argv_keeps_hostile_path_out_of_helper_source():
    path = "/data/team/a'; touch /tmp/pwned; echo '"
    command = build_scan_command("/data/team", path)
    assert path not in command.helper_source
    assert command.argv[-1] == path
    assert command.max_depth == 2
    assert command.max_candidates == 5_000
```

If the transport interface accepts only a shell command string, assert each value is encoded by one reviewed `shlex.quote` call and decoded as one positional argument by the remote helper.

- [ ] **Step 2: Write failing no-follow, containment, and bounds tests**

Script scanner output for a symlinked directory, symlinked FASTQ, candidate whose resolved path escapes target, candidate outside the selected root, malformed JSON, more than 5,000 candidates, byte overflow, and timeout. Expected mappings are:

```python
ERROR_MAP = {
    CommandTimeoutError: REMOTE_SCAN_TIMEOUT,
    CommandOutputLimitError: REMOTE_SCAN_OUTPUT_LIMIT,
    ScanProtocolError: REMOTE_SCAN_INVALID_OUTPUT,
}
```

An escaped candidate fails the whole operation with `REMOTE_PATH_ESCAPE`; it must not return a trusted partial list. Candidate-limit completion returns `truncated=True` with at most the fixed returned-row cap.

Add deadline/budget tests that use the real `BrowseExecutionBudget` and a
scripted deadline-aware transport: all root resolves, target resolve, and scan
must receive one identical absolute deadline; their encoded stdout/stderr byte
counts reduce one shared allowance; DNS/connect time reduces the same deadline;
the next call is rejected after cumulative exhaustion even when each individual
result is below 4 MiB. A policy with 33 active roots must fail before transport,
while exactly 32 can resolve within the same shared budget.

- [ ] **Step 3: Write failing directory grouping and pairing tests**

```python
def test_grouping_returns_canonical_parent_and_basename_rows():
    result = scan_result_from_canonical_files([
        "/data/root/run-a/S1_R1.fastq.gz",
        "/data/root/run-a/S1_R2.fastq.gz",
        "/data/root/run-b/S2_R1.fastq.gz",
        "/data/root/run-b/S2_R2.fastq.gz",
    ])
    assert [g.canonical_directory for g in result.groups] == [
        "/data/root/run-a", "/data/root/run-b",
    ]
    assert result.groups[0].samples[0].fastq_1 == "S1_R1.fastq.gz"
    assert result.groups[0].samples[0].fastq_2 == "S1_R2.fastq.gz"
    assert "/" not in result.groups[0].samples[0].fastq_1
```

Derive `group_id` server-side as an opaque digest over authorization revision, selected root id, canonical parent, and bounded scan nonce/result identity. Sort groups and rows deterministically.

- [ ] **Step 4: Run scanner tests to verify RED**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_remote_browse.py -k "scanner or framing or symlink or output or timeout or group or pair" -vv
```

Expected: tests collect and fail on helper framing, cumulative budget, or typed
grouping assertions. An import/collection failure does not count as RED.

- [ ] **Step 5: Add browse-specific bounded execution to both transports**

Consume Task 3's exact `CommandResult.captured_bytes`, then implement
`execute_bounded(remote_command, absolute_deadline,
max_capture_bytes)` on SystemSSH and Paramiko. SystemSSH computes remaining time
once for `run_command_bounded` and retains process-tree termination. Paramiko
passes the supplied deadline through DNS, socket connect, SSH handshake, channel
wait, and reads; it does not create a new deadline in `_connect` or command
execution. Both stop capturing once the per-call remaining byte allowance is
exceeded. Keep generic `execute`, upload, download, scheduler, and scientific
jobs on their existing contracts.

Build this method directly on commit `5f4e0ca`'s `_remaining_remote_time`,
`_resolve_remote_addresses`, `_connect_remote_socket`, and Paramiko `_connect`
absolute-deadline flow. Do not add another DNS thread, socket connector,
credential lookup path, or parallel transport class; the new protocol merely
exposes a browse-specific bounded call on the two existing transports.

- [ ] **Step 6: Implement one fixed remote helper and wire the shared resolver**

Use a versioned code-owned Python 3 helper and make remote Python 3 an explicit preflight requirement. Pass root, target, depth, item limit, and remaining deadline as positional values. The helper:

- re-resolves root and target;
- verifies target containment again;
- uses `os.scandir` with `follow_symlinks=False`;
- walks no deeper than 2;
- accepts only regular FASTQ-like files;
- resolves and checks each file before emitting it;
- stops producer-side at 5,000 candidates;
- emits one bounded JSON document with escaped strings and a `truncated` flag.

Create one `BrowseExecutionBudget` before any root resolution and reuse it for
all Task 3 `resolve_remote_directory` calls and the scan. Enforce the 32-root cap before
constructing transport. The shared resolver is also the only remote
canonicalization entry Settings preview may call in Task 5. Do not use
`find -print`, `head`, newline splitting, generic `transport.execute`, or a fresh
deadline/output ceiling per command.

- [ ] **Step 7: Parse, filter, pair, and group safely**

Reject duplicate JSON keys, wrong types, extra unbounded structures, too many records without `truncated`, non-absolute canonical candidates, and any candidate outside target/root. Pair after filtering. Emit `BrowseSampleRow` instances containing only basenames and place the exact canonical parent once on the group.

Keep unmatched results and returned samples separately bounded. Set `truncated=True` whenever the producer limit, returned-row cap, or unmatched cap can make the inventory incomplete.

- [ ] **Step 8: Add opt-in real SSH parity coverage**

When the environment supplies a disposable SSH fixture, create symlink escapes and hostile filenames on the remote side, then run both SystemSSH and Paramiko. Mark the file explicitly so normal CI skips it without credentials:

```powershell
$env:PYTHONPATH='src'
$env:RNASEQ_RUN_REMOTE_BROWSE_SSH='1'
.\.venv\Scripts\python.exe -m pytest tests/integration/test_remote_browse_ssh.py -vv
```

Expected with credentials: both transports produce identical service codes, never follow symlinks, and never create a shell-injection marker file.

- [ ] **Step 9: Run GREEN scanner and transport suites**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_remote_browse.py tests/test_execution.py tests/test_remote_transport.py -q
```

Expected: PASS for primary cases `14-22, 26`; audit records contain counts and
codes but no command, stderr, credential, or file inventory.

- [ ] **Step 10: Commit the bounded scanner**

```powershell
git add src/rnaseq_agent/remote_browse.py src/rnaseq_agent/remote_transport.py tests/test_remote_browse.py tests/integration/test_remote_browse_ssh.py tests/test_execution.py tests/test_remote_transport.py
git commit -m "feat: bound and group approved-root FASTQ scans"
```

If the optional integration file was not created, omit it from `git add`.

---

### Task 5: Add The Settings-Only Root Lifecycle

**Primary cases:** none; Settings lifecycle is a prerequisite for route and
tool-mode cases owned by Task 6.

**Files:**
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `src/rnaseq_agent/webtemplates/settings.html`
- Test: `tests/test_webapp.py`
- Test: `tests/test_connection_store.py`

**Interfaces:**
- Consumes: Task 2 typed CAS APIs and Task 4 `BrowseExecutionBudget` plus shared `resolve_remote_directory`; preview must not use generic transport execution.
- Produces: `GET /api/settings/remote-data-roots`, `POST /api/settings/remote-data-roots/preview`, `POST /api/settings/remote-data-roots`, and `DELETE /api/settings/remote-data-roots/{root_id}`.

- [ ] **Step 1: Isolate Settings tests from the user's real store**

Ensure every root test sets `RNASEQ_AGENT_HOME` to `tmp_path` before app creation and restores it afterward. Add an autouse or local fixture rather than depending on developer machine state.

```python
@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    monkeypatch.setenv("RNASEQ_AGENT_HOME", str(tmp_path))
    return TestClient(create_app(project_dir=tmp_path / "project"))
```

- [ ] **Step 2: Write failing list and preview API tests**

List returns only records belonging to the current identity, including active/revoked status from `revoked_at`, plus the revision computed from its active records. `BrowsePolicy.roots` itself remains active-only. Preview receives only `requested_path`, validates it, requires a real normalized current identity, resolves with the current credential-backed transport, verifies a directory, and returns the exact identity/requested/canonical tuple plus the current revision without persisting. Missing or invalid identity returns the existing connection-validation response before credential lookup/transport; it never constructs a placeholder identity.

```python
def test_preview_has_no_durable_authority(client, fake_transport, store_path):
    response = client.post("/api/settings/remote-data-roots/preview", json={"requested_path": "/data/team"})
    assert response.status_code == 200
    body = response.json()
    assert body["canonical_path"] == "/srv/team"
    assert json.loads(store_path.read_text("utf-8")).get("approved_data_roots", []) == []
```

Test invalid path, missing/non-directory target, missing credentials, inactive-identity non-disclosure, and root `/` rejection. A deadline-aware transport spy must prove preview calls `execute_bounded` with the shared resolver's absolute deadline/remaining bytes and never calls generic `execute`.

As an unnumbered Settings-layer prerequisite for Task 6 case 25, parameterize
`None`, booleans, strings, `0`, `-1`, `65536`,
overflowing integers, whitespace, and option-like port values through three real
layers: Settings preview/approve request parsing, LLM tool argument validation,
and `create_remote_transport`. Attach credential-lookup and socket/transport
sentinels and assert neither is called for any invalid port. Do not satisfy all
three assertions by mocking the shared normalizer once.

- [ ] **Step 3: Write failing approve/revoke and race tests**

Approve accepts the exact preview envelope and `expected_revision`. It re-resolves the requested path, requires identical live identity and canonical path, creates `root_<random>`, records `created_at`, sets `revoked_at=None`, and calls `approve_data_root`. Replayed preview, changed alias, changed identity, and stale revision return HTTP 409 with a stable code. Revoke requires `expected_revision` and returns the new revision.

```python
def test_stale_approve_returns_revision_conflict(client, approved_preview):
    mutate_policy_in_another_client()
    response = client.post("/api/settings/remote-data-roots", json=approved_preview)
    assert response.status_code == 409
    assert response.json()["error_code"] == REMOTE_ROOT_REVISION_CONFLICT
```

- [ ] **Step 4: Run the API tests to verify RED**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_webapp.py -k "remote_data_root or approved_root or root_preview or root_revoke" -vv
```

Expected: 404 for the new endpoints.

- [ ] **Step 5: Implement preview and mutation endpoints**

Use one helper to restore runtime credentials without serializing them. Preview and approve use the same fixed canonicalization operation as `remote_browse.py`; do not recreate shell commands in `webapp.py`. Catch `BrowsePolicyConflictError` as 409/`REMOTE_ROOT_REVISION_CONFLICT` and preserve all stable path/transport errors.

Only the dedicated approve endpoint may construct an `ApprovedDataRoot`. Ignore no unknown root fields: reject them with 400 so a generic or stale client cannot smuggle authority.

- [ ] **Step 6: Add the Settings UI flow**

Add a path input, Preview button, immutable requested/canonical preview, explicit Approve button, active-root table, and Revoke button. Store the preview identity and revision in JavaScript state, clear it on any connection edit, and reload on 409.

```javascript
async function approveRemoteRoot() {
  const response = await apiJson('/api/settings/remote-data-roots', {
    method: 'POST',
    body: JSON.stringify({...rootPreview, expected_revision: rootPreview.browse_policy_revision}),
  });
  rootPreview = null;
  await loadRemoteRoots();
}
```

Display requested and canonical paths only in this local Settings page. Do not add any root field to the generic `saveServer` payload.

- [ ] **Step 7: Run GREEN API and template tests**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_webapp.py tests/test_connection_store.py -k "root or policy or settings" -q
```

Expected: PASS for two-step approval, no preview persistence, CAS conflicts, identity filtering, and generic-save preservation.

- [ ] **Step 8: Commit the Settings lifecycle**

```powershell
git add src/rnaseq_agent/webapp.py src/rnaseq_agent/webtemplates/settings.html tests/test_webapp.py tests/test_connection_store.py
git commit -m "feat: add Settings approved data root lifecycle"
```

---

### Task 6: Route All Four Browse Entries Through One Service

**Primary cases:** `6, 23-25, 27-31, 34, 37-41, 43`.

**Files:**
- Create: `src/rnaseq_agent/remote_scan_store.py`
- Create: `src/rnaseq_agent/security_audit.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `src/rnaseq_agent/agent_tools.py`
- Modify: `src/rnaseq_agent/chat_graph.py`
- Modify: `src/rnaseq_agent/project_intake.py`
- Test: `tests/test_webapp.py`
- Test: `tests/test_webapp_project_wizard.py`
- Test: `tests/test_webapp_chat_tools.py`
- Test: `tests/test_chat_graph.py`
- Test: `tests/test_webapp_workbench.py`
- Test: `tests/test_private_files.py`
- Create: `tests/test_remote_scan_store.py`
- Create: `tests/test_security_audit.py`

**Interfaces:**
- Consumes: Tasks 3-4 `browse_remote_fastqs`/`BrowseResult`, Task 5 policy lifecycle, existing `append_history`, and live tool-mode checks.
- Produces: strict browse project resolver, one `_execute_browse_attempt` wrapper used by workbench/project command/rule chat/LLM, one `_finalize_tool_execution_result` audit owner, context-bound `StoredRemoteScan` records and `RemoteScanReference` values, the shared four-channel `ToolExecutionResult`, request-local exact-result delivery, stable errors, an authoritative atomic security audit, and de-identified History projection.

The executor contract becomes:

```python
@dataclass(frozen=True)
class ToolExecutionContext:
    project_id: str
    thread_id: str | None

ToolExecutor = Callable[
    [str, dict[str, Any], Path, bool, ToolExecutionContext],
    ToolExecutionResult,
]
```

- [ ] **Step 0: Add compile-only route and scan-store interfaces**

Create `remote_scan_store.py` and add the fixed
`StoredRemoteScan`/`RemoteScanReference`/store/discard signatures. Task 7 adds
the public consume function. Add the fixed security-audit functions,
four-channel `ToolExecutionResult`, executor-context, attempt-wrapper, and
finalizer signatures before importing them in tests. New function bodies raise
`NotImplementedError`; do not commit the scaffold separately. This keeps every
RED below at the runtime or assertion layer rather than failing test collection.

- [ ] **Step 1: Isolate route tests and write strict project-binding failures**

Set `RNASEQ_AGENT_HOME` in all fixtures that can read policy, especially `tests/test_webapp_project_wizard.py` and `tests/test_webapp_workbench.py`. Test that an explicit unknown `?project=missing` returns 404/`PROJECT_NOT_FOUND` without accessing the legacy project directory. Only a truly absent project id may map to the documented `legacy:default` identity.

```python
def test_explicit_unknown_scan_project_never_falls_back(client, legacy_project, browse_spy):
    response = client.post("/api/samples/scan-remote?project=missing", json={"path": "/data"})
    assert response.status_code == 404
    assert response.json()["error_code"] == PROJECT_NOT_FOUND
    assert browse_spy.calls == []
    assert legacy_project.read_count == 0
```

- [ ] **Step 2: Write four real-validator route tests plus a convergence spy**

For each route, install a real temporary approved-root policy and a scripted
deadline-aware transport, then send `/data/root/../secret` and an in-root path.
Assert the real `validate_remote_browse_path` rejects `..` before transport and
the real central service authorizes the in-root path. These four behavioral
tests must not mock `browse_remote_fastqs`, the validator, or containment logic.

Add a separate wiring test that patches only `webapp.browse_remote_fastqs` with
one spy and invokes:

1. structured workbench scan;
2. `POST /api/projects/{project_id}/command` with `scan_remote_fastq`;
3. deterministic/rule chat `browse_samples` intent;
4. LLM `browse_remote_samples` tool.

Assert every call supplies the same requested path/policy/transport factories,
the Task 4 production `scan_remote_fastqs`, and the expected
`BrowseContext.source`, project id, and thread id. No adapter may call the old
scanner or construct a `find` command. The spy proves convergence only; it is
not the case-6 security proof.

- [ ] **Step 3: Write failing stable-error projection tests**

Parameterize each service code across all applicable adapters. Assert `ok`, `blocked`, `error_code`, and `message` survive. In particular:

- project command must not replace the code with `NOT_EVALUABLE`;
- rule chat must not hide it only inside free text;
- `_run_tool` must retain the scanner code;
- out-of-root denial never becomes a confirmation card.

```python
@pytest.mark.parametrize("mode", ["read_only", "approved_write", "approved_execute"])
def test_out_of_root_llm_denial_is_not_confirmable(mode, llm_client):
    response = invoke_browse_tool(mode=mode, service_code=REMOTE_ROOT_NOT_APPROVED)
    assert response["error_code"] == REMOTE_ROOT_NOT_APPROVED
    assert response.get("confirmation") is None
```

- [ ] **Step 4: Write failing tool-mode independence tests**

Prove primary cases `37-41, 43` and keep case 42's Task 2 mutation-boundary test
as an unnumbered regression:

- in-root LLM browse runs in `read_only` without a card;
- `disabled` stops LLM/rule browse before policy or transport;
- highest LLM mode cannot bypass roots;
- structured workbench ignores `tool_mode` but always enforces roots;
- `edit_connection` cannot mutate roots through top-level, extra, or nested properties;
- provider-visible errors never contain inactive identity root records.

- [ ] **Step 5: Write failing authoritative-audit and TOCTOU tests**

The security audit record, and only that record, contains the bounded exact
authorization envelope:

```python
{
    "event_id": "audit_0123456789abcdef0123456789abcdef",
    "project_id": "p1",
    "thread_id": "t1",
    "source": "llm_tool",
    "identity_digest": "sha256:identity...",
    "requested_path_digest": "sha256:path...",
    "canonical_target": "/srv/team/run-1",
    "root_id": "root_123",
    "browse_policy_revision": "sha256:...",
    "directory_count": 2,
    "sample_count": 12,
    "unmatched_count": 1,
    "truncated": False,
    "outcome": "allowed",
    "error_code": None,
    "started_at": "2026-09-17T12:00:00Z",
    "completed_at": "2026-09-17T12:00:04Z",
}
```

Invoke every route in allowed, denied, and failed modes and assert every attempt
enters `_execute_browse_attempt`, then exactly one
`_finalize_tool_execution_result` call owns the sole
`_record_browse_audit` call. This includes explicit bad project and disabled
tool-mode denials before the inner executor, path denial, timeout, scan-store
failure, and success. Assert the disabled path reaches no policy, credential,
transport, or inner-executor spy but still produces one denied audit event. Use
spawned processes to commit concurrently and prove every published per-event
record is intact, uniquely identified, and bounded. Also test injected short
writes, write-after-prefix failures, temp-file `fsync` failures, atomic replace
failures, process exit before publish and after publish, directory-flush failure,
malformed existing records, deterministic recovery, duplicate identical events,
and conflicting duplicate event ids. Any JSONL export must be regenerated from
validated records and is tested as a non-authoritative projection. History must equal
`browse_history_projection(event)` and contain only event
id/source/counts/truncation/outcome/error/timestamps.

Add four-channel contract tests proving only `model` reaches a provider tool
message, only `log_projection` reaches generic `tool_log`, exact
`security_audit` reaches `_record_browse_audit` once and is cleared before state
persistence, and `local` is delivered only through the request-local sink. A
repository search must find no competing three-channel `ToolExecutionResult`
constructor or direct `_record_browse_audit` caller outside the finalizer.

For each allowed browse, assert the scan store persists every bounded directory
group before the adapter exposes a response and returns a
`RemoteScanReference`. Denied and failed attempts must create no stored scan.
With an injected UTC clock, test 15-minute expiry and cleanup during store/read,
the maximum of eight live records after cleanup, independent random 128-bit
`scan_id`/`source_ref` values, and a canonical result revision covering every
immutable stored field. Spawn writers/readers to prove cross-process locking and
the per-event failure-atomic commit protocol prevent partial records. Verify private directory permissions
with the shared helper: POSIX directory `0o700` and files `0o600`; on Windows,
real security descriptors for the scan directory/files and security audit
directory/file must have protected DACLs whose allow ACEs name only the current
user SID and LocalSystem. Verify exclusive temp creation and exact-temp cleanup on
injected short-write, prefix-write, fsync, replace, and crash/recovery failures.
Prove a scan-store write failure replaces
the allowed event with one failed `REMOTE_SCAN_STORE_FAILED` event and exposes
no exact result. Prove an audit-commit failure invokes `discard_remote_scan`,
returns `REMOTE_SECURITY_AUDIT_FAILED`, leaves no retrievable record, and emits
neither local/model/log success nor a second commit attempt. Separately inject
an `os.replace` failure before publication and assert definitive `absent`
rollback; inject a directory-fsync failure after publication and assert
`AuditCommitUncertainError` retains the pending scan, exposes no channels, and
does not discard the published audit. A later bounded reconciliation must
classify the record as `committed` or `absent` before delivery or rollback.

Name the two real Windows artifact tests
`tests/test_remote_scan_store.py::test_windows_remote_scan_store_has_protected_current_user_dacl`
and
`tests/test_security_audit.py::test_windows_security_audit_has_protected_current_user_dacl`;
helper-only or mocked ACL assertions cannot satisfy them.

Exercise the same successful result through workbench (`thread_id=None`), rule
chat, and LLM chat contexts and assert their independent records preserve
project id, exact thread id (including `None`), source, policy, whole-scan
groups, and result revision. Wrong-reference discard must fail closed. Task 7
enforces this binding for apply; the separate disclosure plan enforces it again
when resolving `source_ref`. No Task 6 API may erase or rewrite the context.

Add service-driven revision and identity race tests, plus one real concurrent connection save using the connection-store lock. Assert scan is not called under a mixed snapshot.

Use sentinel strings for case 27 at every layer: password, private-key path and
contents, encrypted credential token, LLM API key, hostile path, stdout, and
stderr. Recursively serialize the HTTP result, request-local model projection,
`ChatState`, checkpointer snapshot, generic `tool_log`, History, security audit,
and exception/command summary; assert none contains any credential/API sentinel,
and only the authorized local result/security audit may contain the hostile
canonical path where their fixed schemas allow it. This is separate from the
transport-only secret regression verified in Task 0.

- [ ] **Step 6: Run convergence tests to verify RED**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_remote_scan_store.py tests/test_webapp.py tests/test_webapp_project_wizard.py tests/test_webapp_chat_tools.py tests/test_chat_graph.py tests/test_webapp_workbench.py -k "scan or browse or project_not_found or tool_mode or audit or policy_changed or source_ref" -vv
```

Expected: current route-specific scanners, flattened errors, or missing executor context fail the new assertions.

- [ ] **Step 7: Add one attempt wrapper and one terminal audit owner**

Implement `_execute_browse_attempt` as the outer boundary for all four entry
points. It allocates the bounded attempt event before any early exit, then:

1. resolves an explicit project id strictly; `PROJECT_NOT_FOUND` becomes a
   denied four-channel result without legacy fallback;
2. for rule/LLM sources, performs the live `tool_mode` check and returns a
   denied four-channel result while still outside the inner tool executor;
3. builds a credential-restoring `transport_factory(identity)` which verifies
   the requested identity equals the normalized live connection snapshot, then
   calls `create_remote_transport`;
4. calls the one inner `browse_remote_fastqs` service with Task 4's production
   scanner;
5. for an allowed result, stores the whole bounded scan and creates the opaque
   reference; if storage fails, replaces the allowed audit with one failed
   `REMOTE_SCAN_STORE_FAILED` event and removes exact local/model success;
6. returns `ToolExecutionResult(local, model, log_projection, security_audit)`
   without persisting the audit.

Denied/failed outcomes create no scan record. Delete `_scan_remote_samples` or
leave only a thin compatibility adapter which calls `_execute_browse_attempt`
and owns no validation, command, guard, or audit logic. No route may call
`browse_remote_fastqs` directly.

Implement `_finalize_tool_execution_result` as the sole terminal owner described
in the fixed contract. Workbench, project command, deterministic/rule chat, and
`chat_graph.node_execute` each call it exactly once. It attempts the independent
security commit before any channel is delivered or persisted. On success it
appends only the de-identified History projection, clears `security_audit`, and
returns the other three channels. On audit I/O failure it rolls back an
unexposed pending scan and returns only `REMOTE_SECURITY_AUDIT_FAILED`. This
explicit boundary audits project/tool-mode early denials without invoking the
inner executor and prevents double persistence by later disclosure work.

Use this global lock order whenever nesting is unavoidable:

1. connection-store lock;
2. per-project remote-scan-store lock;
3. project-state lock.

Acquire the security-audit commit lock only after all three locks above have
been released. Never hold any filesystem lock during DNS, SSH, or remote
execution. Prefer acquire/read/release phases. Apply is the one atomic
multi-store operation: hold connection lock, then scan-store lock, then
project-state lock; release all before recording the independent security audit
and de-identified History projection. Add a lock-order test that instruments
lock entry and fails on inversion, plus a spawned-process no-deadlock test.

- [ ] **Step 8: Convert the workbench and project command adapters**

Both structured routes construct `BrowseContext`, call
`_execute_browse_attempt`, and pass its result through
`_finalize_tool_execution_result` before reading the local API envelope. The
finalizer alone uses `append_history`, and only with
`browse_history_projection`; History is a display index and never substitutes
for the authoritative security log. Never persist groups, canonical paths, root
ids, or unmatched filenames in History. Preserve token/CSRF protections already
required by these routes.

- [ ] **Step 9: Convert deterministic chat and LLM tool adapters**

Always enter `_execute_browse_attempt` for rule/LLM browse. Inside that wrapper,
keep the live `tool_mode` check immediately before and outside the inner
executor invocation, so a disabled attempt is audited without consulting policy
or transport. `browse_remote_samples` remains `risk="read"` and `POLICY_NEVER`;
roots authorize infrastructure scope, so out-of-root failure is a terminal
structured denial rather than a user-confirmable write. Pass
`ToolExecutionContext(project_id, thread_id)` through `chat_graph.py`.

Construct browse results as:

```python
ToolExecutionResult(
    local={"result": exact_response, "reference": asdict(reference)},
    model=provider_browse_summary(result, reference.source_ref),
    log_projection=browse_log_projection(result.audit),
    security_audit=result.audit,
)
```

`chat_graph.node_execute` first passes the wrapper to the common finalizer. Only
after it returns with `security_audit=None` does the graph send `model` to the
provider, append `log_projection` to generic `tool_log`, and forward `local` to
a request-scoped HTTP/SSE callback. Exact audit fields such as canonical target,
root id, identity, and policy revision belong only in the independently
persisted `BrowseAudit` and must never enter the generic log. Add tests that
inspect final `ChatState` and the SQLite checkpointer and prove exact paths,
filenames, root id, policy revision, `ToolExecutionResult.local`, and
`ToolExecutionResult.security_audit` are absent. The exact callback collector
must be newly allocated per request, never global, and cleared in `finally`. Do
not add disclosure grants in this task.

Adapt existing non-browse tools as
`ToolExecutionResult(local=sanitized_result, model=sanitized_result,
log_projection=sanitized_log_projection, security_audit=None)` so the executor
has one return type. Preserve their existing confirmation and sanitization
behavior; do not widen their provider or generic-log payloads.

- [ ] **Step 10: Run GREEN route and audit tests**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_remote_scan_store.py tests/test_security_audit.py tests/test_webapp.py tests/test_webapp_project_wizard.py tests/test_webapp_chat_tools.py tests/test_chat_graph.py tests/test_webapp_workbench.py -q
```

Expected: PASS for four-source real validation, convergence, strict project
binding, common errors, four-channel separation, exactly-once audit ownership,
pre-executor denial audit, lock ordering, races, and tool-mode independence.

- [ ] **Step 11: Search for bypasses before committing**

```powershell
rg -n "_scan_remote_samples|find .*fastq|find .*fq|scan_remote_fastq|browse_remote_samples" src tests
```

Expected: every product entry reaches `remote_browse.py`; any retained legacy name is a thin call-through verified by tests, and no route owns free-form remote discovery.

- [ ] **Step 12: Commit route convergence**

```powershell
git add src/rnaseq_agent/remote_scan_store.py src/rnaseq_agent/security_audit.py src/rnaseq_agent/webapp.py src/rnaseq_agent/agent_tools.py src/rnaseq_agent/chat_graph.py src/rnaseq_agent/project_intake.py tests/test_private_files.py tests/test_remote_scan_store.py tests/test_security_audit.py tests/test_webapp.py tests/test_webapp_project_wizard.py tests/test_webapp_chat_tools.py tests/test_chat_graph.py tests/test_webapp_workbench.py
git commit -m "feat: route every remote browse through approved roots"
```

---

### Task 7: Apply Server-Verified Directory Groups To Sessions

**Primary cases:** `44-48`.

**Files:**
- Modify: `src/rnaseq_agent/remote_scan_store.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `src/rnaseq_agent/webtemplates/workbench.html`
- Modify: `src/rnaseq_agent/project_intake.py`
- Modify only if needed: `src/rnaseq_agent/sample_detection.py`
- Test: `tests/test_remote_browse.py`
- Modify: `tests/test_remote_scan_store.py`
- Test: `tests/test_webapp_project_wizard.py`
- Test: `tests/test_webapp_workbench.py`
- Test: `tests/test_validation.py`
- Test only if pairing moves: `tests/test_sample_detection.py`

**Interfaces:**
- Consumes: Task 4 groups and authorization envelope; Task 6 context-bound `StoredRemoteScan`/`RemoteScanReference` records and route adapters.
- Produces: context-bound crash-recoverable `consume_remote_scan`, `POST /api/projects/{project_id}/fastq/scan-apply`, one-time server-verifiable selection and replay prevention, filename-only remote-prestaged session draft, canonical `remote_data_dir`, duplicate/truncation enforcement, and multi-directory UI.

- [ ] **Step 0: Add the compile-only consume signature**

Add the fixed `consume_remote_scan(..., expected_context, apply)` signature
before importing it in Task 7 tests. Its body raises `NotImplementedError`; Task
6 stored lifecycle fields but did not commit this public consume scaffold. Do
not commit until the crash/replay tests below are GREEN.

- [ ] **Step 1: Define and test the server-side selection contract**

The apply request contains `scan_id`, `result_revision`, selected `group_id`, and `accept_truncated`. When the stored result is truncated, apply requires `accept_truncated is True`; otherwise it returns 409 without writing. It must not accept a browser-posted canonical path or browser-posted sample rows as authority.

```json
{
  "scan_id": "scan_0123456789abcdef0123456789abcdef",
  "result_revision": "sha256:<digest>",
  "group_id": "group_<opaque>",
  "accept_truncated": false
}
```

Task 6 already stores every allowed exact result in
`<project_dir>/.remote-scans/`, returns its context-bound
`RemoteScanReference`, and tests creation, atomicity, capacity, expiry cleanup,
random 128-bit identifiers, file modes, and failure cleanup. Extend that store
with `consume_remote_scan`; on apply, the endpoint reloads current policy and
consumes the Task 6 record rather than reconstructing authority from request
JSON.

Add direct lookup-helper tests before testing the endpoint. For both `scan_id`
and `source_ref`, reject empty and oversized values, wrong prefixes, upper-case
or non-hex characters, `/`, `\\`, `.`, `..`, repeated or alternate separators,
NUL/control characters, POSIX absolute paths, Windows drive paths, UNC paths,
device names, and Unicode lookalikes. For every rejected value, place a sentinel
file outside `.remote-scans` and assert read, replace, and delete operations
leave the sentinel untouched. Add symlink and Windows reparse-point tests for
the store parent and final component; the helper must fail closed before opening
or replacing anything. Assert that valid identifiers map only to the SHA-256
derived names, that a malformed or mismatched alias cannot redirect a source
lookup, and that recovery of a missing alias never escapes the verified store.

The route constructs, rather than accepts, this expected context:

```python
BrowseContext(project_id=project_id, thread_id=None, source="workbench")
```

Only an exact workbench record may apply. A project-command, rule-chat, LLM,
other-project, or non-null-thread record fails with
`REMOTE_SCAN_REFERENCE_INVALID` before project mutation. The browser continues
to post only the four fields shown above; `thread_id`, `source`, identity, root,
policy, and canonical path are never caller authority.

Add consume tests for revision tampering, every context mismatch, wrong
root/identity/policy, unknown group, successful one-time consumption, replay as
`REMOTE_SCAN_REFERENCE_USED`, expiry as `REMOTE_SCAN_REFERENCE_EXPIRED`, and a
failed apply callback remaining retryable. Lookup must classify the presented
target before sweeping unrelated records. Verify consumed/expired tombstones
survive for the fixed 24-hour terminal TTL and pending capacity ignores them.
Spawn competing consumers to prove the scan/project locks prevent duplicate
application. The apply path follows global
`connection -> remote-scan -> project` order and releases all three before any
History write; group apply is not a new browse attempt and must not call
`_record_browse_audit`.

- [ ] **Step 2: Write failing one-directory persistence and workflow tests**

Run a one-directory scan, POST its `scan_id`, `result_revision`, and sole
`group_id` to `/api/projects/{project_id}/fastq/scan-apply`, and read
`project.json`. Assert:

```python
assert project["samples"]["items"][0]["fastq_1"] == "S1_R1.fastq.gz"
assert project["samples"]["items"][0]["fastq_2"] == "S1_R2.fastq.gz"
assert "/" not in project["samples"]["items"][0]["fastq_1"]
assert project["samples"]["remote_data_dir"] == "/srv/team/run-a"
```

Also assert `project["samples"]["source"] == "remote_path"` and `project["samples"]["remote_prestaged"] is True`, then continue the test through filename validation, plan, and confirm.

Assert the same atomic project update contains one bounded
`remote_scan_apply_receipts` entry with opaque `apply_claim_id`, `scan_id`,
`result_revision`, `group_id`, and timestamp, but no canonical path, basename,
sample id, credential, or provider data. Receipt pruning may remove an entry
only after the corresponding scan record is terminal; unresolved `applying`
claims always retain their receipt.

- [ ] **Step 3: Write failing multi-directory, duplicate, and truncation tests**

Assert multiple parent directories remain separate, no default silently merges them, and apply without a selected group returns a stable 400/422 error. Reject duplicate sample ids, R1/R2 names, or ambiguous basename pairs before writing any session/project file. Test wrong `result_revision`, expired reference, policy change, and replay without touching `project.json`. A truncated result must show a visible warning and fail apply unless the API supports and receives the explicit action required by the spec.

```python
def test_duplicate_selected_group_does_not_write_session(client, duplicate_scan, project_path):
    before = project_path.read_bytes()
    response = apply_scan_group(client, duplicate_scan.group_id)
    assert response.status_code == 422
    assert project_path.read_bytes() == before
```

Add failure injection and spawned-process recovery tests at every durable edge:

- claim-file replace fails before `applying`: project remains unchanged;
- process exits after the durable claim but before project replace: retry uses
  the same claim id and applies once;
- process exits or final scan-record replace fails after the atomic project
  samples+receipt write: retry observes the receipt, does not rewrite samples,
  and completes `consumed`;
- response delivery fails after `consumed`: retry returns
  `REMOTE_SCAN_REFERENCE_USED` and never rewrites the project.

Count project-writer invocations and final sample revisions so a lock-only
implementation that replays after a crash cannot pass.

- [ ] **Step 4: Run group-apply tests to verify RED**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_remote_browse.py tests/test_remote_scan_store.py tests/test_webapp_project_wizard.py tests/test_webapp_workbench.py tests/test_validation.py -k "group or scan_store or reference or expiry or replay or context or claim or receipt or crash or tombstone or basename or duplicate or truncated or remote_prestaged" -vv
```

Expected: tests collect and fail because consume is a compile-only scaffold, the
UI/API trusts full paths or lacks selected-group verification, and no durable
claim/receipt recovery exists. Import failure does not count as RED.

- [ ] **Step 5: Implement server-verified group application**

Implement the exact store interfaces and recovery protocol from the fixed
section. The apply route enters `locked_browse_policy`, validates current
identity/root/revision, then takes the scan lock and validates scan id/result
revision/project/exact workbench context/expiry/state/group. It durably creates
or resumes `apply_claim_id`, then calls the project writer under the
project-state lock in documented order. The writer is idempotent for that claim:
it either observes the matching receipt or atomically writes both filename-only
sample state and receipt. Only then may the scan store commit `consumed_at`.

On recovery, an `applying` record reuses its existing claim id. Matching receipt
means finalize consumption without rewriting; missing receipt means retry the
atomic project callback. Never generate a second claim for the same reference.
Release all locks before the de-identified apply History entry. Pass only
`BrowseSampleRow` basenames plus `canonical_directory` to intake/session
builders; the receipt carries opaque ids only.

Do not weaken absolute-path rejection in `validation.py` or `safety.py`. If pairing remains in `remote_browse.py`, leave `sample_detection.py` unchanged.

- [ ] **Step 6: Implement the grouped workbench UI**

For one complete group, auto-select it and preview basename rows. For multiple groups, render a select/radio list showing the authorized directory and bounded sample counts, require explicit choice, and never merge groups. Show truncation as a persistent warning beside the apply action and require the supported explicit acknowledgment.

The browser posts only `scan_id`, `result_revision`, `group_id`, and truncation action. Treat any exact paths in DOM as display data, never request authority.

- [ ] **Step 7: Run GREEN group and workflow tests**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_remote_browse.py tests/test_remote_scan_store.py tests/test_webapp_project_wizard.py tests/test_webapp_workbench.py tests/test_validation.py tests/test_sample_detection.py -q
```

Expected: PASS for cases `44-48`, including persisted `project.json` and plan/confirm rather than only static HTML assertions.

- [ ] **Step 8: Commit session integration**

```powershell
git add src/rnaseq_agent/remote_scan_store.py src/rnaseq_agent/webapp.py src/rnaseq_agent/webtemplates/workbench.html src/rnaseq_agent/project_intake.py src/rnaseq_agent/sample_detection.py tests/test_remote_browse.py tests/test_remote_scan_store.py tests/test_webapp_project_wizard.py tests/test_webapp_workbench.py tests/test_validation.py tests/test_sample_detection.py
git commit -m "fix: persist approved scan groups as filename-only sessions"
```

Omit unchanged optional files from `git add`.

---

### Task 8: Document Permission Boundaries And Run The Release Gate

**Primary cases:** `49-51`; all earlier cases are regression-only here.

**Files:**
- Modify: `docs/llm_tool_permission_model.md`
- Modify only if a compatibility regression fails: `src/rnaseq_agent/webapp.py`
- Test: `tests/test_webapp_project_wizard.py`
- Test: `tests/test_webapp_workbench.py`
- Test: `tests/test_validation.py`
- Test: all repository tests
- External verify: `C:\Users\Administrator\WorkBuddy\2026-09-15-15-27-02\bkbio-eval`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: documented independent permission axes and release evidence covering the 51-case audit.

- [ ] **Step 1: Add failing compatibility regressions before documentation**

Add or identify tests proving:

- a legacy project with no roots can load, inspect state, plan, confirm, and execute without reading browse policy;
- existing counts/expression upload, preview, plan, and confirm behavior is unchanged;
- a frozen remote project remains valid without a grant because roots govern new discovery only.

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests -k "legacy_project or counts or expression or frozen_remote" -vv
```

Expected before any necessary compatibility fix: the new regression exposes accidental browse-policy coupling. If all pass, record the existing tests as the evidence and make no compatibility code change.

- [ ] **Step 2: Fix only demonstrated compatibility coupling and rerun focused tests**

Ensure root policy is consulted only when an operation invokes remote discovery. Planning, confirming, executing an existing contract, and counts workflows must not require an approved root.

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests -k "legacy_project or counts or expression or frozen_remote" -q
```

Expected: PASS for cases `49-51`.

- [ ] **Step 3: Update the permission-model document**

Document these three independent questions:

| Axis | Question | Authority |
|---|---|---|
| Approved remote root | May the application scan this remote directory? | Settings-only root lifecycle |
| `tool_mode` | May rule/LLM logic invoke this class of tool? | live tool-mode policy |
| `data_scope` | Which exact project data may a provider receive? | separate disclosure grants |

State that local structured UI may display exact authorized results, while provider dispatch defaults to de-identified counts/status until exact disclosure is separately granted. Explain that the protected per-event bounded security-audit spool and its derived JSONL display export are allowed service side effects for a read tool, while only the per-event record directory is authoritative; root mutation is never a read-tool side effect. Record the pathname-race residual risk: re-resolution, no-follow traversal, per-file containment, and one fixed helper reduce but do not eliminate races that require `openat`/`O_NOFOLLOW` on an installed remote helper.

Document the shared four-channel result explicitly: `local` is request-local,
`model` is provider-safe, `log_projection` is the only generic-log payload, and
`security_audit` is finalized exactly once by the independent audit owner before
the other channels are delivered. Document workbench-only scan apply context,
durable claim/project-receipt recovery, and whole-scan `source_ref` semantics.

- [ ] **Step 4: Run the complete repository gate**

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest tests/test_private_files.py::test_windows_private_paths_have_protected_current_user_dacl tests/test_connection_store.py::test_windows_connection_file_has_protected_current_user_dacl tests/test_remote_scan_store.py::test_windows_remote_scan_store_has_protected_current_user_dacl tests/test_security_audit.py::test_windows_security_audit_has_protected_current_user_dacl -q -rs
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

Expected: all tests pass, compileall exits 0, and diff check emits no output. On
the supported Windows release host, the protected-DACL integration test must run
and pass; a skip is a release failure. A non-Windows CI job may skip that test
only when a separate recorded Windows run supplies the required ACL evidence.

- [ ] **Step 5: Run the external evaluator gate**

```powershell
Set-Location 'C:\Users\Administrator\WorkBuddy\2026-09-15-15-27-02\bkbio-eval'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\mutation_check.py --level unit,L0
.\.venv\Scripts\python.exe -m bkbio_eval.cli list --select L1
```

Expected: evaluator tests and unit/L0 mutation checks pass. `L1_tcga_coad_tumor_normal` and `L1_airway_deseq2` remain listed as structured expected refusals, not fabricated runnable successes.

- [ ] **Step 6: Run the final authorization-bypass search**

```powershell
Set-Location 'F:\SysuccRNAseqAgent-master\SysuccRNAseqAgent-master'
rg -n "_scan_remote_samples|find .*fastq|find .*fq|remote_base_dir.*approved|remote_workdir.*approved|remote_data_dir.*approved|approved_data_roots" src tests docs
```

Expected: no independent browse implementation, no automatic migration to approved roots, and no root mutation surface outside dedicated Settings lifecycle functions/routes.

- [ ] **Step 7: Review the complete audit ledger below**

For each numbered case, record the exact test node id and latest passing command in the implementation PR/commit notes. Do not mark a batch green from range coverage alone.

- [ ] **Step 8: Commit documentation and any demonstrated compatibility tests/fix**

```powershell
git add -- docs/llm_tool_permission_model.md tests/test_webapp_project_wizard.py tests/test_webapp_workbench.py tests/test_validation.py src/rnaseq_agent/webapp.py
git commit -m "docs: define approved remote root permission boundary"
```

This is the sole staging command: remove `src/rnaseq_agent/webapp.py` when Step 2
required no product fix, and remove any unchanged optional test path before
running it. Do not use a broad `git add`. Before committing, compare
`git diff --staged --name-only` against the remaining exact allowlist and unstage
anything else.

---

## Complete 51-Case Audit Ledger

Every number appears once under its primary task. Supporting tests in other
tasks are unnumbered prerequisites/regressions.

### Task 2: Store Identity, CAS, And Mutation Boundary (`12-13, 35-36, 42`)

- [ ] `12` Other host/user/port roots are inactive and absent from errors.
- [ ] `13` Root `/` cannot be approved.
- [ ] `35` Concurrent approve/revoke CAS cannot resurrect a root from a stale page.
- [ ] `36` Partial/corrupt store cannot broaden permission or reach transport.
- [ ] `42` Generic settings and model edits preserve existing roots but cannot set, clear, or widen them.

### Task 3: Authorization And Containment (`1-5, 7-11, 32-33`)

- [ ] `1` No approved roots returns `REMOTE_ROOT_NOT_APPROVED` before transport/scan.
- [ ] `2` Exact canonical root succeeds.
- [ ] `3` Canonical descendant succeeds.
- [ ] `4` `/data/root2` does not match `/data/root`.
- [ ] `5` Relative path fails before transport.
- [ ] `7` `.`, leading/repeated `//`, NUL, newline, and other control characters fail consistently.
- [ ] `8` Target symlink resolving outside root returns `REMOTE_PATH_ESCAPE`.
- [ ] `9` Approved-root alias resolving differently returns `REMOTE_ROOT_STALE`.
- [ ] `10` Canonical comparison is case-sensitive.
- [ ] `11` Overlapping roots select and audit the longest match.
- [ ] `32` Revocation between authorization and scan returns `REMOTE_POLICY_CHANGED` before scanner invocation.
- [ ] `33` Identity change between authorization and scan returns `REMOTE_POLICY_CHANGED` before scanner invocation.

### Task 4: Scanner And Bounds (`14-22, 26`)

- [ ] `14` Symlinked subdirectory is not traversed.
- [ ] `15` Symlinked FASTQ is not returned as a regular file.
- [ ] `16` Every file is canonicalized; a raced/out-of-root candidate fails closed without trusted partial results.
- [ ] `17` Spaces, quotes, semicolons, backticks, `$()`, wildcards, and leading-dash components cannot alter the fixed command.
- [ ] `18` Newline-bearing filenames are safely framed or rejected without extra records.
- [ ] `19` More than 5,000 candidates returns at most the limit and `truncated=true`.
- [ ] `20` More than 4 MiB cumulatively across canonicalization and scan terminates as `REMOTE_SCAN_OUTPUT_LIMIT` without materializing an unlimited buffer.
- [ ] `21` One absolute 60-second deadline covers DNS, connect, every canonicalization, and scan; timeout terminates owned resources and returns `REMOTE_SCAN_TIMEOUT`.
- [ ] `22` Paramiko and SystemSSH yield equivalent timeout/output-limit codes.
- [ ] `26` SystemSSH argv cannot turn destination data into an option.

### Task 6: Boundaries, Convergence, Audit, And Tool Mode (`6, 23-25, 27-31, 34, 37-41, 43`)

- [ ] `6` All four routes apply identical `..` rejection through central validation.
- [ ] `23` Host/user beginning with `-` fails before Settings credential lookup or tool execution.
- [ ] `24` Host/user with whitespace, `@`, control characters, or shell metacharacters is rejected at Settings/tool boundaries.
- [ ] `25` Invalid/out-of-range ports fail at Settings, LLM validation, and transport construction without credential or socket access.
- [ ] `27` Multi-layer sentinels prove passwords, keys, API secrets, and ciphertext are absent from transport errors, HTTP/model results, `ChatState`, checkpointer, tool log, History, and security audit.
- [ ] `28` Explicit invalid project returns `PROJECT_NOT_FOUND` and never touches legacy storage.
- [ ] `29` Workbench, project command, rule chat, and LLM invoke one authorization service.
- [ ] `30` `security_audit` reaches the authoritative sink once; generic LLM tool log keeps only `log_projection`.
- [ ] `31` Structured History references the audit event with a bounded de-identified display projection and is never the authoritative audit.
- [ ] `34` Concurrent connection save and scan fail closed without mixed configuration.
- [ ] `37` In-root LLM browse runs in `read_only` without confirmation.
- [ ] `38` `disabled` is audited once but blocks before inner executor, root resolution, or transport.
- [ ] `39` Out-of-root LLM browse is denied in `approved_execute` without a confirmation card.
- [ ] `40` In-root structured workbench browse works while LLM tools are disabled.
- [ ] `41` Out-of-root structured workbench browse fails in every tool mode.
- [ ] `43` Provider-visible payloads never contain other-identity root records.

### Task 7: Grouped Results And Session Application (`44-48`)

- [ ] `44` One-directory apply stores basenames and canonical `remote_data_dir`.
- [ ] `45` The remote-prestaged session passes validation and can plan/confirm.
- [ ] `46` Multiple depth-2 parents remain separate selectable groups.
- [ ] `47` Duplicate basename/sample ids fail before session creation.
- [ ] `48` Truncated inventory cannot apply without visible warning and explicit supported action.

### Task 8: Compatibility (`49-51`)

- [ ] `49` Existing rootless project can load, inspect, plan, confirm, and execute without browsing.
- [ ] `50` Existing counts/expression workflows remain unchanged.
- [ ] `51` Frozen remote project remains valid when its old directory is not an approved browse root.

---

## Dependency And Commit Order

```text
Task 0: verify corrected transport prerequisite at 5f4e0ca (no commit)
  -> Task 1: atomic shared connection store
     -> Task 2: typed roots and policy CAS
        -> Task 3: pure authorization service
           -> Task 4: deadline-aware transport, bounded scanner and grouping
              -> Task 5: Settings lifecycle through the shared resolver
                 -> Task 6: four-route convergence and authoritative audit
                    -> Task 7: crash-recoverable server-verified group apply
                       -> Task 8: compatibility, docs, and release gate
```

Task 5 must follow Task 4 because Settings preview is required to use the same
absolute-deadline/cumulative-byte resolver as browse. Task 6 must convert all
four entry points in one reviewable commit; do not ship a state where any route
still bypasses the central service. Task 7 must follow Task 6 because it consumes
Task 6's context-bound records, route response, private scan store, and common
lock/audit ownership rules.

Planned commit sequence:

1. `fix: make shared connection writes atomic`
2. `feat: add identity-bound approved data roots`
3. `feat: add approved-root browse authorization service`
4. `feat: bound and group approved-root FASTQ scans`
5. `feat: add Settings approved data root lifecycle`
6. `feat: route every remote browse through approved roots`
7. `fix: persist approved scan groups as filename-only sessions`
8. `docs: define approved remote root permission boundary`

Each commit must pass its focused GREEN command. Task 8 must pass the full repository and external evaluator gates before the branch is presented for review.
