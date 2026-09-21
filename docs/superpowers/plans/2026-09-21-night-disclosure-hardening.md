# Night Disclosure Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the released summary-only plus local `sample_ids` disclosure boundary, close the highest-risk replay and leakage test gaps, and document the next scopes without enabling them.

**Architecture:** Keep `approved_root`, `tool_mode`, and `data_scope` independent. The current provider data scope remains summary by default, with only a project/thread/provider-bound, single-use local `sample_ids` grant enabled. Remote exact data, `fastq_filenames`, `report_excerpt`, and `remote_paths` stay structurally unsupported. Changes are fail-closed and are verified through focused tests, full suites, and evaluator gates.

**Tech Stack:** Python 3.11+, FastAPI/TestClient, LangGraph, pytest, existing JSON/SQLite stores, remote scan service, bkbio-eval.

## Global Constraints

- Do not merge `master` or change the branch from `wizard`.
- Do not enable `fastq_filenames`, `report_excerpt`, `remote_paths`, or remote `source_ref` disclosure.
- Unsupported disclosure fields must be rejected at issue, load, claim, and send boundaries.
- Exact values and raw arguments never enter checkpoints, generic logs, History, grants, exceptions, or application-generated transcript projections.
- Only a literal JSON boolean `true` approves a grant or confirmation.
- Exact requests are single-use, bounded, tool-disabled, and never automatically retried.
- New behavior starts with a failing test; every task ends with focused verification and a commit.

---

### Task 1: Fail-closed local disclosure boundary

**Files:**
- Modify: `src/rnaseq_agent/model_data_grants.py`, `src/rnaseq_agent/model_exact_service.py`, `src/rnaseq_agent/model_provider.py`, `src/rnaseq_agent/webapp.py`, and only the smallest related module required by the tests.
- Test: existing grant/exact/web tests plus a focused regression file if needed.

**Deliverable:** The currently enabled scope is exactly local `sample_ids`. Unsupported fields are rejected before durable pending grants are created, project identity must match the stored project, exact dispatch rejects Codex CLI at the gateway invariant, purpose/approval binding cannot be widened at send time, and transient exact HTTP responses are explicitly non-cacheable.

- [x] Add failing direct-function and endpoint tests for unsupported fields at issue/load/claim/send, missing or mismatched stored project id, purpose drift, and exact dispatch through a Codex backend.
- [x] Add the smallest fail-closed implementation that preserves the existing local `sample_ids` API and terminal semantics.
- [x] Add/verify `Cache-Control: no-store, private` and `Pragma: no-cache` on transient exact responses without persisting the body.
- [x] Run focused disclosure/grant/provider/web tests, then commit (`3971399`, `7154287`).

### Task 2: Approved-root audit and remote-scan replay gates

**Files:**
- Modify only remote scan/audit modules if a failing regression proves a code defect.
- Test: `tests/test_security_audit.py`, remote root/scan tests, and a focused concurrency test module.

**Deliverable:** The authoritative security-audit lifecycle, root stale/symlink/nested-root/policy-race failures, and concurrent scan consumption are covered. Any unresolved audit or replay state remains fail-closed and does not fabricate a trusted result.

- [x] Add failing tests for audit success, explicit audit failure, uncertain audit commit, History failure after audit commit, and removal of security-audit payload before provider/log projection.
- [x] Add failing tests for canonical root drift, symlink escape, nested-root longest match, policy change during scan, and no scan record on denied browse.
- [x] Add a two-consumer test proving one callback executes, the other receives a stable consumed/reference-used result, and failed callback retry reuses the durable claim.
- [x] Implement only defects exposed by these tests, run the focused remote/audit suite, then commit (`869b15e`, `008f22b`).

### Task 3: Disclosure concurrency and sentinel containment

**Files:**
- Test: create a focused disclosure concurrency/sentinel test module and extend existing provider/context tests where the existing fixtures are authoritative.
- Modify source only when a reproducible failing test identifies a narrow fail-closed defect.

**Deliverable:** Concurrent sends, binding changes during send, malformed provider responses, and sentinel secrets are proven not to leak exact data or bypass the single-use grant.

- [x] Add a concurrent `/send` test proving exactly one provider dispatch and a stable consumed result for the loser.
- [x] Add cross-thread/provider/tool-mode/revision races and pre-transport versus post-transport terminal-state assertions.
- [x] Send sample, path, password, API-key, private-key, ciphertext, and URL-userinfo sentinels through default, ordinary-tool, exact, and provider-error paths; assert absence from payloads, responses, checkpoints, History, logs, grants, and exceptions (`8331fd0` closes the ordinary-tool projection leak and adds the missing successful/default matrix).
- [x] Verify unsupported `fastq_filenames`, `report_excerpt`, and `remote_paths` remain structurally rejected, then commit the narrow fix and tests (`d06a66c`).

### Task 4: Scope documents and release ledger

**Files:**
- Create: `docs/superpowers/specs/2026-09-21-local-fastq-filenames-scope.md`
- Create: `docs/superpowers/specs/2026-09-21-report-excerpt-scope.md`
- Modify: `docs/superpowers/plans/2026-09-21-llm-disclosure-release-roadmap.md`, `docs/llm_tool_permission_model.md`, `.superpowers/sdd/2026-09-21-disclosure-chatgraph-next/progress.md`

**Deliverable:** The next permission boundaries are explicit before implementation. `fastq_filenames` distinguishes local rows from remote-scan provenance and has a RED matrix; `report_excerpt` defines source allowlists and redaction requirements; current release status and test evidence are recorded without claiming unsupported scopes are live.

- [x] Write the local FASTQ filename scope, provenance rules, bounded output, and negative matrix; keep provider enablement off.
- [x] Write the report excerpt scope as a separate proposal with section allowlists, redaction, size limits, and explicit rejection of logs/tracebacks/full reports.
- [x] Update the roadmap and SDD ledger with current commits and fresh verification counts; leave later scopes closed.
- [x] Run documentation checks, `git diff --check`, and the final full gates before committing (`7e96a74`).

## Release gates

The task is complete only after fresh evidence for the focused suites, full main-project pytest, compileall, diff-check, external bkbio-eval, expected-refusal tests, and unit/L0 mutation checks. All skipped cases and environment limitations must be reported separately; they are not successes.
