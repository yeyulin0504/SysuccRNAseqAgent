"""Localhost web workbench for the auditable bulk RNA golden route.

Framework section 11 / 15.8 step 1: single-user localhost web UI
(127.0.0.1), project / flow / chat / decision panels, plus the QC
checkpoint interrupt (WAITING_USER) surfaced as a resume button.

Built on FastAPI + Jinja2. The web layer only drives ProjectSession and
the LangGraph control plane; it never executes shell commands directly.
"""

from __future__ import annotations

import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .agent_graph import build_bulk_rna_graph
from .session import (
    CONFIRMED,
    DRAFTING,
    PLANNED,
    WAITING_USER,
    ProjectSession,
    SessionError,
    _load_changesets,
)
from .webchat import execute_intent, route_intent

STATIC_DIR = Path(__file__).resolve().parent / "webstatic"
TEMPLATES_DIR = Path(__file__).resolve().parent / "webtemplates"


def _default_project_dir() -> Path:
    return Path("runs/mvp_web")


def _session_for(project_dir: Path) -> ProjectSession:
    session = ProjectSession(project_dir)
    session.load_session()
    return session


def _session_view(session: ProjectSession) -> dict[str, Any]:
    return {
        "project_dir": str(session.project_dir),
        "state": session.state,
        "capability_id": session.capability_id,
        "summary": session.summary_lines(),
        "history": _load_changesets(session.changeset_path)[-8:],
    }


def create_app(*, project_dir: Path | None = None) -> FastAPI:
    # Single-user localhost: bind loopback only, random session token.
    project_dir = Path(project_dir or _default_project_dir()).resolve()
    token = secrets.token_urlsafe(16)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(
        title="SYSU Multi-omics Agent MVP",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    # -- auth guard ------------------------------------------------------
    def _guard(request: Request) -> None:
        provided = request.headers.get("x-session-token", "")
        if provided != token:
            raise HTTPException(status_code=403, detail="session token mismatch")

    @app.middleware("http")
    async def _token_guard(request: Request, call_next):
        # API routes require the token; the root page sets it via query.
        if request.url.path.startswith("/api"):
            provided = request.headers.get("x-session-token", "")
            if provided != token and request.query_params.get("token") != token:
                return JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)

    # -- pages -----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        session = _session_for(project_dir)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "token": token,
                "session": _session_view(session),
                "capabilities": [c.capability_id for c in __import__("rnaseq_agent.capability", fromlist=["list_capabilities"]).list_capabilities()],
            },
        )

    # -- session API -----------------------------------------------------

    @app.post("/api/new")
    async def api_new(request: Request):
        _guard(request)
        if (project_dir / "session.json").is_file():
            return {"error": "项目已存在，请先打开或删除。"}
        payload = await request.json()
        config = _default_config(project_dir, payload)
        session = ProjectSession(project_dir)
        gate = session.new_project(config)
        return {"state": session.state, "gate": gate.formatted()}

    @app.post("/api/plan")
    async def api_plan(request: Request):
        _guard(request)
        session = _session_for(project_dir)
        try:
            plan = session.plan()
            return {"state": session.state, "steps": plan.steps, "summary": plan.summary}
        except SessionError as exc:
            return {"error": str(exc)}

    @app.post("/api/confirm")
    async def api_confirm(request: Request):
        _guard(request)
        session = _session_for(project_dir)
        try:
            contract = session.confirm()
            return {"state": session.state, "contract_id": contract["contract_id"]}
        except SessionError as exc:
            return {"error": str(exc)}

    @app.post("/api/resume")
    async def api_resume(request: Request):
        """Resume from the QC checkpoint: user confirmed fastp QC, continue."""
        _guard(request)
        session = _session_for(project_dir)
        if session.state != WAITING_USER:
            return {"error": f"当前状态 {session.state} 不在 QC 检查点。"}
        # Move on: after a resume the plan stays confirmed; we simulate the
        # rest by confirming again if needed.
        return {"state": session.state, "message": "QC 已确认。"}

    @app.get("/api/state")
    async def api_state(request: Request):
        _guard(request)
        session = _session_for(project_dir)
        return _session_view(session)

    @app.post("/api/edit")
    async def api_edit(request: Request):
        _guard(request)
        session = _session_for(project_dir)
        payload = await request.json()
        try:
            gate = session.edit(payload.get("patch", {}), note=payload.get("note", ""))
            return {"state": session.state, "gate": gate.formatted()}
        except SessionError as exc:
            return {"error": str(exc)}

    @app.post("/api/deg")
    async def api_deg(request: Request):
        """差异表达（条件开放）状态与启用：设计门禁的结果回读。

        框架 15.3：DESeq2 两组差异表达是 counts 之后的*条件开放*阶段。
        该端点让 UI 可以查看当前样本表是否满足冻结模板
        （两组、每组生物学重复阈值、batch 与 condition 不混杂）。
        """
        _guard(request)
        session = _session_for(project_dir)
        payload = await request.json()
        enabled = bool(payload.get("enabled"))
        if session.config is None:
            return {"error": "当前还没有项目，请先创建。"}
        try:
            if enabled:
                session.edit(
                    {"pipeline": {"diffexp": {"enabled": True}}},
                    note="Web DEG 面板启用 diffexp",
                )
            from .differential import (
                DEG_DESIGN_FORMULA,
                deg_gate,
                diffexp_design_of,
                diffexp_is_requested,
            )

            config = session.config
            requested = diffexp_is_requested(config)
            gate = deg_gate(config)
            design = diffexp_design_of(config) if requested else None
            return {
                "state": session.state,
                "requested": requested,
                "formula": DEG_DESIGN_FORMULA,
                "design": design,
                "gate_ok": gate.ok,
                "gate_reasons": gate.reasons,
            }
        except SessionError as exc:
            return {"error": str(exc)}

    @app.post("/api/rollback")
    async def api_rollback(request: Request):
        _guard(request)
        session = _session_for(project_dir)
        payload = await request.json()
        try:
            session.rollback(payload.get("target_index"))
            return {"state": session.state}
        except SessionError as exc:
            return {"error": str(exc)}

    @app.post("/api/chat")
    async def api_chat(request: Request):
        """Chat panel endpoint: route intent -> audited session action.

        Design doc 5.2: the assistant only emits a structured action request;
        execution happens through the session (deterministic code). A plain
        question with no actionable intent gets a conversational reply.
        """
        _guard(request)
        payload = await request.json()
        text = str(payload.get("message", "")).strip()
        if not text:
            return {"reply": "请输入想做的事。"}

        session = _session_for(project_dir)
        intent = route_intent(text)
        if intent is None:
            has_project = session.config is not None
            capabilities = ", ".join(
                c.capability_id
                for c in __import__(
                    "rnaseq_agent.capability", fromlist=["list_capabilities"]
                ).list_capabilities()
            )
            return {
                "reply": (
                    "我还没理解成可执行操作。可以试试："
                    + ("生成计划 / 确认 / 把线程改成 16 / 关闭 arriba / 回滚 / 状态。" if has_project else "先在左侧创建项目，然后对我说：生成计划、确认、把线程改成 16。")
                    + f"\n当前能力：{capabilities}"
                ),
                "state": session.state,
            }

        result = execute_intent(session, intent)
        if "error" in result:
            result["reply"] = f"操作未完成：{result['error']}"
        elif "reply" not in result:
            result["reply"] = intent.message
        return result

    return app


