"""LLM 可调用的工具契约：schema、风险分级、参数校验、tool_calls 解析。

架构原则（用户 2026-09-16 明确要求）：

> 思考/生成交给 LLM，安全/审批交给代码。

在此之前项目的 LLM **完全没有工具能力**：所谓「执行」全靠
``webchat.route_intent`` 的正则匹配，模型只能当分类器。本模块把能力**真正交给
模型**：模型自己决定调哪个工具、填什么参数（这是它擅长的事），而本模块与
``chat_graph`` 负责校验、分级、守门（这是代码擅长的事）。

正则路径没有被删掉，而是退居**兜底**：模型未配置或临时不可用时仍能完成
「贴配置 → 写盘」这类确定性操作。两条路共用同一个执行层，结果没有差别。

注：``llm.py``（服务 CLI 的受控枚举决策链）与 ``webapp._llm_messages``（对话图
不可用时的纯文本兜底）里的「不要调用工具」措辞**仍然有效**——那两条通道确实
没有工具可调，如实告诉模型比让它幻想自己能写盘更安全。

职责边界：

- 本模块是**纯声明 + 纯函数**：不读文件系统、不发网络、不写盘、不 import
  ``ProjectSession``。工具的真实执行在 ``chat_graph``（控制平面）。
- 风险分级与「是否需要人工确认」在这里定义，作为唯一真源：任何执行路径都必须
  问 ``requires_confirmation``，不许各自判断，否则必然漂移。

风险分级对应生物信息学语义：

- ``read``    —— 只读（读状态、扫目录）。自动执行，不打断用户。
- ``write``   —— 写项目配置（样本表 / 参数 / 回滚）。**必须人工确认**。
- ``execute`` —— 冻结契约、真正跑分析。**必须人工确认**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .config_intake import expand_conditions
from .safety import identifier_error, relative_filename_error

# -- 风险分级 ---------------------------------------------------------------

RISK_READ = "read"
RISK_WRITE = "write"
RISK_EXECUTE = "execute"

#: 需要人工确认才允许执行的风险级别。
#: 用户诉求：「风险度高的要给人确认」——写盘与执行都属于这一类，只读不打扰。
_CONFIRM_RISKS = frozenset({RISK_WRITE, RISK_EXECUTE})

#: 合法的链特异性取值。``unknown`` 是**合法值**而非缺失（用户要求「允许特异性未知
#: 的选项先写着」），必须与 webapp._normalize_strandedness 保持一致。
STRANDEDNESS_VALUES = ("auto", "unknown", "unstranded", "forward", "reverse")

DATA_SOURCES = ("remote_path", "local_upload")

#: 可开关的 pipeline 步骤（与 webchat._match_edit 的步骤列表一致）。
PIPELINE_STEPS = (
    "fastp",
    "star",
    "arriba",
    "featurecounts",
    "rsem",
    "diffexp",
    "cms",
)


# -- 工具声明 ---------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """One callable tool exposed to the model.

    ``parameters`` is a plain JSON Schema dict handed to the provider as-is;
    ``risk`` drives the guardrail; ``label`` is what the thinking panel shows.
    """

    name: str
    label: str
    description: str
    parameters: dict[str, Any]
    risk: str

    def as_openai_schema(self) -> dict[str, Any]:
        """Shape the spec the way ``/chat/completions`` expects it."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


_SAMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sample_id": {"type": "string", "description": "样本 ID，例如 SRR28119110"},
        "condition": {
            "type": "string",
            "description": "分组名，例如 control / treat。同一组的样本写同一个名字。",
        },
        "fastq_1": {"type": "string", "description": "R1 文件名（只写文件名，不要带目录）"},
        "fastq_2": {"type": "string", "description": "R2 文件名（双端测序必填）"},
    },
    "required": ["sample_id", "condition", "fastq_1"],
    "additionalProperties": False,
}


