# Demo Connections and Sample Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide saved real SSH/LLM demo settings, connection tests, model discovery, and automatic sample detection for counts and remote FASTQ inputs.

**Architecture:** Add small tested service helpers for OpenAI-compatible requests and sample parsing, expose them through guarded FastAPI endpoints, then connect the existing light settings/project UI to those endpoints. Secret values remain write-only and connection responses are sanitized.

**Tech Stack:** Python 3.11+, FastAPI, urllib, Paramiko transport, Jinja2, browser JavaScript, pytest.

## Global Constraints

- Preset SSH fields: `10.30.24.1:22`, user `yeyulin`, scheduler `slurm`, work directory `/hwdata/home/yeyulin/`.
- Preset LLM fields: `https://llmapi.paratera.com/v1`, model `Deepseek-V4-Flash`.
- Passwords and API keys are never returned by read APIs.
- Connection success requires a real SSH or API response.

---

### Task 1: Connection and model APIs

**Files:**
- Modify: `src/rnaseq_agent/webapp.py`
- Test: `tests/test_webapp.py`

**Interfaces:**
- Consumes: existing `POST /api/config`, `test_server_connection`, and LLM config.
- Produces: `POST /api/config/demo`, `POST /api/test-server`, `POST /api/test-llm`, and `GET /api/llm/models`.

- [x] Write endpoint tests for preset saving, secret masking, real server helper invocation, LLM test parsing, model ID parsing, and sanitized failures.
- [x] Run targeted tests and confirm missing-route failures.
- [x] Implement minimal guarded endpoints and OpenAI-compatible request helper.
- [x] Run targeted tests and commit passing implementation.

### Task 2: Automatic sample detection

**Files:**
- Create: `src/rnaseq_agent/sample_detection.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Test: `tests/test_sample_detection.py`
- Test: `tests/test_webapp_counts_entry.py`

**Interfaces:**
- Produces: `detect_fastq_pairs(paths: list[str]) -> dict`, `read_counts_samples(content: bytes) -> list[str]`, `POST /api/samples/scan-remote`, and counts upload response field `detected_samples`.

- [x] Write failing tests for `_R1/_R2`, `_1/_2`, unmatched files, duplicate sample IDs, and counts headers.
- [x] Run targeted tests and confirm failures.
- [x] Implement deterministic parsing and read-only remote listing endpoint.
- [x] Expose counts header samples through a preview endpoint and run tests.

### Task 3: User interface and smoke verification

**Files:**
- Modify: `src/rnaseq_agent/webtemplates/settings.html`
- Modify: `src/rnaseq_agent/webtemplates/index.html`
- Test: `tests/test_webapp_workspace.py`

**Interfaces:**
- Consumes: Task 1 and Task 2 HTTP endpoints.
- Produces: visible save/test/pull-model/demo buttons and sample preview/bulk group controls.

- [x] Add failing HTML assertions for all required controls.
- [x] Run UI tests and confirm missing-control failures.
- [x] Implement buttons, status panels, model selector, automatic previews, and batch group assignment.
- [x] Run relevant tests, full suite, restart port 8010, and verify pages/endpoints over HTTP.
