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

# 知识提问的识别标记：用户在问概念 / 原理 / 区别，而不是在下指令。
_QUESTION_MARKERS = (
    "解释", "什么是", "什么叫", "区别", "为什么", "原理",
    "怎么理解", "介绍一下", "讲讲", "讲一下", "说明一下", "有什么不同",
    "有何不同", "影响", "含义", "是什么意思", "科普", "怎么看", "合理吗",
    "需要注意", "注意事项", "教我",
)

# 明确的执行指令：出现这些词说明用户确实要系统去做事，提问守卫随即失效，
# 交回下面的规则分支（否则「帮我做差异表达」会被误当成提问放走）。
_EXECUTION_MARKERS = (
    "帮我做", "帮我跑", "帮我生成", "帮我执行", "给我做", "替我",
    "启用", "打开", "开启", "关闭", "关掉", "停用", "禁用", "加上",
    "去掉", "取消", "为对照", "作为参照", "作为对照",
    "生成计划", "制定计划", "确认冻结", "回滚", "撤销",
)

# 把对话里的配置真正写到项目上（用户诉求 2026-09-16）。
#
# 背景：用户把 FASTQ 目录 / 文件名 / 参考基因组贴进对话，然后说「你帮我执行」，
# 此前这句被路由成 ``None`` 交给大模型，而大模型没有工具能力，只能回
# 「我无法直接执行」；用户再点「生成执行计划」，系统又回「还没有分析会话」——
# 形成「AI 说做不到 ↔ 不知道该点哪」的死循环。这里把它识别成**可执行的落地动作**。
_CONFIGURE_MARKERS = (
    "你帮我执行", "帮我执行", "帮我填", "帮我保存", "保存下来", "保存这些",
    "写入配置", "写入项目", "应用到项目", "应用配置", "帮我配置", "帮我落地",
    "把这些信息", "把这些路径", "帮我建项目", "帮我创建会话", "开始配置",
    "按这个配置", "就按这个", "照这个执行",
)

# 这些意图的语义比「落地配置」更具体，命中时不能被抢走。
# 例如「帮我执行差异表达」是要开一个 pipeline 步骤，不是要写样本表。
_CONFIGURE_EXCLUDE = (
    "差异表达", "差异分析", "deseq", "diffexp", "de 分析",
    "线程", "thread", "内存", "memory", "cpu",
    "回滚", "撤销", "计划", "plan", "确认", "冻结",
)


def _is_knowledge_question(text: str) -> bool:
    """True when the message asks about concepts rather than ordering work.

    用户诉求（2026-09-15）：「我需要 llm 的思考执行结果」。此前
    「解释一下差异表达的原理」「测序深度对差异表达有什么影响」这类提问会
    命中下面的规则意图（被当成步骤开关 / 状态查询），于是配好大模型也只能
    拿到一句生硬的规则文案。这里先做一次守卫，把纯提问放行给大模型。
    """
    if not _has(text, list(_QUESTION_MARKERS)):
        return False
    return not _has(text, list(_EXECUTION_MARKERS))


def route_intent(text: str) -> ChatIntent | None:
    """Map a free-text user message to a session action.

    Returns None when the message is not actionable (a plain question), so
    the caller can answer conversationally instead.
    """
    lowered = text.strip().lower()
    if not lowered:
        return None

    # 概念性提问优先交回调用方（大模型）回答，而不是被规则意图抢走。
    if _is_knowledge_question(lowered):
        return None

    # 落地配置：把对话里贴出的路径 / 样本 / 参考真正写进项目。
    # 必须排在其它分支之前——它是「照我说的做」这类最明确的执行指令，
    # 交给下面的 plan / edit 分支会被解释成别的东西。
    if _has(lowered, list(_CONFIGURE_MARKERS)) and not _has(lowered, list(_CONFIGURE_EXCLUDE)):
        return ChatIntent(
            "configure_project",
            message="正在把对话里的配置写入项目。",
        )

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
            message=(
                "我可以帮你：新建项目、生成执行计划、确认冻结契约、修改参数（如线程数）、"
                "回滚变更、查看状态。\n"
                "把 FASTQ 目录与文件名、样本分组、参考基因组贴给我，"
                "最后说一句「你帮我执行」，我就会真正写入项目并把结果回报给你。"
            ),
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

# 需要真实项目配置（project.json）才能执行的动作。项目尚未建立时状态恒为
# ``idle``，StateMachine 会抛「当前状态 idle 不允许该操作」——那是实现细节，
# 用户需要的是「先去哪一步」。
_NEEDS_PROJECT = {"plan", "confirm", "edit", "rollback", "deg_status", "summary", "run"}


def _missing_project_reply(action: str) -> str:
    """Turn a bare state-machine rejection into an actionable next step."""
    if action == "plan":
        return (
            "这个项目还没有分析会话（缺 project.json），所以暂时无法生成执行计划。\n"
            "请先在「分析工作台」完成输入与样本配置：\n"
            "  1) 选择数据来源（远程路径 / 本地上传 / counts 矩阵）；\n"
            "  2) 填好目录与参考基因组，应用样本表；\n"
            "做完这步我就能生成执行计划了。"
        )
    if action == "confirm":
        return "还没有可冻结的执行计划。请先完成输入配置并生成执行计划。"
    if action == "deg_status":
        return "这个项目还没有分析会话。请先在工作台完成输入与样本配置。"
    if action == "run":
        return "这个项目还没有分析会话，无法启动分析。请先完成输入配置、生成计划并确认冻结。"
    return (
        "这个项目还没有分析会话（缺 project.json），无法执行该操作。"
        "请先在「分析工作台」完成数据来源与样本配置。"
    )


def execute_intent(session, intent: ChatIntent) -> dict[str, Any]:
    """Execute a routed intent against the audited ProjectSession.

    Returns a structured response the web panel can render. Exceptions are
    converted into error replies so the UI never crashes.
    """
    from .session import SessionError

    action = intent.action

    # 没有项目配置时状态恒为 idle，任何状态机动作都会抛出
    # 「当前状态 idle 不允许该操作」这类裸错误——用户看不懂该干什么。
    # 这里统一换成「下一步做什么」的可操作指引。
    if action in _NEEDS_PROJECT and session.config is None:
        return {"state": session.state, "reply": _missing_project_reply(action)}

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

        if action == "run":
            # 真正启动分析（LLM 工具路径用）。session.execute 要求契约已冻结，
            # 未冻结时抛出的 SessionError 会被下面的 except 转成可读提示。
            outcome = session.execute(wait=False)
            return {
                "state": outcome.get("state", session.state),
                "reply": (
                    f"分析已启动（状态 {outcome.get('state', session.state)}）。"
                    f"\n{outcome.get('message') or ''}".rstrip()
                ),
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
