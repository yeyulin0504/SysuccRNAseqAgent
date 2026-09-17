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

确认**粒度**由 :func:`confirmation_policy` 给出（用户 2026-09-16 定的边界）：

- 配置类（write）**合并成一张卡片**，一次批准全部执行；
- 执行类（execute）**各自单独一张卡片**，不许被批量批准夹带过去；
- 例外：连接配置（``edit_connection``）虽然是写盘，但改的是远端执行目标，
  语义上比改样本表更重，用 ``ToolSpec.policy`` 钉成单独确认。

工具面覆盖「配置 + 分析执行」的**全量**能力（用户 2026-09-16 要求放开）：

======================  ======  ================================================
工具                    策略    落到哪段既有实现
======================  ======  ================================================
read_project_state      never   ``ProjectSession.config`` 只读投影
browse_remote_samples   never   ``webapp._scan_remote_samples``（只读 SSH）
refresh_project_status  batch   ``session.refresh_status``（更新项目状态并写回本地会话）
get_project_report      batch   ``session.report``（生成/覆盖报告文件）
write_project_config    batch   ``webapp._write_project_session``（首次建会话）
edit_samples            batch   ``session.edit``（``samples.items`` 整体覆盖）
edit_reference          batch   ``session.edit``（``reference`` 浅合并）
edit_connection         solo    ``session.edit``（``server`` 浅合并）
configure_pipeline      batch   ``session.edit``
set_run_resources       batch   ``session.edit``
set_diffexp_reference   batch   ``session.edit``
set_cms_options         batch   ``session.edit``
rollback_changes        batch   ``session.rollback``
generate_plan           solo    ``session.plan``
confirm_contract        solo    ``session.confirm``
run_analysis            solo    ``session.execute`` / ``session.execute_stage``
======================  ======  ================================================
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .config_intake import expand_conditions
from .safety import identifier_error, relative_filename_error
from .ssh_identity import validate_ssh_patch

# -- 风险分级 ---------------------------------------------------------------

RISK_READ = "read"
RISK_WRITE = "write"
RISK_EXECUTE = "execute"

# -- 全局工具权限模式 -------------------------------------------------------

#: 完全关闭模型工具。大模型仍可回答问题，但 provider 请求里不再带 tools。
TOOL_MODE_DISABLED = "disabled"
#: 只允许真正的只读工具。
TOOL_MODE_READ_ONLY = "read_only"
#: 允许只读与经过人工确认的写配置工具，不允许执行分析。
TOOL_MODE_APPROVED_WRITE = "approved_write"
#: 允许全部已声明工具；写入与执行仍必须经过原有人工确认。
TOOL_MODE_APPROVED_EXECUTE = "approved_execute"

TOOL_MODES = (
    TOOL_MODE_DISABLED,
    TOOL_MODE_READ_ONLY,
    TOOL_MODE_APPROVED_WRITE,
    TOOL_MODE_APPROVED_EXECUTE,
)
DEFAULT_TOOL_MODE = TOOL_MODE_APPROVED_EXECUTE

_MODE_RISKS = {
    TOOL_MODE_DISABLED: frozenset(),
    TOOL_MODE_READ_ONLY: frozenset({RISK_READ}),
    TOOL_MODE_APPROVED_WRITE: frozenset({RISK_READ, RISK_WRITE}),
    TOOL_MODE_APPROVED_EXECUTE: frozenset({RISK_READ, RISK_WRITE, RISK_EXECUTE}),
}


def normalize_tool_mode(value: Any) -> str:
    """Return a canonical tool mode, defaulting only when the field is absent.

    Missing settings preserve compatibility with installations created before
    the kill switch. An explicit unknown value is rejected: silently mapping a
    typo to the broadest mode would turn a configuration error into privilege
    escalation.
    """
    if value is None:
        return DEFAULT_TOOL_MODE
    if not isinstance(value, str):
        raise ValueError(
            f"tool_mode 必须是字符串且为以下值之一：{', '.join(TOOL_MODES)}。"
        )
    text = value.strip()
    if text not in TOOL_MODES:
        raise ValueError(
            f"tool_mode 必须是以下值之一：{', '.join(TOOL_MODES)}；收到 {text!r}。"
        )
    return text


