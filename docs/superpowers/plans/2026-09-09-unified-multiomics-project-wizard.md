# Unified Multiomics Project Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Galaxy-inspired multiomics workbench where bulk counts/expression upload and remote bulk FASTQ discovery are runnable first-class routes, while WXS and scRNA are visible reserved routes with honest availability gates.

**Architecture:** Add a project-level intake layer that owns route, input type, previews, sample design draft, and user-visible status. Keep existing session, graph, counts, SSH, and settings code working, but make the workbench call project-scoped APIs and derive all visible state from one source. Rework the UI into a left route/tool panel, wider central wizard plus Agent conversation, and right History panel.

**Tech Stack:** FastAPI, Jinja templates, vanilla JavaScript, pytest, existing `rnaseq_agent` session/workspace/graph modules.

## Global Constraints

- Design baseline: `docs/superpowers/specs/2026-09-09-unified-multiomics-project-wizard-design.md`.
- UI style: white, light, Galaxy-style tools/main/history layout; do not use dark theme.
- Supported runnable routes in this plan: bulk RNA counts/expression matrix upload and bulk RNA FASTQ registration/remote scan.
- Reserved routes in this plan: WXS and scRNA must show input requirements and a non-runnable reason; they must not create fake runnable sessions.
- User-visible status values: `setup`, `input_ready`, `planned`, `confirmed`, `running`, `waiting_user`, `completed`, `failed`.
- Legacy endpoints may remain, but new UI should prefer project-scoped endpoints.
- LLM must not receive raw matrices, FASTQ contents, SSH password, or API keys by default.
- Follow TDD: write each failing test first, run it and see the expected failure, then implement the minimal production change.

---

## File Structure

- Create `src/rnaseq_agent/project_intake.py`: project route/input draft persistence, matrix preview model, history item model, visible status derivation helpers.
- Modify `src/rnaseq_agent/sample_detection.py`: replace `read_counts_samples()` with richer matrix preview while preserving the old function as a compatibility wrapper.
- Modify `src/rnaseq_agent/workspace.py`: derive project list state from session/intake and stop exposing stale registry-only `drafting` for empty projects.
- Modify `src/rnaseq_agent/webapp.py`: add project-scoped intake, route, counts preview/session, FASTQ session, route status, and command endpoints; keep legacy endpoints.
- Replace most of `src/rnaseq_agent/webtemplates/workbench.html`: Galaxy-style three-column layout, four-step wizard, enlarged Agent conversation, right History panel.
- Modify `src/rnaseq_agent/webtemplates/index.html`: display unified project states and route summaries.
- Modify tests in `tests/test_sample_detection.py`, `tests/test_webapp_workspace.py`, `tests/test_webapp_counts_entry.py`, and create `tests/test_project_intake.py`, `tests/test_webapp_project_wizard.py`.

---

### Task 1: Project Intake Persistence And Unified Visible State

**Files:**
- Create: `src/rnaseq_agent/project_intake.py`
- Modify: `src/rnaseq_agent/workspace.py`
- Test: `tests/test_project_intake.py`
- Test: `tests/test_webapp_workspace.py`

**Interfaces:**
- Produces: `load_intake(project_dir: Path) -> dict[str, Any]`
- Produces: `save_intake(project_dir: Path, intake: Mapping[str, Any]) -> dict[str, Any]`
- Produces: `derive_visible_state(project_dir: Path, registry_state: str | None = None) -> str`
- Produces: `append_history(project_dir: Path, item: Mapping[str, Any]) -> dict[str, Any]`
- Produces: `history_items(project_dir: Path) -> list[dict[str, Any]]`

- [ ] **Step 1: Write the failing intake persistence tests**

Add `tests/test_project_intake.py`:

