# Night Run Hardening Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the approved remote-root release gate, then implement the first safe slice of model data disclosure while preserving a summary-only default and fail-closed execution boundaries.

**Architecture:** The application keeps three independent permission axes: Settings-owned approved remote roots decide where remote discovery may occur; live `tool_mode` decides whether a tool may be invoked; a separate model-data grant decides which project fields may cross the provider boundary. Local UI may show an authorized exact scan, while provider calls use a de-identified projection unless a short-lived single-use grant is claimed.

**Tech Stack:** Python 3.11+, FastAPI/TestClient, pytest, existing JSON stores and project locks, LangGraph checkpoints, requests/urllib/Codex CLI adapters.

## Global Constraints

- Work on branch `wizard`; do not merge `master` during the run.
- Do not restore direct remote `find` scanning or weaken approved-root checks to satisfy legacy tests.
- Default provider scope is `summary`; endpoint locality never widens data scope.
- `approved_root`, `tool_mode`, and model `data_scope` are independent checks.
- Exact disclosure is single-use, short-lived, and bound to project, thread, provider identity, tool mode, data revisions, and remote scan/result/policy revisions where applicable.
- Exact values and raw tool arguments never enter checkpoints, generic logs, History, grants, exceptions, or application-generated transcript projections.
- Every provider transport must converge on `ModelProviderGateway`; exact requests disable tools and reject provider tool calls.
- Use TDD for new behavior and fresh verification before any completion claim.

## Task 0: Close the approved-root implementation

**Files:** `src/rnaseq_agent/remote_scan_store.py`, `src/rnaseq_agent/webapp.py`, `tests/test_remote_scan_store.py`, review docs and progress ledger.

- [ ] Commit the already reviewed `f9104e5` follow-up that catches non-`OSError` History projection failures without undoing a consumed scan.
- [ ] Keep legacy revision records fail-closed; document that they require a fresh scan because the old formula cannot authenticate expiry and target fields.
- [ ] Run focused Task 7 tests and the scoped independent review.
- [ ] Update `.superpowers/sdd/2026-09-17-approved-remote-data-roots/progress.md` only after the review confirms Task 6 and Task 7 status.

## Task 1: Update compatibility tests and release documentation

**Files:** `tests/test_webapp_project_wizard.py`, `tests/test_webapp_workbench.py`, `tests/test_validation.py`, `docs/llm_tool_permission_model.md`.

- [ ] Change obsolete tests that expect unapproved remote browsing to assert the stable `REMOTE_ROOT_NOT_APPROVED`/`REMOTE_PATH_INVALID` contract.
- [ ] Rewrite successful command-scan coverage to create a temporary approved root and use the central browse service; do not monkeypatch `_scan_remote_samples` as a production-path proof.
- [ ] Add/retain rootless-project, counts/expression, and frozen-remote compatibility cases.
- [ ] Document the three independent permission axes, four-channel result contract, workbench-only apply, durable claim/receipt recovery, and whole-scan `source_ref` semantics.

## Task 2: Run the approved-root release gate

- [ ] Run the full main-project pytest suite, protected-DACL checks, `compileall`, and `git diff --check`.
- [ ] Run the external `bkbio-eval` suite, unit/L0 mutation checks, and L1 refusal listing.
- [ ] Run the final authorization-bypass search for direct scanners, automatic root migration, and root mutation outside Settings.
- [ ] Record exact commands and counts in the Task 8 ledger; do not treat focused green as release green.

## Task 3: Implement LLM disclosure Task 1

**Files:** `src/rnaseq_agent/model_disclosure.py`, `src/rnaseq_agent/model_provider.py`, `src/rnaseq_agent/model_context.py`, `tests/test_model_context.py`, `tests/test_connection_store.py`.

- [ ] Add RED tests for normalized provider identity, secret-independent revisions, project/sample/report revisions, and provider configuration revisions.
- [ ] Implement the shared disclosure dataclasses, stable error codes, canonical revisions, and provider normalization.
- [ ] Ensure provider identity excludes credentials while configuration revision changes when the normalized API path/model/mode changes.
- [ ] Run the focused Task 1 tests, then the existing connection-store tests.

## Task 4: Implement summary-only context and provider boundary

**Files:** `src/rnaseq_agent/model_context.py`, `src/rnaseq_agent/model_provider.py`, existing chat transport modules, new focused tests.

- [ ] Add tests proving sample IDs, FASTQ names, remote paths, secrets, and report excerpts are absent from default provider payloads.
- [ ] Build explicit allowlist projections for project status, sample counts, route, design readiness, and bounded report metadata.
- [ ] Route every generative HTTP/Responses/Codex request through one gateway and add a last-mile serialized-body secret scanner.
- [ ] Fail closed on missing provider configuration, secret detection, unsafe provider responses, and exact-tool calls.

## Task 5: Implement exact disclosure grants

**Files:** grant store, claim/send path, web endpoints, exact-scope tests.

- [ ] Add metadata-only grant records with project/thread/provider/tool-mode/revision bindings and a 600-second expiry.
- [ ] Add durable single-use claim and terminal outcomes: `consumed_success`, `consumed_failed`, or `consumed_ambiguous`.
- [ ] Revalidate approved-root policy and remote scan/result revisions under the documented lock order before preparing exact data.
- [ ] Keep exact values request-local; do not persist them in ChatState, checkpoints, transcripts, logs, grants, or errors.

## Task 6: Harden exact response collection and legacy replay

- [ ] Add bounded collectors for chat-completions SSE, Responses SSE, and Codex CLI output with common event/byte/tool-call limits.
- [ ] Reject exact responses containing tool calls before content or arguments reach state or executors.
- [ ] Filter old checkpoints/transcripts into safe projections before provider replay while preserving user-authored messages unchanged.

## Task 7: Acceptance matrix and overnight stopping rules

- [ ] Run sentinel tests for sample IDs, FASTQ names, remote paths, report fragments, passwords, API keys, private keys, ciphertext, and URL userinfo.
- [ ] Run concurrency/replay tests for duplicate approvals, cross-thread grants, provider changes, root revocation, crash after claim, and ambiguous transport completion.
- [ ] If a new security failure appears, stop expanding scope, add a reproducer, and leave the failing gate visible in the ledger.
- [ ] Before declaring completion, verify the exact command output and commit only reviewed, test-backed changes.