def tool_allowed(name: str, mode: Any) -> bool:
    """Whether ``name`` may cross the tool boundary under ``mode``.

    Unknown names inherit :func:`risk_of`'s highest-risk classification and
    therefore can pass this *mode* layer only in ``approved_execute``. They are
    still rejected by normal schema/argument validation afterwards.
    """
    try:
        canonical = normalize_tool_mode(mode)
    except ValueError:
        return False
    return risk_of(name) in _MODE_RISKS[canonical]


def tool_mode_block(name: str, mode: Any) -> dict[str, Any] | None:
    """Return a structured denial payload, or ``None`` when the mode allows it."""
    try:
        canonical = normalize_tool_mode(mode)
    except ValueError:
        canonical = TOOL_MODE_DISABLED
    if tool_allowed(name, canonical):
        return None
    return {
        "ok": False,
        "blocked": True,
        "error_code": "llm_tool_mode_blocked",
        "tool_mode": canonical,
        "tool": name,
        "error": f"LLM 工具权限模式 {canonical} 不允许调用 {name}，操作未执行。",
    }

# -- 确认粒度 ---------------------------------------------------------------

#: 不需要确认，直接执行。
POLICY_NEVER = "never"
#: 配置类：同轮的多个调用**合并成一张卡片**，用户一次批准全部落地。
POLICY_BATCH = "batch"
#: 执行类：**每个调用各占一张卡片**，不被批量批准夹带过去。
POLICY_SOLO = "solo"

#: 风险级别 → 确认粒度。这是唯一真源，任何执行路径都不许自己判断。
_CONFIRM_POLICIES = {
    RISK_READ: POLICY_NEVER,
    RISK_WRITE: POLICY_BATCH,
    RISK_EXECUTE: POLICY_SOLO,
}

#: 确认卡片上按此顺序排列，让「先落配置、再动执行」的次序在 UI 上也读得出来。
_POLICY_ORDER = {POLICY_NEVER: 0, POLICY_BATCH: 1, POLICY_SOLO: 2}

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

#: 可单独执行的分析阶段。**必须与 pipeline.ALL_STAGES 一致**（bootstrap 测试锁）。
#: 这里不 import pipeline 是为了让本模块保持「纯声明、无重依赖」，
#: 漂移由 test_chat_graph 的断言兜住。
RUN_STAGES = ("qc", "quant", "de", "cms", "counts")

#: 调度器合法取值（pipeline.render_scheduler_script 只认这三个）。
SCHEDULERS = ("local", "slurm", "pbs")

#: ``reference`` 区块的可编辑字段：工具参数名 → config 里的键名。
#: 参数名刻意用短名（gtf 而不是 remote_gtf_path），因为模型更不容易写错，
#: 落盘时由这里翻译成长名，模型不需要知道 config 的内部命名。
REFERENCE_FIELDS = {
    "gtf": "remote_gtf_path",
    "genome_fasta": "remote_genome_fasta_path",
    "star_index": "star_index_dir",
    "rsem_prefix": "rsem_index_prefix",
}

#: CMS 分型的运行模式（与 cms.py 的解释一致：pipeline = 跑在流水线里，
#: counts = 从 counts 直入）。
CMS_RUN_MODES = ("pipeline", "counts")

#: ``server`` 区块里允许模型改的非秘密字段。这个集合有意比设置页窄：认证模式、
#: shell 和密码只能由用户在设置页管理，不能通过对话工具变更。
CONNECTION_FIELDS = (
    "host",
    "user",
    "port",
    "scheduler",
    "threads",
    "memory_gb",
    "remote_base_dir",
    "remote_workdir",
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
    #: 覆盖按风险推导的确认粒度（见 :func:`confirmation_policy`）。留空表示用默认值。
    #: 存在的理由是「连接配置」：它属于写盘，但改的是远端目标，语义上比改样本表更重，
    #: 必须每次单独确认，不能被合并卡片夹带。
    policy: str = ""
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
    # condition 刻意**不**列入 required：模型可以只给分组名的一部分，
    # 由 normalize_write_arguments 按循环序列补齐（用户明确要的行为）。
    "required": ["sample_id", "fastq_1"],
    "additionalProperties": False,
}

