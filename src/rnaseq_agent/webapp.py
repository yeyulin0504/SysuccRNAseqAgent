"""Localhost web workbench for the auditable bulk RNA golden route.

Framework section 11 / 15.8 + UI design (2026-09-07): a single-user
localhost (127.0.0.1) three-page workbench — project home, analysis
workbench, settings — over a multi-project workspace.

M1 (2026-09-07): the app now drives the real multi-project/thread model
and the LangGraph control plane:

- ``Workspace`` owns the project registry (create / open / archive);
- ``threads`` owns Project -> Thread -> Message persistence;
- the graph is invoked with ``thread_id == project_id`` so a QC interrupt
  resumes from disk (checkpointer) instead of a browser session.

The web layer never executes shell commands directly; it drives
ProjectSession and the graph. The existing single-project ``/api/*``
endpoints are preserved for the default project so the original workbench
stays usable while the three-page surface is introduced.
"""

from __future__ import annotations

import secrets
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .agent_graph import build_bulk_rna_graph, sqlite_checkpointer_for
from .session import (
    CONFIRMED,
    DEFAULT_CAPABILITY_ID,
    DRAFTING,
    PLANNED,
    WAITING_USER,
    ProjectSession,
    SessionError,
    _load_changesets,
)
from .storage import load_json
from .threads import (
    ThreadError,
    append_message,
    archive_thread,
    create_thread,
    get_thread,
    list_threads,
    messages,
    rename_thread,
)
from .webchat import execute_intent, route_intent
from .workspace import Workspace, WorkspaceError

STATIC_DIR = Path(__file__).resolve().parent / "webstatic"
TEMPLATES_DIR = Path(__file__).resolve().parent / "webtemplates"


def _default_workspace_dir() -> Path:
    return Path("runs/workspace")


def _session_for(project_dir: Path) -> ProjectSession:
    session = ProjectSession(project_dir)
    session.load_session()
    return session


def _session_view(session: ProjectSession) -> dict[str, Any]:
    config = session.config
    return {
        "project_dir": str(session.project_dir),
        "state": session.state,
        "capability_id": session.capability_id,
        "summary": session.summary_lines(),
        "history": _load_changesets(session.changeset_path)[-8:],
        # Web-editable connection / LLM configuration (secrets redacted).
        "config": _editable_config(config) if config is not None else None,
        # 样本表（右栏「样本」页签用）；项目尚未创建时为 []。
        "samples": list((config or {}).get("samples", {}).get("items", [])),
    }


def _editable_config(config: dict[str, Any]) -> dict[str, Any]:
    """Surface only the fields the web form may edit, never the api_key."""
    llm = config.get("llm", {})
    return {
        "server": {
            "profile": config.get("server", {}).get("profile", ""),
            "host": config.get("server", {}).get("host", ""),
            "user": config.get("server", {}).get("user", ""),
            "remote_base_dir": config.get("server", {}).get("remote_base_dir", ""),
            "remote_workdir": config.get("server", {}).get("remote_workdir", ""),
            "scheduler": config.get("server", {}).get("scheduler", "local"),
            "threads": config.get("server", {}).get("threads", 8),
            "memory_gb": config.get("server", {}).get("memory_gb", 32),
        },
        "llm": {
            "enabled": bool(llm.get("enabled")),
            "provider": llm.get("provider", ""),
            "api_base": llm.get("api_base", ""),
            "model": llm.get("model", ""),
            "api_key_set": bool(llm.get("api_key")),
        },
    }


