# Evaluation and LLM Permission Next Phase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the project from a hardened summary-only release to an auditable numerical evaluation program without widening LLM data access or claiming unsupported biological capability.

**Architecture:** Keep the product control plane, the independent `bkbio-eval` evaluator, and the provider disclosure boundary separate. First make dataset metadata and capability refusals auditable, then add a real independent-sample L1, then add a named `paired_two_group` capability. Each exact LLM data field remains an independent scope with its own issue/load/claim/send and persistence tests; remote exact is last.

**Tech Stack:** Python 3.11+, pytest, FastAPI/TestClient, the existing JSON/SQLite stores, LangGraph checkpoints, fixed R/DESeq2 runtime for numerical L1, and the external `bkbio-eval` repository.

## Global Constraints

- Work on the `wizard` branch; do not merge `master` during this phase.
- Default provider scope is de-identified `summary`.
- `approved_root`, `tool_mode`, and model `data_scope` remain independent permission axes.
- The only enabled exact scope is local `sample_ids`; it is single-use, short-lived, project/thread/provider/tool-mode/revision bound, request-local, and tool-disabled.
- `fastq_filenames`, `report_excerpt`, `remote_paths`, and remote `source_ref` remain rejected at issue, load, claim, and send boundaries.
- Exact values, raw arguments, secrets, and full reports never enter checkpoints, History, generic logs, grants, exceptions, or application-generated transcript projections.
- A numerical L1 is not accepted until its source, license, retrieval, hashes, export procedure, biological units, condition balance, and batch audit are recorded.
- A paired design is exposed only through a named `paired_two_group` template that generates `~ pair_id + condition`; arbitrary formulas remain unsupported.
- Every production behavior change starts with a failing test and ends with fresh full-suite evidence.

## Current decision record

The current release is stable enough to stop expanding scope:

- Main project: `1122 passed, 8 skipped, 1 warning, 6 subtests passed` after disclosure hardening commit `7dab67e`.
- External `bkbio-eval`: `154 passed, 1 skipped` after metadata-audit contract commit `1daf024`.
- L1 real adapter run: `0 numerical passed / 2 expected_refusal_passed / 0 failed / 0 skipped`.
- Unit/L0 mutation command exits `0`; its caught mutants demonstrate test sensitivity and do not constitute numerical L1 completion.
- Remote audit/replay review: `217 passed, 5 skipped, 1 warning`; no new bypass found.

The existing TCGA and airway cases remain expected refusals. The new metadata-audit contract records their input hashes, unit structure, export scripts, and inclusion rules, while license/redistribution terms, retrieval metadata, source archive hashes, script commits, and explicit batch conclusions remain blocked/null. Their derived inputs must not be described as untouched raw source matrices.

## Task 1: Freeze the L1 metadata-audit contract

**Files:**

- Modify: external `bkbio-eval/docs/L1_METADATA_AUDIT.md`, `HANDOFF.md`, and `README.md`.
- Test: external metadata-audit tests covering every `L1_*` case.

**Requirements:**

- Require accession or dataset identifier, source URL, retrieval timestamp, source archive hash, derived `counts.tsv`/`coldata.tsv`/annotation hashes, export script and commit, license/redistribution URL and decision, biological-unit uniqueness, per-condition counts, batch field or explicit “not available” decision, confounding assessment, and inclusion/exclusion rules.
- Distinguish `expected_refusal` metadata from positive numerical L1 metadata; refusal cases may document why a design is unsupported but cannot satisfy the positive-L1 gate.
- Fail the audit on missing or contradictory metadata. Do not relax numerical thresholds to make an audit pass.
- Record TCGA and airway as blocked until the missing provenance and licensing decisions are supplied.

## Task 2: Select and validate one independent-sample positive L1

**Files:**

- Create or modify one external `cases/L1_*` manifest, input metadata, export script, and audit report only after Task 1 passes.
- Test: metadata audit, counts/coldata alignment, at least three biological replicates per condition, condition/batch confounding, input hash stability, and real adapter execution.