#: ``edit_samples`` 用的样本补丁：只要求 sample_id，其余字段按「给了才改」处理。
#: 与 ``_SAMPLE_SCHEMA`` 分开是因为语义不同——那边是「建会话，必须有文件名」，
#: 这边是「改已有会话的一条」，改分组时不该逼模型重抄文件名。
_SAMPLE_PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": dict(_SAMPLE_SCHEMA["properties"]),
    "required": ["sample_id"],
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
            "只在用户明确要求开始跑分析时调用。\n"
            "默认跑整条流水线；用户只想跑某一段时用 stage 指定"
            f"（{'/'.join(RUN_STAGES)}）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "enum": list(RUN_STAGES),
                    "description": (
                        "只跑某个阶段：qc=fastp 质控，quant=比对定量，de=差异表达，"
                        "cms=分子分型，counts=counts 直入。省略则跑整条流水线。"
                    ),
                }
            },
            "additionalProperties": False,
        },
        risk=RISK_EXECUTE,
    ),
    "refresh_project_status": ToolSpec(
        name="refresh_project_status",
        label="刷新运行状态",
        description=(
            "读取项目当前的真实运行进度（提交了哪些作业、跑到哪一步、有没有失败）。"
            "启动分析之后想知道「跑到哪了」就调用它，不要凭上次的答复猜测。"
            "该操作会更新 project.json 中的项目运行状态，并把会话状态写回 "
            "session.json，因此需要确认。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=RISK_WRITE,
    ),
    "get_project_report": ToolSpec(
        name="get_project_report",
        label="生成项目报告",
        description=(
            "汇总项目的分析结果摘要（各阶段产物、差异表达结果、QC 结论）。"
            "用户问「结果怎么样 / 报告给我看看」时调用。该操作会生成报告文件，"
            "因此需要确认。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=RISK_WRITE,
    ),
    "edit_samples": ToolSpec(
        name="edit_samples",
        label="修改样本表",
        description=(
            "在**已有会话**里修改样本表：改名、改分组、改 FASTQ 文件名、增删样本。"
            "与 write_project_config 的区别：那个是首次建会话（已有会话会被拒绝），"
            "这个是改已建好的会话。用户说「把 S3 分到 treat 组」「去掉 S4」时用这个。\n"
            "只传要改的样本；要删除的样本在 remove 里给 sample_id。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "samples": {
                    "type": "array",
                    "items": _SAMPLE_PATCH_SCHEMA,
                    "description": (
                        "要新增或覆盖的样本。按 sample_id 匹配已有样本，"
                        "匹配到就覆盖，没匹配到就追加。"
                    ),
                },
                "remove": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要删除的 sample_id 列表。",
                },
            },
            "additionalProperties": False,
        },
        risk=RISK_WRITE,
    ),
    "edit_reference": ToolSpec(
        name="edit_reference",
        label="修改参考基因组",
        description=(
            "修改参考基因组相关路径：GTF 注释、基因组 FASTA、STAR 索引目录、"
            "RSEM 索引前缀。用户说「GTF 换成 xxx」时调用。只传要改的那几个。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "gtf": {"type": "string", "description": "GTF 注释文件的绝对路径"},
                "genome_fasta": {"type": "string", "description": "基因组 FASTA 的绝对路径"},
                "star_index": {"type": "string", "description": "STAR 索引目录的绝对路径"},
                "rsem_prefix": {"type": "string", "description": "RSEM 索引前缀"},
            },
            "additionalProperties": False,
        },
        risk=RISK_WRITE,
    ),
    "edit_connection": ToolSpec(
        name="edit_connection",
        label="修改服务器连接",
        description=(
            "修改服务器连接与运行资源：主机、用户名、端口、调度器、线程数、内存、"
            "远端工作目录。用户说「换到另一台服务器 / 目录改到 xxx」时调用。\n"
            "这是改动远端执行目标的操作，会单独请你确认，不会与其他配置合并。"
            "密码/密钥不在这里设置（出于安全考虑，密码只能由用户在设置页填写）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "服务器主机名或 IP"},
                "user": {"type": "string", "description": "登录用户名"},
                "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "scheduler": {"type": "string", "enum": list(SCHEDULERS)},
                "threads": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 128,
                    "description": "并行线程数",
                },
                "memory_gb": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2048,
                    "description": "内存上限（GB）",
                },
                "remote_base_dir": {"type": "string", "description": "远端根目录（绝对路径）"},
                "remote_workdir": {"type": "string", "description": "远端工作目录（绝对路径）"},
            },
            "additionalProperties": False,
        },
        risk=RISK_WRITE,
        # 用户明确要求：连接配置「含，但每次必确认」——不许被合并卡片夹带。
        policy=POLICY_SOLO,
    ),
    "set_cms_options": ToolSpec(
        name="set_cms_options",
        label="配置 CMS 分型",
        description=(
            "启用或关闭 CMScaller 结直肠癌分子分型，并可设置置换次数与 FDR 阈值。"
            "注意：CMS 只适用于结直肠癌（study.cancer_type 需声明为 CRC 类），"
            "且需要至少 30 个样本，门槛不满足时会被门禁拒绝。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "description": "是否启用 CMS 分型"},
                "n_perm": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 100000,
                    "description": "置换检验次数，默认 1000",
                },
                "fdr": {
                    "type": "number",
                    "minimum": 0.001,
                    "maximum": 1.0,
                    "description": "显著性阈值，默认 0.05",
                },
                "run_mode": {
                    "type": "string",
                    "enum": list(CMS_RUN_MODES),
                    "description": "pipeline = 跑在流水线里；counts = 从 counts 直入",
                },
            },
            "required": ["enabled"],
            "additionalProperties": False,
        },
        risk=RISK_WRITE,
    ),
}


