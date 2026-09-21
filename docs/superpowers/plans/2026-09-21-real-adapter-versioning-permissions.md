# Real Adapter, Protocol Versioning, and Permission Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Advance the project from a stable expected-refusal release to an auditable real Adapter and versioned evaluator while keeping the LLM disclosure boundary fail-closed.

**Architecture:** Keep the main product, the independent `bkbio-eval` repository, and provider-facing disclosure projections separate. Add the named paired capability through validated server-generated descriptors, add evaluator protocol dispatch/provenance without changing result semantics, and harden direct permission helpers before any new exact scope is enabled. Airway remains an expected refusal until an immutable R/DESeq2 runtime and reviewed baseline exist.

**Tech Stack:** Python 3.11+, pytest, FastAPI/TestClient, existing JSON/SQLite stores, LangGraph checkpoints, the external `bkbio-eval` repository, and a pinned R 4.3.3/Bioconductor 3.18 runtime when available.

## Global Constraints

- Work on `wizard`; do not merge `master` during this phase.
- Default provider context remains de-identified `summary`.
- `approved_root`, `tool_mode`, and model `data_scope` remain independent axes.
- Only local `sample_ids` exact scope is enabled; it is one-time, request-local, project/thread/provider/revision bound, and tool-disabled.
- `fastq_filenames`, `report_excerpt`, `remote_paths`, and remote exact/source references remain rejected at issue, load, claim, and send boundaries.
- `paired_two_group` is the only paired route and always generates `~ pair_id + condition`; arbitrary formulas and silent downgrade to `~ condition` are forbidden.
- Airway and TCGA remain `NOT_EVALUABLE / unsupported_design` until capability, provenance, licence, runtime, and expected-value gates all pass.
- Every production change starts with a failing test, records the RED command, and ends with focused and full verification.
- Do not modify or delete external evaluator `.tmp-l1-report.json`.

## Task 1: Version the external evaluator protocol

**Files:** external `bkbio-eval/src/bkbio_eval/{protocol.py,case.py,runner.py,cli.py,__init__.py}`, tests, `docs/ADAPTER.md`, `README.md`, `HANDOFF.md`.

- [ ] Add RED migration tests for legacy/explicit case schema v1, invalid/unknown case versions, known and unknown analyzer result schemas, and fail-before-check behavior.
- [ ] Run the focused tests and record the expected failures.
- [ ] Add centralized protocol constants and strict case/result dispatch; missing legacy case version normalizes to 1, all unknown versions fail closed.
- [ ] Add baseline manifest hashing for inputs, expected files, reference result, evaluator commit/version, runtime/script identity, and explicit confirmation state; preserve `.tmp-l1-report.json`.
- [ ] Add additive suite-report protocol provenance while preserving `suite-report@1` fields.
- [ ] Run evaluator focused tests, full pytest, mutation unit/L0, CLI list/doctor, and the real Adapter refusal gate; commit the external repository changes.

## Task 2: Implement the named paired capability in the main project

**Files:** main project design/sample validation and contract modules identified by the existing differential-design helpers; paired capability tests and permission projection tests; capability documentation.

- [ ] Add RED tests for valid three-pair canonicalization/rendering and each refusal: empty/missing pair id, duplicate pair-condition, missing mate, extra row, fewer than three pairs, invalid levels, arbitrary formula, declared batch, non-zero prefilter, and rank deficiency.
- [ ] Run the focused tests to prove the current implementation fails for the intended reasons.
- [ ] Implement a server-generated canonical descriptor with fixed `~ pair_id + condition`, deterministic reference/contrast order, `min_count_prefilter=0`, and no arbitrary formula input.
- [ ] Include template, mapping, levels, and prefilter in contract fingerprints; reject before contract creation or transport.
- [ ] Project only pairing presence/count/gate status to model context; pair IDs and mappings stay local and out of History, checkpoints, generic logs, and current exact scopes.
- [ ] Run focused paired, contract, disclosure, and full main-project tests; commit.

## Task 3: Route the real Adapter through paired preflight

**Files:** `src/rnaseq_agent/bkbio_eval_adapter.py`, adapter tests, evaluator case metadata only where the paired contract requires it.

- [ ] Add RED tests proving the Adapter preserves the declared source pair column long enough to canonicalize it, refuses malformed pairing before project creation/SSH/transport, and never downgrades to an independent design.
- [ ] Run the tests and capture the pre-change failures.
- [ ] Add `paired_two_group` parameter validation and strict refusal output using `bkbio-eval/analyzer-result@1`; keep current airway/TCGA expected-refusal manifests unchanged.
- [ ] Add deterministic provenance fields needed for a future airway numerical baseline without fabricating expected values or claiming R execution.
- [ ] Run adapter focused tests and real L1 refusal integration; commit.

## Task 4: Harden LLM permission defaults and confirmation projections

**Files:** `src/rnaseq_agent/agent_tools.py`, `chat_graph.py`, `webapp.py`, disclosure/grant modules, and focused tests/docs.

- [ ] Add RED parameterized tests showing `normalize_tool_mode(None)` is disabled for explicit null/corrupt values while genuinely missing legacy fields still normalize to the documented compatibility mode.
- [ ] Add RED tests for strict `approved is True`, assistant tool-call coverage on approve/reject/validation-error paths, and confirmation cards using effective global connection values.
- [ ] Run the tests before implementation and preserve the failure evidence.
- [ ] Implement fail-closed normalization without collapsing missing-field compatibility, strict resume approval, complete tool-message replies, and read-only effective-config merging.
- [ ] Keep connection edits `solo`, configuration edits `batch`, execution `solo`; do not widen provider data scopes or enable remote exact.
- [ ] Add endpoint coverage for `set_diffexp_reference`, `set_cms_options`, `browse_remote_samples`, `rollback_changes`, and `record_qc_decision` as applicable.
- [ ] Run focused tool-loop/disclosure tests and the full main-project suite; commit.

## Task 5: Freeze R/DESeq2 environment and airway promotion gate

**Files:** `docs/superpowers/specs/2026-09-21-paired-airway-r-deseq2-design.md`, audit records under `.superpowers/night-audit/`, evaluator case metadata only after evidence exists.

- [ ] Record the current blocker evidence: no R/Rscript, no Docker daemon, no Conda/Apptainer runtime, and no immutable R/Bioc digest.
- [ ] Add a machine-readable checklist for source URL/retrieval, source and derived hashes, exporter commit, licence/re-distribution decision, batch/confounding review, and expected-value confirmation.
- [ ] If and only if a pinned runtime becomes available, regenerate expected values with fixed R/DESeq2, manually review them, and run a fresh numerical L1. Otherwise keep the case as expected refusal and report the exact missing evidence.

## Final verification

- [ ] Main project: `\.venv\Scripts\python.exe -m pytest -q`, `compileall`, `git diff --check`, and permission/disclosure focused suites.
- [ ] External evaluator: full pytest, protocol/refusal focused suites, `scripts/mutation_check.py --level unit,L0`, CLI list/doctor, and real Adapter L1.
- [ ] Confirm the gate reports `0 numerical passed / 2 expected_refusal_passed / 0 failed / 0 skipped` unless a reviewed positive L1 has actually been promoted.
- [ ] Record main/evaluator commits, protocol versions, input hashes, Analysis Contract IDs, runtime digest status, and artifact hashes in the final audit note.
