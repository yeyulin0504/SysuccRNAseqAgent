"""从自然语言里抽出可校验的 bulk RNA-seq 配置草稿。

职责边界（架构原则「LLM 理解需求，确定性代码执行」）：

- 本模块只做**纯文本抽取**：不读文件系统、不发网络请求、不写盘；
- 抽取结果是一份 ``ConfigDraft``，由调用方（webapp）做校验后交给既有的
  写盘链路（``save_intake`` + ``_default_config`` + ``session.new_project``）；
- 信息不足时给出 ``warnings`` / ``blockers``，**不猜**。

用户诉求（2026-09-16）：

> 帮我改对话的写盘能力，4个样本给了2个分组名因为是按ctrl，treat，ctrl，treat
> 的顺序排列的，这个不需要我多说，大语言模型应该自己会理解确定。
> 要允许特异性未知的选项先写着

因此分组按顺序循环展开是**刻意**的选择，不是兜底：用户说「按 ctrl,treat,
ctrl,treat 顺序」时只写两个名字，语义就是一个循环序列。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .sample_detection import detect_fastq_pairs

# 路径：绝对 POSIX 路径，到空白 / 标点为止。
_ABS_PATH_RE = re.compile(r"(/[A-Za-z0-9._~+\-/]+)")
# 明确的文件名后缀，用于把「基础目录」和「具体文件」分开。
_GTF_RE = re.compile(r"(/[A-Za-z0-9._~+\-/]+\.gtf(?:\.gz)?)", re.I)
_FASTA_RE = re.compile(
    r"(/[A-Za-z0-9._~+\-/]+\.(?:fa|fasta|fna)(?:\.gz)?)", re.I
)
_FASTQ_NAME_RE = re.compile(r"\b([A-Za-z0-9._\-]+\.(?:fastq|fq)(?:\.gz)?)\b", re.I)

# 分组：condition 名字的合法形态（字母开头，允许下划线 / 连字符）。
_CONDITION_TOKEN_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_\-]{0,30})\b")

# 「按 X, Y 顺序」这类显式序列说明。
_SEQUENCE_HINTS = ("顺序", "依次", "轮流", "交替", "分别", "order")
_GROUPING_HINTS = ("分组", "分到", "组别", "condition", "group", "control", "treat")

# 序号指派：「1是control 2是treat」/「第1个是ctrl」。
# 名字部分必须**非贪婪 + 前瞻**：用户常写成「1是control2是treat」（两组之间没有
# 空格），贪婪的 ``[A-Za-z0-9_\-]{0,30}`` 会把 control2 整块吃掉，导致只剩一个
# 指派、被 ``len(pairs) < 2`` 判为不成立。前瞻保证名字止于「下一个序号」之前。
_INDEXED_RE = re.compile(
    r"(?:第\s*)?(\d+)\s*(?:个|号|是|为|=|：|:)\s*([A-Za-z][A-Za-z0-9_\-]{0,30}?)"
    r"(?=\s*(?:\d+\s*(?:个|号|是|为|=|：|:)|[,，、;；]|\s|$))"
)

# 链特异性。
_STRANDEDNESS_UNKNOWN_HINTS = ("未知", "不知道", "不确定", "unknown", "不明确")
_STRANDEDNESS_VALUES = {
    "reverse": ("reverse", "反链", "反向"),
    "forward": ("forward", "正链", "正向"),
    "unstranded": ("unstranded", "无链", "非链特异", "unstrand"),
}
_STRANDEDNESS_CONTEXT = ("链特异性", "strandedness", "strand")

# 会被误当成 condition 的常见非分组词。
# 注意：ctrl / control / treat / treatment / case 等**必须保留**——它们正是
# 最常见的分组名，把它们当停用词会把唯一的信号词过滤掉（已踩过）。
_CONDITION_STOPWORDS = {
    "fastq", "fq", "gz", "fasta", "fa", "gtf", "star", "index", "reads",
    "data", "home", "project", "reference", "references", "runs", "seq",
    "rna", "dna", "paired", "single", "auto", "unknown", "and", "the",
    "sample", "samples", "顺序", "分组", "组别", "依次", "交替",
}


@dataclass
class ConditionExpansion:
    """Expanded per-sample conditions plus any honest warnings."""

    conditions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ConfigDraft:
    """A parsed, not-yet-validated configuration draft."""

    data_source: str = "remote_path"
    fastq_dir: str = ""
    samples: list[dict[str, str]] = field(default_factory=list)
    reference: dict[str, str] = field(default_factory=dict)
    strandedness: str = "unknown"
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Whether the draft has enough to create a real project session."""
        return bool(self.fastq_dir and self.samples) and not self.blockers

    def to_payload(self) -> dict[str, Any]:
        """Shape the draft as the payload ``/fastq/session`` expects."""
        payload: dict[str, Any] = {
            "data_source": self.data_source,
            "samples": self.samples,
            "strandedness": self.strandedness,
            "layout": "paired" if any(s.get("fastq_2") for s in self.samples) else "single",
        }
        if self.data_source == "remote_path":
            payload["remote_fastq_dir"] = self.fastq_dir
        else:
            payload["fastq_dir"] = self.fastq_dir
        reference = self.reference
        if reference.get("gtf"):
            payload["gtf"] = reference["gtf"]
        if reference.get("genome_fasta"):
            payload["genome_fasta"] = reference["genome_fasta"]
        if reference.get("star_index"):
            payload["star_index"] = reference["star_index"]
        if reference.get("rsem_prefix"):
            payload["rsem_prefix"] = reference["rsem_prefix"]
        return payload