def tool_schemas(mode: Any = DEFAULT_TOOL_MODE) -> list[dict[str, Any]]:
    """Tools exposed to the provider under the current permission mode."""
    return [
        spec.as_openai_schema()
        for name, spec in sorted(TOOL_SPECS.items())
        if tool_allowed(name, mode)
    ]


def risk_of(name: str) -> str:
    """Risk tier of a tool; unknown tools are treated as the most dangerous."""
    spec = TOOL_SPECS.get(name)
    return spec.risk if spec is not None else RISK_EXECUTE


def requires_confirmation(name: str) -> bool:
    """Whether code must ask the human before running this tool.

    这是**唯一真源**：执行路径不许自己判断风险，否则两处判断必然漂移。
    未知工具（模型幻觉出来的名字）按最高风险处理——宁可多问一次。

    实现委托给 :func:`confirmation_policy`，避免「要不要确认」与「怎么分组确认」
    出现两套判断。
    """
    return confirmation_policy(name) != POLICY_NEVER


def confirmation_policy(name: str) -> str:
    """How this tool's confirmation must be grouped. **唯一真源**。

    ``never`` —— 只读，直接执行。
    ``batch`` —— 配置类，同一轮的多个调用合成一张卡片，一次批准全部落地。
    ``solo``  —— 执行类（及未知工具），每个调用单独一张卡片。

    未知工具按 ``solo`` 处理：宁可多问几次，也不能让模型编出来的名字跟着别的
    调用一起被顺手批准。
    """
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return POLICY_SOLO
    if spec.policy:
        return spec.policy
    return _CONFIRM_POLICIES.get(spec.risk, POLICY_SOLO)


def tool_labels() -> dict[str, str]:
    """Short human labels for the thinking panel."""
    return {name: spec.label for name, spec in TOOL_SPECS.items()}


# -- 本轮处理组拆分 ---------------------------------------------------------


@dataclass(frozen=True)
class CallBatch:
    """一组可以合成一张确认卡片、一起执行的调用。

    ``policy`` 为 ``never`` 时表示无需确认的只读组（可能为空列表，此时不进卡片）。
    """

    policy: str
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.calls)