def _llm_reply_or_none(config: dict[str, Any], text: str, timeout: float = 30.0) -> str | None:
    """Call the configured OpenAI-compatible chat endpoint.

    Returns the assistant reply text, or None when LLM is not enabled /
    not configured / the call fails — the caller falls back to the local
    rule router so the chat panel keeps working without a model.
    """
    llm = config.get("llm", {})
    if not llm.get("enabled") or not llm.get("api_key") or not llm.get("api_base"):
        return None
    api_base = str(llm.get("api_base", "")).rstrip("/")
    model = str(llm.get("model", "")).strip() or "gpt-4o-mini"
    url = f"{api_base}/chat/completions"
    try:
        import requests

        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {llm['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是 SYSU 多组学分析 Agent 的前端助手。用户正在配置一个"
                            "bulk RNA-seq 分析项目。能识别为可执行操作时，简短回复并提示"
                            "可继续点击「生成执行计划 / 确认并冻结契约 / 启用差异表达」。"
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                "temperature": 0.2,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return str(data["choices"][0]["message"]["content"]).strip()
    except Exception:  # noqa: BLE001 - any failure falls back to the rule router
        return None


def _interrupt_payloads(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the JSON-safe payload(s) of a LangGraph interrupt result.

    ``invoke`` returns a state dict carrying ``__interrupt__`` as a list of
    ``Interrupt`` objects; each ``.value`` holds the payload passed to
    ``interrupt(...)``. We normalise that so the web layer never sees the
    runtime objects.
    """
    raw = result.get("__interrupt__") or []
    payloads: list[dict[str, Any]] = []
    for item in raw:
        value = getattr(item, "value", item)
        payloads.append(value if isinstance(value, dict) else {"value": value})
    return payloads


def _result_view(result: dict[str, Any]) -> dict[str, Any]:
    """A JSON-safe view of a graph run result (drops runtime objects)."""
    view = {k: v for k, v in result.items() if k != "__interrupt__"}
    view["interrupts"] = _interrupt_payloads(result)
    return view


def _new_thread_id() -> str:
    """A collision-resistant thread id (all-alphanumeric, threads-safe)."""
    from datetime import datetime, timezone

    return "thread_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def create_app(
    *,
    project_dir: Path | None = None,
    workspace_dir: Path | None = None,
) -> FastAPI:
    # Single-user localhost: bind loopback only, random session token.
    workspace_root = Path(
        workspace_dir or (project_dir.parent if project_dir else _default_workspace_dir())
    ).resolve()
    workspace = Workspace(workspace_root)
    # The single-project ``/api/*`` endpoints keep operating on one "default"
    # project directory so the original workbench stays usable; the three-page
    # surface addresses projects by id via the Workspace registry instead.
    legacy_project_dir = (
        Path(project_dir).resolve() if project_dir else workspace_root / "default"
    )
    token = secrets.token_urlsafe(16)

    # In-memory graph run bookkeeping keyed by project_id. The graph itself
    # persists to the per-project SQLite checkpointer; this dict only tracks
    # the in-flight invoke so the HTTP layer can release while a job runs.
    _graph_runs: dict[str, dict[str, Any]] = {}
    _graph_lock = threading.Lock()

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

    # -- workspace / thread / graph helpers -----------------------------

    def _project_dir_or_404(project_id: str) -> Path:
        if workspace.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail=f"项目 {project_id!r} 不存在。")
        return workspace.project_dir(project_id)

    def _bound_project_id(request: Request, body: dict[str, Any] | None = None) -> str | None:
        """Resolve an optional workspace project binding for a legacy ``/api/*`` call.

        M1.5: the single-project session endpoints gain a project binding so the
        three-pane workbench can drive any registered project. ``?project=<id>``
        (query) or ``project_id`` (JSON body) names the project; only ids that
        actually exist in the workspace registry bind, so ``/api/new``'s
        ``project_id`` field (the config's ``project.id``) never collides. Any
        unbound call keeps operating on the default legacy project directory.
        """
        project_id = str(request.query_params.get("project") or "").strip()
        if not project_id and isinstance(body, dict):
            project_id = str(body.get("project_id") or "").strip()
        if project_id and workspace.get_project(project_id) is not None:
            return project_id
        return None

    def _legacy_dir_for(request: Request, body: dict[str, Any] | None = None) -> Path:
        """Directory a legacy ``/api/*`` endpoint should operate on.

        Binds to the named workspace project when present, else falls back to
        the default legacy directory (backward compatible with the original
        single-project workbench).
        """
        project_id = _bound_project_id(request, body)
        return _project_dir_or_404(project_id) if project_id is not None else legacy_project_dir

    def _capability_id_for(project_dir: Path) -> str:
        """Prefer the capability bound in session.json, else the frozen slot."""
        session_path = project_dir / "session.json"
        if session_path.is_file():
            try:
                payload = load_json(session_path)
            except (OSError, ValueError):
                payload = {}
            capability_id = str(payload.get("capability_id") or "").strip()
            if capability_id:
                return capability_id
        return DEFAULT_CAPABILITY_ID

    def _start_graph_run(
        project_id: str,
        project_dir: Path,
        *,
        resume: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Drive the graph in a background thread and return its bookkeeping entry.

        The HTTP request returns immediately; the graph continues in a daemon
        thread and its durable state lands in the per-project SQLite
        checkpointer (``thread_id == project_id``). Returns ``{"conflict": ...}``
        when a run is already in flight.
        """
        with _graph_lock:
            existing = _graph_runs.get(project_id)
            if existing is not None:
                return {"conflict": True, "state": existing.get("state", "running")}

        entry: dict[str, Any] = {
            "project_id": project_id,
            "state": "running",
            "result": None,
            "error": None,
        }
        with _graph_lock:
            _graph_runs[project_id] = entry

        def _drive() -> None:
            try:
                with sqlite_checkpointer_for(project_dir) as checkpointer:
                    graph = build_bulk_rna_graph(checkpointer)
                    config = {"configurable": {"thread_id": project_id}}
                    if resume is not None:
                        from langgraph.types import Command

                        result = graph.invoke(Command(resume=resume), config=config)
                    else:
                        result = graph.invoke(
                            {
                                "project_dir": str(project_dir),
                                "capability_id": _capability_id_for(project_dir),
                                "status": "",
                                "message": "",
                            },
                            config=config,
                        )
                entry["result"] = _result_view(result)
                interrupts = _interrupt_payloads(result)
                if interrupts:
                    entry["state"] = WAITING_USER
                    entry["interrupt"] = interrupts[0]
                else:
                    entry["state"] = str(result.get("status") or "completed")
            except Exception as exc:  # noqa: BLE001 - surface the error to the UI
                entry["state"] = "error"
                entry["error"] = str(exc)
            finally:
                with _graph_lock:
                    _graph_runs.pop(project_id, None)

        threading.Thread(target=_drive, daemon=True, name=f"graph:{project_id}").start()
        return entry

    def _graph_snapshot(project_id: str, project_dir: Path) -> dict[str, Any]:
        """Read the durable graph state from the per-project checkpointer."""
        with _graph_lock:
            in_flight = _graph_runs.get(project_id)
        if in_flight is not None:
            return {
                "project_id": project_id,
                "state": in_flight.get("state", "running"),
                "in_flight": True,
                "error": in_flight.get("error"),
            }
        try:
            with sqlite_checkpointer_for(project_dir) as checkpointer:
                tup = checkpointer.get_tuple(
                    config={"configurable": {"thread_id": project_id}}
                )
        except Exception as exc:  # noqa: BLE001
            return {"project_id": project_id, "state": "error", "error": str(exc)}
        if tup is None:
            return {"project_id": project_id, "state": "idle", "in_flight": False}

        checkpoint = tup.checkpoint or {}
        channels = dict(checkpoint.get("channel_values", {}) or {})
        # Unstructured ``dict`` state nests the whole state under ``__root__``;
        # a TypedDict schema (BulkRNAState) stores one channel per key.
        if "__root__" in channels and isinstance(channels["__root__"], dict):
            graph_state = dict(channels["__root__"])
        else:
            graph_state = channels
        interrupts: list[dict[str, Any]] = []
        for _task_id, channel, value in (tup.pending_writes or []):
            if channel == "__interrupt__":
                for item in value:
                    interrupts.append(getattr(item, "value", item))
        status = str(graph_state.get("status") or "")
        state = WAITING_USER if interrupts else (status or "completed")
        return {
            "project_id": project_id,
            "state": state,
            "in_flight": False,
            "graph_state": graph_state,
            "interrupt": interrupts[0] if interrupts else None,
        }

    # -- pages -----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "token": token,
                "workspace_root": str(workspace.root),
            },
        )

    @app.get("/workbench", response_class=HTMLResponse)
    async def workbench(request: Request):
        # M1.5: ``?project=<id>`` 让三栏工作台直接绑定到指定项目；未提供或
        # 未注册时页面仍正常渲染，由前端在左栏列出项目供选择。
        project_id = str(request.query_params.get("project") or "").strip()
        if project_id and workspace.get_project(project_id) is None:
            project_id = ""
        return templates.TemplateResponse(
            request,
            "workbench.html",
            {"token": token, "project_id": project_id},
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings(request: Request):
        session = _session_for(legacy_project_dir)
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "token": token,
                "session": _session_view(session),
            },
        )

    # -- session API -----------------------------------------------------

    @app.post("/api/new")
    async def api_new(request: Request):
        _guard(request)
        payload = await request.json()
        project_dir = _legacy_dir_for(request, payload)
        if (project_dir / "session.json").is_file():
            return {"error": "项目已存在，请先打开或删除。"}
        config = _default_config(project_dir, payload)
        session = ProjectSession(project_dir)
        gate = session.new_project(config)
        # 若该目录对应一个已注册的 workspace 项目，同步注册表里的状态。
        project_id = _bound_project_id(request, payload)
        if project_id is not None:
            workspace.touch(project_id, session.state)
        return {"state": session.state, "gate": gate.formatted()}

    @app.post("/api/plan")
    async def api_plan(request: Request):
        _guard(request)
        session = _session_for(_legacy_dir_for(request))
        try:
            plan = session.plan()
            return {"state": session.state, "steps": plan.steps, "summary": plan.summary}
        except SessionError as exc:
            return {"error": str(exc)}

    @app.post("/api/confirm")
    async def api_confirm(request: Request):
        _guard(request)
        session = _session_for(_legacy_dir_for(request))
        try:
            contract = session.confirm()
            return {"state": session.state, "contract_id": contract["contract_id"]}
        except SessionError as exc:
            return {"error": str(exc)}

    @app.post("/api/resume")
    async def api_resume(request: Request):
        """Resume from the QC checkpoint: user confirmed fastp QC, continue."""
        _guard(request)
        session = _session_for(_legacy_dir_for(request))
        if session.state != WAITING_USER:
            return {"error": f"当前状态 {session.state} 不在 QC 检查点。"}
        # Move on: after a resume the plan stays confirmed; we simulate the
        # rest by confirming again if needed.
        return {"state": session.state, "message": "QC 已确认。"}

    @app.get("/api/state")
    async def api_state(request: Request):
        _guard(request)
        session = _session_for(_legacy_dir_for(request))
        return _session_view(session)

    @app.post("/api/edit")
    async def api_edit(request: Request):
        _guard(request)
        payload = await request.json()
        session = _session_for(_legacy_dir_for(request, payload))
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
        payload = await request.json()
        session = _session_for(_legacy_dir_for(request, payload))
        enabled = bool(payload.get("enabled"))
        if session.config is None:
            return {"error": "当前还没有项目，请先创建。"}
        try:
            # reference_condition 与设计开关一同提交（M1.6 显式确认）。
            diffexp_patch: dict[str, Any] = {}
            reference = str(payload.get("reference_condition") or "").strip()
            if reference:
                diffexp_patch["reference_condition"] = reference
            if diffexp_patch or enabled:
                session.edit(
                    {
                        "pipeline": {"diffexp": {"enabled": enabled}},
                        **({"diffexp": diffexp_patch} if diffexp_patch else {}),
                    },
                    note="Web DEG 面板启用 diffexp"
                    + (f"，reference={reference}" if reference else ""),
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
        payload = await request.json()
        session = _session_for(_legacy_dir_for(request, payload))
        try:
            session.rollback(payload.get("target_index"))
            return {"state": session.state}
        except SessionError as exc:
            return {"error": str(exc)}

    @app.get("/api/config")
    async def api_config_get(request: Request):
        """Read back the web-editable connection / LLM settings (api_key masked)."""
        _guard(request)
        session = _session_for(_legacy_dir_for(request))
        if session.config is None:
            return {"config": None}
        return {"config": _editable_config(session.config)}

    @app.post("/api/config")
    async def api_config_post(request: Request):
        """Edit connection (server.*) or LLM settings through the audited session."""
        _guard(request)
        payload = await request.json()
        session = _session_for(_legacy_dir_for(request, payload))
        patch: dict[str, Any] = {}
        note_parts: list[str] = []

        server = payload.get("server")
        if isinstance(server, dict):
            allowed = {"host", "user", "scheduler", "threads", "memory_gb"}
            server_patch = {k: v for k, v in server.items() if k in allowed and v is not None}
            if server_patch:
                # threads / memory_gb arrive as numbers from the form.
                if "threads" in server_patch:
                    server_patch["threads"] = int(server_patch["threads"])
                if "memory_gb" in server_patch:
                    server_patch["memory_gb"] = int(server_patch["memory_gb"])
                patch["server"] = server_patch
                note_parts.append("服务器配置：" + ", ".join(f"{k}={v}" for k, v in server_patch.items()))

        llm = payload.get("llm")
        if isinstance(llm, dict):
            llm_patch: dict[str, Any] = {}
            for key in ("provider", "api_base", "model", "enabled"):
                if key in llm and llm[key] is not None:
                    llm_patch[key] = llm[key]
            # api_key is write-only: an empty string means "keep it".
            if llm.get("api_key"):
                llm_patch["api_key"] = llm["api_key"]
            if "api_key_clear" in llm and llm["api_key_clear"]:
                llm_patch["api_key"] = ""
            if llm_patch:
                patch["llm"] = llm_patch
                note_parts.append(
                    "大模型接入：" + (f"启用 {llm_patch.get('model', '')}" if llm_patch.get("enabled") else "更新")
                )

        if not patch:
            return {"error": "没有可保存的字段。"}
        try:
            gate = session.edit(patch, note=" / ".join(note_parts))
            return {
                "state": session.state,
                "gate": gate.formatted(),
                "config": _editable_config(session.config) if session.config is not None else None,
            }
        except SessionError as exc:
            return {"error": str(exc)}

    @app.post("/api/chat")
    async def api_chat(request: Request):
        """Chat panel endpoint: route intent -> audited session action.

        When the project has an OpenAI-compatible LLM configured and enabled
        the message is sent to the model first. If the model is unavailable
        (connection error / HTTP error / not configured) the local rule router
        drives the session instead, so the panel never breaks.

        Design doc 5.2: the assistant only emits a structured action request;
        execution happens through the session (deterministic code).
        """
        _guard(request)
        payload = await request.json()
        text = str(payload.get("message", "")).strip()
        if not text:
            return {"reply": "请输入想做的事。"}

        session = _session_for(_legacy_dir_for(request, payload))
        config = session.config

        # 1) LLM path (when enabled and reachable).
        if config is not None:
            llm_reply = _llm_reply_or_none(config, text)
            if llm_reply:
                return {"state": session.state, "reply": llm_reply, "via": "llm"}

        # 2) Local rule router fallback.
        intent = route_intent(text)
        if intent is None:
            has_project = config is not None
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

    # -- workspace API (multi-project registry) --------------------------

    @app.get("/api/projects")
    async def api_projects_list(request: Request):
        _guard(request)
        include_archived = request.query_params.get("include_archived") == "1"
        return {"projects": workspace.list_projects(include_archived=include_archived)}

    @app.post("/api/projects")
    async def api_projects_create(request: Request):
        """Create a project entry and its default 「主分析流程」 thread.

        The analysis session (session.json) is created later on the workbench
        via ``/api/new``; this endpoint only owns the registry + directory +
        conversation scaffolding (UI doc section 4).
        """
        _guard(request)
        payload = await request.json()
        project_id = str(payload.get("project_id") or "").strip()
        if not project_id:
            return {"error": "缺少 project_id。"}
        try:
            meta = workspace.create_project(
                project_id,
                title=str(payload.get("title") or ""),
                owner=str(payload.get("owner") or ""),
                description=str(payload.get("description") or ""),
            )
        except WorkspaceError as exc:
            return {"error": str(exc)}
        project_dir = workspace.project_dir(project_id)
        # Auto-create the main analysis thread (idempotent).
        try:
            create_thread(project_dir, "main", title="主分析流程")
        except ThreadError:
            pass
        workspace.set_thread_count(project_id, len(list_threads(project_dir)))
        return meta

    @app.post("/api/projects/{project_id}/archive")
    async def api_projects_archive(project_id: str, request: Request):
        _guard(request)
        try:
            return workspace.archive_project(project_id)
        except WorkspaceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/unarchive")
    async def api_projects_unarchive(project_id: str, request: Request):
        _guard(request)
        try:
            return workspace.unarchive_project(project_id)
        except WorkspaceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # -- threads API (per-project conversations) -------------------------

    @app.get("/api/projects/{project_id}/threads")
    async def api_threads_list(project_id: str, request: Request):
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        return {"threads": list_threads(project_dir)}

    @app.post("/api/projects/{project_id}/threads")
    async def api_threads_create(project_id: str, request: Request):
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        payload = await request.json()
        thread_id = str(payload.get("thread_id") or "").strip()
        if not thread_id:
            thread_id = _new_thread_id()
        try:
            thread = create_thread(project_dir, thread_id, title=str(payload.get("title") or ""))
        except ThreadError as exc:
            return {"error": str(exc)}
        workspace.set_thread_count(project_id, len(list_threads(project_dir)))
        return thread

    @app.get("/api/projects/{project_id}/threads/{thread_id}")
    async def api_threads_get(project_id: str, thread_id: str, request: Request):
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        try:
            return get_thread(project_dir, thread_id)
        except ThreadError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/api/projects/{project_id}/threads/{thread_id}")
    async def api_threads_rename(project_id: str, thread_id: str, request: Request):
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        payload = await request.json()
        title = str(payload.get("title") or "").strip()
        if not title:
            return {"error": "缺少 title。"}
        try:
            return rename_thread(project_dir, thread_id, title)
        except ThreadError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/projects/{project_id}/threads/{thread_id}")
    async def api_threads_archive(project_id: str, thread_id: str, request: Request):
        """Archive a thread; inputs/runs/artifacts are never touched (UI doc 3.6)."""
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        try:
            return archive_thread(project_dir, thread_id)
        except ThreadError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/projects/{project_id}/threads/{thread_id}/messages")
    async def api_threads_messages(project_id: str, thread_id: str, request: Request):
        """Only this thread's messages; no cross-thread reads (UI doc 3.5)."""
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        try:
            return {"messages": messages(project_dir, thread_id)}
        except ThreadError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/threads/{thread_id}/messages")
    async def api_threads_append(project_id: str, thread_id: str, request: Request):
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        payload = await request.json()
        role = str(payload.get("role") or "user")
        if role not in {"user", "agent"}:
            return {"error": "role 只能是 user 或 agent。"}
        content = str(payload.get("content") or "").strip()
        if not content:
            return {"error": "缺少 content。"}
        try:
            message = append_message(
                project_dir,
                thread_id,
                role=role,
                content=content,
                references=payload.get("references"),
            )
        except ThreadError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return message

    # -- graph API (LangGraph control plane, thread_id == project_id) ----
    @app.post("/api/projects/{project_id}/graph/run")
    async def api_graph_run(project_id: str, request: Request):
        """Start the control-plane graph; returns immediately, runs in background."""
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        entry = _start_graph_run(project_id, project_dir)
        if entry.get("conflict"):
            return {"error": "该项目的图运行已在执行中。", "state": entry.get("state")}
        return {"project_id": project_id, "state": "running"}

    @app.get("/api/projects/{project_id}/graph/state")
    async def api_graph_state(project_id: str, request: Request):
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        return _graph_snapshot(project_id, project_dir)

    @app.post("/api/projects/{project_id}/graph/resume")
    async def api_graph_resume(project_id: str, request: Request):
        """Resume from the QC checkpoint (fastp QC decision).

        The decision is recorded against the same attempt via the graph's
        ``wait_qc`` node, so every dialogue reads one artifact/checkpoint.
        """
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        snapshot = _graph_snapshot(project_id, project_dir)
        if snapshot.get("state") != WAITING_USER:
            return {"error": f"当前状态 {snapshot.get('state')} 不在 QC 检查点，无法恢复。"}
        payload = await request.json()
        decision = {
            "approved": bool(payload.get("approved", True)),
            "user": str(payload.get("user") or ""),
            "thread_id": str(payload.get("thread_id") or project_id),
            "note": str(payload.get("note") or ""),
        }
        entry = _start_graph_run(project_id, project_dir, resume=decision)
        if entry.get("conflict"):
            return {"error": "该项目的图运行已在执行中。"}
        return {"project_id": project_id, "state": "running", "resume": decision}

    # -- counts 直入（DE / CMS 一级入口）--------------------------------

    @app.post("/api/projects/{project_id}/counts")
    async def api_projects_counts(project_id: str, request: Request):
        """Create / refresh the counts 直入 analysis session for a project.

        Uploads ``counts_matrix.tsv`` next to the project, then builds an
        analysis session whose diffexp / cms stages read the matrix directly
        (no FASTQ pipeline). Body is ``multipart/form-data``:

        - ``file``: count matrix TSV/CSV (first column = gene id);
        - ``enabled_diffexp`` / ``enabled_cms``: "1" to open the stage;
        - ``reference_condition``: DEG reference group (diffexp required);
        - ``cancer_type`` / ``design`` / ``gtf`` / ``genome_fasta`` /
          ``star_index`` / ``rsem_prefix``: optional metadata.
        """
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        form = await request.form()

        # -- persist the uploaded matrix ---------------------------------
        upload = form.get("file")
        if upload is None:
            return {"error": "缺少 counts 矩阵文件（file）。"}
        content = await upload.read()
        if not content:
            return {"error": "上传的 counts 矩阵为空。"}
        uploads_dir = project_dir / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        counts_path = uploads_dir / "counts_matrix.tsv"
        counts_path.write_bytes(content)

        enabled_diffexp = str(form.get("enabled_diffexp") or "").strip() == "1"
        enabled_cms = str(form.get("enabled_cms") or "").strip() == "1"
        if not enabled_diffexp and not enabled_cms:
            return {"error": "请至少启用差异表达（DE）或 CMS 分型之一。"}
        reference = str(form.get("reference_condition") or "").strip()
        if enabled_diffexp and not reference:
            return {"error": "启用差异表达必须显式提供 reference_condition。"}

        samples: list[dict[str, Any]] = []
        sample_ids = [str(v) for v in form.getlist("sample_id") if str(v).strip()]
        conditions = [str(v) for v in form.getlist("condition") if str(v).strip()]
        for index, sample_id in enumerate(sample_ids):
            condition = conditions[index] if index < len(conditions) else ""
            if sample_id and condition:
                samples.append({"sample_id": sample_id, "condition": condition})

        base = _default_config(project_dir, {"project_id": project_id})
        # counts 直入固定参考路径由表单覆盖（用户可手填服务器路径）。
        for key, form_key in (
            ("remote_gtf_path", "gtf"),
            ("remote_genome_fasta_path", "genome_fasta"),
            ("star_index_dir", "star_index"),
            ("rsem_index_prefix", "rsem_prefix"),
        ):
            value = str(form.get(form_key) or "").strip()
            if value:
                base["reference"][key] = value

        base["samples"] = {
            "source": "counts_upload",
            "local_data_dir": str(uploads_dir),
            "remote_data_dir": "AUTO",
            "counts_path": str(counts_path),
            "items": samples,
        }
        base["pipeline"].update(
            {
                # counts 直入：关闭 FASTQ 主流程，只保留下游条件开放阶段。
                "fastp": {"enabled": False, "version": "0.24.1"},
                "star": {"enabled": False, "version": "2.7.11b"},
                "arriba": {"enabled": False, "version": "2.5.0"},
                "featurecounts": {"enabled": False, "version": "Subread 2.1.1"},
                "rsem": {"enabled": False, "version": "1.2.28"},
                "diffexp": {"enabled": enabled_diffexp, "version": "DESeq2 1.40+ (R 4.2+)"},
                "cms": {"enabled": enabled_cms, "version": "CMScaller 2.0"},
            }
        )
        base["cms"] = {
            "run_mode": "counts",
            "n_perm": 1000,
            "fdr": 0.05,
            "seed": 20260907,
            "do_plot": False,
            "min_samples": 30,
        }
        base["study"].update(
            {
                "cancer_type": str(form.get("cancer_type") or "").strip() or "pan_cancer",
                "design": str(form.get("design") or "").strip() or "independent_two_group",
            }
        )
        if enabled_diffexp:
            base["diffexp"] = {
                "reference_condition": reference,
                "formula": "~ condition",
                "min_replicates_per_group": 3,
            }

        session = ProjectSession(project_dir)
        gate = session.new_project(base)
        workspace.touch(project_id, session.state)
        return {
            "state": session.state,
            "counts_path": str(counts_path),
            "gate": gate.formatted(),
            "cms_counts": True,
        }

    @app.post("/api/cms")
    async def api_cms(request: Request):
        """CMS（条件开放）状态与启用：CMScaller 分型门禁结果回读。

        框架 15.3：CMS 是 counts/TPM 之后的*条件开放*已注册分型模型。该端点
        让 UI 查看当前项目是否满足冻结模板（癌种=CRC / 样本≥30 / featureCounts
        前置）并切换开关。
        """
        _guard(request)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - empty body = status-only read
            payload = {}
        session = _session_for(_legacy_dir_for(request, payload))
        if session.config is None:
            return {"error": "当前还没有项目，请先创建。"}
        try:
            from .cms import (
                CMS_DEFAULT_FDR,
                CMS_DEFAULT_N_PERM,
                cms_design_checks,
                cms_design_of,
                cms_is_requested,
            )

            enabled = bool(payload.get("enabled"))
            run_mode = str(payload.get("run_mode") or "").strip()
            patch: dict[str, Any] = {}
            if enabled or run_mode:
                patch["pipeline"] = {"cms": {"enabled": enabled}}
                cms_patch: dict[str, Any] = {}
                if run_mode in {"pipeline", "counts"}:
                    cms_patch["run_mode"] = run_mode
                if enabled:
                    cms_patch.setdefault("n_perm", CMS_DEFAULT_N_PERM)
                    cms_patch.setdefault("fdr", CMS_DEFAULT_FDR)
                if cms_patch:
                    patch["cms"] = cms_patch
                session.edit(patch, note="Web CMS 面板" + (f" run_mode={run_mode}" if run_mode else ""))
            requested = cms_is_requested(session.config)
            reasons = cms_design_checks(session.config) if requested else []
            return {
                "state": session.state,
                "requested": requested,
                "run_mode": str(session.config.get("cms", {}).get("run_mode", "pipeline")),
                "gate_ok": not reasons,
                "gate_reasons": reasons,
                "design": cms_design_of(session.config) if requested else None,
            }
        except Exception as exc:  # noqa: BLE001 - surface session errors
            return {"error": str(exc)}

    return app


def _default_config(project_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a config from the web form payload (2026-09-08 form).

    The form collects a *data source* (``local_upload`` = upload local FASTQs,
    ``remote_path`` = files already staged under a remote directory), four
    reference paths (GTF / genome FASTA / STAR index / RSEM prefix), the
    sample table, and optionally a counts 直入 entry (DE / CMS from an
    uploaded count matrix — see ``/api/counts/projects``).
    """
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

    # 数据来源：local_upload（本地上传）/ remote_path（服务器已有 reads）。
    source = str(payload.get("data_source") or "local_upload").strip()
    if source not in {"local_upload", "remote_path"}:
        source = "local_upload"
    local_data_dir = str(payload.get("fastq_dir") or "").strip() or str(
        Path("outputs/mvp_demo_data")
    )
    remote_data_dir = str(payload.get("remote_fastq_dir") or "").strip()
    samples_block = {
        "source": source,
        "local_data_dir": local_data_dir,
        # remote_path 模式：FASTQ 已存在于该远端目录，不再本地上传。
        "remote_data_dir": remote_data_dir or "AUTO",
        "items": items,
    }
    if source == "remote_path" and remote_data_dir:
        samples_block["remote_prestaged"] = True

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
            "scheduler": payload.get("scheduler") or "local",
            "threads": int(payload.get("threads") or 8),
            "memory_gb": int(payload.get("memory_gb") or 32),
            "shell": "bash",
            "init_commands": [],
        },
        "reference": {
            "name": "GENCODE_R47_GRCh38p14_ALL",
            "remote_gtf_path": str(payload.get("gtf") or "/ref/gencode.v47.gtf"),
            "remote_genome_fasta_path": str(payload.get("genome_fasta") or "/ref/GRCh38.fa"),
            "star_index_dir": str(payload.get("star_index") or "/ref/star"),
            "rsem_index_prefix": str(payload.get("rsem_prefix") or "/ref/rsem"),
        },
        "sequencing": {
            "layout": str(payload.get("layout") or "paired"),
            "reads_per_sample_million": 40,
            "strandedness": "auto",
        },
        "samples": samples_block,
        "pipeline": {
            "fastp": {"enabled": True, "version": "0.24.1"},
            "star": {"enabled": True, "version": "2.7.11b"},
            "arriba": {"enabled": True, "version": "2.5.0"},
            "featurecounts": {"enabled": True, "version": "Subread 2.1.1"},
            "rsem": {"enabled": True, "version": "1.2.28"},
        },
        # Web chat may optionally connect to an OpenAI-compatible endpoint.
        # Stored in project.json only; never uploaded to the remote workdir.
        "llm": {
            "enabled": False,
            "provider": "",
            "api_base": "",
            "model": "",
            "api_key": "",
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
