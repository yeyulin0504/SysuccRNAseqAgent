"""Local rule router for the web chat panel.

Design-doc 5.2: the user-facing assistant (LLM today, rule router in this
MVP) only emits structured capability/action requests; deterministic code
then validates and executes them. This module is the rule-based stand-in
that the web chat panel can exercise immediately without an API key.

Every intent maps to a *session action* the ProjectSession already supports:
new / plan / confirm / edit / rollback / status / summary / help. The
router never produces shell commands.
"""

from __future__ import annotations

import re
from typing import Any


class ChatIntent:
    """Structured request produced by the router."""

    def __init__(
        self,
        action: str,
        *,
        message: str = "",
        params: dict[str, Any] | None = None,
        note: str = "",
    ) -> None:
        self.action = action
        self.message = message
        self.params = params or {}
        self.note = note

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "message": self.message,
            "params": self.params,
            "note": self.note,
        }


# -- intent matching --------------------------------------------------------


def route_intent(text: str) -> ChatIntent | None:
    """Map a free-text user message to a session action.

    Returns None when the message is not actionable (a plain question), so
    the caller can answer conversationally instead.
    """
    lowered = text.strip().lower()
    if not lowered:
        return None

    # Read-only remote sample discovery.  The executor owns SSH and command
    # construction; the router only extracts an optional absolute path.
    if _has(lowered, ["浏览", "扫描", "查找", "找样本", "找我的样本", "browse", "scan"] ) and _has(
        lowered, ["目录", "服务器", "样本", "fastq", "rna-seq", "rnaseq", "directory"]
    ):
        path_match = re.search(r"(/[a-zA-Z0-9._~+\-/]+)", text)
        params = {"path": path_match.group(1).rstrip(".,，。") } if path_match else {}
        return ChatIntent(
            "browse_samples",
            message="正在只读扫描服务器目录并识别 FASTQ 配对。",
            params=params,
        )

    # Roll back / 撤销
    if _has(lowered, ["回滚", "撤销", "取消刚才", "undo", "rollback"]):
        return ChatIntent("rollback", message="准备回滚最后一次变更。")

    # Plan / 生成计划
    if _has(lowered, ["生成计划", "制定计划", "计划", "plan", "规划", "怎么跑", "流程是什么"]):
        return ChatIntent("plan", message="正在生成执行计划。")

    # Confirm / 确认冻结
    if _has(lowered, ["确认", "冻结", "没问题", "可以执行", "confirm", "freeze", "就这么办", "就这样"]):
        return ChatIntent("confirm", message="正在确认并冻结契约。")

    # Edit / 修改参数（线程、内存、开关步骤）
    edit = _match_edit(lowered)
    if edit is not None:
        return edit

    # DEG（框架 15.3 条件开放）
    deg = _match_diffexp(lowered)
    if deg is not None:
        return deg

    # Status / 状态
    if _has(lowered, ["状态", "现在怎么样", "进行到哪", "status", "看看"]):
        return ChatIntent("status", message="正在读取当前状态。")

    # New / 新建（仅当无项目时由前端处理）
    if _has(lowered, ["新建", "新项目", "create", "new project", "从头开始"]):
        return ChatIntent("new", message="准备新建项目草稿。")

    # Summary / 摘要
    if _has(lowered, ["摘要", "总结", "summary", "概括"]):
        return ChatIntent("summary", message="正在汇总当前项目。")

    # Help
    if _has(lowered, ["帮助", "help", "能做什么", "怎么用", "支持什么"]):
        return ChatIntent(
            "help",
            message="我可以帮你：新建项目、生成执行计划、确认冻结契约、修改参数（如线程数）、回滚变更、查看状态。",
        )

    return None


