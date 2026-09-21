# LLM Disclosure ChatGraph Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the proven local `sample_ids` disclosure service to the conversational workbench without putting exact values into durable chat state, then expand disclosure scopes only through separately reviewed phases.

**Architecture:** Keep ordinary LLM tools and model-data grants on separate control paths. A dedicated disclosure intent creates a metadata-only grant and a disclosure card; approval claims the grant and performs one exact provider request through `ModelProviderGateway.dispatch_exact`. The exact response is request-local and may be rendered to the current user, but it is never appended to ChatState, History, checkpoints, generic tool logs, or model replay. Each later scope gets its own builder, grant field, collector limits, and fail-closed tests.

**Tech Stack:** Python 3.11+, FastAPI/TestClient, LangGraph, existing JSON grant store, provider gateway, workspace UI.

## Global Constraints

- Default provider context remains summary-only.
- `approved_root`, `tool_mode`, and `data_scope` remain independent permission axes.
- Ordinary tool confirmation cards never enlarge `data_scope`.
- Exact values and raw arguments never enter checkpoints, generic logs, History, grants, exceptions, or transcript projections.
- Exact requests are single-use, short-lived, revision-bound, tool-disabled, bounded, and never automatically retried.
- `remote_paths` and remote `source_ref` remain unsupported until a separate approved-root consumer review is complete.
- Every new behavior starts with a failing test and is verified with focused and full suites.

---

### Task 1: ChatGraph disclosure intent

**Files:**
- Modify: `src/rnaseq_agent/agent_tools.py`
- Modify: `src/rnaseq_agent/chat_graph.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Create/modify: `tests/test_chat_graph.py`, `tests/test_webapp_chat_tools.py`

- [ ] Define a dedicated `request_data_disclosure` intent that accepts only the currently supported field `sample_ids`, a bounded purpose, and no exact values or paths.
- [ ] Validate and normalize the intent before any card is created; unknown fields, paths, FASTQ names, and oversized purpose text fail closed.
- [ ] Create a separate disclosure card with field category, record count, provider identity/revision summary, tool mode, expiry, and purpose category only.
- [ ] Preserve assistant tool-call protocol replies for approved, rejected, invalid, and cancelled disclosure calls.
- [ ] Add tests proving ordinary cards and disclosure cards cannot be substituted for one another.

### Task 2: Request-local exact response rendering

**Files:**
- Modify: `src/rnaseq_agent/model_exact_service.py`
- Modify: `src/rnaseq_agent/chat_graph.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Create/modify: `tests/test_model_exact_service.py`, `tests/test_webapp_chat_tools.py`

- [ ] Route the approved disclosure card to `claim_grant_for_send` and `dispatch_exact` without using the ordinary tool executor.
- [ ] Buffer the bounded exact response before exposing it; reject tool calls, secret detections, oversized frames, and ambiguous transport.
- [ ] Render the exact response only in the current request/UI event; store a metadata result and hash, never the response body.
- [ ] Prove that ChatState, SQLite checkpoint, History, thread messages, tool log, grant JSON, and exception text do not contain exact response values.
- [ ] Add cross-project, cross-thread, provider-change, tool-mode-change, revision-change, expiry, duplicate-claim, and crash-after-claim tests.

### Task 3: UI disclosure card and History projection

**Files:**
- Modify: `src/rnaseq_agent/webtemplates/`, `src/rnaseq_agent/webstatic/`
- Modify: `src/rnaseq_agent/webapp.py`
- Create/modify: UI/API tests

- [ ] Add a disclosure card distinct from ordinary tool confirmation cards.
- [ ] Show only field category, count, provider identity summary, revision summary, expiry, and a bounded purpose category.
- [ ] Show exact response in a transient panel with no automatic transcript save; provide an explicit user action if it should be copied into a user-authored message.
- [ ] Display grant terminal outcome as success, failed, ambiguous, rejected, or expired without exposing exact values in History.

### Task 4: Local `fastq_filenames` and `report_excerpt` scopes

**Files:**
- Modify: `src/rnaseq_agent/model_context.py`, `src/rnaseq_agent/model_exact_service.py`
- Modify: `src/rnaseq_agent/model_data_grants.py`, `src/rnaseq_agent/model_disclosure.py`
- Create/modify: focused scope tests

- [ ] Write a separate scope specification before enabling either field.
- [ ] Give each field its own bounded extractor and manifest; do not infer one grant from another.
- [ ] For report excerpts, allow only a bounded, redacted excerpt and reject stdout, stderr, traceback, full report, and arbitrary file reads.
- [ ] Re-run secret, path, replay, and provider-tool-call gates for each scope.

### Task 5: Approved-root remote exact

**Files:**
- Modify only after separate review: `src/rnaseq_agent/model_data_grants.py`, `src/rnaseq_agent/remote_scan_store.py`, `src/rnaseq_agent/model_context.py`
- Create/modify: remote exact integration tests

- [ ] Require a whole-scan `source_ref`, non-null LLM thread, fixed connection → policy/scan → project/grant lock order, and live identity/root/policy/result revision checks.
- [ ] Reject workbench-only scans, client-selected subgroups, stale or revoked roots, partial scans, and any path not covered by the authoritative source reference.
- [ ] Keep remote exact closed until the complete concurrency/replay matrix passes.

## Release gates

Each task must pass focused tests, full project pytest, compileall, diff-check, bkbio-eval, and the applicable mutation/refusal checks. The release notes must state exactly which scope is enabled and which remain structurally unsupported.
