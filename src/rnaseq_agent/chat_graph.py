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

    START → agent ──(调工具)──→ guardrail ──(需确认)──→ [interrupt 挂起]
              ↑                    │                          │
              │                    └──(只读,免确认)──┐        │ resume
              │                                      ↓        ↓
              └──────────── execute ←────────────────┴────────┘

**关键约束：节点副作用**。``interrupt()`` 恢复时 LangGraph 会**从头重跑当前节点**，
所以 ``guardrail`` 必须无副作用（只读 state），真实写盘只能发生在 ``execute``。
把写盘放在 interrupt 之前的同一个节点里，用户每确认一次就会多写一次。

langgraph 缺失时本模块不可用（``build_chat_graph`` 抛错），调用方应退回规则路由，
与项目既有的「可选依赖」策略一致。
"""

from __future__ import annotations

import json
from pathlib import Path
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
    ToolCall,
    ToolCallAccumulator,
    describe_call,
    parse_message_tool_calls,
    requires_confirmation,
    risk_of,
    tool_labels,
    tool_schemas,
    validate_call,
)

#: 一轮对话最多允许的工具往返次数，防止模型陷入自我循环。
MAX_TOOL_ITERATIONS = 8

#: ``ToolExecutor`` 契约：``(工具名, 参数, 项目目录, 是否已获人工批准) -> 结果``。
#: 结果至少含 ``{"ok": bool, "reply": str}``，可附带 ``gate`` / ``samples`` /
#: ``summary`` 等结构化字段供前端渲染。
#:
#: 执行器由调用方注入（依赖倒置）：本模块不 import ``webapp``（会造成循环依赖），
#: 也不自己构造 ``ProjectSession``——真实写盘链路只有一条，必须复用。
ToolExecutor = Callable[[str, dict[str, Any], Path, bool], dict[str, Any]]


class ChatState(TypedDict, total=False):
    """Everything the conversation graph persists between nodes."""

    project_dir: str
    project_id: str
    thread_id: str
    messages: list[dict[str, Any]]  # OpenAI 消息数组，跨轮保留工具结果
    pending_calls: list[dict[str, Any]]  # 等待执行/确认的工具调用
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


SYSTEM_PROMPT = (
    "你是 SYSU 多组学分析 Agent 的助手，用户正在配置一个 bulk RNA-seq 分析项目。\n"
    "\n"
    "你**有工具**可以真正读写项目配置。请直接调用工具，不要只说「我无法执行」——\n"
    "那是旧版本的措辞，现在你能做。\n"
    "\n"
    "工作方式：\n"
    "1. 用户给了数据路径、样本、参考基因组时，调用 write_project_config 真正写盘，\n"
    "   参数按用户原意填。写盘前系统会自动请用户确认，你不需要替他确认。\n"
    "2. 不确定项目里已有什么时，先调用 read_project_state 读，不要凭记忆猜。\n"
    "3. 分组：如果用户给了 N 个样本但只给了 M 个分组名（M < N），这是一个**循环\n"
    "   序列**——按用户给出的顺序循环分配（例如 2 个名字覆盖 4 个样本就是\n"
    "   A,B,A,B）。不要反问用户，也不要添加他没提到的分组名。\n"
    "4. 链特异性：用户说「未知 / 不知道 / 不确定」时填 unknown，不要填 auto。\n"
    "   用户明确说反链/正链/无链特异时按原意填。\n"
    "5. 写盘完成后，如果用户要求继续，再调用 generate_plan。\n"
    "\n"
    "涉及概念解释（例如「差异表达的原理」）时直接用文字回答，不要调用工具。\n"
    "回答用中文，简洁，不要罗列工具名，说人话。"
)


# -- LLM 调用 ---------------------------------------------------------------


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
    tools = tool_schemas()
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    accumulator = ToolCallAccumulator()
    content_parts: list[str] = []
    finish_reason = ""
    try:
        import requests

        with requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
            stream=True,
        ) as response:
            if response.status_code != 200:
                detail = response.text[:300] if response.text else ""
                yield ("error", f"模型接口返回 HTTP {response.status_code}。{detail}")
                return
            for raw in response.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                chunk_text = line[len("data:"):].strip()
                if chunk_text == "[DONE]":
                    break
                try:
                    chunk = json.loads(chunk_text)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    content_parts.append(str(piece))
                    yield ("delta", str(piece))
                if delta.get("tool_calls"):
                    accumulator.add_delta(delta["tool_calls"])
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as a reply
        yield ("error", f"调用模型失败：{type(exc).__name__}: {exc}")
        return

    calls = accumulator.finish()
    yield (
        "message",
        {
            "content": "".join(content_parts),
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.raw_arguments},
                }
                for call in calls
            ],
            "finish_reason": finish_reason,
        },
    )


# -- 图 ---------------------------------------------------------------------


def chat_thread_config(project_id: str, thread_id: str) -> dict[str, Any]:
    """Thread config for the conversation graph.

    前缀 ``chat:`` 把对话的 checkpoint 与分析流水线图（``thread_id == project_id``）
    彻底分开，两者共用同一个 ``langgraph.sqlite3`` 也不会互相覆盖。
    """
    return {"configurable": {"thread_id": f"chat:{project_id}:{thread_id}"}}


def build_chat_graph(
    *,
    executor: ToolExecutor,
    llm_config: dict[str, Any],
    checkpointer: Any = None,
    max_iterations: int = MAX_TOOL_ITERATIONS,
    timeout: float = 60.0,
):
    """Compile the conversation graph.

    ``executor`` 是唯一能产生副作用的入口，由调用方注入真实的写盘链路。
    """
    if not LANGGRAPH_AVAILABLE:  # pragma: no cover - optional dependency
        raise RuntimeError("langgraph 不可用，无法构建对话图。")

    def _append(state: ChatState, *new_messages: dict[str, Any]) -> list[dict[str, Any]]:
        return list(state.get("messages") or []) + list(new_messages)

    def _raw_call(call: ToolCall) -> dict[str, Any]:
        """Re-shape a parsed call back into the provider's ``tool_calls`` form."""
        return {
            "id": call.call_id,
            "type": "function",
            "function": {"name": call.name, "arguments": call.raw_arguments},
        }

    def _pending_call(call: ToolCall) -> dict[str, Any]:
        """Plain-dict form stored in graph state (must stay JSON-serialisable)."""
        return {
            "call_id": call.call_id,
            "name": call.name,
            "arguments": call.arguments,
            "parse_error": call.parse_error,
        }

    def node_agent(state: ChatState) -> ChatState:
        """Ask the model for the next turn. Pure: no writes, no external side effects."""
        iterations = int(state.get("iterations") or 0)
        if iterations >= max_iterations:
            return {
                "status": "max_iterations",
                "via": "llm",
                "pending_calls": [],
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
        for kind, payload in _stream_chat_completion(llm_config, messages, timeout=timeout):
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
                    "reply": "",
                }

        content = str(raw_message.get("content") or "")
        calls = parse_message_tool_calls(raw_message)

        if not calls:
            # 最终答复也要落进 messages：否则下一轮对话拿不到历史，
            # 用户说「刚才那个改成 16 线程」时模型不知道「那个」是什么。
            return {
                "status": "ok",
                "via": "llm",
                "reply": content,
                "messages": _append(
                    state, {"role": "assistant", "content": content}
                ),
                "pending_calls": [],
                # 最终答复已通过 writer 逐字推出；没推过（例如模型只回了空串）
                # 时由上层整段输出，避免重复。
                "streamed": streamed,
            }

        assistant_message: dict[str, Any] = {"role": "assistant", "content": content or None}
        assistant_message["tool_calls"] = [_raw_call(call) for call in calls]
        return {
            "status": "ok",
            "messages": _append(state, assistant_message),
            "pending_calls": [_pending_call(call) for call in calls],
            "iterations": iterations + 1,
        }

    def route_after_agent(state: ChatState) -> str:
        if state.get("pending_calls"):
            return "guardrail"
        return "end"

    def node_guardrail(state: ChatState) -> ChatState:
        """Risk gate. MUST stay side-effect free — LangGraph re-runs it on resume.

        需要确认就把决定权交还给人（``interrupt``）；参数本身不合法的调用在这里挡下，
        写成工具结果回灌给模型，让它自己纠正后重试——这是工具循环的纠错回路。
        """
        pending = list(state.get("pending_calls") or [])
        if not pending:
            return {"pending_calls": []}

        # 参数校验先于确认：不合法的调用没有让人签字的必要。
        rejected: list[dict[str, str]] = []
        valid: list[dict[str, Any]] = []
        for call in pending:
            name = str(call.get("name") or "")
            problems = validate_call(name, call.get("arguments") or {})
            if problems:
                rejected.append(
                    {
                        "call_id": str(call.get("call_id") or ""),
                        "name": name,
                        "reason": "；".join(problems),
                    }
                )
            else:
                valid.append(call)

        if not valid:
            messages = list(state.get("messages") or [])
            for item in rejected:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item["call_id"],
                        "content": json.dumps(
                            {"ok": False, "error": item["reason"]},
                            ensure_ascii=False,
                        ),
                    }
                )
            return {"messages": messages, "pending_calls": []}

        # 只读工具直接放行；写盘/执行必须人工确认（唯一真源在 agent_tools）。
        needs_confirmation = [call for call in valid if requires_confirmation(str(call.get("name")))]
        if not needs_confirmation:
            return {"pending_calls": valid, "confirmed": False}

        cards = [
            {
                "call_id": str(call.get("call_id") or ""),
                "name": str(call.get("name") or ""),
                "risk": risk_of(str(call.get("name") or "")),
                "label": tool_labels().get(str(call.get("name") or ""), str(call.get("name"))),
                "description": describe_call(
                    str(call.get("name") or ""), call.get("arguments") or {}
                ),
            }
            for call in needs_confirmation
        ]
        decision = interrupt(
            {
                "type": "tool_confirmation",
                "message": "以下操作会修改项目或启动分析，需要你确认后才会执行。",
                "calls": cards,
            }
        )

        approved = bool(decision) and decision.get("approved") is not False
        if approved:
            return {"pending_calls": valid, "confirmed": True}

        # 拒绝：不能悄悄丢掉，要让模型知道并给出替代方案。
        note = str((decision or {}).get("note") or "").strip()
        messages = list(state.get("messages") or [])
        for call in valid:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("call_id") or ""),
                    "content": json.dumps(
                        {
                            "ok": False,
                            "error": "用户拒绝了这个操作，没有执行。",
                            "user_note": note,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        return {"messages": messages, "pending_calls": [], "via": "rejected"}

    def route_after_guardrail(state: ChatState) -> str:
        if state.get("pending_calls"):
            return "execute"
        return "agent"

    def node_execute(state: ChatState) -> ChatState:
        """Run the approved calls. The only node that may write."""
        calls = list(state.get("pending_calls") or [])
        project_dir = Path(state.get("project_dir") or "runs/mvp_demo")
        approved = bool(state.get("confirmed"))
        messages = list(state.get("messages") or [])
        log = list(state.get("tool_log") or [])
        latest_reply = ""

        for call in calls:
            name = str(call.get("name") or "")
            arguments = call.get("arguments") or {}
            result = executor(name, arguments, project_dir, approved)
            log.append(
                {
                    "name": name,
                    "risk": risk_of(name),
                    "arguments": arguments,
                    "ok": bool(result.get("ok", True)),
                }
            )
            if result.get("reply"):
                latest_reply = str(result["reply"])
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("call_id") or ""),
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

        return {
            "messages": messages,
            "pending_calls": [],
            "tool_log": log,
            "confirmed": False,
            "reply": latest_reply or state.get("reply") or "",
        }

    builder = StateGraph(ChatState)
    builder.add_node("agent", node_agent)
    builder.add_node("guardrail", node_guardrail)
    builder.add_node("execute", node_execute)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges(
        "agent", route_after_agent, {"guardrail": "guardrail", "end": END}
    )
    builder.add_conditional_edges(
        "guardrail", route_after_guardrail, {"execute": "execute", "agent": "agent"}
    )
    builder.add_edge("execute", "agent")
    return builder.compile(checkpointer=checkpointer)