def _has(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _match_edit(text: str) -> ChatIntent | None:
    """Match edit intents: thread/memory toggles, pipeline step toggles."""
    # server.threads = N
    threads = re.search(r"(?:线程|threads?|cpu)[^\d]{0,6}(\d+)", text)
    if threads and any(k in text for k in ["线程", "thread", "cpu", "核"]):
        return ChatIntent(
            "edit",
            message=f"把线程数改为 {threads.group(1)}。",
            params={"server": {"threads": int(threads.group(1))}},
            note=f"对话修改：线程数 → {threads.group(1)}",
        )

    # memory = N (GB)
    memory = re.search(r"(?:内存|memory)[^\d]{0,6}(\d+)", text)
    if memory:
        return ChatIntent(
            "edit",
            message=f"把内存改为 {memory.group(1)} GB。",
            params={"server": {"memory_gb": int(memory.group(1))}},
            note=f"对话修改：内存 → {memory.group(1)} GB",
        )

    # Enable / disable a pipeline step
    for step in ["fastp", "star", "arriba", "featurecounts", "rsem"]:
        if step not in text:
            continue
        if _has(text, ["关掉", "关闭", "不用", "停用", "disable", "去掉"]):
            return ChatIntent(
                "edit",
                message=f"关闭 {step} 步骤。",
                params={"pipeline": {step: {"enabled": False}}},
                note=f"对话修改：关闭 {step}",
            )
        if _has(text, ["打开", "启用", "开启", "enable", "加上"]):
            return ChatIntent(
                "edit",
                message=f"启用 {step} 步骤。",
                params={"pipeline": {step: {"enabled": True}}},
                note=f"对话修改：启用 {step}",
            )
    return None


def _match_diffexp(text: str) -> ChatIntent | None:
    """Match the conditional DESeq2 DE stage (framework 15.3)."""
    diffexp_requested = _has(text, ["差异表达", "差异分析", "diffexp", "deseq", "de 分析", "跑差异"])
    if not diffexp_requested:
        return None

    # 差异表达本身是一种 pipeline 步骤：启用/关闭语义与其它步骤一致。
    if _has(text, ["关掉", "关闭", "不用", "停用", "disable", "去掉", "不做", "取消"]):
        return ChatIntent(
            "edit",
            message="已关闭差异表达（DESeq2 条件开放阶段不再执行）。",
            params={"pipeline": {"diffexp": {"enabled": False}}},
            note="对话修改：关闭 diffexp",
        )

    # reference / 对照组：control vs treatment 的方向。
    # 指定对照组的语义即“以该组为参考执行差异表达”，因此总是启用 diffexp；
    # 关闭意图已在上面的关闭分支拦截。
    reference = _match_reference_condition(text)
    if reference is not None:
        return ChatIntent(
            "edit",
            message=f"已启用差异表达，对照组为 {reference}（DESeq2 contrast 基于该组）。",
            params={
                "pipeline": {"diffexp": {"enabled": True}},
                "diffexp": {"reference_condition": reference},
            },
            note=f"对话修改：diffexp reference={reference}",
        )

    # 默认：启用（并询问设计是否满足由门禁在 plan/gate 时校验）。
    if _has(text, ["打开", "启用", "enable", "加上", "做", "执行", "跑", "需要"]):
        return ChatIntent(
            "edit",
            message="已启用差异表达（DESeq2 条件开放）。设计门禁（每组≥3、两组、无混杂）会在生成计划时校验。",
            params={"pipeline": {"diffexp": {"enabled": True}}},
            note="对话修改：启用 diffexp",
        )

    # 含糊的差异表达询问：返回 diffexp 状态查询动作。
    return ChatIntent(
        "deg_status",
        message="正在检查差异表达条件（DESeq2 适用性）。",
    )


def _match_reference_condition(text: str) -> str | None:
    """Resolve a reference/control group name like “以 control 为对照”."""
    patterns = [
        r"(?:以|用)\s*([a-z_][a-z0-9_]*)\s*(?:为|作为|做)?\s*(?:对照|参考|reference)",
        r"(?:对照|参考|reference)\s*(?:是|为|:)\s*([a-z_][a-z0-9_]*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


# -- execution -------------------------------------------------------------


def execute_intent(session, intent: ChatIntent) -> dict[str, Any]:
    """Execute a routed intent against the audited ProjectSession.

    Returns a structured response the web panel can render. Exceptions are
    converted into error replies so the UI never crashes.
    """
    from .session import SessionError

    action = intent.action
    try:
        if action == "new":
            if session.config is not None:
                return {"error": "当前已有项目，无法重复新建。请先回滚或使用新目录。"}
            return {"error": "新建项目需要填写样本信息，请在左侧表单创建。"}

        if action == "plan":
            plan = session.plan()
            return {"state": session.state, "reply": intent.message, "steps": plan.steps}

        if action == "confirm":
            contract = session.confirm()
            return {
                "state": session.state,
                "reply": f"契约已冻结：{contract['contract_id'][:24]}…",
                "contract_id": contract["contract_id"],
            }

        if action == "edit":
            gate = session.edit(intent.params, note=intent.note)
            return {"state": session.state, "reply": intent.message, "gate": gate.formatted()}

        if action == "rollback":
            session.rollback()
            return {"state": session.state, "reply": "已回滚到最后一次变更之前。"}

        if action == "status":
            return {"state": session.state, "reply": "当前状态如下。", "summary": session.summary_lines()}

        if action == "deg_status":
            """DEG applicability status: enabled? design satisfies the gate?"""
            if session.config is None:
                return {"error": "当前还没有项目。请先在左侧创建项目。"}
            config = session.config
            requested = bool(config.get("pipeline", {}).get("diffexp", {}).get("enabled"))
            from .differential import (
                DEG_DESIGN_FORMULA,
                deg_gate,
                diffexp_design_of,
                diffexp_is_requested,
            )

            if not diffexp_is_requested(config):
                return {
                    "state": session.state,
                    "reply": (
                        "差异表达（DESeq2，DESeq2 两组对比）尚未启用。"
                        "当样本满足非配对两组设计（每组 ≥3 个生物学重复、batch 与 condition 不混杂）时，"
                        "可对我说“启用差异表达”或“以 control 为对照跑差异表达”。"
                    ),
                }
            design = diffexp_design_of(config)
            gate = deg_gate(config)
            if gate.ok:
                return {
                    "state": session.state,
                    "reply": (
                        f"差异表达已启用并通过设计门禁：{DEG_DESIGN_FORMULA}，"
                        f"对比 {design['contrast']}（reference={design['reference_condition']}）。"
                    ),
                }
            return {
                "state": session.state,
                "reply": "差异表达已启用，但设计不满足冻结模板：",
                "gate": gate.reasons,
            }

        if action == "summary":
            return {"state": session.state, "reply": "项目摘要如下。", "summary": session.summary_lines()}

        if action == "help":
            return {"reply": intent.message, "state": session.state}

        return {"error": f"未知动作：{action}"}
    except SessionError as exc:
        return {"error": str(exc), "state": session.state}
    except Exception as exc:  # noqa: BLE001 - keep the panel alive
        return {"error": f"{type(exc).__name__}: {exc}", "state": session.state}
