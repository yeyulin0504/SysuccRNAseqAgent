from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .configuration import normalize_config
from .defaults import DEFAULT_PIPELINE

MAX_MESSAGE_LENGTH = 2_000
MAX_PROPOSALS = 12
MAX_ONBOARDING_PROPOSALS = 8
MAX_AGENT_PLAN_STEPS = 6
_AGENT_STEP_KINDS = {"validate", "summary", "report", "status", "run", "contract_submission"}
_PUBLIC_REFERENCE_FIELDS = (
    "species",
    "assembly",
    "annotation_release",
    "layout",
    "enabled_tools",
)

_ONBOARDING_FIELDS: dict[str, object] = {
    "project.id": "text",
    "project.title": "text",
    "project.owner": "text",
    "server.profile": "text",
    "server.host": "host",
    "server.user": "user",
    "server.remote_base_dir": "path",
    "server.scheduler": {"slurm", "pbs", "local"},
    "server.threads": (1, 256),
    "server.memory_gb": (1, 4096),
    "server.auth_mode": {"key", "password", "system"},
    "server.key_path": "path",
    "sequencing.layout": {"paired", "single"},
    "sequencing.strandedness": {"auto", "unstranded", "forward", "reverse"},
    "sequencing.reads_per_sample_million": (1, 1_000),
    "samples.local_data_dir": "path",
    "samples.remote_data_dir": "path",
    "polling.interval_seconds": (30, 86_400),
    "polling.timeout_hours": (1, 720),
    **{f"pipeline.{step}.enabled": "bool" for step in DEFAULT_PIPELINE},
}

_ALLOWED_PATHS = {
    "/sequencing/layout": {"paired", "single"},
    "/sequencing/strandedness": {"auto", "unstranded", "forward", "reverse"},
    "/sequencing/reads_per_sample_million": (1, 1_000),
    "/polling/interval_seconds": (30, 86_400),
    "/polling/timeout_hours": (1, 720),
    **{f"/pipeline/{step}/enabled": None for step in DEFAULT_PIPELINE},
}


class ProposalError(ValueError):
    pass


@dataclass(frozen=True)
class ConfigPatch:
    path: str
    value: str | int | bool
    reason: str = ""


@dataclass(frozen=True)
class AgentStep:
    kind: str
    wait: bool | None = None