TOOL_SPECS: dict[str, ToolSpec] = {
    "read_project_state": ToolSpec(
        name="read_project_state",
        label="读取项目状态",
        description=(
            "读取当前项目的状态：状态机阶段、样本表、分组、链特异性、参考基因组、"
            "pipeline 开关、以及门禁结论。在回答用户「现在什么情况」或准备改配置之前"
            "先调用它，不要凭记忆猜测项目里有什么。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=RISK_READ,
    ),
    "browse_remote_samples": ToolSpec(
        name="browse_remote_samples",
        label="扫描服务器目录",
        description=(
            "通过已配置的 SSH 连接，只读扫描服务器上的绝对目录，识别 FASTQ 配对。"
            "当用户说「看看服务器上有什么」或给了目录让你确认数据时调用。"
            "只读操作，不会修改任何东西。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "服务器上的绝对 POSIX 路径，例如 /hwdata/.../fastq",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        risk=RISK_READ,
    ),
    "write_project_config": ToolSpec(
        name="write_project_config",
        label="写入项目配置",
        description=(
            "把样本表、FASTQ 目录、参考基因组、链特异性写入项目，创建分析会话。"
            "这是启动分析的必要步骤。\n"
            "重要：如果用户给了 N 个样本但只给了 M 个分组名（M < N），说明这是一个"
            "循环序列——按用户给出的顺序把分组名循环分配给样本，不要反问用户，"
            "也不要擅自添加未提到的分组名。\n"
            "如果链特异性未知，就填 unknown，不要猜 auto。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "data_source": {
                    "type": "string",
                    "enum": list(DATA_SOURCES),
                    "description": "remote_path = 数据已在服务器上；local_upload = 本地上传",
                },
                "fastq_dir": {
                    "type": "string",
                    "description": (
                        "FASTQ 所在目录。remote_path 时必须是服务器上的绝对 POSIX 路径；"
                        "local_upload 时是本地目录。"
                    ),
                },
                "samples": {
                    "type": "array",
                    "items": _SAMPLE_SCHEMA,
                    "description": "样本表，每个样本一条。顺序即用户给出的顺序。",
                    "minItems": 1,
                },
                "strandedness": {
                    "type": "string",
                    "enum": list(STRANDEDNESS_VALUES),
                    "description": (
                        "链特异性。用户说「未知 / 不知道 / 不确定」时填 unknown，"
                        "不要填 auto。"
                    ),
                },
                "gtf": {"type": "string", "description": "GTF 注释文件的绝对路径"},
                "genome_fasta": {"type": "string", "description": "基因组 FASTA 的绝对路径"},
                "star_index": {"type": "string", "description": "STAR 索引目录的绝对路径"},
                "rsem_prefix": {"type": "string", "description": "RSEM 索引前缀"},
            },
            "required": ["data_source", "fastq_dir", "samples"],
            "additionalProperties": False,
        },
        risk=RISK_WRITE,
    ),
    "configure_pipeline": ToolSpec(
        name="configure_pipeline",
        label="修改分析步骤开关",
        description="启用或关闭某个分析步骤（fastp / star / arriba / featurecounts / rsem / diffexp / cms）。",
        parameters={
            "type": "object",
            "properties": {
                "step": {"type": "string", "enum": list(PIPELINE_STEPS)},
                "enabled": {"type": "boolean"},
            },
            "required": ["step", "enabled"],
            "additionalProperties": False,
        },
        risk=RISK_WRITE,
    ),
    "set_run_resources": ToolSpec(
        name="set_run_resources",
        label="修改运行资源",
        description="修改计算资源：并行线程数、内存上限（GB）。",
        parameters={
            "type": "object",
            "properties": {
                "threads": {"type": "integer", "minimum": 1, "maximum": 128},
                "memory_gb": {"type": "integer", "minimum": 1, "maximum": 2048},
            },
            "additionalProperties": False,
        },
        risk=RISK_WRITE,
    ),
    "set_diffexp_reference": ToolSpec(
        name="set_diffexp_reference",
        label="设置差异表达对照组",
        description=(
            "设置差异表达（DESeq2）的对照组。用户说「以 control 为对照」时调用，"
            "同时会启用差异表达阶段。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "reference_condition": {
                    "type": "string",
                    "description": "作为对照的分组名，必须与样本表里的 condition 一致",
                }
            },
            "required": ["reference_condition"],
            "additionalProperties": False,
        },
        risk=RISK_WRITE,
    ),
    "rollback_changes": ToolSpec(
        name="rollback_changes",
        label="回滚变更",
        description="回滚最后一次配置变更。用户说「撤销 / 回滚 / 刚才那个不算」时调用。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=RISK_WRITE,
    ),
    "generate_plan": ToolSpec(
        name="generate_plan",
        label="生成执行计划",
        description=(
            "在配置就绪后生成确定性的执行计划（会跑门禁检查）。"
            "用户说「生成计划 / 下一步 / 怎么跑」时调用。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=RISK_EXECUTE,
    ),
    "confirm_contract": ToolSpec(
        name="confirm_contract",
        label="冻结分析契约",
        description="冻结当前执行计划为分析契约（含输入指纹）。冻结后配置不可再改，需回滚。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=RISK_EXECUTE,
    ),
    "run_analysis": ToolSpec(
        name="run_analysis",
        label="启动分析",
        description=(
            "真正启动分析流水线（在服务器上执行）。消耗计算资源且耗时较长，"
            "只在用户明确要求开始跑分析时调用。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=RISK_EXECUTE,
    ),
}


def tool_schemas() -> list[dict[str, Any]]:
    """All tools in the shape the provider expects, for the ``tools`` field."""
    return [spec.as_openai_schema() for name, spec in sorted(TOOL_SPECS.items())]


def risk_of(name: str) -> str:
    """Risk tier of a tool; unknown tools are treated as the most dangerous."""
    spec = TOOL_SPECS.get(name)
    return spec.risk if spec is not None else RISK_EXECUTE


def requires_confirmation(name: str) -> bool:
    """Whether code must ask the human before running this tool.

    这是**唯一真源**：执行路径不许自己判断风险，否则两处判断必然漂移。
    未知工具（模型幻觉出来的名字）按最高风险处理——宁可多问一次。
    """
    return name not in TOOL_SPECS or risk_of(name) in _CONFIRM_RISKS


def tool_labels() -> dict[str, str]:
    """Short human labels for the thinking panel."""
    return {name: spec.label for name, spec in TOOL_SPECS.items()}


# -- 参数校验 ---------------------------------------------------------------


def _absolute_posix_path_error(value: Any, field_name: str) -> str | None:
    """Reject anything that is not a plain absolute POSIX path."""
    text = str(value or "").strip()
    if not text:
        return f"{field_name} 不能为空。"
    if not text.startswith("/"):
        return f"{field_name} 必须是服务器上的绝对路径（以 / 开头）：{text}"
    if any(ch in text for ch in ("\n", "\r", "\x00")):
        return f"{field_name} 含非法字符。"
    if ".." in text.split("/"):
        return f"{field_name} 不允许包含 .. ：{text}"
    return None


def validate_call(name: str, arguments: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems; empty means the call is sane.

    模型可能幻觉出不存在的字段、把目录当文件名、或者编造路径。这里在**执行之前**
    挡下来，并把原因如实回报给模型，让它自己纠正后重试——这是工具循环的纠错回路。
    """
    if name not in TOOL_SPECS:
        return [f"不存在名为 {name} 的工具。"]

    problems: list[str] = []
    spec = TOOL_SPECS[name]
    properties = spec.parameters.get("properties", {})

    # 结构：只接受 schema 里声明过的键，避免模型塞入会被下游误读的字段。
    for key in arguments:
        if key not in properties:
            problems.append(f"工具 {name} 不接受参数 {key}。")
    for key in spec.parameters.get("required", []):
        if arguments.get(key) in (None, "", []):
            problems.append(f"工具 {name} 缺少必需参数 {key}。")
    if problems:
        return problems

    if name == "browse_remote_samples":
        error = _absolute_posix_path_error(arguments.get("path"), "path")
        if error:
            problems.append(error)

    if name == "write_project_config":
        problems.extend(_validate_write_config(arguments))

    if name == "configure_pipeline":
        step = str(arguments.get("step", ""))
        if step not in PIPELINE_STEPS:
            problems.append(f"未知的分析步骤 {step}，可选：{', '.join(PIPELINE_STEPS)}。")
        if not isinstance(arguments.get("enabled"), bool):
            problems.append("enabled 必须是布尔值。")

    if name == "set_run_resources":
        if "threads" not in arguments and "memory_gb" not in arguments:
            problems.append("至少要给出 threads 或 memory_gb 之一。")

    return problems


def _validate_write_config(arguments: dict[str, Any]) -> list[str]:
    """Field-level checks for ``write_project_config``."""
    problems: list[str] = []

    data_source = str(arguments.get("data_source", ""))
    if data_source not in DATA_SOURCES:
        problems.append(f"data_source 必须是 {DATA_SOURCES} 之一。")
    else:
        error = _absolute_posix_path_error(arguments.get("fastq_dir"), "fastq_dir")
        # 本地目录是 Windows/Unix 路径都可以，因此只在 remote_path 时强制 POSIX 绝对。
        if error and data_source == "remote_path":
            problems.append(error)
        elif not str(arguments.get("fastq_dir") or "").strip():
            problems.append("fastq_dir 不能为空。")

    strandedness = arguments.get("strandedness")
    if strandedness is not None and str(strandedness) not in STRANDEDNESS_VALUES:
        problems.append(
            f"strandedness 必须是 {STRANDEDNESS_VALUES} 之一（未知请填 unknown）。"
        )

    samples = arguments.get("samples")
    if not isinstance(samples, list) or not samples:
        problems.append("samples 必须是非空数组。")
        return problems

    for index, sample in enumerate(samples, start=1):
        if not isinstance(sample, dict):
            problems.append(f"samples[{index}] 必须是对象。")
            continue
        sample_id = sample.get("sample_id")
        error = identifier_error(sample_id, f"samples[{index}].sample_id")
        if error:
            problems.append(error)
        for key in ("fastq_1", "fastq_2"):
            value = sample.get(key)
            if value in (None, ""):
                if key == "fastq_1":
                    problems.append(f"samples[{index}] 缺少 fastq_1。")
                continue
            error = relative_filename_error(value, f"samples[{index}].{key}")
            if error:
                problems.append(
                    f"{error}（{key} 只写文件名，目录请放进 fastq_dir）"
                )

    seen: set[str] = set()
    for index, sample in enumerate(samples, start=1):
        if not isinstance(sample, dict):
            continue
        sample_id = str(sample.get("sample_id") or "")
        if sample_id and sample_id in seen:
            problems.append(f"样本 ID 重复：{sample_id}")
        seen.add(sample_id)

    return problems


def normalize_write_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fill in conditions the model left blank, by cycling the names it did give.

    模型通常能自己把 N 个样本分好组（这正是有工具之后它该做的事）。但当它只给出
    两个分组名却被要求覆盖四个样本时，这里按用户给出的顺序循环补齐——架构原则
    「LLM 理解需求，确定性代码执行」的落点：模型负责理解，代码负责保证结果可用。

    若模型一个分组名都没给，则补空串，由下游门禁如实报「缺字段」，不猜。
    """
    samples = arguments.get("samples")
    if not isinstance(samples, list):
        return arguments

    conditions = [
        str(sample.get("condition") or "").strip() if isinstance(sample, dict) else ""
        for sample in samples
    ]
    if all(conditions):
        return arguments

    # 保序去重，得到模型实际给出的分组名序列。
    names: list[str] = []
    for condition in conditions:
        if condition and condition not in names:
            names.append(condition)

    normalized = [dict(sample) if isinstance(sample, dict) else sample for sample in samples]
    if not names:
        return {**arguments, "samples": normalized}

    expanded = expand_conditions(names, len(normalized)).conditions
    for index, sample in enumerate(normalized):
        if isinstance(sample, dict) and not str(sample.get("condition") or "").strip():
            sample["condition"] = expanded[index]
    return {**arguments, "samples": normalized}


# -- 确认卡片 ---------------------------------------------------------------


def describe_call(name: str, arguments: dict[str, Any]) -> str:
    """Render one tool call as something a human can approve or reject.

    这是「写盘前要人确认」看到的那段文字。必须**如实**描述将要发生什么，
    不能只写工具名——用户是在为一个具体动作签字，不是在为一个函数名签字。
    """
    spec = TOOL_SPECS.get(name)
    title = spec.label if spec is not None else name

    if name == "write_project_config":
        lines = [f"{title}："]
        data_source = arguments.get("data_source")
        lines.append(
            f"  数据来源：{'服务器已有数据' if data_source == 'remote_path' else '本地上传'}"
        )
        lines.append(f"  FASTQ 目录：{arguments.get('fastq_dir') or '（未给出）'}")
        samples = arguments.get("samples") or []
        if isinstance(samples, list) and samples:
            lines.append(f"  样本（{len(samples)} 个）：")
            for index, sample in enumerate(samples, start=1):
                if not isinstance(sample, dict):
                    continue
                sample_id = sample.get("sample_id") or f"样本{index}"
                condition = sample.get("condition") or "未分组"
                lines.append(f"    {index}. {sample_id} → {condition}")
        lines.append(f"  链特异性：{arguments.get('strandedness') or 'auto'}")
        for key, label in (
            ("gtf", "GTF 注释"),
            ("genome_fasta", "基因组 FASTA"),
            ("star_index", "STAR 索引"),
            ("rsem_prefix", "RSEM 索引前缀"),
        ):
            if arguments.get(key):
                lines.append(f"  {label}：{arguments[key]}")
        return "\n".join(lines)

    if name == "configure_pipeline":
        verb = "启用" if arguments.get("enabled") else "关闭"
        return f"{title}：{verb} {arguments.get('step')}"

    if name == "set_run_resources":
        parts = []
        if arguments.get("threads") is not None:
            parts.append(f"线程数 → {arguments['threads']}")
        if arguments.get("memory_gb") is not None:
            parts.append(f"内存 → {arguments['memory_gb']} GB")
        return f"{title}：{'，'.join(parts) or '（无变化）'}"

    if name == "set_diffexp_reference":
        return f"{title}：以 {arguments.get('reference_condition')} 为对照，并启用差异表达"

    if name == "browse_remote_samples":
        return f"{title}：只读扫描 {arguments.get('path')}"

    return title


# -- tool_calls 解析 --------------------------------------------------------


@dataclass
class ToolCall:
    """One model-requested tool invocation."""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str = ""


def _parse_arguments(raw: Any) -> tuple[dict[str, Any], str]:
    """Arguments arrive as a JSON string; tolerate an already-decoded dict."""
    if isinstance(raw, dict):
        return raw, ""
    text = str(raw or "").strip()
    if not text:
        return {}, ""
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"参数不是合法 JSON：{exc}"
    if not isinstance(decoded, dict):
        return {}, "参数必须是 JSON 对象。"
    return decoded, ""


def parse_message_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    """Extract tool calls from a non-streaming ``choices[0].message``."""
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    calls: list[ToolCall] = []
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        if not isinstance(function, dict):
            function = {}
        arguments, error = _parse_arguments(function.get("arguments"))
        calls.append(
            ToolCall(
                call_id=str(raw.get("id") or f"call_{index}"),
                name=str(function.get("name") or "").strip(),
                arguments=arguments,
                raw_arguments=str(function.get("arguments") or ""),
                parse_error=error,
            )
        )
    return calls


class ToolCallAccumulator:
    """Reassemble tool calls from streaming deltas.

    OpenAI 流式协议里 ``tool_calls`` 是按 ``index`` 分片到达的，``function.arguments``
    是**字符串拼接**（一个 JSON 可能被切成几十段）。不增量拼接就会拿到半截 JSON。
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def add_delta(self, raw_calls: Any) -> None:
        if not isinstance(raw_calls, list):
            return
        for entry in raw_calls:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            slot = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if entry.get("id"):
                slot["id"] = str(entry["id"])
            function = entry.get("function") or {}
            if isinstance(function, dict):
                if function.get("name"):
                    slot["name"] += str(function["name"])
                if function.get("arguments"):
                    slot["arguments"] += str(function["arguments"])

    def finish(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            slot = self._calls[index]
            arguments, error = _parse_arguments(slot["arguments"])
            calls.append(
                ToolCall(
                    call_id=slot["id"] or f"call_{index}",
                    name=slot["name"].strip(),
                    arguments=arguments,
                    raw_arguments=slot["arguments"],
                    parse_error=error,
                )
            )
        return calls