def expand_conditions(names: list[str], sample_count: int) -> ConditionExpansion:
    """Cycle ``names`` across ``sample_count`` samples, warning on imbalance.

    用户明确要求：给了 N 个分组名 + M 个样本（M > N）时，按名字给出的顺序
    循环展开，不要反问。例如 ``["ctrl","treat"]`` × 4 → ctrl/treat/ctrl/treat。
    """
    expansion = ConditionExpansion()
    cleaned = [name.strip() for name in names if name and name.strip()]
    if sample_count <= 0:
        return expansion
    if not cleaned:
        expansion.warnings.append("没有识别到分组名，请补充样本条件（如 ctrl/treat）。")
        return expansion

    conditions = [cleaned[index % len(cleaned)] for index in range(sample_count)]
    expansion.conditions = conditions

    distinct = sorted(set(conditions))
    if len(distinct) < 2:
        expansion.warnings.append(
            f"所有样本都是同一个分组 {distinct[0]!r}，无法构成对比。"
        )
    else:
        tally: dict[str, int] = {}
        for condition in conditions:
            tally[condition] = tally.get(condition, 0) + 1
        counts = sorted(tally.values())
        if counts[0] != counts[-1]:
            expansion.warnings.append(
                "分组不均衡："
                + "、".join(f"{name}={tally[name]}" for name in sorted(tally))
                + "，差异表达统计功效会受影响。"
            )
    return expansion


def extract_config_draft(text: str) -> ConfigDraft:
    """Parse a user message into a :class:`ConfigDraft`."""
    draft = ConfigDraft()

    fastq_filenames, fastq_dir = _extract_fastq(text)
    detected = detect_fastq_pairs(fastq_filenames) if fastq_filenames else {
        "samples": [],
        "unmatched": [],
    }
    samples = [dict(sample) for sample in detected["samples"]]
    for unmatched in detected["unmatched"]:
        draft.warnings.append(f"文件未配对，已忽略：{unmatched}")

    draft.fastq_dir = fastq_dir
    draft.data_source = "remote_path" if fastq_dir.startswith("/") else "local_upload"
    draft.reference = _extract_reference(text)
    draft.strandedness = _extract_strandedness(text)

    # 分组：优先按「序号指派」，其次按显式给出的名字序列。
    conditions = _extract_conditions(text, len(samples))
    for index, sample in enumerate(samples):
        if index < len(conditions):
            sample["condition"] = conditions[index]
    draft.samples = samples

    if not fastq_dir:
        draft.blockers.append("没有识别到 FASTQ 目录，请给出绝对路径。")
    if not samples:
        if fastq_filenames:
            draft.blockers.append("识别到 FASTQ 文件名但没有成对，请检查 _1/_2 后缀。")
        elif fastq_dir:
            draft.blockers.append("FASTQ 目录里没有列出具体文件，请把文件名一并给我。")
        else:
            draft.blockers.append("没有识别到样本，请提供 FASTQ 目录与文件名。")
    else:
        if not any(sample.get("condition") for sample in samples):
            # 分组是能力契约里的必需样本字段（required_sample_fields 含
            # ``condition``），缺了它写出来的会话必然过不了 Gate-A。若先在
            # 这里放行，「先写一半、再补分组」会被「已有会话，不覆盖」挡住，
            # 用户反而卡得更死——所以缺分组必须阻断，并明确告诉他补什么。
            draft.blockers.append(
                "没有识别到样本分组。请告诉我每个样本属于哪一组，"
                "例如「分组按 ctrl, treat 顺序」或「1是control 2是treat」。"
            )
        else:
            expansion = expand_conditions(
                [sample.get("condition", "") for sample in samples if sample.get("condition")],
                len(samples),
            )
            draft.warnings.extend(expansion.warnings)

    if draft.strandedness == "unknown":
        draft.warnings.append(
            "链特异性未知：将按无链特异性（featureCounts -s 0）先执行，"
            "拿到结果后可再校正。"
        )
    if not draft.reference.get("gtf") or not draft.reference.get("genome_fasta"):
        draft.warnings.append("参考基因组信息不完整（缺 GTF 或 FASTA），比对步骤可能被门禁拦截。")
    return draft