def split_calls_for_round(calls: Sequence[dict[str, Any]]) -> list[CallBatch]:
    """Group one model turn's tool calls by confirmation policy.

    模型一轮里可能同时请求「改样本表 + 改资源 + 启动分析」。按用户定的边界：

    - 所有 ``batch``（配置类）合并成**一个** :class:`CallBatch`，用户一次批准；
    - 每个 ``solo``（执行类）各成一个 :class:`CallBatch`，逐个确认；
    - ``never``（只读）合成一个无卡片的组。

    返回顺序为 ``never → batch → solo``，且 ``solo`` 组内保持模型给出的原始顺序：
    卡片会按这个顺序依次弹出，「先落配置、再动执行」对用户是可读的。
    """
    buckets: dict[str, list[dict[str, Any]]] = {
        POLICY_NEVER: [],
        POLICY_BATCH: [],
        POLICY_SOLO: [],
    }
    for call in calls:
        policy = confirmation_policy(str(call.get("name") or ""))
        buckets.setdefault(policy, []).append(call)

    batches: list[CallBatch] = []
    for policy in sorted(buckets, key=lambda item: _POLICY_ORDER.get(item, 99)):
        bucket = buckets[policy]
        if not bucket:
            continue
        if policy == POLICY_SOLO:
            # 执行类逐个成组：一个组 = 一张卡片 = 一次签字。
            batches.extend(CallBatch(policy=policy, calls=[call]) for call in bucket)
        else:
            batches.append(CallBatch(policy=policy, calls=list(bucket)))
    return batches


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

    校验分两层：

    1. **schema 驱动**（:func:`_schema_problems`）：类型、``enum``、``minimum``／
       ``maximum``、``minItems``／``maxItems``。写在 schema 里的约束**必须真的被读**，
       否则它只是给模型看的装饰——``threads: -5`` 会一路走到 sbatch 脚本里。
    2. **语义检查**（本函数下半段）：schema 表达不了的东西，例如「路径必须是
       绝对 POSIX 路径」「对照组必须真的在样本表里」。
    """
    if name not in TOOL_SPECS:
        return [f"不存在名为 {name} 的工具。"]

    spec = TOOL_SPECS[name]
    problems = _schema_problems(name, arguments, spec.parameters, path=name)
    if problems:
        return problems

    if name == "browse_remote_samples":
        error = _absolute_posix_path_error(arguments.get("path"), "path")
        if error:
            problems.append(error)

    if name == "write_project_config":
        problems.extend(_validate_write_config(arguments))

    if name == "edit_samples":
        problems.extend(_validate_edit_samples(arguments))

    if name in {"edit_reference", "set_run_resources", "edit_connection"}:
        problems.extend(_validate_path_fields(name, arguments))

    if name == "set_diffexp_reference":
        reference = str(arguments.get("reference_condition") or "").strip()
        if not reference:
            problems.append("reference_condition 不能为空。")
        elif not _looks_like_condition(reference):
            problems.append(
                f"对照组名 {reference!r} 含空白或特殊字符，请用样本表里的分组名。"
            )

    if name == "set_run_resources":
        if "threads" not in arguments and "memory_gb" not in arguments:
            problems.append("至少要给出 threads 或 memory_gb 之一。")

    if name == "edit_connection":
        if not any(key in arguments for key in CONNECTION_FIELDS):
            problems.append("至少要给出一个要修改的连接字段。")
        problems.extend(validate_ssh_patch(arguments))

    return problems


#: JSON Schema 类型名 → Python 判定。``integer`` 单独处理（``bool`` 是 ``int``
#: 的子类，必须显式排除，否则 ``True`` 会被当成合法的线程数）。
def _schema_problems(
    name: str,
    values: Any,
    schema: dict[str, Any],
    *,
    path: str,
) -> list[str]:
    """Recursively check ``values`` against ``schema``.

    写 schema 时标了 ``minimum``／``maximum``／``enum`` 却不是空架子——这就是本
    函数存在的全部理由。校验失败返回人类可读的原因（会原样回灌给模型）。
    """
    problems: list[str] = []
    if not isinstance(schema, dict) or not isinstance(values, dict):
        return problems

    declared = schema.get("properties") or {}

    # 只接受 schema 里声明过的键：模型塞进来的额外字段会被下游误读。
    for key in values:
        if key not in declared:
            problems.append(f"{path} 不接受参数 {key}。")

    # 必需参数：None / 空串 / 空列表都算缺失。
    for key in schema.get("required", []):
        if values.get(key) in (None, "", []):
            problems.append(f"{path} 缺少必需参数 {key}。")

    for key, value in values.items():
        field_schema = declared.get(key)
        if not isinstance(field_schema, dict) or value is None:
            continue
        problems.extend(
            _field_problems(f"{path}.{key}", value, field_schema)
        )
    return problems


def _field_problems(path: str, value: Any, schema: dict[str, Any]) -> list[str]:
    """Type / enum / range / item-count checks for a single field."""
    problems: list[str] = []
    expected = str(schema.get("type") or "")

    if expected == "string":
        if not isinstance(value, str):
            return [f"{path} 必须是字符串。"]
    elif expected == "boolean":
        if not isinstance(value, bool):
            return [f"{path} 必须是布尔值（true/false）。"]
    elif expected == "integer":
        # bool 是 int 的子类，必须先排除，否则 True 会被当成 1 放行。
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{path} 必须是整数。"]
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"{path} 必须是数字。"]
    elif expected == "array":
        if not isinstance(value, list):
            return [f"{path} 必须是数组。"]
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            problems.append(f"{path} 至少需要 {minimum_items} 项，当前 {len(value)} 项。")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            problems.append(f"{path} 最多允许 {maximum_items} 项，当前 {len(value)} 项。")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                problems.extend(
                    _schema_problems(
                        "", item, item_schema, path=f"{path}[{index + 1}]"
                    )
                )
        return problems

    choices = schema.get("enum")
    if isinstance(choices, list) and choices and value not in choices:
        return [f"{path} 必须是 {choices} 之一，收到的是 {value!r}。"]

    # 数值范围：这是本轮修掉的真实漏洞（``threads: -5`` 以前会被放行）。
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            problems.append(f"{path} 不能小于 {minimum}，收到的是 {value}。")
        if isinstance(maximum, (int, float)) and value > maximum:
            problems.append(f"{path} 不能大于 {maximum}，收到的是 {value}。")

    return problems


def _validate_path_fields(name: str, arguments: dict[str, Any]) -> list[str]:
    """Absolute-POSIX-path checks for the tools that carry server-side paths.

    这些字段最终会拼进服务器上的命令行，所以必须与 ``browse_remote_samples``
    用同一套判定：绝对路径、无换行、无 ``..``。
    """
    field_map = {
        "edit_reference": ("gtf", "genome_fasta", "star_index", "rsem_prefix"),
        "edit_connection": ("remote_base_dir", "remote_workdir"),
        "set_run_resources": (),
    }
    problems: list[str] = []
    for argument_name in field_map.get(name, ()):
        if arguments.get(argument_name) in (None, ""):
            continue
        error = _absolute_posix_path_error(arguments.get(argument_name), argument_name)
        if error:
            problems.append(error)
    return problems


def _looks_like_condition(value: str) -> bool:
    """Reject obviously-not-a-group-name input (spaces, quotes, path separators)."""
    if any(ch.isspace() for ch in value):
        return False
    return not any(ch in value for ch in ('"', "'", "/", "\\", "\n", "\r", "\x00"))


def _validate_edit_samples(arguments: dict[str, Any]) -> list[str]:
    """Field-level checks for ``edit_samples`` (mirrors the write-config checks)."""
    problems: list[str] = []
    samples = arguments.get("samples")
    remove = arguments.get("remove")

    if samples is None and remove is None:
        return ["至少要给出 samples（要改的样本）或 remove（要删的 sample_id）之一。"]
    if samples is not None and not isinstance(samples, list):
        problems.append("samples 必须是数组。")
        samples = None

    for index, sample in enumerate(samples or [], start=1):
        if not isinstance(sample, dict):
            problems.append(f"samples[{index}] 必须是对象。")
            continue
        error = identifier_error(sample.get("sample_id"), f"samples[{index}].sample_id")
        if error:
            problems.append(error)
        for key in ("fastq_1", "fastq_2"):
            value = sample.get(key)
            if value in (None, ""):
                continue
            error = relative_filename_error(value, f"samples[{index}].{key}")
            if error:
                problems.append(f"{error}（{key} 只写文件名，目录请放进 fastq_dir）")

    if remove is not None:
        if not isinstance(remove, list):
            problems.append("remove 必须是 sample_id 数组。")
        else:
            for index, sample_id in enumerate(remove, start=1):
                error = identifier_error(sample_id, f"remove[{index}]")
                if error:
                    problems.append(error)

    # 同一个 sample_id 不能既改又删——模型自相矛盾时问清楚，别猜。
    overlap = {
        str(sample.get("sample_id") or "")
        for sample in samples or []
        if isinstance(sample, dict)
    } & {str(item or "") for item in remove or []}
    if overlap:
        problems.append(f"样本 {sorted(overlap)} 同时出现在 samples 和 remove 里，请二选一。")
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

#: 连接配置字段 → 中文标签（卡片上「旧值 → 新值」用的措辞）。
_CONNECTION_LABELS = {
    "host": "主机",
    "user": "登录用户",
    "port": "端口",
    "scheduler": "调度器",
    "threads": "线程数",
    "memory_gb": "内存（GB）",
    "remote_base_dir": "远端根目录",
    "remote_workdir": "远端工作目录",
}

_REFERENCE_LABELS = {
    "gtf": "GTF 注释",
    "genome_fasta": "基因组 FASTA",
    "star_index": "STAR 索引",
    "rsem_prefix": "RSEM 索引前缀",
}


def _current_value(current: dict[str, Any], section: str, key: str) -> str:
    """Read one config value for the card's "old value" column."""
    block = current.get(section)
    if not isinstance(block, dict):
        return "（未设置）"
    value = block.get(key)
    return "（未设置）" if value in (None, "") else str(value)


