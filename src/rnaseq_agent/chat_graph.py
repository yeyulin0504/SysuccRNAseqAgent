"""对话控制平面：带工具调用与人工确认的 LangGraph 图。

用户诉求（2026-09-16）：

> 我要修复的是大语言模型应该要有自己的执行工具啊
> 思考/生成交给 LLM，安全/审批交给代码（LangGraph）
> 风险度高的要给人确认
> 没有大模型时保留正则兜底

此前项目的 LLM **完全没有工具能力**，而且是明文禁止的（见 ``agent_tools`` 的说明）。
本模块把能力真正交给模型：模型自己决定调哪个工具、填什么参数；代码负责校验参数、
分级风险、在写盘/执行前把决定权交还给人。

为什么单独建一张图，而不是接进 ``agent_graph``：

- ``agent_graph`` 是**分析流水线**图（gate → plan → confirm → execute_qc → wait_qc
  → report），它的 checkpointer 用 ``thread_id == project_id``。对话是另一条链路，
  塞进去会撞同一个 checkpoint 键，把分析进度和聊天记录搅在一起。
- 本图的 thread 用 ``chat:{project_id}:{thread_id}``，与分析图完全隔离。

图结构::

    START → agent ──(调工具)──→ guardrail ──(需确认)──→ confirmation
              ↑                    │                         │ interrupt/resume
              │                    └──(只读,免确认)──┐       ↓
              └──── execute ←────────────────────────┴───────┘
                       │
                       └──(还有顺延的组)──→ guardrail

**关键约束：节点副作用**。``interrupt()`` 恢复时 LangGraph 会**从头重跑当前节点**。
因此 ``guardrail`` 只准备并持久化确认上下文，``confirmation`` 只读取该上下文并
interrupt；两者都无项目副作用，真实写盘只能发生在 ``execute``。把随机卡号放在
interrupt 节点或把写盘放在 interrupt 前，都会让重放改变授权或重复执行。

**确认粒度**（用户 2026-09-16 定的边界「配置合并、执行单独」）由
``agent_tools.split_calls_for_round`` 决定：``guardrail`` **每轮只处理一组**——
``batch``（配置类）合并成一张卡片，``solo``（执行类）各占一张卡片。其余组存进
``deferred_calls`` 顺延，``execute`` 之后不回到 ``agent`` 而是直接回到
``guardrail``，于是卡片按「先配置、后执行」依次弹出，用户逐个签字。

langgraph 缺失时本模块不可用（``build_chat_graph`` 抛错），调用方应退回规则路由，
与项目既有的「可选依赖」策略一致。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import secrets
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Callable, Iterator, TypedDict

try:
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without langgraph
    LANGGRAPH_AVAILABLE = False
    END = "END"  # type: ignore[assignment]
    START = "START"  # type: ignore[assignment]
    StateGraph = None  # type: ignore[assignment]

    def interrupt(value: Any) -> Any:  # type: ignore[misc]
        return value

    def get_stream_writer() -> Callable[[Any], None]:  # type: ignore[misc]
        return lambda _value: None


from .agent_tools import (
    CONNECTION_FIELDS,
    TOOL_MODE_DISABLED,
    POLICY_NEVER,
    POLICY_SOLO,
    ToolCall,
    ToolCallAccumulator,
    confirmation_policy,
    describe_call,
    normalize_write_arguments,
    parse_message_tool_calls,
    risk_of,
    split_calls_for_round,
    normalize_tool_mode,
    tool_allowed,
    tool_labels,
    tool_mode_block,
    tool_schemas,
    validate_call,
)
from .model_disclosure import PreparedModelRequest, ProviderCredentials, ProviderRequestError
from .model_provider import ModelProviderGateway, normalize_provider_config, provider_identity
from .model_context import (
    EphemeralToolCallStore,
    ModelContextBuilder,
    project_assistant_tool_call,
    project_tool_arguments_for_model,
    project_tool_result_for_model,
)

_EPHEMERAL_TOOL_CALL_STORE = EphemeralToolCallStore()

#: 一轮对话最多允许的工具往返次数，防止模型陷入自我循环。
MAX_TOOL_ITERATIONS = 8

#: ``ToolExecutor`` 契约：工具执行始终携带精确项目/线程上下文；结果分成
#: request-local、provider、generic-log 与 authoritative-audit 四个通道。
#: 结果至少含 ``{"ok": bool, "reply": str}``，可附带 ``gate`` / ``samples`` /
#: ``summary`` 等结构化字段供前端渲染。
#:
#: 执行器由调用方注入（依赖倒置）：本模块不 import ``webapp``（会造成循环依赖），
#: 也不自己构造 ``ProjectSession``——真实写盘链路只有一条，必须复用。
@dataclass(frozen=True)
class ToolExecutionContext:
    project_id: str
    thread_id: str | None


@dataclass(frozen=True)
class ToolExecutionResult:
    local: dict[str, Any]
    model: dict[str, Any]
    log_projection: dict[str, Any]
    security_audit: Any | None

    # Temporary compatibility for callers that inspected the old dict result.
    # New provider/checkpoint paths must use the named channels above.
    def __getitem__(self, key: str) -> Any:
        return self.local[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.local.get(key, default)


ToolExecutor = Callable[[str, dict[str, Any], Path, bool, ToolExecutionContext], ToolExecutionResult]
ToolResultFinalizer = Callable[[ToolExecutionResult, Path | None], ToolExecutionResult]
DisclosureRequester = Callable[[Path, str, str, dict[str, Any]], dict[str, Any]]
DisclosureDecider = Callable[[Path, str, bool], dict[str, Any]]

_LEGACY_MODEL_KEYS = frozenset({
    "ok", "blocked", "error", "error_code", "message", "reply", "state",
    "has_session", "layout", "strandedness", "pipeline", "status", "run_state",
    "action", "via", "confirmation_required", "tool_mode", "tool", "steps",
    "summary", "gate", "contract_id", "gate_ok", "gate_reasons", "gate_warnings",
    "warnings", "requested", "design", "stage", "result", "sample_count",
    "directory_count", "unmatched_count", "record_count",
})
_LEGACY_EXACT_KEYS = frozenset(
    {
        "samples", "sample_id", "fastq_1", "fastq_2", "path", "scanned_path",
        "remote_fastq_dir", "fastq_dir", "report_path", "report", "reference",
        "config", "local_data_dir", "remote_data_dir", "canonical_target", "root_id",
        "identity", "identity_digest", "browse_policy_revision", "stdout", "stderr",
    }
)
_SAFE_LEGACY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_LEGACY_ERROR_CODES = frozenset({"TOOL_MODE_DISABLED", "MODEL_TOOL_RESULT_UNMAPPED"})
_PROVIDER_EXACT_TEXT_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|/)[^\s,，。；;]+|[^\s,，。；;]+\.(?:fastq|fq)(?:\.gz)?|"
    r"\b(?:PATIENT|SAMPLE|REPORT|SECRET)(?:[_-][A-Z0-9][A-Z0-9_-]*|[A-Z0-9]{3,})\b|"
    r"\bAPI[_-]?KEY(?:[_-][A-Z0-9][A-Z0-9_-]*|[A-Z0-9]{3,})\b|"
    r"\b[A-Z0-9_-]*SENTINEL[A-Z0-9_-]*\b",
    re.IGNORECASE,
)


def _legacy_model_projection(value: Any) -> Any:
    """Project legacy executor output without carrying application free text."""
    if not isinstance(value, Mapping):
        return {"ok": False, "error_code": "MODEL_TOOL_RESULT_UNMAPPED"}
    projected: dict[str, Any] = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        lowered = key.lower()
        if lowered in _LEGACY_EXACT_KEYS or lowered not in _LEGACY_MODEL_KEYS:
            continue
        if lowered in {"reply", "message", "error"}:
            projected["reply_category"] = "tool_error" if lowered == "error" else "tool_result"
        elif lowered in {"ok", "blocked", "has_session", "confirmation_required", "gate_ok", "requested"}:
            projected[key] = bool(item)
        elif lowered in {"sample_count", "directory_count", "unmatched_count", "record_count", "steps", "warning_count"}:
            try:
                projected[key] = max(0, min(int(item), 1000000))
            except (TypeError, ValueError, OverflowError):
                projected[key] = 0
        elif lowered == "error_code":
            if str(item) in _SAFE_LEGACY_ERROR_CODES:
                projected[key] = str(item)
        elif lowered in {"state", "run_state", "layout", "strandedness", "action", "via", "tool", "tool_mode", "stage"}:
            token = str(item)
            projected[key] = token if _SAFE_LEGACY_TOKEN_RE.fullmatch(token) else "unknown"
        elif lowered in {"summary", "result", "pipeline", "status", "gate", "design"}:
            projected[f"{lowered}_present"] = item is not None
        elif lowered in {"warnings", "gate_warnings", "gate_reasons", "steps"}:
            projected[f"{lowered[:-1] if lowered.endswith('s') else lowered}_count"] = min(len(item) if isinstance(item, list) else 0, 64)
    if "ok" not in projected:
        projected["ok"] = False
    return projected


def _safe_provider_content(value: Any) -> str:
    """Keep ordinary prose durable while replacing likely exact-data echoes."""
    text = str(value or "")
    if not text:
        return ""
    # Tool results are already projected through the per-tool allowlist.  When
    # they are serialized as JSON, inspect only string *values*: scanning the
    # property names would classify safe keys such as ``sample_count`` and
    # ``report_hash`` as exact-data leaks and erase the structured result.
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        def _string_values(item: Any) -> Iterator[str]:
            if isinstance(item, str):
                yield item
            elif isinstance(item, dict):
                for nested in item.values():
                    yield from _string_values(nested)
            elif isinstance(item, list):
                for nested in item:
                    yield from _string_values(nested)

        if any(_PROVIDER_EXACT_TEXT_RE.search(item) for item in _string_values(parsed)):
            return "模型回复已生成。"
        return text[:16_384]
    if _PROVIDER_EXACT_TEXT_RE.search(text):
        return "模型回复已生成。"
    return text[:16_384]

#: ``ConfigReader`` 契约：``(项目目录) -> 当前配置``（读不到就返回 ``{}``）。
#: 只用来在确认卡片上渲染「旧值 → 新值」，**必须只读**——它会跑在侧效应敏感的位置。
ConfigReader = Callable[[Path], dict[str, Any]]

#: ``ApprovalContextReader`` returns a secret-free fingerprint of every mutable
#: server-side value that an approval must bind to.  The web implementation
#: includes both project.json and the user-level shared connection store.
ApprovalContextReader = Callable[[Path], dict[str, Any]]

#: Read the user-level tool permission at every security boundary. The value
#: must not be captured when the graph is built because settings can tighten
#: while an approval card is waiting in a durable checkpoint.
ToolModeReader = Callable[[], str]

#: Increment whenever confirmation grouping or interpretation changes.  A card
#: issued under an older policy version must be re-issued instead of being
#: interpreted under new rules.
CONFIRMATION_POLICY_VERSION = "tool-confirmation.v1"
APPROVAL_CONTEXT_VERSION = 1
DEFAULT_APPROVAL_TTL_SECONDS = 15 * 60


class ChatState(TypedDict, total=False):
    """Everything the conversation graph persists between nodes."""

    project_dir: str
    project_id: str
    thread_id: str
    messages: list[dict[str, Any]]  # OpenAI 消息数组，跨轮保留工具结果
    pending_calls: list[dict[str, Any]]  # 等待执行/确认的工具调用
    #: 本轮**尚未处理**的调用组（已按确认策略分组）。``guardrail`` 每轮只取
    #: 第一组；``execute`` 之后再回到 ``guardrail`` 处理下一组。
    deferred_calls: list[dict[str, Any]]
    reply: str  # 最终给用户看的答复
    via: str  # llm / tool / rejected / error
    tool_log: list[dict[str, Any]]  # 本轮已执行工具（含风险级别，供审计）
    iterations: int
    status: str  # ok / llm_error / max_iterations
    error: str
    #: 最终答复是否已通过 stream writer 逐字推出。推过就不该再整段重复一次。
    streamed: bool
    #: 当前 pending_calls 是否已获人工批准（写盘/执行前必须为 True）。
    confirmed: bool
    #: 当前这组是否已被用户拒绝。**必须记进 state**：``guardrail`` 在 resume 时
    #: 会被重跑，局部变量会丢，只有 state 里的值能带到 ``execute``。
    rejected: bool
    #: Secret-free, durable binding prepared *before* the interrupt node.  It
    #: survives interrupt replay and is consumed immediately after execution.
    approval_context: dict[str, Any]
    confirmation_card: dict[str, Any]
    disclosure_context: dict[str, Any]
    disclosure_card: dict[str, Any]
    disclosure_result: dict[str, Any]


SYSTEM_PROMPT = (
    "你是 SYSU 多组学分析 Agent 的助手，用户正在配置并运行一个 bulk RNA-seq 分析项目。\n"
    "\n"
    "你**有工具**可以真正读写项目配置、并真正启动分析。请直接调用工具，不要只说\n"
    "「我无法执行」——那是旧版本的措辞，现在你能做。\n"
    "\n"
    "工作方式：\n"
    "1. 不确定项目里已有什么时，先调用 read_project_state 读，不要凭记忆猜。\n"
    "2. 首次建会话（项目还没有样本表）用 write_project_config，参数按用户原意填。\n"
    "   写盘前系统会自动请用户确认，你不需要替他确认。\n"
    "3. 已有会话要改样本表用 edit_samples（不是 write_project_config，那个会因\n"
    "   「已有会话」被拒绝）；改参考基因组用 edit_reference；改服务器/资源用\n"
    "   edit_connection。只传真正要改的字段。\n"
    "4. 分组：如果用户给了 N 个样本但只给了 M 个分组名（M < N），这是一个**循环\n"
    "   序列**——按用户给出的顺序循环分配（例如 2 个名字覆盖 4 个样本就是\n"
    "   A,B,A,B）。不要反问用户，也不要添加他没提到的分组名。\n"
    "5. 链特异性：用户说「未知 / 不知道 / 不确定」时填 unknown，不要填 auto。\n"
    "   用户明确说反链/正链/无链特异时按原意填。\n"
    "6. 执行有先后：先 generate_plan 生成计划，再 confirm_contract 冻结契约，\n"
    "   最后 run_analysis 启动。用户只要求跑某一段时，给 run_analysis 传 stage。\n"
    "7. 启动分析是异步的：用户问「跑到哪了」时调用 refresh_project_status 读真实\n"
    "   进度，不要凭上次的答复猜。问结果时用 get_project_report。\n"
    "\n"
    "涉及概念解释（例如「差异表达的原理」）时直接用文字回答，不要调用工具。\n"
    "回答用中文，简洁，不要罗列工具名，说人话。"
)


# -- LLM 调用 ---------------------------------------------------------------


def _provider_messages_for_model(
    llm_config: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    project_dir: Path | None,
    project_id: str,
    thread_id: str,
) -> tuple[list[dict[str, Any]], Any]:
    """Build a summary-bound provider history while retaining tool-call shape.

    ``ModelContextBuilder`` owns the project projection.  The graph still needs
    to retain assistant ``tool_calls`` and tool replies for protocol continuity,
    so those fields are copied only after the builder has supplied the bounded
    project summary and fixed system context.
    """
    llm_block = llm_config.get("llm", llm_config)
    if not isinstance(llm_block, dict):
        llm_block = {}
    model = str(llm_block.get("model") or "").strip() or "gpt-4o-mini"
    config = normalize_provider_config(
        {
            "provider": llm_block.get("provider") or "openai",
            "api_base": llm_block.get("api_base"),
            "model": model,
            "api_mode": llm_block.get("api_mode") or "chat_completions",
            "backend": llm_block.get("backend") or "api",
        }
    )
    system_prompt = next(
        (
            str(item.get("content") or "")
            for item in messages
            if isinstance(item, dict) and item.get("role") == "system"
        ),
        SYSTEM_PROMPT,
    )
    context = ModelContextBuilder.build(
        project_dir=project_dir,
        project_id=project_id,
        thread_id=thread_id,
        provider=config,
        system_prompt=system_prompt,
        current_user_message="",
        durable_messages=(),
        claimed_grant=None,
    )
    provider_messages = [
        dict(item)
        for item in context.messages
        if not (item.get("role") == "user" and not str(item.get("content") or ""))
    ]
    for message in messages:
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        projected_message = dict(message)
        calls = projected_message.get("tool_calls")
        if isinstance(calls, list):
            safe_calls: list[dict[str, Any]] = []
            for item in calls:
                if not isinstance(item, dict):
                    continue
                function = item.get("function") if isinstance(item.get("function"), dict) else {}
                safe_calls.append(
                    {
                        "id": str(item.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(function.get("name") or ""),
                            "arguments": str(function.get("arguments") or "{}"),
                        },
                    }
                )
            projected_message["tool_calls"] = safe_calls
        projected_message.pop("call_ref", None)
        projected_message.pop("tool_projection_version", None)
        projected_message.pop("arguments_hash", None)
        if projected_message.get("role") == "tool":
            projected_message["content"] = _safe_provider_content(projected_message.get("content"))
        provider_messages.append(projected_message)
    return provider_messages, config


def _chat_endpoint(llm_config: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return ``(url, api_key, model)``; mirrors ``webapp._llm_endpoint``.

    刻意重复这一小段解析：本模块不能 import ``webapp``（``webapp`` 要 import 本
    模块，会循环）。字段形状由 ``connection_store`` 与 ``_default_config`` 定义。
    """
    llm = llm_config.get("llm", llm_config)
    if not llm.get("enabled") or not llm.get("api_key") or not llm.get("api_base"):
        return None
    api_base = str(llm["api_base"]).rstrip("/")
    model = str(llm.get("model") or "").strip() or "gpt-4o-mini"
    return f"{api_base}/chat/completions", str(llm["api_key"]), model


