# Approved Remote Data Roots Design

Date: 2026-09-17

Status: implementation baseline

## Problem

Remote FASTQ discovery currently accepts any absolute directory that the configured SSH account can read. The structured workbench, project command, deterministic chat, and LLM tool reach overlapping implementations with different validation and audit behavior. A read-only model tool can therefore enumerate filenames outside the user's intended data area, and a structured route can bypass model-specific controls entirely.

Remote browsing needs a reusable infrastructure permission that is narrower than the SSH account. The permission must remain independent from the LLM `tool_mode` and from model data-disclosure grants.

## Policy

A remote directory may be browsed only when its canonical path is the exact path, or a descendant, of a canonical data root explicitly approved in Settings for the live SSH identity.

- Only Settings can preview, approve, or revoke a data root.
- The model and project-edit tools cannot create, widen, migrate, or revoke roots.
- No approved roots means every new remote browse fails closed.
- Existing projects may still load, plan, confirm, and execute without browsing.
- Existing `remote_base_dir`, `remote_workdir`, and `samples.remote_data_dir` values are never promoted automatically.
- A normal tool confirmation card cannot authorize an out-of-root request.
- `tool_mode` governs LLM/rule access to the browse tool; it does not exempt any route from root authorization.
- A data-disclosure grant governs what scan details a provider may receive; it does not authorize the scan itself.

## Stored Model

The user-level connection store gains `approved_data_roots`. Each record has this shape:

```json
{
  "root_id": "root_<random>",
  "host": "hpc.example.org",
  "user": "analyst",
  "port": 22,
  "requested_path": "/srv/reads/current",
  "canonical_path": "/srv/storage/team/reads",
  "created_at": "2026-09-17T00:00:00Z",
  "revoked_at": null
}
```

The record contains no password, private key, key contents, API key, token, or credential-store ciphertext. A root is active only when `revoked_at` is null and host, user, and port exactly match the normalized live SSH identity.

`browse_policy_revision` is a SHA-256 digest over the normalized SSH identity and canonical JSON for the active root records. Unrelated connection and LLM settings do not affect it.

Legacy connection files without the collection load with an empty list. Invalid or corrupt root collections fail closed for browsing and are not silently repaired into broader permissions.

## Connection Store Consistency

Connection changes and root changes use one process-wide plus cross-process lock. Mutations perform a locked read, validate the expected revision when supplied, build a complete new object, write and flush a temporary file in the same directory, atomically replace the old file, and then release the lock.

Root approval and revocation require the caller's expected browse-policy revision. A stale Settings page receives a conflict and must reload. This prevents a stale write from restoring a revoked root. Ordinary connection edits preserve the root collection but cannot set it through extra fields.

## Settings Flow

Root approval is a two-step operation:

1. Preview receives an absolute requested path and the current SSH identity. It validates the path, resolves it remotely with `realpath -e --`, verifies it is a directory, and returns the requested and canonical paths plus the current policy revision. It does not persist anything.
2. Approve receives the exact preview result and expected policy revision. It re-resolves the requested path, requires the canonical path and live identity to match the preview, and atomically appends the grant.

Revoke receives a root id and expected policy revision, marks that root revoked, and writes a new policy revision. Editing the SSH identity makes old roots inactive; it never rebinds them.

The Settings page displays only roots for the current identity, including requested and canonical paths, status, and approval time. Root records belonging to another identity are not returned by the active-root API.

## Central Browse Service

Create `src/rnaseq_agent/remote_browse.py` as the only owner of remote discovery. The service exposes deterministic typed operations for path validation, policy snapshots, canonicalization, authorization, bounded scanning, pairing, and audit projection.

Every product route calls this service:

- structured workbench scan;
- project command `scan_remote_fastq`;
- deterministic/rule chat browse intent;
- LLM `browse_remote_samples` tool.

The web layer and tool executor no longer assemble their own `find` command or perform independent path checks.

For each browse request, the service:

1. Validates a normalized SSH identity and an absolute POSIX path with no control characters, NUL, relative components, or ambiguous form.
2. Loads a policy snapshot and requires at least one active root for that identity.
3. Re-resolves every active root's requested path. A canonical mismatch makes that root stale.
4. Resolves the requested target and requires a directory.
5. Selects the longest active canonical root containing the target by POSIX path components.
6. Re-reads the live policy immediately before scanning. Any identity, selected-root, or revision change returns `REMOTE_POLICY_CHANGED` without starting the scan.
7. Runs one bounded, fixed-template scan that rechecks the root and target, does not follow directory symlinks, and emits NUL-safe or JSON-safe records.
8. Canonicalizes each candidate and rejects any result outside both the target and the selected root.
9. Pairs FASTQ files only after containment filtering and returns bounded results plus authorization metadata.

Raw string prefix checks are forbidden. `/data/root2` is outside `/data/root`. POSIX comparison remains case-sensitive.

## Bounds And Transport

Canonicalization and scan commands use fixed templates owned by code. User paths enter only through validated, shell-quoted values. SSH destination identity is validated before credential lookup and transport creation.

The browse operation has:

- one monotonic wall-clock deadline;
- a combined stdout/stderr byte ceiling;
- a producer-side item limit;
- process-tree termination on timeout or overflow;
- a bounded number of returned sample rows and unmatched entries;
- an explicit `truncated` flag whenever the inventory may be incomplete.