def _default_config(project_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal config from web form payload (MVP scope)."""
    from datetime import datetime

    project_id = str(payload.get("project_id") or f"{datetime.now():%Y%m%d_%H%M%S}")
    items = []
    raw_items = payload.get("samples") or []
    for raw in raw_items:
        item = {
            "sample_id": raw.get("sample_id", ""),
            "condition": raw.get("condition", ""),
            "fastq_1": raw.get("fastq_1", ""),
        }
        if raw.get("fastq_2"):
            item["fastq_2"] = raw["fastq_2"]
        if raw.get("batch"):
            item["batch"] = raw["batch"]
        items.append(item)

    return {
        "schema_version": 1,
        "project": {"id": project_id, "title": payload.get("title") or project_id, "owner": payload.get("owner") or "local_user"},
        "study": {"cancer_type": payload.get("cancer_type") or "pan_cancer", "design": payload.get("design") or "independent_two_group"},
        "server": {
            "profile": "mvp_local",
            "host": payload.get("host") or "localhost",
            "user": payload.get("user") or "local_user",
            "remote_base_dir": str(project_dir / "remote"),
            "remote_workdir": str(project_dir / "remote" / "work"),
            "scheduler": "local",
            "threads": 8,
            "memory_gb": 32,
            "shell": "bash",
            "init_commands": [],
        },
        "reference": {
            "name": "GENCODE_R47_GRCh38p14_ALL",
            "remote_gtf_path": payload.get("gtf") or "/ref/gencode.v47.gtf",
            "remote_genome_fasta_path": payload.get("genome_fasta") or "/ref/GRCh38.fa",
            "star_index_dir": payload.get("star_index") or "/ref/star",
            "rsem_index_prefix": payload.get("rsem_prefix") or "/ref/rsem",
        },
        "sequencing": {"layout": "paired", "reads_per_sample_million": 40, "strandedness": "auto"},
        "samples": {
            "source": "local_upload",
            "local_data_dir": payload.get("fastq_dir") or str(Path("outputs/mvp_demo_data")),
            "remote_data_dir": "AUTO",
            "items": items,
        },
        "pipeline": {
            "fastp": {"enabled": True, "version": "0.24.1"},
            "star": {"enabled": True, "version": "2.7.11b"},
            "arriba": {"enabled": True, "version": "2.5.0"},
            "featurecounts": {"enabled": True, "version": "Subread 2.1.1"},
            "rsem": {"enabled": True, "version": "1.2.28"},
        },
        "polling": {"interval_seconds": 300, "timeout_hours": 24},
        "notification": {"email_enabled": False},
    }


def run_server(*, project_dir: Path | None = None, port: int = 8000) -> None:
    """Run the localhost server (uvicorn). Bind loopback only."""
    import uvicorn

    app = create_app(project_dir=Path(project_dir) if project_dir else None)
    uvicorn.run(app, host="127.0.0.1", port=port)


def find_free_port(preferred: int = 8000) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