def _extract_fastq(text: str) -> tuple[list[str], str]:
    """Return ``(filenames, directory)`` for the FASTQ block.

    目录必须是 **FASTQ 文件名所在行的前一行**里最近的那个绝对路径。
    早期实现取「最后一个非 FASTQ 行的绝对路径」，在真实消息里会一路走到
    文件末尾的参考基因组段落，把 ``star_index`` 当成 FASTQ 目录（已踩过）。
    """
    filenames = _FASTQ_NAME_RE.findall(text)
    if not filenames:
        return [], ""

    lines = text.splitlines()
    first_fastq_line = next(
        (index for index, line in enumerate(lines) if _FASTQ_NAME_RE.search(line)), None
    )
    directory = ""
    if first_fastq_line is not None:
        # 只向上回溯，且跳过参考基因组相关的行——它们在消息里通常排在 FASTQ
        # 之后，不该反过来影响 FASTQ 目录的判定。
        for line in reversed(lines[:first_fastq_line]):
            if _looks_like_reference_line(line):
                continue
            match = _ABS_PATH_RE.search(line)
            if match:
                directory = match.group(1).rstrip("/")
                break

    if not directory:
        # 目录只写在文件名里时（`/data/reads/A_1.fastq.gz`），从文件名反推。
        first_dir = re.search(
            r"(/[A-Za-z0-9._~+\-/]+)/[A-Za-z0-9._\-]+\.(?:fastq|fq)", text, re.I
        )
        if first_dir:
            directory = first_dir.group(1).rstrip("/")
    return filenames, directory


def _looks_like_reference_line(line: str) -> bool:
    """Whether a line describes reference files rather than FASTQ inputs."""
    if _GTF_RE.search(line) or _FASTA_RE.search(line):
        return True
    lowered = line.lower()
    return "star_index" in lowered or "star index" in lowered or "rsem" in lowered


def _extract_reference(text: str) -> dict[str, str]:
    reference: dict[str, str] = {}
    gtf = _GTF_RE.search(text)
    if gtf:
        reference["gtf"] = gtf.group(1)
    fasta = _FASTA_RE.search(text)
    if fasta:
        reference["genome_fasta"] = fasta.group(1)

    # STAR 索引：显式路径优先，否则用参考基础目录推断 star_index 子目录。
    star = re.search(r"(/[A-Za-z0-9._~+\-/]*star[_\-]?index[A-Za-z0-9._~+\-/]*)", text, re.I)
    if star:
        reference["star_index"] = star.group(1).rstrip("/")
    elif gtf:
        base = gtf.group(1).rsplit("/", 1)[0]
        reference["star_index"] = f"{base}/star_index"
    return reference


def _extract_strandedness(text: str) -> str:
    """Return ``unknown`` unless the message states a known value."""
    lowered = text.lower()
    if not any(hint in lowered for hint in _STRANDEDNESS_CONTEXT):
        return "unknown"
    for value, aliases in _STRANDEDNESS_VALUES.items():
        if any(alias in lowered for alias in aliases):
            return value
    if any(hint in lowered for hint in _STRANDEDNESS_UNKNOWN_HINTS):
        return "unknown"
    return "unknown"


def _extract_conditions(text: str, sample_count: int) -> list[str]:
    """Resolve per-sample conditions from indexed or ordered mentions."""
    if sample_count <= 0:
        return []

    indexed = _resolve_indexed_conditions(text, sample_count)
    if indexed is not None:
        return indexed

    names = _resolve_ordered_names(text)
    if not names:
        return []
    return expand_conditions(names, sample_count).conditions


def _resolve_indexed_conditions(text: str, sample_count: int) -> list[str] | None:
    """Handle「1是control 2是treat」style assignments.

    指派数少于样本数时（用户只写了「1是control 2是treat」但有 4 个样本），
    按用户给出的名字序列**循环补齐**——与「按 ctrl,treat 顺序排列」同一语义，
    不要反问。已明确指派的序号不会被覆盖。
    """
    # 仅在出现分组语境时才启用，避免把「1是」误当分组。
    if not any(hint in text.lower() for hint in _GROUPING_HINTS):
        return None
    pairs = _INDEXED_RE.findall(text)
    if len(pairs) < 2:
        return None
    conditions: list[str] = [""] * sample_count
    assigned_order: list[str] = []
    for raw_index, name in pairs:
        index = int(raw_index)
        if 1 <= index <= sample_count and not conditions[index - 1]:
            conditions[index - 1] = name
            assigned_order.append(name)
    if not any(conditions):
        return None

    missing = [index for index, value in enumerate(conditions) if not value]
    if missing and assigned_order:
        for offset, index in enumerate(missing):
            conditions[index] = assigned_order[offset % len(assigned_order)]
    return conditions


def _resolve_ordered_names(text: str) -> list[str]:
    """Pull an explicit ordered run of condition names out of the message."""
    lowered = text.lower()
    if not any(hint in lowered for hint in _SEQUENCE_HINTS + _GROUPING_HINTS):
        return []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _FASTQ_NAME_RE.search(line) or _ABS_PATH_RE.search(line):
            continue
        candidates = [
            token
            for token in _CONDITION_TOKEN_RE.findall(line)
            if token.lower() not in _CONDITION_STOPWORDS and not token.isdigit()
        ]
        # 一行里出现多个条件名（逗号/顿号分隔）才是序列说明。
        if len(candidates) >= 2 and ("," in line or "，" in line or "、" in line or " " in line):
            return candidates
    return []
