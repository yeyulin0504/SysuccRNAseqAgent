# Downstream Container and Chat Sample Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a reproducible R/DESeq2/CMScaller Docker image with remote Apptainer management, and let project chat discover remote FASTQ samples before the analysis form is completed.

**Architecture:** Keep SSH and command execution behind deterministic services. The chat router emits a structured `browse_samples` intent, and both chat and the existing scan endpoint call the same read-only discovery function. Container settings are audited through the existing config endpoint; separate guarded endpoints validate and run fixed Apptainer pull/test commands.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2/vanilla JavaScript, Paramiko transport abstraction, Docker, Apptainer, R/Bioconductor, pytest.

## Global Constraints

- The downstream image contains R, DESeq2 and CMScaller only; STAR, fastp, RSEM and Arriba remain outside it.
- Remote execution uses `apptainer exec --cleanenv` with structured bind paths.
- Image sources are limited to `docker://` and `oras://`; image targets are absolute `.sif` paths.
- Directory discovery is read-only and never starts an analysis.
- Secrets never appear in chat, API responses, commands, or saved audit records.

---

### Task 1: Conversational remote sample discovery

**Files:**
- Modify: `src/rnaseq_agent/webchat.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `src/rnaseq_agent/webtemplates/workbench.html`
- Test: `tests/test_webchat.py`
- Test: `tests/test_webapp.py`

**Interfaces:**
- Produces: `ChatIntent(action="browse_samples", params={"path": str})` and a chat response containing `samples`, `unmatched`, and `scanned_path`.

- [x] Add router tests for explicit and omitted paths, plus API tests proving chat uses the SSH transport and does not invoke the LLM for this tool intent.
- [x] Run the targeted tests and verify they fail because `browse_samples` does not exist.
- [x] Extract one validated read-only scan helper, route chat to it before free-form LLM replies, and render the candidate list with an “应用到样本表” action.
- [x] Run the targeted tests and commit the passing feature.

### Task 2: Downstream image and Apptainer service

**Files:**
- Create: `containers/downstream-r/Dockerfile`
- Create: `containers/downstream-r/verify_packages.R`
- Create: `src/rnaseq_agent/container_service.py`
- Modify: `src/rnaseq_agent/container.py`
- Modify: `src/rnaseq_agent/defaults.py`
- Test: `tests/test_container_service.py`

**Interfaces:**
- Produces: `validate_image_settings(container)`, `build_pull_command(container)`, and `build_test_command(container)`.

- [x] Add tests for accepted URI/path values, rejected schemes/relative targets, fixed `apptainer pull` construction, `--cleanenv`, binds, and package probes.
- [x] Run tests and verify the service import fails.
- [x] Implement minimal validation/command builders and the fixed-version downstream Docker image with a build-time package verification script.
- [x] Run the targeted tests and commit the passing feature.

### Task 3: Container web configuration and remote actions

**Files:**
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `src/rnaseq_agent/webtemplates/settings.html`
- Test: `tests/test_webapp.py`

**Interfaces:**
- Produces: audited container fields in `POST /api/config`, `POST /api/container/pull`, and `POST /api/container/test`.

- [x] Add endpoint and HTML tests for save/read-back, sanitized failures, pull, and test controls.
- [x] Run tests and verify the fields/routes/controls are absent.
- [x] Implement config filtering, remote calls through `create_remote_transport`, and settings controls with visible status output.
- [x] Run targeted and full tests; restart and browser smoke testing are recorded in the final verification.