```python
from pathlib import Path

from rnaseq_agent.project_intake import (
    append_history,
    derive_visible_state,
    history_items,
    load_intake,
    save_intake,
)


def test_empty_project_defaults_to_setup(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    assert load_intake(project_dir)["state"] == "setup"
    assert derive_visible_state(project_dir, registry_state="drafting") == "setup"


def test_input_ready_comes_from_intake_when_no_session_exists(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    save_intake(project_dir, {"route": "bulk_rna", "input_type": "counts_matrix", "state": "input_ready"})

    assert derive_visible_state(project_dir) == "input_ready"


def test_session_state_overrides_intake_state(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    save_intake(project_dir, {"route": "bulk_rna", "input_type": "counts_matrix", "state": "input_ready"})
    (project_dir / "session.json").write_text('{"state":"planned"}', encoding="utf-8")

    assert derive_visible_state(project_dir) == "planned"


def test_history_items_are_project_local_and_ordered(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    first = append_history(project_dir, {"type": "input_matrix", "name": "counts.tsv", "state": "ready"})
    second = append_history(project_dir, {"type": "analysis_plan", "name": "Plan", "state": "planned"})

    rows = history_items(project_dir)
    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    assert rows[0]["type"] == "input_matrix"
    assert rows[1]["state"] == "planned"
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/test_project_intake.py -v`

Expected: FAIL because `rnaseq_agent.project_intake` does not exist.

- [ ] **Step 3: Implement `project_intake.py`**

Create `src/rnaseq_agent/project_intake.py` with JSON files under each project directory:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

