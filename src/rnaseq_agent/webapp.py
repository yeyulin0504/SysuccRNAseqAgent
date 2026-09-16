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

import json
import secrets
import shlex
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
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
from .webchat import ChatIntent, execute_intent, route_intent
from .remote_transport import create_remote_transport, test_server_connection
from .project_intake import (
    append_history,
    derive_visible_state,
    history_items,
    load_intake,
    save_intake,
)
from .sample_detection import (
    detect_fastq_pairs,
    preview_expression_matrix,
    read_counts_samples,
)
from .container_service import (
    ContainerSettingsError,
    build_pull_command,
    build_test_command,
    validate_image_settings,
)
from .connection_store import (
    apply_connection_to_config,
    apply_llm_to_config,
    llm_model_name,
    load_connection,
    load_llm,
    save_connection,
    save_llm,
)
from .ssh_auth import get_ssh_credential, normalize_auth_mode, set_ssh_credential
from .workspace import Workspace, WorkspaceError

STATIC_DIR = Path(__file__).resolve().parent / "webstatic"
TEMPLATES_DIR = Path(__file__).resolve().parent / "webtemplates"


def _default_workspace_dir() -> Path:
    return Path("runs/workspace")


#: 项目报告回灌给模型时的截断长度。整份报告可能上千行，全塞进对话会把模型的
#: 上下文挤爆，反而让它答不好。截断处如实标注，模型知道自己看到的是节选。
_REPORT_EXCERPT_LIMIT = 4000


def _session_for(project_dir: Path) -> ProjectSession:
    session = ProjectSession(project_dir)
    session.load_session()
    # 任何会话入口都先恢复全局凭据：密码只存在用户级配置里（DPAPI 加密），
    # 进程重启后内存凭据为空，不在这里补齐就会出现「每次进来都要重填密码」。
    _restore_runtime_credential()
    # 用户级共享连接与大模型：所有项目共用同一套配置（配一次即可）。仅在
    # 项目已有配置时在内存中覆盖，不回写磁盘，避免污染项目自身记录。
    if session.config is not None:
        session.config = apply_connection_to_config(session.config, load_connection())
        session.config = apply_llm_to_config(session.config, load_llm())
    return session


def _shared_llm_config() -> dict[str, Any] | None:
    """全局大模型配置包装成 ``{"llm": ...}``，供 LLM helper 直接消费。

    项目尚未创建（无 project.json）时，``session.config is None``；此时对话
    仍应走用户已配好的全局大模型，而不是回退成规则式答复。
    """
    shared = load_llm()
    if not shared.get("enabled") or not shared.get("api_key") or not shared.get("api_base"):
        return None
    return {"llm": shared}


def _connection_as_config() -> dict[str, Any] | None:
    """Wrap the shared connection as a minimal ``{"server": ...}`` config.

    ``create_remote_transport`` expects a config dict with a ``server`` block.
    The user-level connection is a flat field map, so adapt it here; return
    None when no usable host is configured.

    顺带把凭据恢复进运行时存储：调用方拿到 config 后经常直接建 transport，
    若这里不注入，`create_remote_transport` 会因为「本次程序中没有临时密码」
    直接失败，用户就不得不反复重填密码。
    """
    shared = load_connection()
    if not shared.get("host"):
        return None
    _restore_runtime_credential(shared)
    from copy import deepcopy

    return {"server": deepcopy(shared)}


def _restore_runtime_credential(connection: dict[str, Any] | None = None) -> dict[str, Any]:
    """把全局连接配置里的凭据重新注入运行时存储。

    密码只存在全局配置（DPAPI 加密）里，进程重启后内存凭据会丢失。任何
    需要连接的服务端入口先调这里，用户便不必每次重新输入密码。
    """
    stored = connection if connection is not None else load_connection()
    host = str(stored.get("host") or "").strip()
    user = str(stored.get("user") or "").strip()
    if not host or not user:
        return stored
    mode = normalize_auth_mode(str(stored.get("auth_mode") or "key"))
    password = str(stored.get("password") or "")
    existing = get_ssh_credential(host, user)
    # 已有更新的内存凭据时不覆盖（用户刚在当前进程里填过）。
    if existing.mode != "key" and existing.password:
        return stored
    if mode == "password" and not password:
        return stored
    set_ssh_credential(host, user, mode=mode, password=password)
    return stored


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
    # 大模型配置也是用户级共享的：项目里缺失的字段用全局配置补齐，设置页
    # 与后端看到同一套 provider / api_base / model 与「key 已保存」状态。
    config = apply_llm_to_config(config, load_llm())
    llm = config.get("llm", {})
    # 连接信息是用户级共享的：项目里缺失的字段用全局连接补齐，UI 与后端
    # 逻辑才能看到同一套 host / 目录，不必在每个项目里重复填写。
    shared = load_connection()
    merged_server = apply_connection_to_config(config, shared).get("server", {})
    host = str(merged_server.get("host", ""))
    user = str(merged_server.get("user", ""))
    auth_mode = get_ssh_credential(host, user).mode
    # 进程重启后内存凭据丢失，依次回退到全局连接配置、再回退到项目内
    # 持久化的认证方式，避免 UI 把已选中的「密码」静默显示成「SSH 密钥」。
    if auth_mode == "key":
        shared_auth_mode = str(shared.get("auth_mode") or "")
        if shared_auth_mode:
            auth_mode = normalize_auth_mode(shared_auth_mode)
        elif config.get("server", {}).get("auth_mode"):
            auth_mode = normalize_auth_mode(str(config["server"]["auth_mode"]))
    return {
        "server": {
            "profile": merged_server.get("profile", ""),
            "host": host,
            "user": user,
            "port": merged_server.get("port", 22),
            "auth_mode": auth_mode,
            "remote_base_dir": merged_server.get("remote_base_dir", ""),
            "remote_workdir": merged_server.get("remote_workdir", ""),
            "scheduler": merged_server.get("scheduler", "local"),
            "threads": merged_server.get("threads", 8),
            "memory_gb": merged_server.get("memory_gb", 32),
        },
        "llm": {
            "enabled": bool(llm.get("enabled")),
            "provider": llm.get("provider", ""),
            "api_base": llm.get("api_base", ""),
            "model": llm.get("model", ""),
            "api_key_set": bool(llm.get("api_key")),
        },
        "container": {
            "enabled": bool(config.get("container", {}).get("enabled")),
            "engine": config.get("container", {}).get("engine", "apptainer"),
            "image_uri": config.get("container", {}).get("image_uri", ""),
            "image_path": config.get("container", {}).get("image_path", ""),
            "bind_paths": list(config.get("container", {}).get("bind_paths", [])),
        },
    }


def _connection_error_hint(exc: Exception) -> str:
    """Turn a connection exception into a one-line, user-actionable message.

    ``RuntimeError`` carries the SSH stderr (e.g. ``Permission denied``) but
    also multi-line scaffolding from ``run_command``; surface the essential
    reason instead of only the exception class name so the settings page
    shows *why* the connection failed.
    """
    text = str(exc).strip() or type(exc).__name__
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        lowered = line.lower()
        if "permission denied" in lowered:
            return "认证被拒绝（Permission denied）。请确认用户名/密码正确，或该账号已分配登录节点权限。"
        if "authentication failed" in lowered:
            return "密码认证失败（Authentication failed）：服务器已接受该方法但拒绝了这组凭据。请核对用户名与密码；若密码含首尾空格，请确认原样输入。"
        if "临时密码" in line:
            return line
        if "connection refused" in lowered:
            return f"无法建立连接（Connection refused）。请确认主机与端口可达：{line}"
        if "timed out" in lowered or "timeout" in lowered:
            return f"连接超时。请确认主机可达、端口正确：{line}"
        if "no route to host" in lowered or "could not resolve" in lowered:
            return f"主机不可达或域名无法解析：{line}"
        if "host key verification failed" in lowered:
            return "服务器主机指纹未被信任，请先在终端手动 ssh 一次并接受指纹。"
    return lines[0][:300]


def _llm_messages(text: str) -> list[dict[str, str]]:
    """The system + user turn for the **fallback** plain-text LLM path.

    这条通道只在对话图不可用（未装 langgraph、图构建失败）时使用，模型确实
    没有可调用的工具——所以措辞要如实：不要声称写盘，交给确定性代码。
    正常路径的提示词在 ``chat_graph.SYSTEM_PROMPT``（那里模型是有工具的）。
    """
    return [
        {
            "role": "system",
            "content": (
                "你是 SYSU 多组学分析 Agent 的前端助手，用户正在配置一个 "
                "bulk RNA-seq 分析项目。\n"
                "\n"
                "重要约束：这条通道里你没有可调用的工具，也看不到文件系统。"
                "绝对不要声称你已经「保存了配置」「写入了文件」「修改了参数」"
                "或「完成了分析」——这些只有系统的确定性代码才能做。"
                "当用户给你路径、样本或参考基因组信息时，"
                "只做归纳与确认（例如列出识别到的样本和参考文件），"
                "然后明确提示用户：说一句「你帮我执行」即可，"
                "系统会真实写入并把结果（含门禁）回报给他。\n"
                "\n"
                "涉及可执行操作时，简短回复并提示可继续点「生成执行计划 / "
                "确认并冻结契约 / 启用差异表达」。"
            ),
        },
        {"role": "user", "content": text},
    ]