def field_changes(
    name: str,
    arguments: dict[str, Any],
    current: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Render a call as label / old / new triples for the confirmation card.

    用户 2026-09-16 明确要求：**连接配置的卡片必须明写旧值 → 新值**。改远端目标
    是最不能「看着像没事」的一类变更，只写「已修改服务器配置」等于没写。

    ``current`` 为空（例如项目还没有配置）时不编造旧值，如实写「（未设置）」。
    """
    current = current or {}
    changes: list[dict[str, str]] = []

    if name == "edit_connection":
        for key in CONNECTION_FIELDS:
            if arguments.get(key) is None:
                continue
            changes.append(
                {
                    "label": _CONNECTION_LABELS.get(key, key),
                    "old": _current_value(current, "server", key),
                    "new": str(arguments[key]),
                }
            )
        return changes

    if name == "edit_reference":
        for key, config_key in REFERENCE_FIELDS.items():
            if not arguments.get(key):
                continue
            changes.append(
                {
                    "label": _REFERENCE_LABELS.get(key, key),
                    "old": _current_value(current, "reference", config_key),
                    "new": str(arguments[key]),
                }
            )
        return changes

    if name == "set_run_resources":
        for key, label, unit in (
            ("threads", "线程数", ""),
            ("memory_gb", "内存（GB）", ""),
        ):
            if arguments.get(key) is None:
                continue
            changes.append(
                {
                    "label": label,
                    "old": _current_value(current, "server", key),
                    "new": f"{arguments[key]}{unit}",
                }
            )
        return changes

    if name == "configure_pipeline":
        step = str(arguments.get("step") or "")
        enabled = bool(arguments.get("enabled"))
        old = "未设置"
        pipeline = current.get("pipeline")
        if isinstance(pipeline, dict) and isinstance(pipeline.get(step), dict):
            old = "已启用" if pipeline[step].get("enabled") else "已关闭"
        changes.append(
            {"label": step, "old": old, "new": "启用" if enabled else "关闭"}
        )
        return changes

    if name == "set_diffexp_reference":
        reference = str(arguments.get("reference_condition") or "")
        old = str(current.get("diffexp", {}).get("reference_condition") or "") if isinstance(
            current.get("diffexp"), dict
        ) else ""
        changes.append(
            {
                "label": "差异表达对照组",
                "old": old or "（未设置）",
                "new": f"{reference}（并启用差异表达）",
            }
        )
        return changes

    if name == "set_cms_options":
        enabled = bool(arguments.get("enabled"))
        changes.append(
            {
                "label": "CMS 分型",
                "old": (
                    "已启用"
                    if isinstance(current.get("pipeline"), dict)
                    and current["pipeline"].get("cms", {}).get("enabled")
                    else "已关闭"
                ),
                "new": "启用" if enabled else "关闭",
            }
        )
        for key, label in (("n_perm", "置换次数"), ("fdr", "FDR 阈值")):
            if arguments.get(key) is None:
                continue
            old = ""
            if isinstance(current.get("cms"), dict):
                old = str(current["cms"].get(key) or "")
            changes.append(
                {"label": label, "old": old or "（默认）", "new": str(arguments[key])}
            )
        return changes

    if name == "edit_samples":
        samples = arguments.get("samples") or []
        remove = arguments.get("remove") or []
        existing = {
            str(sample.get("sample_id") or ""): sample
            for sample in (current.get("samples", {}).get("items") or [])
            if isinstance(sample, dict)
        }
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            sample_id = str(sample.get("sample_id") or "")
            if sample_id in existing:
                old_condition = str(existing[sample_id].get("condition") or "未分组")
                new_condition = str(sample.get("condition") or old_condition)
            else:
                old_condition = "（新增）"
                new_condition = str(sample.get("condition") or "未分组")
            changes.append(
                {
                    "label": f"样本 {sample_id}",
                    "old": old_condition,
                    "new": new_condition,
                }
            )
        for sample_id in remove:
            changes.append(
                {"label": f"样本 {sample_id}", "old": "存在", "new": "删除"}
            )
        return changes

    return changes


def _render_changes(changes: list[dict[str, str]]) -> list[str]:
    return [
        f"  {item['label']}：{item['old']} → {item['new']}" for item in changes
    ]


def describe_call(
    name: str,
    arguments: dict[str, Any],
    current: dict[str, Any] | None = None,
) -> str:
    """Render one tool call as something a human can approve or reject.

    这是「写盘前要人确认」看到的那段文字。必须**如实**描述将要发生什么，
    不能只写工具名——用户是在为一个具体动作签字，不是在为一个函数名签字。

    ``current`` 是项目当前配置；给了就在卡片上写「旧值 → 新值」，让用户能直接
    看出这次批准改了哪几个字段（用户明确要求连接配置必须如此）。
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

    if name in {
        "edit_connection",
        "edit_reference",
        "set_run_resources",
        "configure_pipeline",
        "set_diffexp_reference",
        "set_cms_options",
        "edit_samples",
    }:
        changes = field_changes(name, arguments, current)
        lines = [f"{title}："]
        if changes:
            lines.extend(_render_changes(changes))
        else:
            lines.append("  （无实际变化）")
        if name == "edit_connection":
            lines.append("  注意：这会影响后续作业提交到哪台机器、哪个目录。")
        return "\n".join(lines)

    if name == "run_analysis":
        stage = arguments.get("stage")
        if stage:
            return f"{title}：只跑 {stage} 阶段（在服务器上执行）"
        return f"{title}：跑完整流水线（在服务器上执行）"

    if name == "browse_remote_samples":
        return f"{title}：只读扫描 {arguments.get('path')}"

    if name == "refresh_project_status":
        return (
            f"{title}：读取远端运行进度，更新项目运行状态，"
            "并把最新状态写回本地会话。"
        )

    if name == "get_project_report":
        return f"{title}：生成或覆盖当前项目的报告文件。"

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