def validate_agent_plan(raw: object) -> tuple[AgentStep, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_AGENT_PLAN_STEPS:
        raise ProposalError("Agent 计划必须是包含 1 至 6 项的数组。")
    steps: list[AgentStep] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ProposalError("Agent 步骤必须是对象。")
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in _AGENT_STEP_KINDS:
            raise ProposalError("Agent 计划包含不允许的步骤。")
        allowed_fields = {"kind", "wait"} if kind == "run" else {"kind"}
        if set(item) != allowed_fields:
            raise ProposalError("Agent 步骤包含不允许的字段。")
        if kind == "run":
            wait = item.get("wait")
            if type(wait) is not bool:
                raise ProposalError("运行步骤必须提供布尔类型的 wait 参数。")
            steps.append(AgentStep(kind=kind, wait=wait))
        else:
            steps.append(AgentStep(kind=kind))
    return tuple(steps)


@dataclass(frozen=True)
class OnboardingPatch:
    field: str
    value: str | int | bool
    reason: str = ""


def validate_onboarding_proposals(raw: object) -> tuple[OnboardingPatch, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ONBOARDING_PROPOSALS:
        raise ProposalError("引导填写建议必须是包含 1 至 8 项的数组。")
    patches: list[OnboardingPatch] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"field", "value", "reason"}:
            raise ProposalError("引导填写建议字段不符合允许的结构。")
        field = item.get("field")
        if not isinstance(field, str) or field not in _ONBOARDING_FIELDS or field in seen:
            raise ProposalError("引导填写建议包含不允许或重复的字段。")
        value = _normalize_onboarding_value(field, item.get("value"))
        _validate_onboarding_value(field, value)
        reason = item.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 240:
            raise ProposalError("引导填写建议理由格式不正确。")
        seen.add(field)
        patches.append(OnboardingPatch(field=field, value=value, reason=reason.strip()))
    return tuple(patches)


def _normalize_onboarding_value(field: str, value: object) -> object:
    if field != "sequencing.layout" or not isinstance(value, str):
        return value
    normalized = value.strip().lower()
    aliases = {
        "双端": "paired",
        "双端测序": "paired",
        "pe": "paired",
        "paired-end": "paired",
        "单端": "single",
        "单端测序": "single",
        "se": "single",
        "single-end": "single",
    }
    return aliases.get(normalized, normalized)


def _validate_onboarding_value(field: str, value: object) -> None:
    rule = _ONBOARDING_FIELDS[field]
    if rule == "bool":
        if type(value) is not bool:
            raise ProposalError("流程开关必须是布尔值。")
        return
    if isinstance(rule, set):
        if not isinstance(value, str) or value not in rule:
            raise ProposalError("引导填写建议的枚举值不允许。")
        return
    if isinstance(rule, tuple):
        if type(value) is not int or not rule[0] <= value <= rule[1]:
            raise ProposalError("引导填写建议数值超出允许范围。")
        return
    if not isinstance(value, str) or not value.strip() or len(value) > 300 or "\x00" in value or "\n" in value or "\r" in value:
        raise ProposalError("引导填写建议文本格式不正确。")
    if rule == "host" and (any(char.isspace() for char in value) or "://" in value or any(char in value for char in ";|&`$")):
        raise ProposalError("服务器地址格式不正确。")
    if rule == "user" and (any(char.isspace() for char in value) or any(char in value for char in ";|&`$")):
        raise ProposalError("服务器用户名格式不正确。")
    if rule == "path" and (any(char in value for char in ";|&`$") or value.startswith(("~", "-"))):
        raise ProposalError("目录路径格式不正确。")


def build_onboarding_context(*, current_field: str) -> dict[str, str]:
    if current_field not in _ONBOARDING_FIELDS:
        raise ProposalError("引导字段不允许。")
    return {"current_field": current_field}


def apply_onboarding_proposals(
    config: dict[str, Any],
    patches: tuple[OnboardingPatch, ...],
) -> dict[str, Any]:
    candidate = deepcopy(config)
    for patch in patches:
        section, *parts = patch.field.split(".")
        target = candidate.setdefault(section, {})
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = patch.value
    return normalize_config(candidate)


def build_llm_context(
    *,
    has_project: bool,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the only project metadata permitted to leave the application."""
    context: dict[str, Any] = {"has_project": has_project}
    if config is None:
        return context
    sequencing = config.get("sequencing", {})
    polling = config.get("polling", {})
    pipeline = config.get("pipeline", {})
    context["workflow"] = {
        "layout": sequencing.get("layout"),
        "strandedness": sequencing.get("strandedness"),
        "reads_per_sample_million": sequencing.get("reads_per_sample_million"),
        "enabled_steps": sorted(
            step for step, value in pipeline.items()
            if isinstance(value, dict) and value.get("enabled") is True
        ),
        "polling_interval_seconds": polling.get("interval_seconds"),
        "polling_timeout_hours": polling.get("timeout_hours"),
    }
    return context


def build_reference_search_query(
    *,
    species: str,
    assembly: str,
    annotation_release: str,
    layout: str,
    enabled_tools: tuple[str, ...],
) -> dict[str, object]:
    values: dict[str, object] = {
        "species": species,
        "assembly": assembly,
        "annotation_release": annotation_release,
        "layout": layout,
        "enabled_tools": enabled_tools,
    }
    if not all(isinstance(values[field], str) and str(values[field]).strip() for field in _PUBLIC_REFERENCE_FIELDS[:-1]):
        raise ProposalError("公开参考查询缺少已确认的物种、组装、注释版本或测序类型。")
    if layout not in {"paired", "single"}:
        raise ProposalError("公开参考查询的测序类型不允许。")
    if not enabled_tools or any(not isinstance(tool, str) or not tool.isidentifier() for tool in enabled_tools):
        raise ProposalError("公开参考查询的工具列表不允许。")
    return values


def validate_proposals(raw: object) -> tuple[ConfigPatch, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_PROPOSALS:
        raise ProposalError("配置建议必须是包含 1 至 12 项的数组。")
    patches: list[ConfigPatch] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"op", "path", "value", "reason"}:
            raise ProposalError("配置建议字段不符合允许的结构。")
        if item.get("op") != "replace" or not isinstance(item.get("path"), str):
            raise ProposalError("配置建议只能使用 replace 操作。")
        path = item["path"]
        if path not in _ALLOWED_PATHS or path in seen:
            raise ProposalError("配置建议包含不允许或重复的字段。")
        seen.add(path)
        reason = item.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 400:
            raise ProposalError("配置建议理由格式不正确。")
        value = item.get("value")
        _validate_value(path, value)
        patches.append(ConfigPatch(path=path, value=value, reason=reason.strip()))
    return tuple(patches)


def apply_proposals(config: dict[str, Any], patches: tuple[ConfigPatch, ...]) -> dict[str, Any]:
    candidate = deepcopy(config)
    for patch in patches:
        _, section, key = patch.path.split("/", 2)
        if section == "pipeline":
            step, field = key.split("/", 1)
            candidate.setdefault("pipeline", {}).setdefault(step, {})[field] = patch.value
        else:
            candidate.setdefault(section, {})[key] = patch.value
    return normalize_config(candidate)


def _validate_value(path: str, value: object) -> None:
    allowed = _ALLOWED_PATHS[path]
    if allowed is None:
        if type(value) is not bool:
            raise ProposalError("流程开关必须是布尔值。")
        return
    if isinstance(allowed, set):
        if not isinstance(value, str) or value not in allowed:
            raise ProposalError("配置建议的枚举值不允许。")
        return
    if type(value) is not int or not allowed[0] <= value <= allowed[1]:
        raise ProposalError("配置建议数值超出允许范围。")