Upload, download, scheduler submission, and long-running analysis retain their separate transport behavior. Browse-specific bounds do not become arbitrary timeouts for scientific jobs.

## Result Model

The local application may display the exact authorized scan result. The project sample draft stores only relative FASTQ basenames and the separately authorized canonical directory. A directory-aware grouping field preserves samples discovered in multiple directories without embedding absolute paths in every row.

The result includes a bounded authorization envelope:

```json
{
  "ok": true,
  "sample_count": 12,
  "unmatched_count": 1,
  "truncated": false,
  "authorization": {
    "root_id": "root_...",
    "canonical_target": "/srv/storage/team/reads/batch-1",
    "browse_policy_revision": "sha256:..."
  }
}
```

The LLM data-disclosure layer may replace exact paths and filenames with counts before provider dispatch. That projection occurs after browse authorization and does not change the locally visible result.

## Audit

Every attempt records a bounded, secret-free audit event with:

- project id and thread id when present;
- source route;
- normalized SSH identity digest;
- requested-path digest and canonical target when authorized;
- matched root id and policy revision;
- sample/unmatched counts and truncation;
- outcome and stable error code;
- start and completion timestamps.

Audit events never store credentials, command lines, raw stdout/stderr, or an unbounded file list. Authorization failures do not expose roots belonging to another SSH identity.

## Error Contract

All four entry paths use the same stable codes and return no fabricated success:

- `REMOTE_PATH_INVALID`
- `REMOTE_ROOT_NOT_APPROVED`
- `REMOTE_ROOT_STALE`
- `REMOTE_PATH_NOT_FOUND`
- `REMOTE_PATH_NOT_DIRECTORY`
- `REMOTE_PATH_ESCAPE`
- `REMOTE_POLICY_CHANGED`
- `REMOTE_SCAN_TIMEOUT`
- `REMOTE_SCAN_OUTPUT_LIMIT`
- `REMOTE_SCAN_INVALID_OUTPUT`
- `REMOTE_SCAN_FAILED`
- `REMOTE_ROOT_REVISION_CONFLICT`

An out-of-root request is a structured denial. It does not become a write/execute confirmation card. Error messages may direct the user to Settings but must not disclose inactive roots for another identity.

## Concurrency And Failure Semantics

- Preview has no durable authority and cannot be replayed after identity or policy changes.
- Approval and revocation are serialized and compare revisions under the lock.
- Browse uses a snapshot followed by an execute-time live recheck.
- Revocation or identity change observed before the bounded scan prevents the scan.
- A transport failure never causes fallback to an unbounded or unauthorised implementation.
- A truncated scan cannot be presented as a complete sample inventory or automatically frozen into a contract.
- If a candidate escapes during the remote-filesystem race window, the operation fails closed and does not return the partial candidate list as trusted.

A remote rename race cannot be eliminated fully without an installed helper using directory file descriptors and `openat`/`O_NOFOLLOW`. Re-resolution, non-following traversal, per-file containment, and one bounded helper process reduce this risk; it remains documented rather than treated as solved.

## Migration

- Existing installations start with zero approved roots.
- Existing connection values may be offered as suggestions in Settings, but approval still requires preview and explicit commit.
- Existing projects and frozen contracts remain valid when no browse is requested.
- The legacy scan endpoint remains temporarily but routes through the central service.
- An explicitly invalid project binding fails; it never falls back to shared connection browsing.

## Test Requirements

Automated coverage must prove:

1. No-root denial occurs before transport creation or scan execution.
2. Exact-root and true-descendant targets succeed; sibling-prefix and `..` paths fail.
3. Target symlinks escaping the root fail, and retargeted approved aliases are stale.
4. Overlapping roots select the longest match.
5. Shell metacharacters, whitespace, quotes, wildcard characters, newlines, and leading-dash components cannot alter the fixed command.
6. NUL-safe or JSON-safe framing cannot turn one filename into multiple records.
7. Timeout, output ceiling, producer limit, and process-tree cleanup are enforced.
8. Host/user/port validation and IPv4/IPv6 normalization remain intact.
9. Concurrent approve/revoke with a stale revision cannot restore removed authority.
10. Revocation or identity change between authorization and scan returns `REMOTE_POLICY_CHANGED` and does not execute the scan.
11. All four entry paths use the central service and identical error codes.
12. LLM/rule browse is blocked by `disabled`; other modes still require an approved root.
13. Structured browse ignores `tool_mode` but always requires an approved root.
14. `edit_connection` and model tools cannot mutate `approved_data_roots`.
15. Exact local results remain compatible with project sample drafts, while provider-bound summaries contain no unauthorized filenames or paths.
16. Existing projects can load, plan, confirm, and run without approved roots when they do not browse.

The detailed 51-case RED matrix in `.superpowers/sdd/testing_and_evaluation_roadmap/remote_root_audit.md` remains the release checklist.

## Acceptance Criteria

The feature is ready when every remote browse entry point uses `remote_browse.py`, Settings is the only authority for root lifecycle, connection mutations are atomic and serialized, canonical containment and execute-time policy revalidation are enforced, result and audit bounds are explicit, the 51-case matrix passes, and an independent review finds no path that scans without an active matching root.