def _llm_endpoint(config: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return ``(url, api_key, model)`` when a usable endpoint is configured."""
    llm = config.get("llm", {})
    if not llm.get("enabled") or not llm.get("api_key") or not llm.get("api_base"):
        return None
    api_base = str(llm.get("api_base", "")).rstrip("/")
    model = str(llm.get("model", "")).strip() or "gpt-4o-mini"
    return f"{api_base}/chat/completions", str(llm["api_key"]), model


def _llm_reply_or_none(config: dict[str, Any], text: str, timeout: float = 30.0) -> str | None:
    """Call the configured OpenAI-compatible chat endpoint.

    Returns the assistant reply text, or None when LLM is not enabled /
    not configured / the call fails — the caller falls back to the local
    rule router so the chat panel keeps working without a model.
    """
    endpoint = _llm_endpoint(config)
    if endpoint is None:
        return None
    url, api_key, model = endpoint
    try:
        import requests

        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": _llm_messages(text), "temperature": 0.2},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return str(data["choices"][0]["message"]["content"]).strip()
    except Exception:  # noqa: BLE001 - any failure falls back to the rule router
        return None


def _llm_stream_chunks(config: dict[str, Any], text: str, timeout: float = 60.0):
    """Yield the assistant reply in pieces so the UI can render as it arrives.

    Prefers the endpoint's own ``stream=true`` SSE (real token streaming);
    when that is unavailable the full reply is yielded as a single piece,
    which still lets the caller keep one uniform code path. An empty
    sequence means "no model reply" so the caller falls back to the rules.
    """
    endpoint = _llm_endpoint(config)
    if endpoint is None:
        return
    url, api_key, model = endpoint
    streamed = False
    try:
        import requests

        with requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": _llm_messages(text), "temperature": 0.2, "stream": True},
            timeout=timeout,
            stream=True,
        ) as resp:
            if resp.status_code == 200:
                for raw in resp.iter_lines(decode_unicode=True):
                    if not raw:
                        continue
                    line = raw.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    piece = (choices[0].get("delta") or {}).get("content")
                    if piece:
                        streamed = True
                        yield piece
    except Exception:  # noqa: BLE001 - fall through to the non-streaming call
        streamed = False
    if streamed:
        return
    reply = _llm_reply_or_none(config, text, timeout=timeout)
    if reply:
        yield reply


def _scan_remote_samples(config: dict[str, Any], remote_dir: str) -> dict[str, Any]:
    """Read a shallow FASTQ listing through the configured SSH transport."""
    if not remote_dir.startswith("/") or any(ch in remote_dir for ch in ("\n", "\r", "\x00")):
        return {"ok": False, "message": "远程样本目录必须是绝对 POSIX 路径"}
    try:
        transport = create_remote_transport(config)
        command = (
            "find " + shlex.quote(remote_dir)
            + " -maxdepth 2 -type f \\( -name '*.fastq.gz' -o -name '*.fq.gz'"
            + " -o -name '*.fastq' -o -name '*.fq' \\) -print"
        )
        result = transport.execute(command)
        if result.returncode != 0:
            return {"ok": False, "message": "远程目录扫描失败"}
        detected = detect_fastq_pairs(
            [line.strip() for line in result.stdout.splitlines() if line.strip()]
        )
        return {"ok": True, "scanned_path": remote_dir, **detected}
    except Exception as exc:  # noqa: BLE001 - sanitized response
        return {"ok": False, "message": f"远程目录扫描失败：{_connection_error_hint(exc)}"}
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

    def _route_statuses() -> dict[str, dict[str, object]]:
        """Wizard route cards: which analysis routes are runnable today."""
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

    def _create_counts_direct_session(
        project_dir: Path,
        project_id: str,
        counts_path: Path,
        samples: list[dict[str, Any]],
        *,
        enabled_diffexp: bool,
        reference_condition: str = "",
        enabled_cms: bool = False,
        cancer_type: str = "",
        design: str = "",
    ) -> tuple[ProjectSession, dict[str, Any]]:
        """Build a counts 直入 analysis session for an uploaded matrix.

        Shared by the legacy ``POST /api/projects/{id}/counts`` multipart entry
        and the wizard ``POST /api/projects/{id}/counts/session`` JSON entry:
        both disable the FASTQ pipeline and leave only the conditional
        downstream stages (diffexp now, CMS later) enabled.
        """
        uploads_dir = counts_path.parent
        base = _default_config(project_dir, {"project_id": project_id})
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
                "cancer_type": cancer_type or "pan_cancer",
                "design": design or "independent_two_group",
            }
        )
        if enabled_diffexp:
            base["diffexp"] = {
                "reference_condition": reference_condition,
                "formula": "~ condition",
                "min_replicates_per_group": 3,
            }

        session = ProjectSession(project_dir)
        gate = session.new_project(base)
        workspace.touch(project_id, session.state)
        return session, {"state": session.state, "gate": gate.formatted()}

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

    @app.get("/chat", response_class=HTMLResponse)
    async def chat_page(request: Request):
        """全屏对话页（Codex 风格）：大片消息流 + 思考过程 + 流式输出。

        ``?project=<id>`` 绑定工作区项目；``?thread=<id>`` 可直达某个对话。
        未提供或未注册时页面照常渲染，由前端列出项目/对话供选择。
        """
        project_id = str(request.query_params.get("project") or "").strip()
        if project_id and workspace.get_project(project_id) is None:
            project_id = ""
        thread_id = str(request.query_params.get("thread") or "").strip()
        return templates.TemplateResponse(
            request,
            "chat.html",
            {"token": token, "project_id": project_id, "thread_id": thread_id},
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

    @app.get("/new-analysis", response_class=HTMLResponse)
    async def new_analysis(request: Request):
        # 参考 OncoOmics 新建分析向导：能力卡片（Expression/Fusion/Splicing
        # 及 counts 直入）在第 1 步选择，数据位置/样本/参考/计划逐步收尾。
        # 页面本身是静态向导壳；提交走既有 /api/new、/api/plan、/api/confirm
        # 与 /api/projects/{id}/counts，故此处只渲染空模板 + token。
        # The project home already contains the tested new-analysis form.
        # Reuse it here so both entry URLs stay functional during the MVP.
        return templates.TemplateResponse(
            request,
            "index.html",
            {"token": token, "workspace_root": str(workspace.root)},
        )

    @app.get("/api/overview")
    async def api_overview(request: Request):
        """Capability + connection overview for the app shell.

        Provides the data the header badges and the New Analysis step 1 need:

        - ``capabilities``: which RNA-seq analyses this build can actually run
          today (expression = yes; fusion = Arriba calls + artifact, review by
          human; splicing = deferred, STAR ``SJ.out.tab`` preserved as input).
        - ``server``: connection state of the configured scheduler target so
          the header can show ``院内 Slurm 已连接``-style state without a full
          probe blocking page render.

        The probe is best-effort: any failure reports ``unreachable`` instead
        of raising, and ``local`` skips probing entirely.
        """
        _guard(request)
        default_session = _session_for(legacy_project_dir)
        config = _editable_config(default_session.config) if default_session.config else None
        server_state = "unconfigured"
        scheduler = ""
        host = ""
        if config and config.get("server"):
            server = config["server"]
            scheduler = str(server.get("scheduler") or "local")
            host = str(server.get("host") or "")
            if scheduler == "local" or host in {"", "localhost", "127.0.0.1"}:
                server_state = "local"
            else:
                try:
                    result = test_server_connection(default_session.config)
                    server_state = "connected" if result.returncode == 0 else "unreachable"
                except Exception:  # noqa: BLE001 - badge must never block
                    server_state = "unreachable"
        llm_enabled = bool(config and config.get("llm", {}).get("enabled"))
        return {
            "capabilities": [
                {
                    "id": "expression",
                    "label": "Expression",
                    "status": "available",
                    "note": "表达定量（featureCounts counts + RSEM TPM）",
                },
                {
                    "id": "fusion",
                    "label": "Fusion",
                    "status": "available",
                    "note": "Arriba 融合调用与产物（人工审阅）",
                },
                {
                    "id": "splicing",
                    "label": "Splicing",
                    "status": "deferred",
                    "note": "差异剪接排期；已保留 STAR SJ.out.tab 作为输入",
                },
                {
                    "id": "counts",
                    "label": "Counts 直入",
                    "status": "available",
                    "note": "上传矩阵直跑差异表达 / CMS 分型",
                },
            ],
            "server": {
                "state": server_state,
                "scheduler": scheduler,
                "host": host,
                "label": ("院内 " + scheduler.upper() + " 已连接") if server_state == "connected" else ("本地执行") if server_state == "local" else ("模型与服务器 → 未连接"),
            },
            "llm": {"enabled": llm_enabled},
        }

    # -- session API -----------------------------------------------------

    @app.post("/api/new")
    async def api_new(request: Request):
        _guard(request)
        payload = await request.json()
        project_dir = _legacy_dir_for(request, payload)
        if (project_dir / "session.json").is_file():
            return {"error": "项目已存在，请先打开或删除。"}
        config = _default_config(project_dir, payload)
        # 新项目继承用户级共享连接（配一次、所有项目共用）。
        config = apply_connection_to_config(config, load_connection())
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
        # 先恢复全局连接凭据：进程重启后密码仍在，用户无需重填。
        _restore_runtime_credential()
        session = _session_for(_legacy_dir_for(request))
        if session.config is None:
            # 项目尚未创建：仍回显全局连接与大模型，设置页不会显示成空白。
            shared = load_connection()
            shared_llm = load_llm()
            if not shared and not shared_llm:
                return {"config": None}
            return {"config": _editable_config({"server": shared, "llm": shared_llm})}
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
            allowed = {"host", "user", "port", "scheduler", "threads", "memory_gb", "remote_base_dir", "remote_workdir", "auth_mode"}
            server_patch = {k: v for k, v in server.items() if k in allowed and v is not None}
            if server_patch:
                # threads / memory_gb arrive as numbers from the form.
                if "threads" in server_patch:
                    server_patch["threads"] = int(server_patch["threads"])
                if "memory_gb" in server_patch:
                    server_patch["memory_gb"] = int(server_patch["memory_gb"])
                if "port" in server_patch:
                    server_patch["port"] = int(server_patch["port"])
                if "auth_mode" in server_patch:
                    # 前端按钮值为 'pass'：落盘前归一化为规范的 'password'。
                    server_patch["auth_mode"] = normalize_auth_mode(server_patch["auth_mode"])
                patch["server"] = server_patch
                note_parts.append("服务器配置：" + ", ".join(f"{k}={v}" for k, v in server_patch.items()))
            host = str(server.get("host") or (session.config or {}).get("server", {}).get("host", ""))
            user = str(server.get("user") or (session.config or {}).get("server", {}).get("user", ""))
            if host and user and (server.get("auth_mode") or server.get("password")):
                new_password = str(server.get("password") or "")
                requested_mode = normalize_auth_mode(str(server.get("auth_mode") or "key"))
                # 提供了密码即视为密码模式；否则沿用表单所选模式。
                mode = "password" if (new_password or requested_mode == "password") else requested_mode
                if mode == "password" and not new_password:
                    # 重保存时密码框留空：沿用内存中/全局配置里已有的密码。
                    new_password = (
                        get_ssh_credential(host, user).password
                        or str(load_connection().get("password") or "")
                    )
                set_ssh_credential(host, user, mode=mode, password=new_password)

                # 连接信息是用户级、跨项目共用的：同步写入全局配置并持久化
                # （密码经 DPAPI 加密），这样新建项目自动继承、重启后仍生效。
                shared_values = {k: v for k, v in server_patch.items() if k != "password"}
                if mode == "password" and new_password:
                    shared_values["password"] = new_password
                elif mode != "password":
                    shared_values["password"] = ""
                save_connection(shared_values)

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
                # 大模型接入同样是用户级、跨项目共用的：同步写入全局配置并
                # 持久化（API Key 经 DPAPI 加密），这样任何项目、重启后都生效。
                save_llm(
                    {k: v for k, v in llm_patch.items() if k in ("enabled", "provider", "api_base", "model", "api_key")}
                )

        container = payload.get("container")
        if isinstance(container, dict):
            try:
                container_patch = validate_image_settings(container)
            except ContainerSettingsError as exc:
                return {"error": str(exc)}
            patch["container"] = container_patch
            note_parts.append("下游容器配置")

        if not patch:
            return {"error": "没有可保存的字段。"}
        if session.config is not None:
            changed = any(
                not isinstance(values, dict)
                or not isinstance(session.config.get(section), dict)
                or any(session.config[section].get(key) != value for key, value in values.items())
                for section, values in patch.items()
            )
            if not changed:
                return {
                    "state": session.state,
                    "unchanged": True,
                    "config": _editable_config(session.config),
                }
        try:
            if session.config is None:
                base = _default_config(session.project_dir, {
                    "project_id": "connection_settings",
                    "title": "连接配置",
                })
                for section, values in patch.items():
                    if isinstance(values, dict) and isinstance(base.get(section), dict):
                        base[section].update(values)
                    else:
                        base[section] = values
                gate = session.new_project(base)
            else:
                gate = session.edit(patch, note=" / ".join(note_parts))
            return {
                "state": session.state,
                "gate": gate.formatted(),
                "config": _editable_config(session.config) if session.config is not None else None,
            }
        except SessionError as exc:
            return {"error": str(exc)}

    @app.post("/api/config/demo")
    async def api_config_demo(request: Request):
        _guard(request)
        session = _session_for(_legacy_dir_for(request))
        demo_patch = {
            "server": {
                "host": "10.30.24.1", "user": "yeyulin", "port": 22,
                "scheduler": "slurm", "remote_base_dir": "/hwdata/home/yeyulin/",
                "remote_workdir": "/hwdata/home/yeyulin/",
            },
            "llm": {
                "enabled": True, "provider": "paratera",
                "api_base": "https://llmapi.paratera.com/v1",
                "model": "Deepseek-V4-Flash",
            },
        }
        try:
            if session.config is None:
                base = _default_config(session.project_dir, {
                    "project_id": "demo_connection",
                    "title": "演示连接配置",
                })
                base["server"].update(demo_patch["server"])
                base["llm"].update(demo_patch["llm"])
                session.new_project(base)
            else:
                session.edit(demo_patch, note="加载真实连接演示配置")
        except SessionError as exc:
            return {"error": str(exc)}
        return {"state": session.state, "config": _editable_config(session.config or {})}

    @app.post("/api/test-server")
    async def api_test_server(request: Request):
        _guard(request)
        session = _session_for(_legacy_dir_for(request))
        if session.config is None:
            return {"ok": False, "message": "请先创建项目并保存服务器配置"}
        try:
            result = test_server_connection(session.config)
            ok = result.returncode == 0 and "RNASEQ_AGENT_SSH_OK" in result.stdout
            return {"ok": ok, "message": "SSH 连接成功" if ok else "SSH 连接失败"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"SSH 连接失败：{_connection_error_hint(exc)}"}

    @app.post("/api/container/pull")
    async def api_container_pull(request: Request):
        _guard(request)
        session = _session_for(_legacy_dir_for(request))
        if session.config is None:
            return {"ok": False, "message": "请先保存服务器和容器配置"}
        try:
            command = build_pull_command(session.config.get("container", {}))
            result = create_remote_transport(session.config).execute(command)
            return {
                "ok": result.returncode == 0,
                "message": "Apptainer 镜像拉取成功" if result.returncode == 0 else "Apptainer 镜像拉取失败",
            }
        except (ContainerSettingsError, Exception) as exc:  # noqa: BLE001
            return {"ok": False, "message": f"Apptainer 镜像拉取失败：{type(exc).__name__}"}

    @app.post("/api/container/test")
    async def api_container_test(request: Request):
        _guard(request)
        session = _session_for(_legacy_dir_for(request))
        if session.config is None:
            return {"ok": False, "message": "请先保存服务器和容器配置"}
        try:
            command = build_test_command(session.config.get("container", {}))
            result = create_remote_transport(session.config).execute(command)
            ok = result.returncode == 0 and "RNASEQ_DOWNSTREAM_OK" in result.stdout
            return {"ok": ok, "message": "下游容器测试成功" if ok else "下游容器测试失败"}
        except (ContainerSettingsError, Exception) as exc:  # noqa: BLE001
            return {"ok": False, "message": f"下游容器测试失败：{type(exc).__name__}"}

    def _configured_llm(request: Request) -> dict[str, Any] | None:
        session = _session_for(_legacy_dir_for(request))
        # _session_for 已把全局大模型合并进项目配置；项目尚未创建时直接
        # 用全局配置，设置页与「拉取模型 / 测试连接」在无项目时也可用。
        if session.config is None:
            return load_llm() or None
        return (session.config or {}).get("llm")

    @app.get("/api/llm/models")
    async def api_llm_models(request: Request):
        _guard(request)
        llm = _configured_llm(request) or {}
        if not llm.get("api_base") or not llm.get("api_key"):
            return {"ok": False, "message": "请先保存 API Base 和 API Key", "models": []}
        try:
            import requests as http_requests
            response = http_requests.get(
                str(llm["api_base"]).rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {llm['api_key']}"}, timeout=20,
            )
            response.raise_for_status()
            models = sorted({str(item.get("id")) for item in response.json().get("data", []) if item.get("id")})
            return {"ok": True, "models": models}
        except http_requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", "unknown")
            return {"ok": False, "message": f"模型列表拉取失败：HTTP {status}", "models": []}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"模型列表拉取失败：{type(exc).__name__}", "models": []}

    @app.post("/api/test-llm")
    async def api_test_llm(request: Request):
        _guard(request)
        llm = _configured_llm(request) or {}
        if not llm.get("api_base") or not llm.get("api_key") or not llm.get("model"):
            return {"ok": False, "message": "请先保存 API Base、API Key 和模型"}
        try:
            import requests as http_requests
            response = http_requests.post(
                str(llm["api_base"]).rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {llm['api_key']}", "Content-Type": "application/json"},
                json={"model": llm["model"], "messages": [{"role": "user", "content": "请只回复：连接正常"}], "temperature": 0},
                timeout=30,
            )
            response.raise_for_status()
            reply = str(response.json()["choices"][0]["message"]["content"]).strip()
            return {"ok": True, "message": "LLM API 连接成功", "reply": reply}
        except http_requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", "unknown")
            return {"ok": False, "message": f"LLM API 连接失败：HTTP {status}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"LLM API 连接失败：{type(exc).__name__}"}

    @app.post("/api/samples/counts-preview")
    async def api_counts_preview(request: Request):
        _guard(request)
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            return {"ok": False, "message": "请选择 counts 文件"}
        try:
            return {"ok": True, "samples": read_counts_samples(await upload.read())}
        except (UnicodeError, ValueError, IndexError) as exc:
            return {"ok": False, "message": str(exc)}

    @app.post("/api/samples/scan-remote")
    async def api_scan_remote_samples(request: Request):
        _guard(request)
        payload = await request.json()
        remote_dir = str(payload.get("path") or "").strip()
        session = _session_for(_legacy_dir_for(request, payload))
        scan_config = session.config or _connection_as_config()
        if not remote_dir or scan_config is None:
            return {"ok": False, "message": "请先保存服务器配置并填写远程 FASTQ 目录"}
        return _scan_remote_samples(scan_config, remote_dir)

    def _configure_project_from_chat(
        project_id: str | None,
        session: ProjectSession,
        history_text: list[str],
    ) -> dict[str, Any]:
        """把**对话文本**里的配置真正写入项目（正则兜底路径）。

        只负责「从自然语言里抽取」，抽完交给 ``_write_project_session`` 落盘。
        有 LLM 时走的是 ``_tool_write_project_config``——模型直接给出结构化参数，
        无需抽取，但两条路最终落到同一个写盘函数，所以结果与审计完全一致。

        ``history_text`` 是**当前对话的全部用户消息**（按时间顺序）。只取单条
        是不够的：用户经常第一条消息贴 FASTQ 路径、第二条补参考基因组、第三条
        才说「你帮我执行」——配置信息必须从整个对话里汇总。
        """
        from .config_intake import extract_config_draft

        if project_id is None:
            return {
                "reply": (
                    "还没有绑定项目，所以无法写入配置。\n"
                    "请先在左侧选择一个项目（或新建项目），再把路径和样本发我，我来写入。"
                ),
                "state": session.state,
            }

        draft = extract_config_draft("\n".join(history_text))
        if not draft.ready:
            lines = ["我还没法写入配置，缺这些信息："]
            lines.extend(f"  - {item}" for item in draft.blockers)
            if draft.warnings:
                lines.append("同时提醒：")
                lines.extend(f"  - {item}" for item in draft.warnings)
            return {"reply": "\n".join(lines), "state": session.state}

        return _write_project_session(
            project_id,
            session,
            {**draft.to_payload(), "project_id": project_id},
            warnings=draft.warnings,
        )

    def _write_project_session(
        project_id: str,
        session: ProjectSession,
        payload: dict[str, Any],
        *,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        """Write a project session to disk. **The single write path.**

        正则抽取路径（``_configure_project_from_chat``）与 LLM 工具路径
        （``_run_tool`` 的 ``write_project_config``）都走这里，所以「对话写盘」
        「按钮写盘」「模型写盘」三者产生的结果没有任何差别，审计记录一致。

        ``payload`` 的形状与 ``POST /api/projects/{id}/fastq/session`` 一致。
        """
        samples = list(payload.get("samples") or [])
        layout = str(payload.get("layout") or "paired")
        data_source = str(payload.get("data_source") or "remote_path")
        fastq_dir = str(payload.get("fastq_dir") or payload.get("remote_fastq_dir") or "")

        # 已有会话：不覆盖。与 fastq/session 的 SESSION_EXISTS 语义一致，
        # 避免反复把用户已经确认过的样本设计冲掉。
        if (session.project_dir / "session.json").is_file():
            return {
                "ok": False,
                "reply": (
                    "这个项目已经有分析会话了，我没有重复写入（避免覆盖你已确认的样本设计）。\n"
                    "如果确实要用新配置，请先在工作台删除/回滚该项目，或换一个项目。"
                ),
                "state": session.state,
            }

        try:
            save_intake(
                session.project_dir,
                {
                    "route": "bulk_rna",
                    "input_type": (
                        "remote_fastq" if data_source == "remote_path" else "local_fastq"
                    ),
                    "state": "input_ready",
                    "fastq": {
                        "data_source": data_source,
                        "remote_fastq_dir": fastq_dir if data_source == "remote_path" else "",
                        "fastq_dir": fastq_dir if data_source != "remote_path" else "",
                    },
                    "samples": samples,
                },
            )
            config = _default_config(session.project_dir, payload)
            # 新项目继承用户级共享连接（与 /api/new 一致）。
            config = apply_connection_to_config(config, load_connection())
            created = ProjectSession(session.project_dir)
            gate = created.new_project(config)
            workspace.touch(project_id, created.state)
            append_history(
                session.project_dir,
                {
                    "type": "sample_design",
                    "name": "样本分组",
                    "state": created.state,
                    "details": {"count": len(samples), "layout": layout},
                },
            )
        except (SessionError, OSError, ValueError) as exc:
            return {"ok": False, "reply": f"写入配置时出错：{exc}", "state": session.state}

        lines = [
            f"已写入项目 {project_id}（{len(samples)} 个样本，{layout} 布局）。"
        ]
        lines.append("样本与分组：")
        lines.extend(
            f"  {index}. {sample.get('sample_id')} → {sample.get('condition') or '未分组'}"
            for index, sample in enumerate(samples, start=1)
        )
        lines.append(f"链特异性：{payload.get('strandedness') or 'auto'}")
        if warnings:
            lines.append("需要你知道：")
            lines.extend(f"  - {item}" for item in warnings)
        lines.extend(gate.formatted())
        lines.append("下一步：对我说「生成执行计划」。")
        return {
            "ok": True,
            "reply": "\n".join(lines),
            "state": created.state,
            "gate": gate.formatted(),
            "action": "configure_project",
            "samples": samples,
        }

    def _fallback_reply(config: dict[str, Any] | None) -> str:
        """Rule-router hint when the message is neither an action nor answerable."""
        capabilities = ", ".join(
            c.capability_id
            for c in __import__(
                "rnaseq_agent.capability", fromlist=["list_capabilities"]
            ).list_capabilities()
        )
        return (
            "我还没理解成可执行操作。可以试试："
            + (
                "生成计划 / 确认 / 把线程改成 16 / 关闭 arriba / 回滚 / 状态；"
                "或者把 FASTQ 路径与样本贴出来再说「你帮我执行」，我会写入项目。"
                if config is not None
                else "先选一个项目，把 FASTQ 路径与样本贴出来再说「你帮我执行」。"
            )
            + f"\n当前能力：{capabilities}"
        )

    def _browse_samples_reply(
        session: ProjectSession,
        intent: Any,
        config: dict[str, Any] | None,
        connection_config: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Read-only remote FASTQ discovery, shared by both chat paths."""
        browse_config = config or connection_config
        if browse_config is None:
            return {"reply": "请先保存服务器连接配置。", "state": session.state}
        remote_dir = str(
            intent.params.get("path")
            or browse_config.get("server", {}).get("remote_workdir")
            or ""
        ).strip()
        if not remote_dir:
            return {"reply": "请告诉我要浏览的绝对目录，或先保存远程工作目录。", "state": session.state}
        result = _scan_remote_samples(browse_config, remote_dir)
        if not result.get("ok"):
            return {"reply": result.get("message", "远程目录扫描失败"), "state": session.state}
        count = len(result["samples"])
        return {
            **result,
            "action": "browse_samples",
            "state": session.state,
            "scanned_path": remote_dir,
            "reply": f"已只读扫描 {remote_dir}，识别到 {count} 个配对样本。请检查后再应用到样本表。",
        }

    def _execute_chat_intent(session: ProjectSession, intent: Any) -> dict[str, Any]:
        """Run a deterministic session action and normalise its reply text."""
        result = execute_intent(session, intent)
        if "error" in result:
            result["reply"] = f"操作未完成：{result['error']}"
        elif "reply" not in result:
            result["reply"] = intent.message
        return result

    def _chat_history_messages(project_dir: Path, thread_id: str) -> list[dict[str, Any]]:
        """Prior turns of this thread as OpenAI messages, for graph continuity.

        只取 ``user`` / ``agent`` 两类并映射成 ``user`` / ``assistant``。工具调用
        与工具结果由**图自己的 checkpoint** 保存，不从这里重建——否则会把历史
        当成「模型说过的话」，污染上下文。
        """
        history: list[dict[str, Any]] = []
        try:
            for message in messages(project_dir, thread_id):
                role = message.get("role")
                content = str(message.get("content") or "")
                if not content:
                    continue
                if role == "user":
                    history.append({"role": "user", "content": content})
                elif role == "agent":
                    history.append({"role": "assistant", "content": content})
        except (ThreadError, OSError):
            return []
        return history

    def _drive_chat_graph(
        project_id: str,
        project_dir: Path,
        thread_id: str,
        text: str,
        llm_config: dict[str, Any],
        outcome: dict[str, Any],
        *,
        resume: dict[str, Any] | None = None,
    ):
        """Run one conversational turn through the tool-calling graph.

        Yields ``(event, data)`` pairs for streaming progress only (``delta`` /
        ``step`` / ``confirm``); the caller encodes them as SSE, keeps the step
        ledger for persistence, and emits the final ``done`` event itself.
        结果写入 ``outcome``（reply / via / awaiting_confirmation / confirmation /
        tool_log），把「生成器 yield」与「返回值」两件事分开，避免闭包赋值。

        ``resume`` 非空时用 ``Command(resume=...)`` 恢复被 ``interrupt`` 挂起的
        线程：此时**不再传入新的用户输入**，图会从守卫节点继续往下走到执行节点。
        恢复必须带 checkpointer，所以此处与普通回合共用同一个 SqliteSaver。

        抛异常表示图不可用（缺 langgraph、构建失败等），调用方退回规则路由——
        与项目其它可选依赖的处理方式一致。
        """
        from .agent_graph import sqlite_checkpointer_for
        from .chat_graph import build_chat_graph, chat_thread_config

        config = chat_thread_config(project_id, thread_id)
        with sqlite_checkpointer_for(project_dir) as checkpointer:
            graph = build_chat_graph(
                executor=_run_tool,
                llm_config=llm_config,
                checkpointer=checkpointer,
                # 确认卡片要写「旧值 → 新值」，所以守卫节点需要读项目配置。
                # 这个回调只读、不写，符合 guardrail「无副作用」的约束。
                config_reader=_read_project_config,
            )
            if resume is not None:
                from langgraph.types import Command

                graph_input: Any = Command(resume=resume)
            else:
                snapshot = graph.get_state(config)
                seeded = list((snapshot.values or {}).get("messages") or [])
                if seeded:
                    # 图自己的 checkpoint 已存了完整消息（含工具调用与工具结果），
                    # 只追加本轮用户输入，不重复灌入 thread 历史。
                    graph_input = {
                        "messages": [{"role": "user", "content": text}],
                        "iterations": 0,
                    }
                else:
                    graph_input = {
                        "project_dir": str(project_dir),
                        "project_id": project_id,
                        "thread_id": thread_id,
                        "messages": [
                            *_chat_history_messages(project_dir, thread_id),
                            {"role": "user", "content": text},
                        ],
                        "iterations": 0,
                        "tool_log": [],
                    }

            reply_parts: list[str] = []
            tool_log: list[dict[str, Any]] = []
            interrupted: dict[str, Any] | None = None

            for mode, chunk in graph.stream(
                graph_input, config=config, stream_mode=["custom", "updates"]
            ):
                if mode == "custom":
                    if isinstance(chunk, dict) and chunk.get("type") == "delta":
                        reply_parts.append(str(chunk.get("text") or ""))
                        yield "delta", {"text": chunk.get("text")}
                    continue

                if "__interrupt__" in chunk:
                    raw = chunk["__interrupt__"]
                    first = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
                    interrupted = getattr(first, "value", first)
                    continue

                for node, values in chunk.items():
                    if not isinstance(values, dict):
                        continue
                    if values.get("tool_log"):
                        tool_log = list(values["tool_log"])
                    # 模型请求的工具现在先落在 deferred_calls（由守卫按确认策略
                    # 一组组取出），所以思考面板要读这个字段，不再读 pending_calls。
                    requested = values.get("deferred_calls")
                    if node == "agent" and requested:
                        names = "、".join(
                            str(call.get("name")) for call in requested
                        )
                        yield "step", {
                            "id": "tool",
                            "label": f"模型请求工具：{names}",
                            "status": "running",
                            "detail": "",
                        }

            if interrupted:
                # 挂起：确认卡片交给前端，等 /api/chat/resume。
                labels = "；".join(
                    str(call.get("label")) for call in (interrupted.get("calls") or [])
                )
                yield "step", {
                    "id": "confirm",
                    "label": "等待你确认",
                    "status": "running",
                    "detail": labels,
                }
                yield "confirm", interrupted
                outcome.update(
                    {
                        "reply": str(interrupted.get("message") or ""),
                        "via": "tool",
                        "status": "ok",
                        "awaiting_confirmation": True,
                        "confirmation": interrupted,
                        "tool_log": tool_log,
                    }
                )
                return

            final_state = graph.get_state(config).values or {}
            outcome.update(
                {
                    "reply": str(final_state.get("reply") or "".join(reply_parts) or ""),
                    "via": str(final_state.get("via") or "llm"),
                    "streamed": bool(final_state.get("streamed")),
                    "tool_log": tool_log,
                    # 透出终态，调用方据此判断模型是否报错、要不要退回规则路由。
                    "status": str(final_state.get("status") or "ok"),
                    "error": str(final_state.get("error") or ""),
                }
            )

    def _read_project_config(project_dir: Path) -> dict[str, Any]:
        """Read a project's config for confirmation-card rendering. **只读**。

        它跑在对话图的守卫节点里（``interrupt`` 之前），所以绝不能触发写盘、
        也不能抛异常——读不到就返回空 dict，卡片上如实写「（未设置）」。
        """
        try:
            config_path = Path(project_dir) / "project.json"
            if not config_path.is_file():
                return {}
            from .storage import load_json

            payload = load_json(config_path)
            return payload if isinstance(payload, dict) else {}
        except Exception:  # noqa: BLE001 - card rendering must never break a turn
            return {}

    def _run_tool(
        name: str,
        arguments: dict[str, Any],
        project_dir: Path,
        approved: bool,
    ) -> dict[str, Any]:
        """Execute one LLM-requested tool against the audited session.

        这是对话图与真实系统之间**唯一**的桥。每个工具都必须映射到既有的确定性
        实现，绝不允许模型自己拼路径、拼命令、或者绕过门禁：

        - ``read_project_state``  → 只读 ``ProjectSession``；
        - ``browse_remote_samples`` → 复用 ``_scan_remote_samples``（只读 SSH）；
        - ``refresh_project_status`` / ``get_project_report`` → 复用
          ``session.refresh_status`` / ``session.report``（只读）；
        - ``write_project_config``  → 复用 ``_write_project_session``（与按钮同一条链路）；
        - ``edit_samples`` / ``edit_reference`` / ``edit_connection``
          / ``configure_pipeline`` / ``set_run_resources`` / ``set_diffexp_reference``
          / ``set_cms_options`` / ``rollback_changes`` → 复用 ``session.edit`` / ``rollback``；
        - ``generate_plan`` / ``confirm_contract`` → 复用 ``session.plan`` / ``confirm``；
        - ``run_analysis`` → 复用 ``session.execute``（带 stage 时走 ``execute_stage``）；
        - ``record_qc_decision`` → 复用 ``session.record_qc_decision``。

        ``approved`` 由图上的人工确认关卡给出。写盘/执行类工具在没有批准时一律
        拒绝——这是纵深防御：即使图的守卫被绕过，这里仍然拦得住。
        """
        from .agent_tools import (
            REFERENCE_FIELDS,
            RISK_READ,
            normalize_write_arguments,
            risk_of,
        )

        session = _session_for(project_dir)
        # Workspace 里项目目录就是 root / project_id，所以目录名即 project_id。
        # 旧版单项目模式（legacy_project_dir）下取目录名同样是合理标识。
        project_id = project_dir.name

        if risk_of(name) != RISK_READ and not approved:
            return {
                "ok": False,
                "error": "这个操作会修改项目，但还没有得到你的确认，所以没有执行。",
            }

        if name == "read_project_state":
            config = session.config
            if config is None:
                return {
                    "ok": True,
                    "reply": "这个项目还没有分析会话（缺 project.json），所以还没有样本表。",
                    "state": session.state,
                    "has_session": False,
                }
            samples = config.get("samples", {}).get("items", [])
            payload = {
                "ok": True,
                "state": session.state,
                "has_session": True,
                "layout": config.get("sequencing", {}).get("layout"),
                "strandedness": config.get("sequencing", {}).get("strandedness"),
                "reference": config.get("reference", {}),
                "pipeline": {
                    step: bool(value.get("enabled"))
                    for step, value in (config.get("pipeline") or {}).items()
                    if isinstance(value, dict)
                },
                "samples": [
                    {
                        "sample_id": sample.get("sample_id"),
                        "condition": sample.get("condition"),
                        "fastq_1": sample.get("fastq_1"),
                        "fastq_2": sample.get("fastq_2"),
                    }
                    for sample in samples
                ],
            }
            conditions = sorted(
                {str(s.get("condition") or "") for s in samples} - {""}
            )
            payload["reply"] = (
                f"当前状态 {session.state}，{len(samples)} 个样本，"
                f"分组 {conditions or '（无）'}，"
                f"链特异性 {payload['strandedness'] or '未设置'}。"
            )
            return payload

        if name == "browse_remote_samples":
            remote_dir = str(arguments.get("path") or "").strip()
            scan_config = session.config or _connection_as_config()
            if scan_config is None:
                return {"ok": False, "error": "请先保存服务器连接配置。"}
            result = _scan_remote_samples(scan_config, remote_dir)
            if not result.get("ok"):
                return {"ok": False, "error": result.get("message", "远程目录扫描失败")}
            count = len(result.get("samples") or [])
            return {
                **result,
                "ok": True,
                "scanned_path": remote_dir,
                "reply": f"已只读扫描 {remote_dir}，识别到 {count} 个配对样本。",
            }

        if name == "refresh_project_status":
            if session.config is None:
                return {
                    "ok": False,
                    "error": "这个项目还没有分析会话，没有可刷新的运行状态。",
                }
            status = session.refresh_status()
            state = str(status.get("state") or session.state)
            message = str(status.get("message") or "").strip()
            return {
                "ok": True,
                "state": session.state,
                "run_state": state,
                "status": status,
                "reply": f"最新运行状态：{state}。" + (f"\n{message}" if message else ""),
            }

        if name == "get_project_report":
            if session.config is None:
                return {"ok": False, "error": "这个项目还没有分析会话，没有结果可汇总。"}
            path = session.report()
            try:
                text = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                return {"ok": False, "error": f"报告已生成但读取失败：{exc}"}
            # 报告可能很长：截断后回灌，避免把模型的上下文挤爆。
            excerpt = text if len(text) <= _REPORT_EXCERPT_LIMIT else text[:_REPORT_EXCERPT_LIMIT] + "\n…（已截断）"
            return {
                "ok": True,
                "state": session.state,
                "report_path": str(path),
                "report": excerpt,
                "reply": f"已生成项目报告：{path}\n{excerpt}",
            }

        if name == "write_project_config":
            normalized = normalize_write_arguments(arguments)
            payload = {
                "data_source": normalized.get("data_source") or "remote_path",
                "samples": normalized.get("samples") or [],
                "strandedness": normalized.get("strandedness") or "unknown",
                "layout": (
                    "paired"
                    if any(s.get("fastq_2") for s in normalized.get("samples") or [])
                    else "single"
                ),
                "project_id": project_id,
            }
            directory = str(normalized.get("fastq_dir") or "")
            if payload["data_source"] == "remote_path":
                payload["remote_fastq_dir"] = directory
            else:
                payload["fastq_dir"] = directory
            for key in ("gtf", "genome_fasta", "star_index", "rsem_prefix"):
                if normalized.get(key):
                    payload[key] = normalized[key]
            return _write_project_session(project_id, session, payload)

        if name == "configure_pipeline":
            patch = {"pipeline": {arguments["step"]: {"enabled": bool(arguments["enabled"])}}}
            verb = "启用" if arguments["enabled"] else "关闭"
            result = execute_intent(
                session,
                ChatIntent(
                    "edit",
                    message=f"已{verb} {arguments['step']}。",
                    params=patch,
                    note=f"模型修改：{verb} {arguments['step']}",
                ),
            )
            return _tool_result_from_intent(result)

        if name == "set_run_resources":
            server: dict[str, Any] = {}
            parts = []
            if arguments.get("threads") is not None:
                server["threads"] = int(arguments["threads"])
                parts.append(f"线程数 → {arguments['threads']}")
            if arguments.get("memory_gb") is not None:
                server["memory_gb"] = int(arguments["memory_gb"])
                parts.append(f"内存 → {arguments['memory_gb']} GB")
            result = execute_intent(
                session,
                ChatIntent(
                    "edit",
                    message=f"已修改：{'，'.join(parts)}。",
                    params={"server": server},
                    note=f"模型修改：{'，'.join(parts)}",
                ),
            )
            return _tool_result_from_intent(result)

        if name == "set_diffexp_reference":
            reference = str(arguments["reference_condition"]).strip()
            result = execute_intent(
                session,
                ChatIntent(
                    "edit",
                    message=f"已启用差异表达，对照组为 {reference}。",
                    params={
                        "pipeline": {"diffexp": {"enabled": True}},
                        "diffexp": {"reference_condition": reference},
                    },
                    note=f"模型修改：diffexp reference={reference}",
                ),
            )
            return _tool_result_from_intent(result)

        if name == "edit_samples":
            return _edit_samples_tool(session, arguments)

        if name == "edit_reference":
            if session.config is None:
                return {
                    "ok": False,
                    "error": "这个项目还没有分析会话，请先用 write_project_config 建会话。",
                }
            reference = {
                config_key: str(arguments[key]).strip()
                for key, config_key in REFERENCE_FIELDS.items()
                if arguments.get(key)
            }
            if not reference:
                return {"ok": False, "error": "没有给出要修改的参考基因组字段。"}
            parts = "，".join(f"{k}={v}" for k, v in reference.items())
            result = execute_intent(
                session,
                ChatIntent(
                    "edit",
                    message=f"已修改参考基因组：{parts}。",
                    params={"reference": reference},
                    note=f"模型修改：reference {parts}",
                ),
            )
            return _tool_result_from_intent(result)

        if name == "edit_connection":
            return _edit_connection_tool(project_id, session, arguments)

        if name == "set_cms_options":
            return _set_cms_options_tool(session, arguments)

        if name == "record_qc_decision":
            if session.config is None:
                return {"ok": False, "error": "这个项目还没有分析会话。"}
            try:
                decision = session.record_qc_decision(
                    approved=bool(arguments.get("approved")),
                    note=str(arguments.get("note") or ""),
                )
            except SessionError as exc:
                return {"ok": False, "error": str(exc), "state": session.state}
            verdict = "通过，继续下游" if decision["approved"] else "不通过，停在这里"
            return {
                "ok": True,
                "state": session.state,
                "qc": decision,
                "reply": f"已记录 QC 检查点决定：{verdict}。",
            }

        if name == "rollback_changes":
            result = execute_intent(session, ChatIntent("rollback", message="已回滚最后一次变更。"))
            return _tool_result_from_intent(result)

        if name == "generate_plan":
            result = execute_intent(
                session, ChatIntent("plan", message="正在生成执行计划。")
            )
            return _tool_result_from_intent(result)

        if name == "confirm_contract":
            result = execute_intent(
                session, ChatIntent("confirm", message="正在确认并冻结契约。")
            )
            return _tool_result_from_intent(result)

        if name == "run_analysis":
            stage = str(arguments.get("stage") or "").strip()
            if stage:
                # 分阶段执行：必须已冻结契约（execute_stage 自己会校验状态）。
                if session.config is None:
                    return {"ok": False, "error": "这个项目还没有分析会话。"}
                try:
                    outcome = session.execute_stage(stage, wait=False)
                except SessionError as exc:
                    return {"ok": False, "error": str(exc), "state": session.state}
                return {
                    "ok": True,
                    "state": session.state,
                    "stage": stage,
                    "result": outcome,
                    "reply": (
                        f"{stage} 阶段已启动（状态 {outcome.get('state', session.state)}）。"
                        f"\n{outcome.get('message') or ''}".rstrip()
                    ),
                }
            result = execute_intent(session, ChatIntent("run", message="正在启动分析。"))
            return _tool_result_from_intent(result)

        return {"ok": False, "error": f"未知工具：{name}"}

    def _edit_samples_tool(
        session: ProjectSession,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge/remove samples on an **existing** session.

        这是 ``write_project_config`` 的补位：那个会因为「已有会话」被拒，模型就
        再也改不了单个样本。这里按 ``sample_id`` 合并，未提到的样本原样保留——
        用户说「把 S3 分到 treat 组」时不该把 S1/S2 的分组冲掉。
        """
        if session.config is None:
            return {
                "ok": False,
                "error": (
                    "这个项目还没有分析会话，请先用 write_project_config 把样本表和"
                    "FASTQ 目录写进去。"
                ),
            }

        current_items = [
            sample
            for sample in (session.config.get("samples", {}).get("items") or [])
            if isinstance(sample, dict)
        ]
        by_id = {str(sample.get("sample_id") or ""): dict(sample) for sample in current_items}
        order = [str(sample.get("sample_id") or "") for sample in current_items]

        patches = arguments.get("samples") or []
        remove = [str(item) for item in (arguments.get("remove") or [])]

        for patch in patches:
            if not isinstance(patch, dict):
                continue
            sample_id = str(patch.get("sample_id") or "")
            if not sample_id:
                continue
            existing = by_id.get(sample_id, {})
            merged = dict(existing)
            for key in ("condition", "fastq_1", "fastq_2"):
                if patch.get(key) not in (None, ""):
                    merged[key] = patch[key]
            merged["sample_id"] = sample_id
            if sample_id not in by_id:
                order.append(sample_id)
            by_id[sample_id] = merged

        for sample_id in remove:
            if sample_id in by_id:
                del by_id[sample_id]
                order = [item for item in order if item != sample_id]

        if not order:
            return {"ok": False, "error": "改动后样本表会变成空的，已拒绝。"}

        updated = [by_id[sample_id] for sample_id in order]
        removed_actual = [sample_id for sample_id in remove if sample_id not in by_id]
        summary = f"样本表已更新：现有 {len(updated)} 个样本。"
        if removed_actual:
            summary += f"已删除 {', '.join(removed_actual)}。"

        result = execute_intent(
            session,
            ChatIntent(
                "edit",
                message=summary,
                params={"samples.items": updated},
                note=f"模型修改：样本表（{len(updated)} 个样本）",
            ),
        )
        return _tool_result_from_intent(result)

    def _edit_connection_tool(
        project_id: str,
        session: ProjectSession,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Edit the connection block, reusing the same normalisation as ``/api/config``.

        连接信息是用户级共享的：项目里改完要同步进全局配置，否则下一个项目还是
        旧主机。密码绝不在这里落盘——模型看不到也不该设置凭据。
        """
        from .agent_tools import CONNECTION_FIELDS

        server_patch: dict[str, Any] = {}
        for key in CONNECTION_FIELDS:
            value = arguments.get(key)
            if value is None:
                continue
            if key in {"port", "threads", "memory_gb"}:
                server_patch[key] = int(value)
            else:
                server_patch[key] = str(value).strip()

        if not server_patch:
            return {"ok": False, "error": "没有给出要修改的连接字段。"}

        if "scheduler" in server_patch:
            from .agent_tools import SCHEDULERS

            if server_patch["scheduler"] not in SCHEDULERS:
                return {
                    "ok": False,
                    "error": f"scheduler 必须是 {SCHEDULERS} 之一。",
                }

        if session.config is None:
            # 还没有项目：连接配置写在全局共享层，新建项目时自动继承。
            save_connection(server_patch)
            return {
                "ok": True,
                "state": session.state,
                "reply": (
                    "已保存服务器连接配置（当前项目还没有分析会话，"
                    "配置已存为全局默认，新建项目会自动继承）。"
                ),
            }

        parts = "，".join(f"{k}={v}" for k, v in server_patch.items())
        result = execute_intent(
            session,
            ChatIntent(
                "edit",
                message=f"已修改服务器连接：{parts}。",
                params={"server": server_patch},
                note=f"模型修改：server {parts}",
            ),
        )
        normalized = _tool_result_from_intent(result)
        if normalized.get("ok"):
            # 与 /api/config 一致：连接是用户级共享的，改完同步到全局，
            # 下一个项目才继承得到。凭据不在这里碰（模型不设置密码）。
            save_connection({k: v for k, v in server_patch.items() if k != "auth_mode"})
        return normalized

    def _set_cms_options_tool(
        session: ProjectSession,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Enable/disable CMS and set its permutation/FDR knobs."""
        from .agent_tools import CMS_RUN_MODES

        if session.config is None:
            return {
                "ok": False,
                "error": "这个项目还没有分析会话，请先建会话再配置 CMS。",
            }

        enabled = bool(arguments.get("enabled"))
        patch: dict[str, Any] = {"pipeline": {"cms": {"enabled": enabled}}}
        cms_patch: dict[str, Any] = {}
        if arguments.get("n_perm") is not None:
            cms_patch["n_perm"] = int(arguments["n_perm"])
        if arguments.get("fdr") is not None:
            cms_patch["fdr"] = float(arguments["fdr"])
        run_mode = str(arguments.get("run_mode") or "").strip()
        if run_mode:
            if run_mode not in CMS_RUN_MODES:
                return {"ok": False, "error": f"run_mode 必须是 {CMS_RUN_MODES} 之一。"}
            cms_patch["run_mode"] = run_mode
        if cms_patch:
            patch["cms"] = cms_patch

        verb = "启用" if enabled else "关闭"
        result = execute_intent(
            session,
            ChatIntent(
                "edit",
                message=f"已{verb} CMS 分型。" + (f"（{cms_patch}）" if cms_patch else ""),
                params=patch,
                note=f"模型修改：CMS enabled={enabled}",
            ),
        )
        normalized = _tool_result_from_intent(result)
        if normalized.get("ok") and enabled:
            # 门禁不满足时如实告知，别让用户以为一开就能跑。
            from .cms import cms_design_checks

            reasons = cms_design_checks(session.config or {})
            if reasons:
                normalized["gate_warnings"] = reasons
                normalized["reply"] = (
                    str(normalized.get("reply") or "")
                    + "\n注意，CMS 门禁目前不满足：\n"
                    + "\n".join(f"  - {reason}" for reason in reasons)
                )
        return normalized

    def _tool_result_from_intent(result: dict[str, Any]) -> dict[str, Any]:
        """Normalise a session-action result into the tool-result shape.

        工具结果会整段回灌给模型（JSON），所以 ``ok`` 必须如实反映成功与否——
        ``execute_intent`` 用 ``error`` 键表达失败，这里翻译过去。
        """
        if result.get("error"):
            return {
                "ok": False,
                "error": str(result["error"]),
                "state": result.get("state"),
                "reply": result.get("reply") or str(result["error"]),
            }
        return {
            **result,
            "ok": True,
            "reply": result.get("reply") or "操作已完成。",
        }

    def _intent_label(intent: Any) -> str:
        """Human-readable name for the thinking panel."""
        labels = {
            "plan": "生成执行计划",
            "confirm": "确认并冻结契约",
            "edit": "修改分析参数",
            "rollback": "回滚变更",
            "status": "读取项目状态",
            "summary": "汇总项目摘要",
            "help": "说明可用操作",
            "new": "新建项目草稿",
            "deg_status": "检查差异表达门禁",
            "browse_samples": "浏览服务器目录",
        }
        action = getattr(intent, "action", "")
        return labels.get(action, action or "会话动作")

    def _draft_summary(history_text: list[str]) -> str:
        """One-line「我读到了什么」for the thinking panel, before any write."""
        from .config_intake import extract_config_draft

        draft = extract_config_draft("\n".join(history_text))
        if not draft.ready:
            return "；".join(draft.blockers) or "没读到可用配置"
        conditions = "、".join(
            f"{sample['sample_id']}→{sample.get('condition') or '未分组'}"
            for sample in draft.samples
        )
        return (
            f"{len(draft.samples)} 个样本（{conditions}）；"
            f"目录 {draft.fastq_dir}；链特异性 {draft.strandedness}"
        )

    def _conversation_text(project_dir: Path, thread_id: str, current: str) -> list[str]:
        """User turns of a thread (chronological) plus the message being sent now.

        配置信息常常分散在多轮消息里（先贴 FASTQ、再补参考基因组、最后说
        「你帮我执行」），所以抽取要基于**整个对话**而不是最后一句。
        只取 ``role == "user"``：Agent 自己归纳过的那段文字里也有路径，若不
        排除会把同一份信息读两遍，且会把 Agent 的措辞当成用户输入。
        """
        history: list[str] = []
        try:
            for message in messages(project_dir, thread_id):
                if message.get("role") == "user":
                    history.append(str(message.get("content") or ""))
        except (ThreadError, OSError):
            history = []
        history.append(current)
        return history

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
        project_id = _bound_project_id(request, payload)
        project_dir = session.project_dir
        thread_id = str(payload.get("thread_id") or "").strip()
        # 连接信息是用户级共享的：项目尚未创建（无 project.json）时，仍可用
        # 全局连接做只读的服务器操作，不再要求用户先建项目再保存配置。
        # load_connection() 是扁平的连接字段，包成 {"server": {...}} 以复用
        # 既有的 create_remote_transport(config) 契约。
        connection_config = _connection_as_config()

        # 工作台的这个非流式端点保持**确定性**：它没有确认卡片，而写盘/执行类
        # 工具在拿到人工批准前一律不执行（见 chat_graph.node_guardrail）。
        # 需要模型自主调工具的对话走 /api/chat/stream（chat.html 有批准入口）。
        intent = route_intent(text)
        if intent is not None and intent.action == "browse_samples":
            return _browse_samples_reply(session, intent, config, connection_config)

        # 落地配置走确定性写盘，并把真实结果（含门禁）回报给用户。
        if intent is not None and intent.action == "configure_project":
            return _configure_project_from_chat(
                project_id,
                session,
                _conversation_text(project_dir, thread_id or "main", text),
            )

        # 1) LLM path (when enabled and reachable).
        # 大模型配置是用户级共享的：项目里没有 project.json 时回退到全局
        # 配置，用户配一次之后任何对话都能拿到模型答复。
        llm_config = config if config is not None else _shared_llm_config()
        if llm_config is not None:
            llm_reply = _llm_reply_or_none(llm_config, text)
            if llm_reply:
                return {"state": session.state, "reply": llm_reply, "via": "llm"}

        # 2) Local rule router fallback.
        if intent is None:
            return {"reply": _fallback_reply(config), "state": session.state}

        return _execute_chat_intent(session, intent)

    @app.post("/api/chat/stream")
    async def api_chat_stream(request: Request):
        """SSE variant of ``/api/chat`` that exposes the thinking process.

        Emits ``step`` events (understand / context / llm / tool) before the
        answer, then the reply as ``delta`` chunks, and finally ``done``. The
        user turn and the agent answer (with its steps) are persisted to the
        target thread so the conversation survives a reload.

        Event framing::

            event: step
            data: {"id":"understand","label":"…","status":"done","detail":"…"}

            event: delta
            data: {"text":"…"}

            event: done
            data: {"state":"…","via":"llm","reply":"…","thread_id":"…"}
        """
        _guard(request)
        payload = await request.json()
        text = str(payload.get("message", "")).strip()

        def sse(event: str, data: dict[str, Any]) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        if not text:
            return StreamingResponse(
                iter([sse("error", {"message": "请输入想做的事。"})]),
                media_type="text/event-stream",
            )

        session = _session_for(_legacy_dir_for(request, payload))
        config = session.config
        connection_config = _connection_as_config()
        project_id = _bound_project_id(request, payload)
        project_dir = session.project_dir
        requested_thread = str(payload.get("thread_id") or "").strip()

        def run():
            steps: list[dict[str, Any]] = []

            def step(step_id: str, label: str, status: str, detail: str = "") -> str:
                record = {"id": step_id, "label": label, "status": status, "detail": detail}
                # 同名步骤后出现的状态覆盖前一条，前端据此原地更新。
                steps.append(record)
                return sse("step", record)

            # 1) 理解问题：把「系统理解成了什么」先摊开给用户看。
            yield step("understand", "理解你的问题", "running")
            intent = route_intent(text)
            intent_label = _intent_label(intent) if intent is not None else ""
            if intent is not None:
                yield step("understand", "理解你的问题", "done", f"识别为可执行操作：{intent_label}")
            else:
                yield step("understand", "理解你的问题", "done", "未匹配到可执行操作，按提问处理")

            # 2) 读取项目上下文：有没有项目、配没配模型，直接决定后面走哪条路。
            yield step("context", "读取项目上下文", "running")
            has_project = config is not None
            llm_config = config if has_project else _shared_llm_config()
            context_detail = "已载入项目配置" if has_project else "该项目还没有分析会话，使用全局配置"
            if llm_config is not None:
                model_name = llm_model_name(llm_config) or "未指定模型"
                context_detail += f"；大模型：{model_name}"
            else:
                context_detail += "；未配置大模型，使用规则路由"
            yield step("context", "读取项目上下文", "done", context_detail)

            via = "rule"
            reply = ""
            # 答复是否已经逐字推给前端（llm 流式）。True 时收尾不再整段重推。
            streamed_by_graph = False

            payload_extra: dict[str, Any] = {}
            # 写盘动作会让状态从 idle 变 drafting，收尾时用这个会话回报真实状态。
            active_session = session

            # 3) **模型优先**：思考与生成交给 LLM，它能真正调工具读写项目。
            #    这是 2026-09-16 用户诉求的核心——此前正则抢在模型前面，
            #    模型根本没有机会「有自己的执行工具」。
            #    模型不可用（未配置 / 接口失败 / 图不可用）时，下面退回正则兜底。
            llm_handled = False
            if llm_config is not None:
                model_name = llm_model_name(llm_config) or "未指定"
                yield step("llm", "调用大模型生成答复", "running", f"模型：{model_name}")
                outcome: dict[str, Any] = {}
                graph_thread = requested_thread or "main"
                graph_failed = False
                try:
                    for event, data in _drive_chat_graph(
                        project_id or project_dir.name,
                        project_dir,
                        graph_thread,
                        text,
                        llm_config,
                        outcome,
                    ):
                        if event == "step":
                            yield step(
                                data["id"], data["label"], data["status"], data["detail"]
                            )
                        else:
                            yield sse(event, data)
                except Exception as exc:  # noqa: BLE001 - fall back to the rule router
                    graph_failed = True
                    yield step(
                        "llm",
                        "调用大模型生成答复",
                        "failed",
                        f"工具循环不可用（{type(exc).__name__}），改用规则路由",
                    )

                # 成功判据是「图正常结束且拿到了答复」：模型接口报错时图也会
                # 把状态置成 llm_error 并返回空串，那种情况要走下面的兜底，
                # 否则用户只看到一句空回复。
                graph_ok = (
                    not graph_failed
                    and outcome.get("status") == "ok"
                    and bool(outcome.get("reply"))
                )
                if graph_ok:
                    llm_handled = True
                    via = str(outcome.get("via") or "llm")
                    reply = str(outcome.get("reply") or "")
                    # 挂起等确认时 ``_drive_chat_graph`` 已推过 confirm 步骤与卡片，
                    # 这里不再补「已生成」——那会让用户以为本轮已经结束了。
                    if not outcome.get("awaiting_confirmation"):
                        yield step(
                            "llm",
                            "调用大模型生成答复",
                            "done",
                            f"已生成 {len(reply)} 字"
                            + (
                                f"；用了 {len(outcome.get('tool_log') or [])} 个工具"
                                if outcome.get("tool_log")
                                else ""
                            ),
                        )
                    payload_extra.update(
                        {
                            k: outcome[k]
                            for k in ("awaiting_confirmation", "confirmation", "tool_log")
                            if k in outcome
                        }
                    )
                    # 模型流式输出过就不再整段重推，避免答复出现两遍。
                    # ``outcome["reply"]`` 已含兜底（终态 reply 为空时用累积的
                    # token 文本），这里不重复拼接。
                    streamed_by_graph = bool(via == "llm" and outcome.get("streamed"))
                    # 写盘类工具会改状态，收尾时用磁盘上的真实状态回报。
                    if outcome.get("tool_log"):
                        active_session = _session_for(project_dir)
                else:
                    # 图不可用：退回纯文本模型调用（仍然优于正则）。
                    chunks: list[str] = []
                    for piece in _llm_stream_chunks(llm_config, text):
                        if not piece:
                            continue
                        chunks.append(piece)
                        yield sse("delta", {"text": piece})
                    if chunks:
                        llm_handled = True
                        via = "llm"
                        reply = "".join(chunks)
                        streamed_by_graph = True
                        yield step("llm", "调用大模型生成答复", "done", f"已生成 {len(reply)} 字")
                    else:
                        yield step("llm", "调用大模型生成答复", "failed", "模型无响应，改用规则路由")

            # 4) 正则兜底：模型没给出答复时才轮到规则路由。
            #    保留这条路径有两个作用：模型未配置时不至于瘫掉；模型临时不可用时
            #    仍能完成「贴配置 → 写盘」这类确定性操作。
            if not llm_handled:
                if intent is not None and intent.action == "browse_samples":
                    yield step("tool", f"执行操作：{intent_label}", "running")
                    result = _browse_samples_reply(session, intent, config, connection_config)
                    reply = str(result.get("reply") or "")
                    yield step("tool", f"执行操作：{intent_label}", "done")
                    payload_extra = {
                        k: result[k]
                        for k in ("action", "samples", "unmatched", "scanned_path")
                        if k in result
                    }
                elif intent is not None and intent.action == "configure_project":
                    yield step("tool", "读取对话中的配置", "running")
                    chat_history = _conversation_text(
                        project_dir,
                        requested_thread or "main",
                        text,
                    )
                    draft_summary = _draft_summary(chat_history)
                    yield step("tool", "读取对话中的配置", "done", draft_summary)
                    yield step("tool", "写入项目配置", "running")
                    result = _configure_project_from_chat(project_id, session, chat_history)
                    via = "tool"
                    reply = str(result.get("reply") or "")
                    failed = "gate" not in result and "已写入" not in reply
                    yield step(
                        "tool",
                        "写入项目配置",
                        "failed" if failed else "done",
                        "" if not failed else reply.splitlines()[0] if reply else "",
                    )
                    payload_extra = {
                        k: result[k]
                        for k in ("gate", "action", "samples")
                        if k in result
                    }
                    # 写盘后状态从 idle 变成 drafting，前端要看到新状态。
                    active_session = _session_for(project_dir)
                elif intent is not None:
                    yield step("tool", f"执行操作：{intent_label}", "running")
                    result = _execute_chat_intent(session, intent)
                    via = "tool"
                    reply = str(result.get("reply") or "")
                    failed = bool(result.get("error"))
                    yield step(
                        "tool",
                        f"执行操作：{intent_label}",
                        "failed" if failed else "done",
                        str(result.get("error") or ""),
                    )
                    payload_extra = {
                        k: result[k]
                        for k in ("steps", "summary", "gate", "contract_id")
                        if k in result
                    }

            if not reply:
                reply = _fallback_reply(config)
            # 等待确认时确认消息已在卡片头部展示，这里再整段推一遍会覆盖卡片
            # （appendDelta 会重置 bubble 内容），所以跳过。
            awaiting = bool(payload_extra.get("awaiting_confirmation"))
            if not awaiting and (via != "llm" or not streamed_by_graph):
                # 规则/工具答复没有 token 流，整段推一次保持前端逻辑统一。
                yield sse("delta", {"text": reply})

            # 写盘动作会刷新会话，用它的真实状态收尾。
            state = active_session.state
            result: dict[str, Any] = {
                "state": state,
                "via": via,
                "reply": reply,
                **payload_extra,
            }

            # 4) 落盘：用户提问 + Agent 答复（含思考步骤），刷新后仍可回看。
            thread_id = requested_thread
            try:
                if project_id is not None:
                    if not thread_id:
                        existing = list_threads(project_dir)
                        thread_id = existing[0]["thread_id"] if existing else "main"
                    try:
                        get_thread(project_dir, thread_id)
                    except ThreadError:
                        create_thread(project_dir, thread_id, title="主分析流程")
                    append_message(project_dir, thread_id, role="user", content=text)
                    saved = append_message(
                        project_dir,
                        thread_id,
                        role="agent",
                        content=reply,
                        references={"via": via, "steps": steps},
                    )
                    result["thread_id"] = thread_id
                    result["message_id"] = saved["message_id"]
                    workspace.set_thread_count(project_id, len(list_threads(project_dir)))
            except (ThreadError, OSError):  # noqa: BLE001 - 落盘失败不该毁掉这次回答
                pass

            yield sse("done", result)

        return StreamingResponse(run(), media_type="text/event-stream")

    @app.post("/api/chat/resume")
    async def api_chat_resume(request: Request):
        """Resume a chat turn that is suspended on a tool confirmation.

        ``/api/chat/stream`` answers a write/execute tool call with a ``confirm``
        event and leaves the graph parked at its guardrail node. The UI renders
        that as an approval card; pressing 批准 / 拒绝 posts here and the graph
        continues — for an approval, straight into the execute node.

        Body::

            {"project_id": "...", "thread_id": "main",
             "approved": true, "note": "可选备注"}

        Same SSE framing as ``/api/chat/stream`` (``step`` / ``delta`` /
        ``confirm`` / ``done``). The user message was already persisted by the
        original turn, so only the agent answer is appended here.
        """
        _guard(request)
        payload = await request.json()
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            return {"error": "缺少 approved（true / false）。"}
        note = str(payload.get("note") or "").strip()

        def sse(event: str, data: dict[str, Any]) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        session = _session_for(_legacy_dir_for(request, payload))
        config = session.config
        project_id = _bound_project_id(request, payload)
        project_dir = session.project_dir
        thread_id = str(payload.get("thread_id") or "").strip() or "main"
        llm_config = config if config is not None else _shared_llm_config()

        def run():
            steps: list[dict[str, Any]] = []

            def step(step_id: str, label: str, status: str, detail: str = "") -> str:
                record = {"id": step_id, "label": label, "status": status, "detail": detail}
                steps.append(record)
                return sse("step", record)

            outcome: dict[str, Any] = {}
            if llm_config is None:
                yield step("confirm", "继续执行", "failed", "没有可用的大模型配置，无法恢复这次确认")
                yield sse(
                    "done",
                    {
                        "state": session.state,
                        "via": "error",
                        "reply": "没有可用的大模型配置，无法恢复这次确认，请重新发起请求。",
                        "thread_id": thread_id,
                    },
                )
                return

            yield step("confirm", "已收到你的决定", "done", "批准执行" if approved else "拒绝执行")
            try:
                for event, data in _drive_chat_graph(
                    project_id or project_dir.name,
                    project_dir,
                    thread_id,
                    "",
                    llm_config,
                    outcome,
                    resume={"approved": approved, "note": note},
                ):
                    if event == "step":
                        yield step(data["id"], data["label"], data["status"], data["detail"])
                    else:
                        yield sse(event, data)
            except Exception as exc:  # noqa: BLE001 - surfaced as a reply
                yield step("confirm", "继续执行", "failed", f"{type(exc).__name__}")
                yield sse(
                    "done",
                    {
                        "state": session.state,
                        "via": "error",
                        "reply": f"恢复执行失败（{type(exc).__name__}: {exc}）。",
                        "thread_id": thread_id,
                    },
                )
                return

            via = str(outcome.get("via") or ("tool" if approved else "rejected"))
            reply = str(outcome.get("reply") or "")
            # 恢复后又触发新的确认（模型换一种写法继续请求）时，``_drive_chat_graph``
            # 已经推过 confirm 步骤与卡片，这里不再重复，也不要覆盖卡片。
            awaiting = bool(outcome.get("awaiting_confirmation"))
            if not awaiting:
                if not reply:
                    reply = "已按你的决定处理。"
                if not outcome.get("streamed"):
                    yield sse("delta", {"text": reply})

            state = _session_for(project_dir).state
            result: dict[str, Any] = {
                "state": state,
                "via": via,
                "reply": reply,
                "thread_id": thread_id,
                **{
                    k: outcome[k]
                    for k in ("awaiting_confirmation", "confirmation", "tool_log")
                    if k in outcome
                },
            }

            try:
                if project_id is not None:
                    try:
                        get_thread(project_dir, thread_id)
                    except ThreadError:
                        create_thread(project_dir, thread_id, title="主分析流程")
                    saved = append_message(
                        project_dir,
                        thread_id,
                        role="agent",
                        content=reply,
                        references={"via": via, "steps": steps},
                    )
                    result["message_id"] = saved["message_id"]
            except (ThreadError, OSError):  # noqa: BLE001 - 落盘失败不该毁掉这次回答
                pass

            yield sse("done", result)

        return StreamingResponse(run(), media_type="text/event-stream")

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

    # -- project wizard API（项目级多组学向导）--------------------------

    @app.get("/api/projects/{project_id}/intake")
    async def api_project_intake(project_id: str, request: Request):
        """Project-scoped wizard state: setup progress + route cards + history."""
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        return {
            "project_id": project_id,
            "state": derive_visible_state(project_dir),
            "intake": load_intake(project_dir),
            "routes": _route_statuses(),
            "history": history_items(project_dir),
        }

    @app.post("/api/projects/{project_id}/route")
    async def api_project_route(project_id: str, request: Request):
        """Record the chosen analysis route in the project intake.

        Routes that are still reserved (available False) only record the
        intent — they never create an analysis session, so the project stays
        in the ``setup`` state until a runnable route's inputs arrive.
        """
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        payload = await request.json()
        route = str(payload.get("route") or "").strip()
        route_info = _route_statuses().get(route)
        if route_info is None:
            return {"error": "未知 route。"}
        save_intake(project_dir, {"route": route})
        return {
            "route": route,
            "available": route_info["available"],
            "state": "setup",
            "reason": route_info["reason"],
        }

    @app.post("/api/projects/{project_id}/counts/preview")
    async def api_project_counts_preview(project_id: str, request: Request):
        """Upload + preview a counts / expression matrix for the project wizard.

        Persists the file under ``uploads/<upload_id>_<safe_name>`` so a later
        ``POST .../counts/session`` can read the same bytes back, records the
        upload in the project intake / history, and returns the detection
        preview (matrix type, samples, DESeq2 eligibility).
        """
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            return {"error": "请选择 counts 文件。"}
        content = await upload.read()
        if not content:
            return {"error": "上传的文件为空。"}

        # -- persist the uploaded matrix --------------------------------
        import re

        uploads_dir = project_dir / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        upload_id = secrets.token_urlsafe(8)
        raw_name = Path(upload.filename or "matrix.tsv").name
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name).strip("._") or "matrix.tsv"
        stored_name = f"{upload_id}_{safe_name}"
        file_path = uploads_dir / stored_name
        file_path.write_bytes(content)

        # -- parse + record ---------------------------------------------
        preview = preview_expression_matrix(content, filename=safe_name)
        input_type = "counts_matrix" if preview["matrix_type"] == "raw_counts" else "expression_matrix"
        save_intake(
            project_dir,
            {
                "route": "bulk_rna",
                "input_type": input_type,
                "state": "input_ready",
                "matrix_preview": preview,
                "upload_id": upload_id,
                # counts/session 需要回读上传文件：存落盘文件名以定位
                # ``uploads/<stored_name>``。
                "upload_file": stored_name,
            },
        )
        append_history(
            project_dir,
            {
                "type": "input_matrix",
                "name": safe_name,
                "state": "ready",
                "details": {
                    "matrix_type": preview["matrix_type"],
                    "sample_count": preview.get("sample_count", 0),
                },
            },
        )
        return {
            "upload_id": upload_id,
            "preview": preview,
            "state": "input_ready",
            "history": history_items(project_dir),
        }

    @app.post("/api/projects/{project_id}/counts/session")
    async def api_project_counts_session(project_id: str, request: Request):
        """Turn a previously previewed matrix into a counts 直入 analysis session.

        Reads the stored preview intake (upload file + matrix type), validates
        that DESeq2 is only requested for raw counts, then reuses the counts
        直入 config builder shared with the legacy ``/api/projects/{id}/counts``
        endpoint so both entries produce identical sessions.
        """
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        payload = await request.json()
        upload_id = str(payload.get("upload_id") or "").strip()
        intake = load_intake(project_dir)
        matrix_preview = intake.get("matrix_preview")
        if not upload_id or upload_id != intake.get("upload_id") or not isinstance(matrix_preview, dict):
            return {"error_code": "NOT_FOUND", "message": "未找到该上传文件，请先预览矩阵。"}
        upload_file = intake.get("upload_file")
        uploads_dir = project_dir / "uploads"
        counts_path = (
            uploads_dir / str(upload_file)
            if upload_file
            else next(uploads_dir.glob(f"{upload_id}_*"), None)
        )
        if counts_path is None or not Path(counts_path).is_file():
            return {"error_code": "NOT_FOUND", "message": "未找到该上传文件，请先预览矩阵。"}
        counts_path = Path(counts_path)

        matrix_type = str(matrix_preview.get("matrix_type") or "")
        enabled_diffexp = bool(payload.get("enabled_diffexp"))
        if enabled_diffexp and matrix_type != "raw_counts":
            return {
                "error_code": "NOT_EVALUABLE",
                "message": "当前矩阵不是 raw counts，不能用于 DESeq2 raw counts 流程。",
            }
        if not enabled_diffexp:
            return {"error_code": "NOT_EVALUABLE", "message": "请至少启用差异表达（DE）。"}

        # -- build the counts 直入 session ------------------------------
        raw_samples = payload.get("samples") or []
        samples: list[dict[str, Any]] = []
        for raw in raw_samples:
            if not isinstance(raw, dict):
                continue
            sample_id = str(raw.get("sample_id") or "").strip()
            condition = str(raw.get("condition") or "").strip()
            if sample_id and condition:
                samples.append({"sample_id": sample_id, "condition": condition})

        # 建 session 前先做输入级校验：畸形 payload 在此被字段级错误拦截，
        # 而不是放进 helper 内部门禁、以 drafting 假成功返回。
        # 语义与 legacy POST /api/projects/{id}/counts 对齐（reference 显式必填）。
        if not samples:
            return {
                "error_code": "NOT_EVALUABLE",
                "message": "samples 至少需要 1 个非空的 sample_id/condition 样本。",
            }
        preview_sample_ids = [
            str(sample) for sample in (matrix_preview.get("samples") or []) if str(sample).strip()
        ]
        seen_sample_ids: set[str] = set()
        for sample in samples:
            sample_id = sample["sample_id"]
            if sample_id in seen_sample_ids:
                return {
                    "error_code": "NOT_EVALUABLE",
                    "message": f"重复的样本 sample_id：{sample_id}。",
                }
            seen_sample_ids.add(sample_id)
            if preview_sample_ids and sample_id not in preview_sample_ids:
                return {
                    "error_code": "NOT_EVALUABLE",
                    "message": f"样本 {sample_id} 不在上传矩阵检测到的样本列中，"
                    f"请基于预览结果填写样本：{preview_sample_ids}。",
                }

        reference_condition = str(payload.get("reference_condition") or "").strip()
        # Finding 1：enabled_diffexp 时 reference_condition 显式必填，且必须落在
        # 本批样本的 condition 集合中（与 helper 内部门禁 diffexp_design_checks
        # 同语义，但放在 helper 之前、避免把畸形输入写进 project.json）。
        if enabled_diffexp and not reference_condition:
            return {
                "error_code": "NOT_EVALUABLE",
                "message": "启用差异表达必须显式提供 reference_condition。",
            }
        condition_set = sorted({str(sample["condition"]) for sample in samples})
        if enabled_diffexp and reference_condition not in condition_set:
            return {
                "error_code": "NOT_EVALUABLE",
                "message": f"reference_condition={reference_condition!r} 不在样本 condition 中："
                f"{condition_set}。",
            }

        session, base_view = _create_counts_direct_session(
            project_dir,
            project_id,
            counts_path,
            samples,
            enabled_diffexp=True,
            reference_condition=reference_condition,
        )
        append_history(
            project_dir,
            {
                "type": "sample_design",
                "name": "样本分组",
                "state": session.state,
                "details": {"count": len(samples)},
            },
        )
        return {
            **base_view,
            "counts_path": str(counts_path),
            "history": history_items(project_dir),
        }

    # -- remote FASTQ 向导 session（bulk RNA 一级入口）------------------

    @app.post("/api/projects/{project_id}/fastq/session")
    async def api_project_fastq_session(project_id: str, request: Request):
        """Create a bulk-RNA FASTQ wizard session from wizard intake samples.

        Persists project intake (route=bulk_rna, input_type=remote_fastq or
        local_fastq, state=input_ready), then creates the real FASTQ analysis
        session via the same config builder used by ``/api/new`` so existing
        plan/confirm logic remains usable.
        """
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        payload = await request.json()

        data_source = str(payload.get("data_source") or "").strip()
        if data_source not in {"remote_path", "local_upload"}:
            return {"error_code": "INVALID_DATA_SOURCE", "message": "data_source 必须为 remote_path 或 local_upload。"}

        samples = payload.get("samples") or []
        # Finding：畸形 samples（非 list，或 list 含非 dict 元素）必须在解析前
        # 被拦截，与 counts/session 段（isinstance(dict) 防御）语义一致，否则
        # 会以 .get AttributeError 抛 500。
        if not isinstance(samples, list):
            return {"error_code": "INVALID_SAMPLES", "message": "samples 必须是样本对象列表。"}
        # 先剔除非 dict 元素，再做 sample_id/condition 双非空筛选；剔除后的
        # 纯 dict 列表回写 payload，供 _default_config 构建 project.json items。
        dict_samples = [s for s in samples if isinstance(s, dict)]
        valid_samples = [
            s for s in dict_samples
            if str(s.get("sample_id") or "").strip() and str(s.get("condition") or "").strip()
        ]
        if not valid_samples:
            return {"error_code": "EMPTY_SAMPLES", "message": "缺少有效样本设计（sample_id + condition）。"}
        payload = {**payload, "samples": dict_samples}

        # 重复提交短路：已有 session.json 时不再重跑 new_project（后者会抛
        # 状态机错误），对齐 legacy /api/new 的“项目已存在”语义。
        if (project_dir / "session.json").is_file():
            return {"error_code": "SESSION_EXISTS", "error": "项目已存在，请先打开或删除。"}

        # 1) persist intake first so the UI / history reads an input_ready state.
        intake = save_intake(
            project_dir,
            {
                "route": "bulk_rna",
                "input_type": "remote_fastq" if data_source == "remote_path" else "local_fastq",
                "state": "input_ready",
                "fastq": {
                    "data_source": data_source,
                    "remote_fastq_dir": str(payload.get("remote_fastq_dir") or "").strip(),
                    "fastq_dir": str(payload.get("fastq_dir") or "").strip(),
                },
                "samples": samples,  # keep the raw list for downstream sample design editing
            },
        )

        # 2) build the normal FASTQ project session (same path as /api/new?project=...).
        config = _default_config(project_dir, {**payload, "project_id": project_id})
        session = ProjectSession(project_dir)
        gate = session.new_project(config)
        workspace.touch(project_id, session.state)

        # 3) record a sample-design history item.
        append_history(
            project_dir,
            {
                "type": "sample_design",
                "name": "样本分组",
                "state": session.state,
                "details": {"count": len(valid_samples), "layout": str(payload.get("layout") or "paired")},
            },
        )

        return {
            "state": session.state,
            "gate": gate.formatted(),
            "intake": intake,
            "history": history_items(project_dir),
        }

    # -- 统一 command 路由（按钮 + 对话快速动作共用）-------------------

    @app.post("/api/projects/{project_id}/command")
    async def api_project_command(project_id: str, request: Request):
        """Route a wizard action (plan/confirm/set_route/scan_remote_fastq)
        against a registered project, returning its new state + history.

        Buttons and chat quick-actions call the same command endpoint so state
        transitions stay auditable and intake/history stay in sync.
        """
        _guard(request)
        project_dir = _project_dir_or_404(project_id)
        payload = await request.json()
        # Finding：非 dict JSON body（如 json=[1,2]）不得以 AttributeError 抛 500；
        # counts/session(:1277) 与 fastq/session(:1395) 同样存在此守卫缺口，
        # 本轮只修 command 端点，避免扩大改动面。
        if not isinstance(payload, dict):
            return {"error_code": "INVALID_REQUEST", "message": "请求体必须是 JSON 对象。"}
        command = str(payload.get("command") or "").strip()

        if command == "set_route":
            route = str(payload.get("route") or "").strip()
            info = _route_statuses().get(route)
            if info is None:
                return {"error_code": "UNKNOWN_ROUTE", "message": f"未知 route：{route}"}
            save_intake(project_dir, {"route": route})
            append_history(project_dir, {"type": "route", "name": str(info["label"]), "state": "ready",
                                         "details": {"available": bool(info["available"])}})
            return {"state": derive_visible_state(project_dir), "action": "set_route", "route": route,
                    "available": bool(info["available"]), "history": history_items(project_dir)}

        if command == "scan_remote_fastq":
            remote_dir = str(payload.get("remote_fastq_dir") or "").strip()
            if not remote_dir:
                return {"error_code": "NOT_EVALUABLE", "message": "远程样本目录不能为空。"}
            session = _session_for(project_dir)
            if session.config is None:
                return {"error_code": "NO_SESSION", "message": "项目还没有分析会话，请先创建输入会话。"}
            try:
                result = _scan_remote_samples(session.config, remote_dir)
            except SessionError as exc:
                return {"error_code": "NOT_EVALUABLE", "message": str(exc)}
            # Finding：_scan_remote_samples 对失败返回 {ok:False,...} 而不抛异常；
            # 只有 ok=True 才把 fastq_scan/ready 写进审计 history，失败必须透传
            # 给调用者，绝不把失败记为成功事实。
            if not result.get("ok"):
                return {**result, "error_code": "NOT_EVALUABLE",
                        "action": "scan_remote_fastq", "history": history_items(project_dir)}
            append_history(project_dir, {"type": "fastq_scan", "name": remote_dir or "remote_scan",
                                         "state": "ready", "details": {"sample_count": len(result.get("samples", []))}})
            return {**result, "action": "scan_remote_fastq", "history": history_items(project_dir)}

        # plan / confirm: drive the audited session state machine.
        session = _session_for(project_dir)
        if session.config is None:
            return {"error_code": "NO_SESSION", "message": "项目还没有分析会话，请先创建输入会话。"}
        try:
            if command == "plan":
                plan = session.plan()
                workspace.touch(project_id, session.state)
                append_history(project_dir, {"type": "analysis_plan", "name": "执行计划", "state": session.state,
                                             "details": {"step_count": len(plan.steps)}})
                return {"state": session.state, "action": "plan", "steps": plan.steps,
                        "summary": getattr(plan, "summary", None), "history": history_items(project_dir)}
            if command == "confirm":
                contract = session.confirm()
                workspace.touch(project_id, session.state)
                append_history(project_dir, {"type": "contract", "name": "契约", "state": session.state,
                                             "details": {"contract_id": contract["contract_id"]}})
                return {"state": session.state, "action": "confirm", "contract_id": contract["contract_id"],
                        "history": history_items(project_dir)}
        except SessionError as exc:
            return {"error_code": "NOT_EVALUABLE", "message": str(exc)}

        return {"error_code": "UNKNOWN_COMMAND", "message": "该动作不在允许列表中。"}

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

        # counts 直入固定参考路径由表单覆盖（用户可手填服务器路径）。
        reference_overrides: dict[str, str] = {}
        for key, form_key in (
            ("remote_gtf_path", "gtf"),
            ("remote_genome_fasta_path", "genome_fasta"),
            ("star_index_dir", "star_index"),
            ("rsem_index_prefix", "rsem_prefix"),
        ):
            value = str(form.get(form_key) or "").strip()
            if value:
                reference_overrides[key] = value

        session, base_view = _create_counts_direct_session(
            project_dir,
            project_id,
            counts_path,
            samples,
            enabled_diffexp=enabled_diffexp,
            reference_condition=reference,
            enabled_cms=enabled_cms,
            cancer_type=str(form.get("cancer_type") or "").strip(),
            design=str(form.get("design") or "").strip(),
        )
        if reference_overrides:
            from .actions import load_project_config
            from .storage import load_json, save_json

            config = load_json(session.config_path)
            config.setdefault("reference", {}).update(reference_overrides)
            save_json(session.config_path, config)
            session.config = load_project_config(session.config_path)
        return {
            **base_view,
            "counts_path": str(counts_path),
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


def _normalize_strandedness(value: Any) -> str:
    """Keep ``unknown`` as a first-class value instead of collapsing to ``auto``.

    featureCounts 只接受 ``-s 0/1/2``，但「还没确定」和「已确认无链特异性」是
    两件不同的事：计划里的注释要靠这个区分（见 ``pipeline._strand_comment``）。
    因此这里保留 ``unknown`` 原样落盘，只把未提供的值默认成 ``auto``。
    """
    text = str(value or "").strip().lower()
    if text in {"auto", "unknown", "unstranded", "forward", "reverse"}:
        return text
    return "auto"


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
            # 链特异性：允许 unknown 落盘（用户明确要求「允许未知的选项先写着」）。
            # 早期硬编码 "auto"，会把对话里说清楚的「未知」覆盖掉，计划里也就
            # 看不出 -s 0 是「未确定」而非「已确认无链特异性」。
            "strandedness": _normalize_strandedness(payload.get("strandedness")),
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