def _stream_chat_completion(
    llm_config: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    timeout: float = 60.0,
    gateway: ModelProviderGateway | None = None,
) -> Iterator[tuple[str, Any]]:
    """Stream one assistant turn, yielding ``("delta", text)`` then ``("message", ...)``.

    工具调用回合用户不需要看 token，但**不调工具的最终答复需要**逐字显示——所以
    这里始终用流式，边收边把 content 推给调用方，同时用 ``ToolCallAccumulator``
    把切成碎片的 ``tool_calls`` 拼回完整 JSON。
    """
    endpoint = _chat_endpoint(llm_config)
    if endpoint is None:
        yield ("error", "未配置可用的模型端点。")
        return
    url, api_key, model = endpoint
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "stream": True,
    }
    llm_block = llm_config.get("llm", llm_config)
    try:
        mode = normalize_tool_mode(llm_block.get("tool_mode"))
    except (AttributeError, ValueError):
        mode = TOOL_MODE_DISABLED
    tools = tool_schemas(mode)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    accumulator = ToolCallAccumulator()
    content_parts: list[str] = []
    finish_reason = ""
    if gateway is None:
        gateway = ModelProviderGateway()
    try:
        config = normalize_provider_config({"provider": llm_block.get("provider") or "openai", "api_base": llm_block.get("api_base"), "model": model, "api_mode": "chat_completions"})
        request = PreparedModelRequest(provider=config, identity=provider_identity(config), api_mode="chat_completions", payload=payload, credentials=ProviderCredentials(api_key=api_key), timeout_seconds=timeout, stream=True)
        events = gateway.stream(request)
        for event in events:
            if event.kind == "delta":
                streamed_value = str(event.value)
                content_parts.append(streamed_value)
                yield ("delta", streamed_value)
            elif event.kind == "tool_call_fragment":
                fragment = event.value if isinstance(event.value, dict) else {}
                accumulator.add_delta([{"id": fragment.get("id", ""), "function": {"name": fragment.get("name", ""), "arguments": fragment.get("arguments", "")}}])
            elif event.kind == "message":
                message_value = event.value if isinstance(event.value, dict) else {}
                finish_reason = str(message_value.get("finish_reason") or "stop")
        calls = accumulator.finish()
        yield ("message", {"content": "".join(content_parts), "tool_calls": [{"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.raw_arguments}} for call in calls], "finish_reason": finish_reason})
        return
    except ProviderRequestError as exc:
        yield ("error", str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        yield ("error", f"调用模型失败：{type(exc).__name__}")
        return


# -- 图 ---------------------------------------------------------------------


def chat_thread_config(project_id: str, thread_id: str) -> dict[str, Any]:
    """Thread config for the conversation graph.

    前缀 ``chat:`` 把对话的 checkpoint 与分析流水线图（``thread_id == project_id``）
    彻底分开，两者共用同一个 ``langgraph.sqlite3`` 也不会互相覆盖。
    """
    return {"configurable": {"thread_id": f"chat:{project_id}:{thread_id}"}}


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _normalize_call_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Return the exact JSON-safe arguments passed to the executor."""
    raw = deepcopy(arguments)
    if name == "write_project_config":
        normalized = normalize_write_arguments(raw)
        normalized["data_source"] = str(normalized.get("data_source") or "remote_path").strip()
        normalized["fastq_dir"] = str(normalized.get("fastq_dir") or "").strip()
        normalized["strandedness"] = str(
            normalized.get("strandedness") or "unknown"
        ).strip()
        return normalized
    if name == "edit_samples":
        samples: list[dict[str, Any]] = []
        for item in raw.get("samples") or []:
            if not isinstance(item, dict):
                continue
            sample: dict[str, Any] = {"sample_id": str(item.get("sample_id") or "")}
            for key in ("condition", "fastq_1", "fastq_2"):
                if item.get(key) not in (None, ""):
                    sample[key] = str(item[key])
            samples.append(sample)
        result: dict[str, Any] = {}
        if "samples" in raw:
            result["samples"] = samples
        if "remove" in raw:
            result["remove"] = [str(item) for item in (raw.get("remove") or [])]
        return result
    if name == "edit_connection":
        result = {}
        for key in CONNECTION_FIELDS:
            if raw.get(key) is None:
                continue
            result[key] = (
                int(raw[key])
                if key in {"port", "threads", "memory_gb"}
                else str(raw[key]).strip()
            )
        return result
    if name == "edit_reference":
        return {
            key: str(raw[key]).strip()
            for key in ("gtf", "genome_fasta", "star_index", "rsem_prefix")
            if raw.get(key)
        }
    if name == "set_run_resources":
        return {
            key: int(raw[key])
            for key in ("threads", "memory_gb")
            if raw.get(key) is not None
        }
    if name == "configure_pipeline":
        return {"step": str(raw.get("step") or "").strip(), "enabled": bool(raw.get("enabled"))}
    if name == "set_diffexp_reference":
        return {"reference_condition": str(raw.get("reference_condition") or "").strip()}
    if name == "set_cms_options":
        result = {"enabled": bool(raw.get("enabled"))}
        if raw.get("n_perm") is not None:
            result["n_perm"] = int(raw["n_perm"])
        if raw.get("fdr") is not None:
            result["fdr"] = float(raw["fdr"])
        if str(raw.get("run_mode") or "").strip():
            result["run_mode"] = str(raw["run_mode"]).strip()
        return result
    if name == "run_analysis":
        stage = str(raw.get("stage") or "").strip()
        return {"stage": stage} if stage else {}
    if name == "browse_remote_samples":
        return {"path": str(raw.get("path") or "").strip()}
    if name == "request_data_disclosure":
        return {
            "fields": [str(item).strip() for item in (raw.get("fields") or [])],
            "purpose": str(raw.get("purpose") or "").strip(),
        }
    return raw


def _approval_description(
    name: str,
    arguments: dict[str, Any],
    current: dict[str, Any],
) -> str:
    """Render a durable confirmation summary without exact project data.

    Exact sample IDs, FASTQ names, server paths, and report fragments stay in
    the request-local argument store.  This text is checkpointed in the
    interrupt card, so it must describe the change using counts, field names,
    and bounded enums only.
    """
    labels = tool_labels()
    title = labels.get(name, name)

    if name == "request_data_disclosure":
        fields = arguments.get("fields") or []
        return (
            f"{title}：仅申请本地 sample_ids（{len(fields)} 个字段类别）；"
            "精确值不会写入确认卡或项目记录。"
        )

    if name == "write_project_config":
        samples = [item for item in arguments.get("samples") or [] if isinstance(item, dict)]
        configured = sum(
            1
            for item in samples
            if item.get("fastq_1") or item.get("fastq_2")
        )
        source = "服务器已有数据" if arguments.get("data_source") == "remote_path" else "本地上传"
        lines = [
            f"{title}：",
            f"  数据来源：{source}",
            f"  FASTQ 目录：已提供（{configured} 个样本含 FASTQ 信息）" if configured else "  FASTQ 目录：已提供",
            f"  样本：{len(samples)} 个（仅显示数量，精确样本名按需确认）",
            f"  链特异性：{arguments.get('strandedness') or 'unknown'}",
        ]
        for key, label in (
            ("gtf", "GTF 注释"),
            ("genome_fasta", "基因组 FASTA"),
            ("star_index", "STAR 索引"),
            ("rsem_prefix", "RSEM 索引前缀"),
        ):
            if arguments.get(key):
                lines.append(f"  {label}：已提供（精确路径按需确认）")
        return "\n".join(lines)

    if name == "edit_samples":
        samples = [item for item in arguments.get("samples") or [] if isinstance(item, dict)]
        removals = [item for item in arguments.get("remove") or [] if item]
        fields: set[str] = set()
        for item in samples:
            fields.update(str(key) for key in item if key != "sample_id")
        lines = [f"{title}：", f"  修改样本：{len(samples)} 个，删除样本：{len(removals)} 个"]
        if fields:
            lines.append("  修改字段：" + "、".join(sorted(fields)))
        lines.append("  精确样本名与 FASTQ 文件名按需确认。")
        return "\n".join(lines)

    if name == "edit_connection":
        changed = [str(key) for key in arguments if key]
        lines = [f"{title}：", "  修改字段：" + ("、".join(changed) if changed else "（无）")]
        lines.append("  主机、用户和远端路径的精确值按需确认。")
        return "\n".join(lines)

    if name == "edit_reference":
        changed = [str(key) for key in arguments if key]
        return "\n".join(
            [f"{title}：", "  修改字段：" + ("、".join(changed) if changed else "（无）"), "  精确参考文件路径按需确认。"]
        )

    if name == "set_run_resources":
        changes = [
            f"{label}：{arguments[key]}"
            for key, label in (("threads", "线程数"), ("memory_gb", "内存（GB）"))
            if arguments.get(key) is not None
        ]
        return f"{title}：" + ("；".join(changes) if changes else "无实际变化")

    if name == "configure_pipeline":
        step = str(arguments.get("step") or "未知步骤")
        return f"{title}：{step} → {'启用' if arguments.get('enabled') else '关闭'}"

    if name == "set_diffexp_reference":
        return f"{title}：更新对照组（精确分组名按需确认）"

    if name == "set_cms_options":
        lines = [f"{title}：", f"  CMS 分型：{'启用' if arguments.get('enabled') else '关闭'}"]
        for key, label in (("n_perm", "置换次数"), ("fdr", "FDR 阈值"), ("run_mode", "运行模式")):
            if arguments.get(key) is not None:
                value = arguments[key]
                if key == "run_mode" and isinstance(current.get("cms"), dict):
                    old = current["cms"].get("run_mode") or "（默认）"
                    lines.append(f"  {label}：{old} → {value}")
                else:
                    lines.append(f"  {label}：{value}")
        return "\n".join(lines)

    if name == "run_analysis":
        stage = arguments.get("stage")
        return f"{title}：{'只跑 ' + str(stage) + ' 阶段' if stage else '跑完整流水线'}（在服务器上执行）"

    return describe_call(name, {}, {})


def _approval_claim_path(project_dir: Path, approval_id: str) -> Path:
    """Return a path-safe durable claim filename for one approval card."""
    digest = hashlib.sha256(approval_id.encode("utf-8")).hexdigest()
    return project_dir / ".approval_claims" / f"{digest}.json"


def _write_claim_payload(path: Path, payload: dict[str, Any], *, exclusive: bool) -> None:
    """Persist a claim before side effects, optionally using O_EXCL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_WRONLY
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _secret_free(value: Any) -> Any:
    """Return a deterministic view that never preserves credential values."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if any(
                marker in lowered
                for marker in ("password", "api_key", "secret", "credential", "token")
            ):
                result[key] = "<redacted>" if item not in (None, "") else ""
            else:
                result[key] = _secret_free(item)
        return result
    if isinstance(value, list):
        return [_secret_free(item) for item in value]
    if isinstance(value, tuple):
        return [_secret_free(item) for item in value]
    return value


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_chat_graph(
    *,
    executor: ToolExecutor,
    llm_config: dict[str, Any],
    checkpointer: Any = None,
    max_iterations: int = MAX_TOOL_ITERATIONS,
    timeout: float = 60.0,
    config_reader: ConfigReader | None = None,
    approval_context_reader: ApprovalContextReader | None = None,
    clock: Callable[[], datetime] | None = None,
    approval_ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    tool_mode_reader: ToolModeReader | None = None,
    result_finalizer: ToolResultFinalizer | None = None,
    disclosure_requester: DisclosureRequester | None = None,
    disclosure_decider: DisclosureDecider | None = None,
):
    """Compile the conversation graph.

    ``executor`` 是唯一能产生副作用的入口，由调用方注入真实的写盘链路。
    ``config_reader`` 只用于确认卡片上的「旧值 → 新值」；它跑在 ``guardrail``
    （不会产生副作用的位置）里，因此**必须只是读**。没给就退化成「（未设置）」。
    """
    if not LANGGRAPH_AVAILABLE:  # pragma: no cover - optional dependency
        raise RuntimeError("langgraph 不可用，无法构建对话图。")

    now = clock or (lambda: datetime.now(timezone.utc))
    ttl_seconds = max(1, int(approval_ttl_seconds))
    try:
        _executor_accepts_context = len(inspect.signature(executor).parameters) >= 5
    except (TypeError, ValueError):
        _executor_accepts_context = True

    def _live_tool_mode() -> str:
        try:
            raw = (
                tool_mode_reader()
                if tool_mode_reader is not None
                else llm_config.get("llm", llm_config).get("tool_mode")
            )
            return normalize_tool_mode(raw)
        except Exception:  # noqa: BLE001 - unreadable policy must fail closed
            return TOOL_MODE_DISABLED

    def _current_config(state: ChatState) -> dict[str, Any]:
        """Read the project config for card rendering; never raises."""
        if config_reader is None:
            return {}
        try:
            return config_reader(Path(state.get("project_dir") or "")) or {}
        except Exception:  # noqa: BLE001 - card rendering must never break the turn
            return {}

    def _current_approval_snapshot(
        state: ChatState, current_config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Read only the secret-free fields that an approval binds to."""
        if approval_context_reader is not None:
            try:
                raw = approval_context_reader(Path(state.get("project_dir") or "")) or {}
            except Exception:  # noqa: BLE001 - unavailable context must fail closed
                raw = {
                    "available": False,
                    "config_revision": "unavailable",
                    "config_hash": "unavailable",
                }
        else:
            safe = _secret_free(current_config if current_config is not None else _current_config(state))
            digest = _canonical_hash(safe)
            raw = {
                "available": True,
                "config_revision": digest,
                "config_hash": digest,
                "contract_id": (
                    (safe.get("execution") or {}).get("verified_contract_id") or ""
                    if isinstance(safe, dict)
                    else ""
                ),
                "run_id": (
                    (safe.get("status") or {}).get("run_id") or ""
                    if isinstance(safe, dict)
                    else ""
                ),
            }
        revision = str(raw.get("config_revision") or "")
        config_hash = str(raw.get("config_hash") or "")
        available = raw.get("available") is not False
        if "unavailable" in revision.lower() or "unavailable" in config_hash.lower():
            available = False
        return {
            "available": available,
            "config_revision": revision,
            "config_hash": config_hash,
            "contract_id": str(raw.get("contract_id") or ""),
            "run_id": str(raw.get("run_id") or ""),
        }

    def _append(state: ChatState, *new_messages: dict[str, Any]) -> list[dict[str, Any]]:
        return list(state.get("messages") or []) + list(new_messages)

    ephemeral_store = _EPHEMERAL_TOOL_CALL_STORE

    def _disclosure_request(state: ChatState, call: Mapping[str, Any]) -> dict[str, Any]:
        if disclosure_requester is None:
            raise ValueError("MODEL_DATA_DISCLOSURE_UNAVAILABLE")
        arguments = _call_arguments(state, call)
        result = disclosure_requester(
            Path(state.get("project_dir") or ""),
            str(state.get("project_id") or ""),
            str(state.get("thread_id") or ""),
            arguments,
        )
        if not isinstance(result, dict):
            raise ValueError("MODEL_DATA_DISCLOSURE_INVALID")
        card = result.get("card")
        grant_id = str(result.get("grant_id") or "")
        if not grant_id or not isinstance(card, dict) or card.get("type") != "model_data_disclosure_confirmation":
            raise ValueError("MODEL_DATA_DISCLOSURE_INVALID")
        if card.get("fields") != ["sample_ids"]:
            raise ValueError("MODEL_DATA_SCOPE_UNSUPPORTED")
        safe_card = {
            key: card[key]
            for key in (
                "type", "grant_id", "fields", "record_counts", "purpose_category",
                "provider", "provider_config_revision", "tool_mode", "revisions", "expires_at",
            )
            if key in card
        }
        return {"grant_id": grant_id, "card": safe_card}

    def _raw_call(call: ToolCall, call_ref: str | None = None) -> dict[str, Any]:
        """Re-shape a parsed call back into the provider's ``tool_calls`` form."""
        try:
            projected = project_assistant_tool_call(call.call_id, call.name, call.arguments)
        except ValueError:
            projected = {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps({"unmapped": True, "argument_hash": _canonical_hash(call.arguments)}, separators=(",", ":"))},
                "tool_projection_version": 1,
                "arguments_hash": _canonical_hash(call.arguments),
            }
        if call_ref:
            projected["call_ref"] = call_ref
        return projected

    def _pending_call(call: ToolCall, call_ref: str | None = None) -> dict[str, Any]:
        """Plain-dict form stored in graph state (must stay JSON-serialisable)."""
        result = {
            "call_id": call.call_id,
            "name": call.name,
            "arguments_hash": _canonical_hash(call.arguments),
            "argument_projection": project_tool_arguments_for_model(call.name, call.arguments),
            "parse_error": call.parse_error,
        }
        if call_ref:
            result["call_ref"] = call_ref
        return result

    def _call_arguments(state: ChatState, call: Mapping[str, Any]) -> dict[str, Any]:
        call_ref = str(call.get("call_ref") or "")
        if call_ref:
            return ephemeral_store.get(
                call_ref,
                project_id=str(state.get("project_id") or ""),
                thread_id=str(state.get("thread_id") or ""),
                call_id=str(call.get("call_id") or ""),
                name=str(call.get("name") or ""),
            )
        raise ValueError("EPHEMERAL_ARGUMENT_UNAVAILABLE")


    def node_agent(state: ChatState) -> ChatState:
        """Ask the model for the next turn. Pure: no writes, no external side effects."""
        iterations = int(state.get("iterations") or 0)
        if iterations >= max_iterations:
            return {
                "status": "max_iterations",
                "via": "llm",
                "pending_calls": [],
                "deferred_calls": [],
                "approval_context": {},
                "confirmation_card": {},
                "disclosure_context": {},
                "disclosure_card": {},
                "reply": (
                    "我在这一轮里来回调用工具太多次了，先停在这里。\n"
                    f"当前进度：{state.get('reply') or '还没有可回报的结果'}。\n"
                    "如果还需要继续，请再发一条消息告诉我下一步。"
                ),
            }

        messages = list(state.get("messages") or [])
        if not messages or messages[0].get("role") != "system":
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]

        writer = get_stream_writer()
        raw_message: dict[str, Any] = {}
        streamed = False
        runtime_llm_config = deepcopy(llm_config)
        runtime_block = runtime_llm_config.setdefault("llm", {})
        if not isinstance(runtime_block, dict):
            runtime_block = {}
            runtime_llm_config["llm"] = runtime_block
        runtime_block["tool_mode"] = _live_tool_mode()
        provider_messages, _provider_config = _provider_messages_for_model(
            runtime_llm_config,
            messages,
            project_dir=Path(state.get("project_dir") or "") if state.get("project_dir") else None,
            project_id=str(state.get("project_id") or ""),
            thread_id=str(state.get("thread_id") or ""),
        )
        for kind, payload in _stream_chat_completion(
            runtime_llm_config, provider_messages, timeout=timeout
        ):
            if kind == "delta":
                streamed = True
                try:
                    writer({"type": "delta", "text": payload})
                except Exception:  # noqa: BLE001 - streaming is best-effort
                    pass
            elif kind == "message":
                raw_message = payload
            elif kind == "error":
                return {
                    "status": "llm_error",
                    "via": "error",
                    "error": str(payload),
                    "pending_calls": [],
                    "deferred_calls": [],
                    "approval_context": {},
                    "confirmation_card": {},
                    "reply": "",
                }

        content = str(raw_message.get("content") or "")
        calls = parse_message_tool_calls(raw_message)

        if not calls:
            # 最终答复也要落进 messages：否则下一轮对话拿不到历史，
            # 用户说「刚才那个改成 16 线程」时模型不知道「那个」是什么。
            rejected_turn = bool(state.get("rejected"))
            return {
                "status": "ok",
                "via": "rejected" if rejected_turn else "llm",
                "reply": _safe_provider_content(content),
                "messages": _append(
                    state, {"role": "assistant", "content": _safe_provider_content(content) or "模型回复已生成。", "model_context_version": 1}
                ),
                "pending_calls": [],
                "deferred_calls": [],
                "approval_context": {},
                "confirmation_card": {},
                "rejected": False,
                # 最终答复已通过 writer 逐字推出；没推过（例如模型只回了空串）
                # 时由上层整段输出，避免重复。
                "streamed": streamed,
            }

        projected_calls: list[dict[str, Any]] = []
        pending_calls: list[dict[str, Any]] = []
        for call in calls:
            try:
                ref = ephemeral_store.put(str(state.get("project_id") or ""), str(state.get("thread_id") or ""), call.call_id, call.name, call.arguments)
            except Exception:
                ref = ""
            projected_calls.append(_raw_call(call, ref or None))
            pending_calls.append(_pending_call(call, ref or None))
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": "模型正在请求工具。" if content else None,
            "model_context_version": 1,
        }
        assistant_message["tool_calls"] = projected_calls
        # 本轮所有调用进 deferred 队列（保持模型给出的顺序），由 guardrail 按确认
        # 策略**一组一组**取出来处理。分组在这里不做：guardrail 是唯一判断风险的
        # 地方，让它自己分组，避免两处逻辑漂移。
        return {
            "status": "ok",
            "messages": _append(state, assistant_message),
            "pending_calls": [],
            "deferred_calls": pending_calls,
            "approval_context": {},
            "confirmation_card": {},
            "disclosure_context": {},
            "disclosure_card": {},
            "confirmed": False,
            "rejected": False,
            "iterations": iterations + 1,
        }

    def route_after_agent(state: ChatState) -> str:
        if state.get("deferred_calls"):
            return "guardrail"
        return "end"

    def _tool_error_message(call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(payload, ensure_ascii=False),
            "model_projection_version": 1,
        }

    def _split_head(
        state: ChatState,
        mode: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Peel the first confirmation group off the deferred queue.

        Returns ``(head_calls, rest_calls, rejected_calls)`` where ``rejected_calls``
        are the head's calls that failed parameter validation. ``head_calls`` is what
        this round will confirm+execute; ``rest_calls`` stays queued.
        """
        queue = list(state.get("deferred_calls") or [])
        batches = split_calls_for_round(queue)
        if not batches:
            return [], [], []

        head = batches[0]
        rest: list[dict[str, Any]] = []
        for batch in batches[1:]:
            rest.extend(batch.calls)

        valid: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for call in head.calls:
            name = str(call.get("name") or "")
            try:
                arguments = _call_arguments(state, call)
            except Exception as exc:
                rejected.append({**call, "_reason": "工具参数引用不可用，请重新发起调用。"})
                continue
            blocked = None if name == "browse_remote_samples" else tool_mode_block(name, mode)
            if blocked is not None:
                rejected.append({**call, "_blocked": blocked})
                continue
            problems = validate_call(name, arguments)
            if problems:
                rejected.append({**call, "_reason": "；".join(problems)})
            else:
                try:
                    normalized = _normalize_call_arguments(name, arguments)
                    call_ref = str(call.get("call_ref") or "")
                    if not call_ref:
                        raise ValueError("missing ephemeral argument reference")
                    ephemeral_store.replace(call_ref, normalized)
                except Exception as exc:  # noqa: BLE001 - malformed args fail closed
                    rejected.append(
                        {
                            **call,
                            "_reason": f"参数规范化失败（{type(exc).__name__}）。",
                        }
                    )
                    continue
                valid.append({
                    **call,
                    "arguments_hash": _canonical_hash(normalized),
                    "argument_projection": project_tool_arguments_for_model(name, normalized),
                })
        return valid, rest, rejected

    def node_guardrail(state: ChatState) -> ChatState:
        """Validate one group and durably prepare its approval card.

        This node is side-effect free.  Crucially, it is a separate checkpointed
        node from ``node_confirmation``: the random approval id and timestamps are
        created here once, then the interrupt node can replay without changing the
        card the browser actually saw.
        """
        if not state.get("deferred_calls"):
            return {
                "pending_calls": [],
                "approval_context": {},
                "confirmation_card": {},
            }

        # 参数校验先于确认：不合法的调用没有让人签字的必要。
        mode = _live_tool_mode()
        valid, rest, rejected = _split_head(state, mode)

        if rejected:
            messages = list(state.get("messages") or [])
            for item in rejected:
                blocked = item.get("_blocked")
                messages.append(
                    _tool_error_message(
                        str(item.get("call_id") or ""),
                        (
                            blocked
                            if isinstance(blocked, dict)
                            else {"ok": False, "error": str(item.get("_reason") or "")}
                        ),
                    )
                )
            if not valid:
                # 这组全废：发完错误就交给下一组，没有下一组则回模型。
                return {
                    "messages": messages,
                    "pending_calls": [],
                    "deferred_calls": rest,
                    "confirmed": False,
                    "rejected": False,
                    "approval_context": {},
                    "confirmation_card": {},
                }
            # 部分有效：把错误留在对话里，有效的那部分继续走确认。
            state = {**state, "messages": messages}

        if not valid:
            return {
                "pending_calls": [],
                "deferred_calls": rest,
                "confirmed": False,
                "approval_context": {},
                "confirmation_card": {},
            }

        # Data disclosure is a separate control path. It creates a
        # metadata-only grant/card and must never be folded into the ordinary
        # tool confirmation card or executor.
        if len(valid) == 1 and str(valid[0].get("name") or "") == "request_data_disclosure":
            try:
                disclosure = _disclosure_request(state, valid[0])
            except Exception as exc:  # noqa: BLE001 - malformed/unavailable disclosure fails closed
                message = _tool_error_message(
                    str(valid[0].get("call_id") or ""),
                    {"ok": False, "error_code": str(exc) or "MODEL_DATA_DISCLOSURE_INVALID"},
                )
                return {
                    "messages": list(state.get("messages") or []) + [message],
                    "pending_calls": [],
                    "deferred_calls": rest,
                    "confirmed": False,
                    "rejected": False,
                    "approval_context": {},
                    "confirmation_card": {},
                    "disclosure_context": {},
                    "disclosure_card": {},
                }
            card = dict(disclosure["card"])
            issued_at = now()
            expires_at = issued_at + timedelta(seconds=ttl_seconds)
            approval_id = "discl_" + secrets.token_urlsafe(24)
            card.update(
                {
                    "approval_id": approval_id,
                    "project_id": str(state.get("project_id") or ""),
                    "thread_id": str(state.get("thread_id") or ""),
                    "call_id": str(valid[0].get("call_id") or ""),
                    "expires_at": card.get("expires_at") or _utc_iso(expires_at),
                }
            )
            disclosure_context = {
                "approval_id": approval_id,
                "grant_id": disclosure["grant_id"],
                "project_id": str(state.get("project_id") or ""),
                "thread_id": str(state.get("thread_id") or ""),
                "call_id": str(valid[0].get("call_id") or ""),
                "tool_mode": mode,
                "issued_at": _utc_iso(issued_at),
                "expires_at": card["expires_at"],
            }
            return {
                "messages": list(state.get("messages") or []),
                "pending_calls": valid,
                "deferred_calls": rest,
                "confirmed": False,
                "rejected": False,
                "approval_context": {},
                "confirmation_card": {},
                "disclosure_context": disclosure_context,
                "disclosure_card": card,
            }

        # 只读工具直接放行；写盘/执行必须人工确认（唯一真源在 agent_tools）。
        policy = confirmation_policy(str(valid[0].get("name") or ""))
        if policy == POLICY_NEVER:
            return {
                "messages": list(state.get("messages") or []),
                "pending_calls": valid,
                "deferred_calls": rest,
                "confirmed": False,
                "rejected": False,
                "approval_context": {},
                "confirmation_card": {},
            }

        current = _current_config(state)
        snapshot = _current_approval_snapshot(state, current)
        if not snapshot.get("available"):
            return _reject_approval(
                {
                    **state,
                    "pending_calls": valid,
                    "deferred_calls": rest,
                },
                error="无法完整读取当前项目或共享配置，操作未执行。",
            )
        issued_at = now()
        expires_at = issued_at + timedelta(seconds=ttl_seconds)
        bound_calls = [
            {
                "call_id": str(call.get("call_id") or ""),
                "name": str(call.get("name") or ""),
                "arguments_hash": str(call.get("arguments_hash") or _canonical_hash(_call_arguments(state, call))),
            }
            for call in valid
        ]
        approval_context: dict[str, Any] = {
            "approval_context_version": APPROVAL_CONTEXT_VERSION,
            "approval_id": "appr_" + secrets.token_urlsafe(24),
            "project_id": str(state.get("project_id") or ""),
            "thread_id": str(state.get("thread_id") or ""),
            "calls": bound_calls,
            "policy": policy,
            "tool_mode": mode,
            "confirmation_policy_version": CONFIRMATION_POLICY_VERSION,
            **snapshot,
            "issued_at": _utc_iso(issued_at),
            "expires_at": _utc_iso(expires_at),
        }
        cards = [
            {
                "call_id": str(call.get("call_id") or ""),
                "name": str(call.get("name") or ""),
                "arguments_hash": str(call.get("arguments_hash") or _canonical_hash(_call_arguments(state, call))),
                "risk": risk_of(str(call.get("name") or "")),
                "label": tool_labels().get(str(call.get("name") or ""), str(call.get("name"))),
                "description": _approval_description(
                    str(call.get("name") or ""), _call_arguments(state, call), current
                ),
            }
            for call in valid
        ]
        card = {
            **approval_context,
            "type": "tool_confirmation",
            "message": (
                "以下配置改动会一起写入项目，需要你确认后才会执行。"
                if policy != POLICY_SOLO
                else "以下操作会真正执行，需要你单独确认。"
            ),
            "calls": cards,
        }
        return {
            "messages": list(state.get("messages") or []),
            "pending_calls": valid,
            "deferred_calls": rest,
            "approval_context": approval_context,
            "confirmation_card": card,
            "disclosure_context": {},
            "disclosure_card": {},
            "confirmed": False,
            "rejected": False,
        }

    def _approval_context_error(state: ChatState, live_mode: str | None = None) -> str:
        context = state.get("approval_context") or {}
        if not isinstance(context, dict) or not context.get("approval_id"):
            return "没有可用的持久化确认上下文，操作未执行。"
        if context.get("approval_context_version") != APPROVAL_CONTEXT_VERSION:
            return "确认上下文版本已变化，请重新发起操作。"
        if context.get("confirmation_policy_version") != CONFIRMATION_POLICY_VERSION:
            return "确认策略版本已变化，请重新发起操作。"
        live_mode = live_mode or _live_tool_mode()
        if str(context.get("tool_mode") or "") != live_mode:
            return f"LLM 工具权限模式已变为 {live_mode}，旧确认卡不能继续执行。"
        if str(context.get("project_id") or "") != str(state.get("project_id") or ""):
            return "确认卡不属于当前项目，操作未执行。"
        if str(context.get("thread_id") or "") != str(state.get("thread_id") or ""):
            return "确认卡不属于当前对话，操作未执行。"
        if context.get("available") is not True:
            return "无法读取当前项目或共享配置，操作未执行。"
        if not str(context.get("config_revision") or ""):
            return "无法读取当前项目配置版本，操作未执行。"
        if not str(context.get("config_hash") or ""):
            return "无法读取当前项目配置摘要，操作未执行。"
        expires_at = _parse_utc(context.get("expires_at"))
        current_time = now()
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        if expires_at is None or current_time.astimezone(timezone.utc) >= expires_at:
            return "确认卡已过期，请重新发起操作。"

        calls = list(state.get("pending_calls") or [])
        bound = context.get("calls") or []
        actual = [
            {
                "call_id": str(call.get("call_id") or ""),
                "name": str(call.get("name") or ""),
                "arguments_hash": str(call.get("arguments_hash") or _canonical_hash(_call_arguments(state, call))),
            }
            for call in calls
        ]
        if actual != bound:
            return "待执行工具与确认卡不一致，操作未执行。"
        for call in calls:
            name = str(call.get("name") or "")
            arguments = _call_arguments(state, call)
            problems = validate_call(name, arguments)
            if problems:
                return "执行前参数校验失败：" + "；".join(problems)
            if confirmation_policy(name) != context.get("policy"):
                return "工具确认策略已变化，请重新确认。"

        fresh = _current_approval_snapshot(state)
        if not fresh.get("available"):
            return "无法读取当前项目或共享配置，操作未执行。"
        for key in ("config_revision", "config_hash", "contract_id", "run_id"):
            if str(fresh.get(key) or "") != str(context.get(key) or ""):
                return "项目或共享连接配置已变化，请重新确认。"
        return ""

    def _reject_for_mode(state: ChatState, mode: str) -> ChatState:
        """Consume all queued calls with structured live-policy denials."""
        messages = list(state.get("messages") or [])
        calls = [
            *list(state.get("pending_calls") or []),
            *list(state.get("deferred_calls") or []),
        ]
        for call in calls:
            name = str(call.get("name") or "")
            payload = tool_mode_block(name, mode) or {
                "ok": False,
                "blocked": True,
                "error_code": "llm_tool_mode_changed",
                "tool_mode": mode,
                "tool": name,
                "error": f"LLM 工具权限模式已变化，{name} 未执行。",
            }
            messages.append(
                _tool_error_message(str(call.get("call_id") or ""), payload)
            )
        return {
            "messages": messages,
            "pending_calls": [],
            "deferred_calls": [],
            "confirmed": False,
            "rejected": True,
            "via": "rejected",
            "approval_context": {},
            "confirmation_card": {},
        }

    def _reject_approval(
        state: ChatState,
        *,
        error: str,
        note: str = "",
        explicit_user_rejection: bool = False,
    ) -> ChatState:
        """Answer every abandoned tool call and consume the approval context."""
        messages = list(state.get("messages") or [])
        calls = list(state.get("pending_calls") or [])
        rest = list(state.get("deferred_calls") or [])
        primary_error = "用户拒绝了这个操作，没有执行。" if explicit_user_rejection else error
        deferred_error = (
            "因为同一轮前面的操作被用户拒绝，这个动作也一并放弃，没有执行。"
            if explicit_user_rejection
            else "因为同一轮前面的确认没有生效，这个动作也一并放弃，没有执行。"
        )
        for call in calls:
            messages.append(
                _tool_error_message(
                    str(call.get("call_id") or ""),
                    {
                        "ok": False,
                        "error": primary_error,
                        "user_note": note,
                    },
                )
            )
        for call in rest:
            messages.append(
                _tool_error_message(
                    str(call.get("call_id") or ""),
                    {
                        "ok": False,
                        "error": deferred_error,
                        "user_note": note,
                    },
                )
            )
        return {
            "messages": messages,
            "pending_calls": [],
            "deferred_calls": [],
            "confirmed": False,
            "rejected": True,
            "via": "rejected",
            "approval_context": {},
            "confirmation_card": {},
        }

    def node_confirmation(state: ChatState) -> ChatState:
        """Wait for the exact visible card id; this node has no side effects."""
        card = state.get("confirmation_card") or {}
        decision = interrupt(card)
        context = state.get("approval_context") or {}
        supplied_id = str(decision.get("approval_id") or "") if isinstance(decision, dict) else ""
        expected_id = str(context.get("approval_id") or "") if isinstance(context, dict) else ""
        if not supplied_id or supplied_id != expected_id:
            return _reject_approval(
                state, error="确认标识缺失或与当前确认卡不一致，操作未执行。"
            )
        note = str(decision.get("note") or "").strip() if isinstance(decision, dict) else ""
        if not (isinstance(decision, dict) and decision.get("approved") is True):
            return _reject_approval(
                state,
                error="用户拒绝了这个操作，没有执行。",
                note=note,
                explicit_user_rejection=True,
            )
        live_mode = _live_tool_mode()
        if any(
            not tool_allowed(str(call.get("name") or ""), live_mode)
            for call in list(state.get("pending_calls") or [])
        ):
            return _reject_for_mode(state, live_mode)
        problem = _approval_context_error(state, live_mode)
        if problem:
            return _reject_approval(state, error=problem, note=note)
        return {"confirmed": True, "rejected": False}

    def node_disclosure_confirmation(state: ChatState) -> ChatState:
        """Approve/reject a metadata-only disclosure grant."""
        card = state.get("disclosure_card") or {}
        decision = interrupt(card)
        context = state.get("disclosure_context") or {}
        supplied_id = str(decision.get("approval_id") or "") if isinstance(decision, dict) else ""
        expected_id = str(context.get("approval_id") or "") if isinstance(context, dict) else ""
        call_id = str(context.get("call_id") or "") if isinstance(context, dict) else ""
        if not supplied_id or supplied_id != expected_id:
            payload = {"ok": False, "error_code": "MODEL_DATA_GRANT_INVALID"}
        elif str(context.get("project_id") or "") != str(state.get("project_id") or "") or str(context.get("thread_id") or "") != str(state.get("thread_id") or ""):
            payload = {"ok": False, "error_code": "MODEL_DATA_GRANT_INVALID"}
        elif str(context.get("tool_mode") or "") != _live_tool_mode():
            payload = {"ok": False, "error_code": "LLM_TOOL_MODE_CHANGED"}
        else:
            current_time = now()
            if current_time.tzinfo is None:
                current_time = current_time.replace(tzinfo=timezone.utc)
            expires_at = _parse_utc(context.get("expires_at"))
            if expires_at is None or expires_at <= current_time.astimezone(timezone.utc):
                payload = {"ok": False, "error_code": "MODEL_DATA_GRANT_EXPIRED"}
            else:
                payload = None
        if payload is None and not (isinstance(decision, dict) and decision.get("approved") is True):
            if disclosure_decider is not None:
                try:
                    disclosure_decider(
                        Path(state.get("project_dir") or ""),
                        str(context.get("grant_id") or ""),
                        False,
                    )
                except Exception:
                    pass
            payload = {"ok": False, "error_code": "MODEL_DATA_GRANT_REJECTED"}
        elif payload is None and disclosure_decider is None:
            payload = {"ok": False, "error_code": "MODEL_DATA_DISCLOSURE_UNAVAILABLE"}
        elif payload is None:
            try:
                outcome = disclosure_decider(
                    Path(state.get("project_dir") or ""),
                    str(context.get("grant_id") or ""),
                    True,
                )
                payload = {
                    "ok": True,
                    "status": str(outcome.get("status") or "approved") if isinstance(outcome, dict) else "approved",
                    "fields": ["sample_ids"],
                    "grant_id_hash": hashlib.sha256(str(context.get("grant_id") or "").encode("utf-8")).hexdigest(),
                    "next_step": "send_exact_disclosure",
                }
            except Exception as exc:  # noqa: BLE001 - grant decision fails closed
                payload = {"ok": False, "error_code": str(getattr(exc, "code", None) or "MODEL_DATA_GRANT_INVALID")}
        messages = list(state.get("messages") or [])
        if call_id:
            messages.append(_tool_error_message(call_id, payload) if not payload.get("ok") else {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(payload, ensure_ascii=False),
                "model_projection_version": 1,
            })
        return {
            "messages": messages,
            "pending_calls": [],
            "deferred_calls": list(state.get("deferred_calls") or []),
            "confirmed": False,
            "rejected": not bool(payload.get("ok")),
            "via": "rejected" if not payload.get("ok") else "tool",
            "reply": "精确样本信息披露申请已批准。" if payload.get("ok") else "精确样本信息披露申请未批准。",
            "disclosure_result": payload,
            "disclosure_context": {},
            "disclosure_card": {},
            "approval_context": {},
            "confirmation_card": {},
        }

    def route_after_guardrail(state: ChatState) -> str:
        if state.get("disclosure_context") and state.get("disclosure_card"):
            return "disclosure_confirmation"
        if state.get("approval_context"):
            return "confirmation"
        if state.get("pending_calls"):
            return "execute"
        if state.get("deferred_calls"):
            # 这组被参数校验整组挡下或被拒绝：继续处理队列里的下一组。
            return "guardrail"
        return "agent"

    def route_after_confirmation(state: ChatState) -> str:
        if state.get("pending_calls") and state.get("confirmed"):
            return "execute"
        return "agent"

    def node_execute(state: ChatState) -> ChatState:
        """Run the approved calls. The only node that may write.

        执行器抛异常**不能**掀掉整轮对话：真实工具会碰网络（SSH 扫目录、查远端
        状态），连不上是很正常的事，用户应该看到「这一步失败了，原因是 X」而不是
        「恢复执行失败」。所以这里把异常翻译成工具结果回灌给模型，让它自己决定
        是重试、换做法还是如实回答。
        """
        calls = list(state.get("pending_calls") or [])
        project_dir = Path(state.get("project_dir") or "runs/mvp_demo")
        approved = bool(state.get("confirmed"))
        messages = list(state.get("messages") or [])
        log = list(state.get("tool_log") or [])
        latest_reply = ""

        live_mode = _live_tool_mode()
        if any(str(call.get("name") or "") != "browse_remote_samples" and not tool_allowed(str(call.get("name") or ""), live_mode) for call in calls):
            return _reject_for_mode(state, live_mode)

        # Validate again immediately before the only side-effecting boundary.
        # This catches policy changes, argument/state tampering, expiry, and
        # project/shared-config drift that happened after the card was issued.
        for call in calls:
            name = str(call.get("name") or "")
            try:
                arguments = _call_arguments(state, call)
            except Exception:
                return _reject_approval(state, error="工具参数引用不可用，请重新发起调用。")
            problems = validate_call(name, arguments)
            if problems:
                return _reject_approval(
                    state, error="执行前参数校验失败：" + "；".join(problems)
                )
        current_policies = {
            confirmation_policy(str(call.get("name") or "")) for call in calls
        }
        needs_approval = any(policy != POLICY_NEVER for policy in current_policies)
        if needs_approval:
            if not approved:
                return _reject_approval(state, error="当前工具需要确认，但没有有效批准。")
            problem = _approval_context_error(state, live_mode)
            if problem:
                return _reject_approval(state, error=problem)
        elif state.get("approval_context"):
            return _reject_approval(state, error="工具确认策略已变化，请重新发起操作。")

        claim_path: Path | None = None
        claim_payload: dict[str, Any] = {}
        if needs_approval:
            context = state.get("approval_context") or {}
            approval_id = str(context.get("approval_id") or "")
            claim_path = _approval_claim_path(project_dir, approval_id)
            claim_payload = {
                "claim_version": 1,
                "status": "started",
                "approval_id_hash": hashlib.sha256(
                    approval_id.encode("utf-8")
                ).hexdigest(),
                "project_id": str(state.get("project_id") or ""),
                "thread_id": str(state.get("thread_id") or ""),
                "started_at": _utc_iso(now()),
                "calls": list(context.get("calls") or []),
            }
            try:
                _write_claim_payload(claim_path, claim_payload, exclusive=True)
            except FileExistsError:
                return _reject_approval(
                    state,
                    error=(
                        "这张确认卡已经开始执行或已被使用。为避免重复副作用，"
                        "本次没有再次执行。"
                    ),
                )
            except OSError:
                return _reject_approval(
                    state,
                    error="无法持久化一次性执行凭据，操作未执行。",
                )

        result_summaries: list[dict[str, Any]] = []
        for call in calls:
            name = str(call.get("name") or "")
            arguments = _call_arguments(state, call)
            try:
                execution_context = ToolExecutionContext(
                    project_id=str(state.get("project_id") or project_dir.name),
                    thread_id=state.get("thread_id"),
                )
                if _executor_accepts_context:
                    result = executor(name, arguments, project_dir, approved, execution_context)
                else:
                    # Compatibility for installations still injecting the pre-Task-6
                    # four-argument executor; production webapp uses the typed path.
                    result = executor(name, arguments, project_dir, approved)  # type: ignore[call-arg]
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                result = {
                    "ok": False,
                    "error": f"工具执行失败（{type(exc).__name__}）：{exc}",
                }
            if isinstance(result, ToolExecutionResult):
                if result_finalizer is not None:
                    result = result_finalizer(result, project_dir)
                provider_result = project_tool_result_for_model(name, result.model)
                log_result = project_tool_result_for_model(name, result.log_projection)
                ok_value = bool(provider_result.get("ok", True))
                reply_value = result.get("reply") or result.get("message")
                reply_present = bool(reply_value)
            else:
                # Legacy executors still return internal dicts.  Treat them as
                # request-local data and project a conservative provider/log
                # view so sample names, paths and report excerpts never enter
                # messages, checkpoints, or generic tool_log.
                provider_result = (
                    _legacy_model_projection(result)
                    if isinstance(result, dict)
                    else {"ok": False, "error": "工具返回了非预期的结果。"}
                )
                log_result = _legacy_model_projection(provider_result)
                ok_value = bool(provider_result.get("ok", True))
                reply_value = None
                reply_present = bool(provider_result.get("reply_category"))
            if not isinstance(result, dict):
                if not isinstance(result, ToolExecutionResult):
                    result = {"ok": False, "error": "工具返回了非预期的结果。"}
            result_summaries.append(
                {
                    "call_id": str(call.get("call_id") or ""),
                    "name": name,
                    "ok": ok_value,
                    "has_reply": reply_present,
                }
            )
            log.append(
                {
                    "name": name,
                    "risk": risk_of(name),
                    "arguments_hash": _canonical_hash(arguments),
                    # Durable logs keep only the versioned safe projection;
                    # raw arguments may contain sample ids, FASTQ names, or
                    # remote paths and remain request-local in the ephemeral
                    # argument store.
                    "argument_projection": project_tool_arguments_for_model(name, arguments),
                    "ok": ok_value,
                    "projection": log_result,
                }
            )
            if reply_value:
                latest_reply = _safe_provider_content(reply_value)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("call_id") or ""),
                    "content": json.dumps(provider_result, ensure_ascii=False, default=str),
                    "model_projection_version": 1,
                }
            )
            call_ref = str(call.get("call_ref") or "")
            if call_ref:
                ephemeral_store.delete(call_ref)

        if claim_path is not None:
            _write_claim_payload(
                claim_path,
                {
                    **claim_payload,
                    "status": "consumed",
                    "completed_at": _utc_iso(now()),
                    "results": result_summaries,
                },
                exclusive=False,
            )

        return {
            "messages": messages,
            "pending_calls": [],
            # deferred_calls 保持在 state 里，由 route_after_execute 决定是否继续。
            "tool_log": log,
            "confirmed": False,
            "reply": latest_reply or state.get("reply") or "",
            "approval_context": {},
            "confirmation_card": {},
        }

    def route_after_execute(state: ChatState) -> str:
        """还有顺延的组就继续确认，否则回模型收尾。"""
        if state.get("deferred_calls"):
            return "guardrail"
        return "agent"

    builder = StateGraph(ChatState)
    builder.add_node("agent", node_agent)
    builder.add_node("guardrail", node_guardrail)
    builder.add_node("confirmation", node_confirmation)
    builder.add_node("disclosure_confirmation", node_disclosure_confirmation)
    builder.add_node("execute", node_execute)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges(
        "agent", route_after_agent, {"guardrail": "guardrail", "end": END}
    )
    builder.add_conditional_edges(
        "guardrail",
        route_after_guardrail,
        {
            "confirmation": "confirmation",
            "disclosure_confirmation": "disclosure_confirmation",
            "execute": "execute",
            "guardrail": "guardrail",
            "agent": "agent",
        },
    )
    builder.add_conditional_edges(
        "confirmation", route_after_confirmation, {"execute": "execute", "agent": "agent"}
    )
    builder.add_edge("disclosure_confirmation", "agent")
    builder.add_conditional_edges(
        "execute", route_after_execute, {"guardrail": "guardrail", "agent": "agent"}
    )
    return builder.compile(checkpointer=checkpointer)