**Requirements:**

- Freeze `~ condition`, `paired=false`, and `min_count_prefilter=0` for the first positive case.
- Use a fixed DESeq2 environment with a non-empty container or runtime digest.
- Verify the adapter’s output structure, threshold consistency, reference/contrast reversal property, and reproducibility from the recorded input hashes.
- If no candidate satisfies license or provenance review, stop with a documented blocked candidate list; do not add a synthetic or repackaged case and call it independent.

## Task 3: Design and implement the named paired template

**Files:**

- Spec: external evaluator design document and main-project capability contract documentation.
- Modify only after the spec is approved: sample schema, design gate, contract fingerprinting, adapter parameter validation, and UI/LLM tool schema.
- Test: RED tests for missing/duplicate pair rows, pair-condition imbalance, empty `pair_id`, rank deficiency, contract drift, and refusal-before-transport.

**Requirements:**

- Accept a canonical `pair_id` column; every pair has exactly two rows, one row per condition.
- Generate `~ pair_id + condition` in code. Reject arbitrary formulas and silent downgrades to `~ condition`.
- Include template, pair mapping, reference/contrast, and prefilter semantics in the Analysis Contract fingerprint.
- Re-audit TCGA and airway before promoting either case from expected refusal to numerical L1.

## Task 4: Harden consent UX without widening data scope

**Files:**

- Modify disclosure card/API/UI modules and their focused tests.
- Update `docs/llm_tool_permission_model.md` and the release roadmap.

**Requirements:**

- Distinguish human-initiated structured disclosure UI from model-initiated exact sends in the `tool_mode` documentation and tests.
- Constrain disclosure purpose to a safe category allowlist and show that category on the card; never display exact values before approval.
- State on the card that the grant is one-time, request-local, tools are disabled, the result is not written to History/chat/checkpoints/logs, and failure or ambiguous transport requires a new request rather than automatic retry.
- Treat free-form condition labels as untrusted metadata. Add sentinel tests first; if labels can carry identifiers, project only safe group counts or opaque ordinal labels in default context. Do not silently hash labels in a way that makes the analysis unusable without documenting the trade-off.

## Task 5: Evaluate the next exact scopes independently

**Files:**

- Keep the existing proposals as separate specs: local FASTQ basename and bounded report excerpt.
- Add independent RED matrices and implementation plans only after Task 4 is stable.

**Requirements:**

- Local FASTQ exact may include only project-local, non-remote-origin basenames; any remote provenance or `source_ref` rejects.
- Report exact may include only allowlisted sections after redaction and size limits; full reports, logs, stderr, stdout, tracebacks, credentials, and paths reject.
- Each scope gets its own issue/load/claim/send, concurrency, replay, secret-scanner, persistence, and provider-tool-call tests. A `sample_ids` grant never upgrades another scope.

## Task 6: Keep remote exact last

**Files:**

- No implementation until Tasks 1–5 are complete and reviewed.
- Later spec must cover approved-root canonicalization, complete scan provenance, connection identity, policy/root/result revisions, whole-scan `source_ref`, claim ordering, and crash ambiguity.

**Stopping rule:** Any uncertainty about remote identity, scan completeness, or transport outcome fails closed and leaves the scope disabled.

## Verification matrix

Run after each task as applicable:

```powershell
# Main project
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
git diff --check

# External evaluator
Set-Location C:\Users\Administrator\WorkBuddy\2026-09-15-15-27-02\bkbio-eval
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m bkbio_eval.cli list --select L1
.\.venv\Scripts\python.exe scripts\mutation_check.py --level unit,L0
```

The L1 release command must use the real adapter and report expected refusals separately from numerical passes. A skipped expected-refusal case is a failed suite gate. No task may claim a positive L1, a new exact scope, or remote exact readiness without fresh evidence for its own acceptance matrix.
