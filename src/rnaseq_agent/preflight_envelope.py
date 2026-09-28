"""Stable, redacted aggregation of existing local and remote preflight reports.

This module deliberately does not probe tools, SSH, containers, or schedulers.
Those checks belong to the existing preflight providers; this layer only
normalizes their findings into the envelope consumed by audit and UI code.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from .container_service import summarize_container_preflight
from .configuration import normalize_config


_MISSING_LEVELS = ("required", "preferred", "optional", "runtime")
_SENSITIVE_KEYS = {
    "host",
    "hostname",
    "user",
    "username",
    "password",
    "secret",
    "token",
    "remote_workdir",
    "remote_data_dir",
}
_PATH_KEY_PARTS = ("path", "dir", "file", "workdir", "fasta", "fastq", "gtf", "index")


def build_preflight_envelope(
    config: dict[str, Any],
    *,
    local: dict[str, Any] | None = None,
    remote: dict[str, Any] | None = None,
    install_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate already-produced preflight reports without performing I/O."""

    config = normalize_config(config)
    local_report = _redact_report(local or {})
    remote_report = _redact_report(remote or {})
    reports = (local_report, remote_report)

    tools = _merge_named_reports(reports, "tools")
    references = _merge_named_reports(reports, "references")
    scheduler = _merge_scheduler(local_report, remote_report, config)
    container = summarize_container_preflight(config.get("container", {}), _first_component(reports, "container"))
    permissions = _build_permissions(config, local_report, remote_report)

    missing = {level: [] for level in _MISSING_LEVELS}
    pipeline = config.get("pipeline", {})
    for key, step in pipeline.items():
        if not isinstance(step, dict) or not step.get("enabled"):
            continue
        report = tools.get(key, {})
        if not report.get("available", False):
            missing["required"].append(
                _missing("tool." + key, "required", "enabled tool is unavailable")
            )
        elif report.get("version_matches") is False or report.get("reported_version") in {None, "", "unreported"}:
            missing["preferred"].append(
                _missing("tool." + key + ".version", "preferred", "tool version is not verified")
            )

    for command, details in scheduler.get("commands", {}).items():
        if not details.get("available", False):
            level = "required" if details.get("requirement") == "required" else "preferred"
            missing[level].append(
                _missing("scheduler." + command, level, "scheduler command is unavailable")
            )

    for key, details in references.items():
        state = str(details.get("state") or "unknown")
        if state not in {"readable", "accessible", "available", "present"}:
            missing["required"].append(
                _missing("reference." + key, "required", f"reference is {state}")
            )

    for name in ("writable", "searchable"):
        if permissions["workdir"].get(name) is False:
            missing["required"].append(
                _missing("permissions.workdir." + name, "required", "work directory permission is unavailable")
            )

    if container.get("enabled"):
        if not container.get("engine_available", True):
            missing["runtime"].append(
                _missing("container.engine", "runtime", "container engine is unavailable")
            )
        if container.get("engine") == "docker" and container.get("daemon_available") is False:
            missing["runtime"].append(
                _missing("container.docker_daemon", "runtime", "Docker daemon is unavailable")
            )
        if container.get("image_state") in {"missing", "unreadable", "inaccessible"}:
            missing["required"].append(
                _missing("container.image", "required", "container image is unavailable")
            )

    requested_install_plan = deepcopy(install_plan or {})
    plan = {
        **requested_install_plan,
        "default_mode": "review_only",
        "mode": "review_only",
        "executed": False,
        "requires_approval": True,
        "commands": list(requested_install_plan.get("commands", [])),
    }

    missing = {key: _dedupe(items) for key, items in missing.items()}
    blocked = bool(missing["required"] or missing["runtime"])
    has_warnings = bool(missing["preferred"] or missing["optional"])
    overall = "blocked" if blocked else "warning" if has_warnings else "pass"
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "ready_to_execute": not blocked,
        "missing": missing,
        "tools": tools,
        "references": references,
        "scheduler": scheduler,
        "container": container,
        "permissions": permissions,
        "install_plan": plan,
    }


def _missing(identifier: str, level: str, reason: str) -> dict[str, str]:
    return {"id": identifier, "level": level, "reason": reason}


def _dedupe(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result = []
    for item in items:
        if item["id"] not in seen:
            seen.add(item["id"])
            result.append(item)
    return result


def _first_component(reports: tuple[dict[str, Any], ...], name: str) -> dict[str, Any]:
    for report in reports:
        value = report.get(name)
        if isinstance(value, dict):
            return value
    return {}


def _merge_named_reports(reports: tuple[dict[str, Any], ...], name: str) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for report in reports:
        values = report.get(name, {})
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if not isinstance(value, dict):
                continue
            current = merged.setdefault(str(key), {})
            current.update(value)
            if value.get("available") is True:
                current["available"] = True
            if value.get("state") in {"readable", "accessible", "available", "present"}:
                current["state"] = value["state"]
    return merged


def _merge_scheduler(local: dict[str, Any], remote: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    values = [report.get("scheduler") for report in (local, remote) if isinstance(report.get("scheduler"), dict)]
    result = deepcopy(values[-1]) if values else {"configured": config.get("server", {}).get("scheduler", "local"), "commands": {}}
    commands: dict[str, dict[str, Any]] = {}
    for value in values:
        for command, details in (value.get("commands") or {}).items():
            if isinstance(details, dict):
                current = commands.setdefault(str(command), {})
                current.update(details)
                if details.get("available") is True:
                    current["available"] = True
    result["configured"] = str(result.get("configured") or config.get("server", {}).get("scheduler", "local"))
    result["commands"] = commands
    return result


def _build_permissions(config: dict[str, Any], local: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any]:
    workdir = {}
    for report in (local, remote):
        for source in (report.get("workdir_access"), report.get("permissions", {}).get("workdir")):
            if isinstance(source, dict):
                workdir.update(source)
    policy = config.get("permissions", {}).get("data_exfiltration", {})
    if not isinstance(policy, dict):
        policy = {}
    approved = bool(policy.get("approved", False))
    return {
        "workdir": {key: workdir[key] for key in ("writable", "searchable") if key in workdir},
        "data_exfiltration": {
            "policy": "allow" if approved and policy.get("allowed") is True else "deny",
            "allowed": bool(approved and policy.get("allowed") is True),
            "reason": "Preflight and install planning never upload sample data or expose remote paths.",
        },
    }


def _redact_report(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        result = {}
        for raw_key, child in value.items():
            child_key = str(raw_key)
            normalized = child_key.lower()
            if normalized in _SENSITIVE_KEYS:
                result[child_key] = "[REDACTED]"
            else:
                result[child_key] = _redact_report(child, child_key)
        return result
    if isinstance(value, list):
        return [_redact_report(item, key) for item in value]
    if isinstance(value, str):
        if "@" in value and "://" in value:
            value = re.sub(r"(://)([^/@\s]+)@", r"\1[REDACTED]@", value)
        if "/" in value or "\\" in value:
            key_lower = key.lower()
            if any(part in key_lower for part in _PATH_KEY_PARTS):
                name = PurePosixPath(value.replace("\\", "/")).name
                return f"<remote-path>/{name}" if name else "<remote-path>"
        return value
    return value
