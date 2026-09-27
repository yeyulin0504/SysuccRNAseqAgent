# RNA-seq SSH Workflow Usability Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the configured-SSH RNA-seq path usable from remote FASTQ scan through plan, confirmation, execution, result validation, History, and report.

**Architecture:** Preserve the existing remote browse security boundary and ProjectSession/ LangGraph state model. Add only the missing translation and state propagation at the web/API boundaries, then make execution status reflect durable task and artifact state. Counts remains a separate validated entry path.

**Tech Stack:** Python, FastAPI/Starlette, LangGraph, SQLite checkpointer, pytest, SSH transport.

## Global Constraints

- Do not guess condition, cancer type, or pair identity from FASTQ names.
- Remote scanning is read-only and limited to the configured approved SSH root.
- Only a confirmed Analysis Contract may execute.
- queued/running tasks must never be reported as result missing.
- Counts normalized/GEO matrices must remain ineligible for DESeq2.
- Existing LLM data-scope and tool-mode fail-closed rules must remain unchanged.

---

### Task 1: Reproduce and document current web/API boundaries

**Files:**
- Inspect: `src/rnaseq_agent/webapp.py`, `src/rnaseq_agent/remote_browse.py`, `src/rnaseq_agent/remote_scan_store.py`, `src/rnaseq_agent/session.py`
- Test: `tests/test_webapp_workbench.py`, `tests/test_webapp_project_wizard.py`, `tests/test_remote_browse.py`, `tests/test_remote_scan_store.py`

- [ ] Run the existing remote/web focused tests and inspect endpoint routes for scan, apply, plan, confirm, run, status, and report.
- [ ] Record the first failing or missing transition as a test name and response payload before editing code.
- [ ] Commit only diagnostic tests if a reproducible gap is found.

### Task 2: Make remote scan results enter the project draft

**Files:**
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `src/rnaseq_agent/remote_scan_store.py` only if the existing apply API cannot expose a stable project-bound result
- Test: `tests/test_webapp_workbench.py`, `tests/test_remote_scan_store.py`

**Interfaces:**
- Consume the stored scan reference and project id.
- Produce a project draft whose `samples.items` contain `sample_id`, `fastq_1`, and `fastq_2`, plus a scan provenance reference; never add guessed conditions.

- [ ] Add a failing API test that scans a fake SSH result, applies one selected group to a project, reloads `session.json`, and asserts the FASTQ sample rows are present.
- [ ] Add a failing test for expired, cross-project, or already-consumed scan references returning a structured error.
- [ ] Implement the smallest project-bound apply path using the existing claim/consume store methods.
- [ ] Run the focused tests and verify the applied draft remains unconfirmed.
- [ ] Commit: `feat: connect remote scan results to project drafts`.

### Task 3: Make batch sample grouping and plan preview consistent

**Files:**
- Modify: `src/rnaseq_agent/webapp.py`, `src/rnaseq_agent/session.py`, `src/rnaseq_agent/webchat.py`
- Test: `tests/test_webapp_project_wizard.py`, `tests/test_webapp_workbench.py`, `tests/test_session.py`, `tests/test_webchat.py`

- [ ] Add failing tests for saving a batch condition map and then generating a plan whose sample count, groups, and enabled stages match the saved configuration.
- [ ] Reject missing or duplicate sample ids with a field-level response; do not silently drop rows.
- [ ] Implement one normalization function for sample rows used by API, session, and chat edit paths.
- [ ] Ensure plan generation reruns Gate-A after grouping edits and invalidates a stale plan.
- [ ] Run focused tests and commit: `fix: synchronize sample grouping with plan state`.

### Task 4: Correct remote execution status and result validation

**Files:**
- Modify: `src/rnaseq_agent/session.py`, `src/rnaseq_agent/agent_graph.py`, `src/rnaseq_agent/webapp.py`
- Test: `tests/test_agent_graph.py`, `tests/test_session.py`, `tests/test_webapp_workbench.py`

- [ ] Add failing tests for queued and running outcomes staying non-terminal, and for completed requiring validated artifacts.
- [ ] Add a test that a failed remote job exposes its scheduler/error message without being converted to result missing.
- [ ] Implement explicit status mapping: queued, running, completed, failed, result_missing, unavailable.
- [ ] Ensure `wait=True` is used for the terminal execution path or that polling continues until a terminal state before validation.
- [ ] Bind result artifacts to contract/attempt/stage before marking completed.
- [ ] Run focused graph/session/web tests and commit: `fix: report remote execution states accurately`.

### Task 5: Make History and report read the same durable state

**Files:**
- Modify: `src/rnaseq_agent/webapp.py`, templates under `src/rnaseq_agent/templates/`
- Test: `tests/test_webapp_workbench.py`, `tests/test_webapp.py`

- [ ] Add failing tests asserting History includes scan, sample confirmation, contract, run, artifact, and report entries for one project.
- [ ] Add failing tests for failed and result-missing states with human-readable reasons.
- [ ] Implement a single project snapshot/history projection reused by workbench and report endpoints.
- [ ] Ensure refresh/reopen reads the durable SQLite/session state rather than in-memory graph bookkeeping only.
- [ ] Run focused UI/API tests and commit: `fix: unify workbench history and report state`.

### Task 6: Verify counts/DESeq2 entry remains safe and usable

**Files:**
- Inspect/modify only as required: `src/rnaseq_agent/sample_detection.py`, `src/rnaseq_agent/adapter.py`, `src/rnaseq_agent/webapp.py`
- Test: `tests/test_webapp_counts_entry.py`, `tests/test_sample_detection.py`, `tests/test_bkbio_eval_adapter.py`

- [ ] Add or update tests for raw integer counts becoming eligible and normalized/GEO-like matrices being rejected for DESeq2.
- [ ] Verify batch sample assignment reaches the same plan/confirm path as remote FASTQ projects.
- [ ] Ensure reports explain why a matrix is or is not eligible without exposing raw data to the model provider.
- [ ] Run focused counts and adapter tests and commit: `test: verify counts route through unified project state`.

### Task 7: Full regression and handoff

**Files:**
- Test: all existing tests
- Docs: `docs/superpowers/specs/2026-09-28-rnaseq-ssh-workflow-usability-design.md`

- [ ] Run remote/web/session/graph/counts focused suites.
- [ ] Run the complete main-project test suite and record exact pass/skip/fail counts.
- [ ] Run `compileall` and `git diff --check`.
- [ ] Review permissions and confirm no new exact-data disclosure path was introduced.
- [ ] Commit any final documentation and report the remaining environment-only blockers separately.