INTAKE_FILE = "intake.json"
HISTORY_FILE = "history.json"
VISIBLE_STATES = {
    "setup",
    "input_ready",
    "planned",
    "confirmed",
    "running",
    "waiting_user",
    "completed",
    "failed",
}
SESSION_STATE_MAP = {
    "idle": "setup",
    "drafting": "input_ready",
    "planned": "planned",
    "confirmed": "confirmed",
    "executing": "running",
    "waiting": "waiting_user",
    "waiting_user": "waiting_user",
    "completed": "completed",
    "failed": "failed",
    "error": "failed",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_intake(project_dir: Path) -> dict[str, Any]:
    path = project_dir / INTAKE_FILE
    if not path.is_file():
        return {"state": "setup", "route": None, "input_type": None, "updated_at": None}
    payload = json.loads(path.read_text(encoding="utf-8"))
    state = str(payload.get("state") or "setup")
    if state not in VISIBLE_STATES:
        payload["state"] = "setup"
    return payload


def save_intake(project_dir: Path, intake: Mapping[str, Any]) -> dict[str, Any]:
    project_dir.mkdir(parents=True, exist_ok=True)
    payload = {**load_intake(project_dir), **dict(intake)}
    state = str(payload.get("state") or "setup")
    if state not in VISIBLE_STATES:
        raise ValueError(f"invalid project intake state: {state}")
    payload["state"] = state
    payload["updated_at"] = _now()
    (project_dir / INTAKE_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def derive_visible_state(project_dir: Path, registry_state: str | None = None) -> str:
    session_path = project_dir / "session.json"
    if session_path.is_file():
        try:
            session = json.loads(session_path.read_text(encoding="utf-8"))
            return SESSION_STATE_MAP.get(str(session.get("state") or "").lower(), "input_ready")
        except (OSError, json.JSONDecodeError):
            return "failed"
    intake_state = str(load_intake(project_dir).get("state") or "setup")
    if intake_state in VISIBLE_STATES:
        return intake_state
    if registry_state in {"completed", "failed", "planned", "confirmed"}:
        return registry_state
    return "setup"


def history_items(project_dir: Path) -> list[dict[str, Any]]:
    path = project_dir / HISTORY_FILE
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("items", []))


def append_history(project_dir: Path, item: Mapping[str, Any]) -> dict[str, Any]:
    project_dir.mkdir(parents=True, exist_ok=True)
    rows = history_items(project_dir)
    row = {
        "id": f"h{len(rows) + 1:04d}",
        "created_at": _now(),
        "type": str(item.get("type") or "event"),
        "name": str(item.get("name") or item.get("type") or "event"),
        "state": str(item.get("state") or "ready"),
        "details": item.get("details") or {},
    }
    rows.append(row)
    (project_dir / HISTORY_FILE).write_text(
        json.dumps({"items": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return row
```

- [ ] **Step 4: Update `Workspace` state derivation**

In `src/rnaseq_agent/workspace.py`, import `derive_visible_state` and use it in project listing/getting code where `meta["state"]` is currently returned directly. A registered project without `session.json` must return `setup`.

- [ ] **Step 5: Run focused tests**

Run:

```bash
pytest tests/test_project_intake.py tests/test_webapp_workspace.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/rnaseq_agent/project_intake.py src/rnaseq_agent/workspace.py tests/test_project_intake.py tests/test_webapp_workspace.py
git commit -m "feat: add project intake state"
```

---

### Task 2: Rich Matrix Preview For Counts And GEO Expression Matrices

**Files:**
- Modify: `src/rnaseq_agent/sample_detection.py`
- Test: `tests/test_sample_detection.py`

**Interfaces:**
- Produces: `preview_expression_matrix(content: bytes, filename: str = "") -> dict[str, Any]`
- Preserves: `read_counts_samples(content: bytes) -> list[str]`

- [ ] **Step 1: Add failing matrix preview tests**

Append to `tests/test_sample_detection.py`:

```python
from rnaseq_agent.sample_detection import preview_expression_matrix


def test_preview_expression_matrix_detects_raw_counts() -> None:
    content = b"gene\tCTRL_1\tCTRL_2\tCASE_1\nENSG1\t1\t2\t3\nENSG2\t0\t4\t9\n"

    preview = preview_expression_matrix(content, filename="counts.tsv")

    assert preview["ok"] is True
    assert preview["matrix_type"] == "raw_counts"
    assert preview["samples"] == ["CTRL_1", "CTRL_2", "CASE_1"]
    assert preview["can_run_deseq2"] is True


def test_preview_expression_matrix_detects_normalized_expression() -> None:
    content = b"gene\tA\tB\nGene1\t6.23\t7.81\nGene2\t4.01\t3.98\n"

    preview = preview_expression_matrix(content, filename="expr.tsv")

    assert preview["ok"] is True
    assert preview["matrix_type"] == "normalized_expression"
    assert preview["can_run_deseq2"] is False
    assert "不是 raw counts" in " ".join(preview["warnings"])


def test_preview_expression_matrix_detects_geo_series_matrix_like() -> None:
    content = (
        b"!Series_title\tExample\n"
        b"!series_matrix_table_begin\n"
        b"ID_REF\tGSM1\tGSM2\n"
        b"1007_s_at\t5.1\t6.2\n"
        b"1053_at\t7.0\t8.4\n"
        b"!series_matrix_table_end\n"
    )

    preview = preview_expression_matrix(content, filename="GSE_series_matrix.txt")

    assert preview["ok"] is True
    assert preview["matrix_type"] == "geo_series_matrix_like"
    assert preview["gene_id_column"] == "ID_REF"
    assert preview["samples"] == ["GSM1", "GSM2"]
    assert preview["can_run_deseq2"] is False
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/test_sample_detection.py -v`

Expected: FAIL because `preview_expression_matrix` is missing.

- [ ] **Step 3: Implement preview parsing**

In `src/rnaseq_agent/sample_detection.py`, add a parser that:

- decodes UTF-8 with `errors="replace"`;
- for GEO files, starts reading after `!series_matrix_table_begin` and stops before `!series_matrix_table_end`;
- otherwise ignores blank lines and leading lines beginning with `!`;
- chooses delimiter by counting tabs versus commas in the header;
- treats first column as `gene_id_column` and remaining columns as samples;
- scans up to 100 data rows;
- counts numeric cells, integer-like cells, negative cells, and decimal cells;
- returns `raw_counts` only when all scanned numeric cells are non-negative integers;
- returns `geo_series_matrix_like` for GEO series matrix input with decimals;
- returns `normalized_expression` when decimals are present;
- returns `unknown` when there are no data rows or no numeric cells.

- [ ] **Step 4: Preserve old function**

Update `read_counts_samples(content)` so it calls `preview_expression_matrix(content)` and returns `preview["samples"]` when `preview["ok"]` is true. Keep the existing duplicate/missing sample errors.

- [ ] **Step 5: Run focused tests**

Run: `pytest tests/test_sample_detection.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/rnaseq_agent/sample_detection.py tests/test_sample_detection.py
git commit -m "feat: preview expression matrix semantics"
```

---

### Task 3: Project-Scoped Wizard APIs

**Files:**
- Modify: `src/rnaseq_agent/webapp.py`
- Test: `tests/test_webapp_project_wizard.py`
- Test: `tests/test_webapp_counts_entry.py`

**Interfaces:**
- Produces: `GET /api/projects/{project_id}/intake`
- Produces: `POST /api/projects/{project_id}/route`
- Produces: `POST /api/projects/{project_id}/counts/preview`
- Produces: `POST /api/projects/{project_id}/counts/session`
- Produces: `POST /api/projects/{project_id}/fastq/session`

- [ ] **Step 1: Add failing API tests**

Create `tests/test_webapp_project_wizard.py`:

```python
from pathlib import Path
import re

import pytest

fastapi_missing = False
try:
    from fastapi.testclient import TestClient
    from rnaseq_agent.webapp import create_app
except ImportError:
    fastapi_missing = True

pytestmark = pytest.mark.skipif(fastapi_missing, reason="fastapi/httpx not installed")


def _token(client) -> str:
    page = client.get("/").text
    return re.search(r'const TOKEN = "([^"]+)"', page).group(1)


def _headers(token: str) -> dict[str, str]:
    return {"x-session-token": token}


def _create_project(client, token: str, project_id: str) -> None:
    resp = client.post(
        "/api/projects",
        json={"project_id": project_id, "title": project_id},
        headers=_headers(token),
    )
    assert resp.status_code == 200, resp.text


def test_new_project_uses_setup_state(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    _create_project(client, token, "wiz_a")

    projects = client.get("/api/projects", headers=_headers(token)).json()["projects"]
    row = next(p for p in projects if p["project_id"] == "wiz_a")
    assert row["state"] == "setup"

    intake = client.get("/api/projects/wiz_a/intake", headers=_headers(token)).json()
    assert intake["state"] == "setup"
    assert intake["routes"]["bulk_rna"]["available"] is True
    assert intake["routes"]["wxs"]["available"] is False
    assert intake["routes"]["scrna"]["available"] is False


def test_set_reserved_route_records_reason_without_session(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_b")

    body = client.post("/api/projects/wiz_b/route", json={"route": "scrna"}, headers=h).json()

    assert body["route"] == "scrna"
    assert body["available"] is False
    assert body["state"] == "setup"
    assert "session.json" not in [p.name for p in (tmp_path / "legacy" / "workspace" / "wiz_b").glob("*")]


def test_counts_preview_records_history_and_blocks_deseq2_for_geo(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_c")
    geo = (
        b"!series_matrix_table_begin\n"
        b"ID_REF\tGSM1\tGSM2\n"
        b"1007_s_at\t5.1\t6.2\n"
        b"!series_matrix_table_end\n"
    )

    body = client.post(
        "/api/projects/wiz_c/counts/preview",
        files={"file": ("GSE_series_matrix.txt", geo, "text/plain")},
        headers=h,
    ).json()

    assert body["preview"]["matrix_type"] == "geo_series_matrix_like"
    assert body["preview"]["can_run_deseq2"] is False
    assert body["state"] == "input_ready"
    assert body["history"][-1]["type"] == "input_matrix"


def test_counts_session_refuses_deseq2_for_normalized_matrix(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_d")
    content = b"gene\tA1\tA2\tA3\tB1\tB2\tB3\nG1\t1.1\t1.2\t1.3\t2.1\t2.2\t2.3\n"

    preview = client.post(
        "/api/projects/wiz_d/counts/preview",
        files={"file": ("expr.tsv", content, "text/tab-separated-values")},
        headers=h,
    ).json()
    upload_id = preview["upload_id"]

    body = client.post(
        "/api/projects/wiz_d/counts/session",
        json={
            "upload_id": upload_id,
            "enabled_diffexp": True,
            "reference_condition": "normal",
            "samples": [
                {"sample_id": "A1", "condition": "tumor"},
                {"sample_id": "A2", "condition": "tumor"},
                {"sample_id": "A3", "condition": "tumor"},
                {"sample_id": "B1", "condition": "normal"},
                {"sample_id": "B2", "condition": "normal"},
                {"sample_id": "B3", "condition": "normal"},
            ],
        },
        headers=h,
    ).json()

    assert body["error_code"] == "NOT_EVALUABLE"
    assert "raw counts" in body["message"]
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/test_webapp_project_wizard.py -v`

Expected: FAIL because project-scoped wizard endpoints do not exist.

- [ ] **Step 3: Add project route status helper**

In `src/rnaseq_agent/webapp.py`, add a local helper:

```python
def _route_statuses() -> dict[str, dict[str, object]]:
    return {
        "bulk_rna": {
            "label": "bulk RNA-seq",
            "available": True,
            "capability_id": "workflow.bulk_rna.grch38_pe_expression_fusion",
            "reason": "",
        },
        "wxs": {
            "label": "WXS",
            "available": False,
            "capability_id": "workflow.wes.grch38_paired_somatic_small_variant",
            "reason": "WXS 执行链尚未接入；当前仅展示输入要求。",
        },
        "scrna": {
            "label": "scRNA-seq",
            "available": False,
            "capability_id": "analysis.scrna.scanpy_standard",
            "reason": "scRNA 执行链尚未接入；当前仅展示 10x/h5ad/Seurat RDS 输入要求。",
        },
    }
```

- [ ] **Step 4: Implement intake and route endpoints**

Use `workspace.project_dir(project_id)`, `load_intake`, `save_intake`, `derive_visible_state`, `history_items`, and `_route_statuses()`.

`GET /api/projects/{id}/intake` returns:

```json
{
  "project_id": "wiz_a",
  "state": "setup",
  "intake": {},
  "routes": {},
  "history": []
}
```

`POST /api/projects/{id}/route` accepts `{"route":"bulk_rna"}`. For reserved routes, save route but keep state `setup`, return `available:false` and no session creation.

- [ ] **Step 5: Implement project counts preview endpoint**

`POST /api/projects/{id}/counts/preview` must:

- require multipart `file`;
- write uploaded bytes to `project_dir/uploads/<upload_id>_<safe_filename>`;
- call `preview_expression_matrix(content, filename)`;
- save intake with `route="bulk_rna"`, `input_type="counts_matrix"` or `"expression_matrix"`, `state="input_ready"`, `matrix_preview`, and `upload_id`;
- append an `input_matrix` history item;
- return `upload_id`, `preview`, `state`, and `history`.

- [ ] **Step 6: Implement project counts session endpoint**

`POST /api/projects/{id}/counts/session` must:

- load the saved intake and upload path by `upload_id`;
- validate sample list contains `sample_id` and `condition`;
- if `enabled_diffexp` is true and preview `matrix_type` is not `raw_counts`, return:

```json
{"error_code":"NOT_EVALUABLE","message":"当前矩阵不是 raw counts，不能用于 DESeq2 raw counts 流程。"}
```

- otherwise reuse the existing counts session creation logic from `/api/projects/{id}/counts`;
- append `sample_design` and `analysis_plan` or `contract` history as the existing state transition allows;
- return the same keys as the legacy counts endpoint plus `history`.

- [ ] **Step 7: Run focused tests**

Run:

```bash
pytest tests/test_webapp_project_wizard.py tests/test_webapp_counts_entry.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/rnaseq_agent/webapp.py tests/test_webapp_project_wizard.py tests/test_webapp_counts_entry.py
git commit -m "feat: add project wizard APIs"
```

---

### Task 4: Remote FASTQ Wizard Session Endpoint

**Files:**
- Modify: `src/rnaseq_agent/webapp.py`
- Test: `tests/test_webapp_project_wizard.py`

**Interfaces:**
- Consumes: `save_intake`, `append_history`, existing `detect_fastq_pairs`
- Produces: `POST /api/projects/{project_id}/fastq/session`

- [ ] **Step 1: Add failing FASTQ session test**

Append to `tests/test_webapp_project_wizard.py`:

```python
def test_fastq_session_from_detected_samples_enters_input_ready(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_e")

    body = client.post(
        "/api/projects/wiz_e/fastq/session",
        json={
            "data_source": "remote_path",
            "remote_fastq_dir": "/hwdata/home/yeyulin/demo_fastq",
            "cancer_type": "crc",
            "design": "independent_two_group",
            "layout": "paired",
            "samples": [
                {
                    "sample_id": "CTRL_1",
                    "condition": "control",
                    "fastq_1": "CTRL_1_R1.fastq.gz",
                    "fastq_2": "CTRL_1_R2.fastq.gz",
                },
                {
                    "sample_id": "CASE_1",
                    "condition": "case",
                    "fastq_1": "CASE_1_R1.fastq.gz",
                    "fastq_2": "CASE_1_R2.fastq.gz",
                },
            ],
        },
        headers=h,
    ).json()

    assert body["state"] in {"input_ready", "drafting"}
    assert body["intake"]["route"] == "bulk_rna"
    assert body["intake"]["input_type"] == "remote_fastq"
    assert body["history"][-1]["type"] == "sample_design"
```

- [ ] **Step 2: Run test and verify it fails**

Run: `pytest tests/test_webapp_project_wizard.py::test_fastq_session_from_detected_samples_enters_input_ready -v`

Expected: FAIL because endpoint is missing.

- [ ] **Step 3: Implement FASTQ session endpoint**

In `src/rnaseq_agent/webapp.py`, add `POST /api/projects/{id}/fastq/session`. It should call the same internal project creation/session code used by `/api/new?project=...`, but persist intake first:

```python
intake = save_intake(
    project_dir,
    {
        "route": "bulk_rna",
        "input_type": "remote_fastq" if body["data_source"] == "remote_path" else "local_fastq",
        "state": "input_ready",
        "fastq": {
            "data_source": body["data_source"],
            "remote_fastq_dir": body.get("remote_fastq_dir", ""),
            "fastq_dir": body.get("fastq_dir", ""),
        },
        "samples": body["samples"],
    },
)
```

Then create/update the normal FASTQ project session so existing plan/confirm logic remains usable.

- [ ] **Step 4: Run focused tests**

Run: `pytest tests/test_webapp_project_wizard.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/rnaseq_agent/webapp.py tests/test_webapp_project_wizard.py
git commit -m "feat: create fastq sessions from wizard intake"
```

---

### Task 5: Galaxy-Style Workbench UI

**Files:**
- Modify: `src/rnaseq_agent/webtemplates/workbench.html`
- Modify: `src/rnaseq_agent/webtemplates/index.html`
- Test: `tests/test_webapp_project_wizard.py`

**Interfaces:**
- Consumes: `/api/projects`, `/api/projects/{id}/intake`, `/api/projects/{id}/route`, `/api/projects/{id}/counts/preview`, `/api/projects/{id}/counts/session`, `/api/projects/{id}/fastq/session`
- Produces: browser-visible three-column workbench with left tools, wide center wizard/conversation, right History

- [ ] **Step 1: Add failing HTML smoke tests**

Append to `tests/test_webapp_project_wizard.py`:

```python
def test_workbench_renders_galaxy_style_regions(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    page = client.get("/workbench").text

    assert 'id="toolPanel"' in page
    assert 'id="mainWorkspace"' in page
    assert 'id="historyPanel"' in page
    assert 'id="agentConversation"' in page
    assert "History" in page


def test_index_labels_setup_state_for_new_projects(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_home")

    projects = client.get("/api/projects", headers=h).json()["projects"]
    assert next(p for p in projects if p["project_id"] == "wiz_home")["state"] == "setup"
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `pytest tests/test_webapp_project_wizard.py::test_workbench_renders_galaxy_style_regions -v`

Expected: FAIL because the current template does not expose these regions.

- [ ] **Step 3: Rework layout CSS**

In `workbench.html`, change the grid to:

```css
.layout {
  display: grid;
  grid-template-columns: 250px minmax(640px, 1fr) 340px;
  height: calc(100vh - 58px);
}
.middle {
  padding: 0;
  display: grid;
  grid-template-rows: minmax(360px, 1fr) minmax(260px, 38vh);
  overflow: hidden;
}
.wizard-main,
.conversation-main {
  padding: 16px 20px;
  overflow: auto;
}
.conversation-main {
  border-top: 1px solid var(--border);
  background: #fbfbfc;
}
```

Use IDs `toolPanel`, `mainWorkspace`, `agentConversation`, and `historyPanel` on the corresponding containers.

- [ ] **Step 4: Add route/tool cards**

In the left panel, render route buttons:

- bulk RNA-seq, enabled;
- WXS, disabled/reserved but clickable for requirements;
- scRNA-seq, disabled/reserved but clickable for requirements;
- Model Center, reserved;
- Report, reserved.

Clicking enabled bulk calls `/api/projects/{id}/route`. Clicking reserved routes shows their unavailable reason from `/api/projects/{id}/intake`.

- [ ] **Step 5: Add four-step center wizard**

In `mainWorkspace`, render:

1. route selection summary;
2. input type selector for bulk;
3. sample/design confirmation;
4. plan/confirm/execute controls.

Only show controls relevant to current state. Move the existing counts upload and remote FASTQ scan controls into the input step. Keep existing settings links for SSH/API/container details.

- [ ] **Step 6: Enlarge Agent conversation**

Move chat into `agentConversation`. Include:

- message list area;
- large textarea;
- send button;
- quick actions for “浏览服务器目录”, “生成计划”, “解释当前门禁”.

The send path can still call existing `/api/chat` in this task; Task 6 will unify command routing.

- [ ] **Step 7: Render right History panel**

Use intake `history` from `/api/projects/{id}/intake` to render records. Each record must show `type`, `name`, `state`, and `created_at`. When no history exists, show “尚无项目记录”.

- [ ] **Step 8: Run template tests**

Run: `pytest tests/test_webapp_project_wizard.py -v`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/rnaseq_agent/webtemplates/workbench.html src/rnaseq_agent/webtemplates/index.html tests/test_webapp_project_wizard.py
git commit -m "feat: redesign workbench around wizard and history"
```

---

### Task 6: Unified Command Routing For Buttons And Chat

**Files:**
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `src/rnaseq_agent/webtemplates/workbench.html`
- Test: `tests/test_webapp_project_wizard.py`

**Interfaces:**
- Produces: `POST /api/projects/{project_id}/command`
- Consumes: existing `/api/plan`, `/api/confirm`, remote scan helpers, intake APIs

- [ ] **Step 1: Add failing command tests**

Append to `tests/test_webapp_project_wizard.py`:

```python
def test_command_plan_uses_project_state(tmp_path: Path) -> None:
    client = TestClient(create_app(project_dir=tmp_path / "legacy"))
    token = _token(client)
    h = _headers(token)
    _create_project(client, token, "wiz_cmd")
    matrix = b"gene\tA1\tA2\tA3\tB1\tB2\tB3\nG1\t1\t2\t3\t4\t5\t6\n"
    preview = client.post(
        "/api/projects/wiz_cmd/counts/preview",
        files={"file": ("counts.tsv", matrix, "text/tab-separated-values")},
        headers=h,
    ).json()
    client.post(
        "/api/projects/wiz_cmd/counts/session",
        json={
            "upload_id": preview["upload_id"],
            "enabled_diffexp": True,
            "reference_condition": "B",
            "samples": [
                {"sample_id": "A1", "condition": "A"},
                {"sample_id": "A2", "condition": "A"},
                {"sample_id": "A3", "condition": "A"},
                {"sample_id": "B1", "condition": "B"},
                {"sample_id": "B2", "condition": "B"},
                {"sample_id": "B3", "condition": "B"},
            ],
        },
        headers=h,
    )

    body = client.post(
        "/api/projects/wiz_cmd/command",
        json={"command": "plan"},
        headers=h,
    ).json()

    assert body["state"] == "planned"
    assert body["action"] == "plan"
```

- [ ] **Step 2: Run test and verify it fails**

Run: `pytest tests/test_webapp_project_wizard.py::test_command_plan_uses_project_state -v`

Expected: FAIL because command endpoint is missing.

- [ ] **Step 3: Implement command endpoint**

In `webapp.py`, add command dispatch:

- `plan`: calls the existing plan logic bound to project;
- `confirm`: calls existing confirm logic;
- `set_route`: calls route helper;
- `scan_remote_fastq`: calls existing scan helper and appends `fastq_scan` history;
- unknown command returns `{"error_code":"UNKNOWN_COMMAND","message":"该动作不在允许列表中。"}`.

- [ ] **Step 4: Update frontend buttons**

Change plan/confirm buttons in `workbench.html` to call `/api/projects/{id}/command` with `{"command":"plan"}` or `{"command":"confirm"}`. Chat quick actions should call the same command endpoint.

- [ ] **Step 5: Run focused tests**

Run: `pytest tests/test_webapp_project_wizard.py tests/test_webchat.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/rnaseq_agent/webapp.py src/rnaseq_agent/webtemplates/workbench.html tests/test_webapp_project_wizard.py
git commit -m "feat: route wizard actions through project commands"
```

---

### Task 7: Browser Smoke Test And Regression Sweep

**Files:**
- Modify only if tests expose a defect: files touched in earlier tasks
- Test: existing pytest suite

**Interfaces:**
- Consumes: completed Tasks 1-6
- Produces: verified local workbench URL and regression evidence

- [ ] **Step 1: Run full test suite**

Run:

```bash
pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Start local server**

Run:

```bash
python -m rnaseq_agent.webapp --host 127.0.0.1 --port 8010
```

Expected: server listens at `http://127.0.0.1:8010`.

- [ ] **Step 3: Manual browser smoke path**

Open `http://127.0.0.1:8010/workbench` and verify:

- a new project shows `setup`;
- left panel shows route/tool cards;
- center panel is wider than side panels and contains the wizard plus enlarged conversation;
- right panel shows History;
- uploading a small raw counts matrix shows `raw_counts` and sample names;
- uploading `GSE13294_series_matrix.txt` shows `geo_series_matrix_like` or `normalized_expression` and blocks DESeq2;
- choosing WXS or scRNA shows requirements and an unavailable reason.

- [ ] **Step 4: Commit any smoke fixes**

If fixes were needed:

```bash
git add src/rnaseq_agent tests
git commit -m "fix: polish wizard smoke path"
```

If no fixes were needed, do not create an empty commit.

---

## Handoff Notes For WorkBuddy

- Current design commits to start from:
  - `a316194 docs: specify unified multiomics project wizard`
  - `c17b4e1 docs: align wizard UI with Galaxy workbench`
- Recent related implementation commits:
  - `6cf7a8e feat: discover remote samples from chat`
  - `739319d feat: add downstream Apptainer image service`
  - `af434ee feat: manage downstream containers from web`
  - `b1496a4 fix: retain connection secrets after successful tests`
  - `c5333ed fix: retain api key when connection test fails`
- Main current pain point: registered project state and real session state disagree. Fix Task 1 before touching UI.
- The user wants Galaxy-like layout but a simpler product: tools/main/history, white page, larger chat, no dark page.
- The user wants counts upload and remote server FASTQ discovery to feel automatic. Avoid making them type sample tables from scratch.
- Treat `GSE13294_series_matrix.txt` as a likely GEO expression matrix, not raw counts.
- Do not remove old endpoints until new project-scoped endpoints are covered by tests and the UI has moved over.

## Self-Review

- Spec coverage: all sections of the unified wizard spec map to tasks. State model is Task 1, matrix semantics is Task 2, project APIs are Tasks 3-4, Galaxy UI and larger conversation are Task 5, command unification is Task 6, verification is Task 7.
- Scope check: WXS/scRNA execution remains out of scope; route cards and unavailable reasons are included.
- Placeholder scan: this plan contains no unresolved marker wording.
- Type consistency: `ProjectIntake`, `MatrixPreview`, `history`, and visible state names are consistent across tasks.
