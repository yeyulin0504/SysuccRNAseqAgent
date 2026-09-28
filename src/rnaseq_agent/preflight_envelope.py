"""Stable, redacted aggregation of existing local and remote preflight reports.

This module deliberately does not probe tools, SSH, containers, or schedulers.
Those checks belong to the existing preflight providers; this layer only
normalizes their findings into the envelope consumed by audit and UI code.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
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
_SENSITIVE_TEXT_KEYS = {
    "hostname",
    "remote_host",
    "source",
    "path",
    "remote_path",
    "detail",
    "authorization",
}
_GOOD_REFERENCE_STATES = {"readable", "accessible", "available", "present"}


def build_preflight_envelope(
    config: dict[str, Any],
    *,
    local: dict[str, Any] | None = None,
    remote: dict[str, Any] | None = None,
    install_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate already-produced preflight reports without performing I/O."""

    config = normalize_config(config)
    target = "remote" if remote is not None else "local"
    selected_report = _redact_report(
        remote if target == "remote" else (local or {}),
        secrets=_sensitive_values(config),
    )
    reports = (selected_report,)

    tools = _merge_named_reports(reports, "tools")
    references = _merge_named_reports(reports, "references")
    scheduler = _merge_scheduler(selected_report, {}, config)
    container = summarize_container_preflight(config.get("container", {}), _first_component(reports, "container"))
    permissions = _build_permissions(config, selected_report, {})

    missing = {level: [] for level in _MISSING_LEVELS}
    _copy_report_missing(selected_report, missing)
    report_status = str(selected_report.get("overall") or "").lower()
    if report_status in {"fail", "failed", "blocked", "error"}:
        missing["runtime"].append(
            _missing(f"{target}.preflight", "runtime", f"{target} preflight reported {report_status}")
        )
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
        if state not in _GOOD_REFERENCE_STATES:
            missing["required"].append(
                _missing("reference." + key, "required", f"reference is {state}")
            )

    for key in sorted(_expected_reference_labels(config).difference(references)):
        missing["required"].append(
            _missing("reference." + key, "required", "required reference was not reported")
        )

    for name in ("writable", "searchable"):
        if permissions["workdir"].get(name) is False:
            missing["required"].append(
                _missing("permissions.workdir." + name, "required", "work directory permission is unavailable")
            )

    if container.get("enabled"):
        if container.get("engine_available") is not True:
            missing["runtime"].append(
                _missing("container.engine", "runtime", "container engine is unavailable")
            )
        if container.get("engine") == "docker" and container.get("daemon_available") is not True:
            missing["runtime"].append(
                _missing("container.docker_daemon", "runtime", "Docker daemon is unavailable")
            )
        if container.get("image_state") not in _GOOD_REFERENCE_STATES:
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
    summary = {
        "errors": len(missing["required"]) + len(missing["runtime"]),
        "warnings": len(missing["preferred"]) + len(missing["optional"]),
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "overall": overall,
        "ready_to_execute": not blocked,
        "summary": summary,
        "missing": missing,
        "tools": tools,
        "references": references,
        "scheduler": scheduler,
        "container": container,
        "permissions": permissions,
        "install_plan": plan,
        "diagnostics": selected_report.get("diagnostics", {}),
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


def _copy_report_missing(report: dict[str, Any], missing: dict[str, list[dict[str, str]]]) -> None:
    reported = report.get("missing")
    if not isinstance(reported, dict):
        return
    for level in _MISSING_LEVELS:
        items = reported.get(level)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            missing[level].append(
                _missing(
                    str(item["id"]),
                    level,
                    str(item.get("reason") or f"reported {level} preflight finding"),
                )
            )


def _expected_reference_labels(config: dict[str, Any]) -> set[str]:
    pipeline = config.get("pipeline", {})
    reference = config.get("reference", {})
    labels: set[str] = set()
    if config.get("container", {}).get("enabled", False):
        labels.add("container_image")
    if pipeline.get("star", {}).get("enabled", False):
        labels.add("star_index_dir")
        labels.update(
            f"star_core_{name}"
            for name in ("Genome", "SA", "SAindex", "chrLength.txt", "chrName.txt", "chrNameLength.txt", "chrStart.txt", "genomeParameters.txt")
        )
    if pipeline.get("featurecounts", {}).get("enabled", False) or pipeline.get("arriba", {}).get("enabled", False):
        labels.add("gtf")
    if pipeline.get("arriba", {}).get("enabled", False):
        labels.add("genome_fasta")
        if reference.get("arriba_blacklist_path"):
            labels.add("arriba_blacklist")
        if reference.get("arriba_known_fusions_path"):
            labels.add("arriba_known_fusions")
    if pipeline.get("rsem", {}).get("enabled", False):
        labels.update(f"rsem_prefix{suffix.replace('.', '_')}" for suffix in (".grp", ".ti", ".seq"))
    declared = reference.get("required") or reference.get("required_keys")
    if isinstance(declared, (list, tuple, set)):
        labels.update(str(item) for item in declared if str(item).strip())
    return labels


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


def _sensitive_values(config: dict[str, Any]) -> tuple[str, ...]:
    values: set[str] = set()

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, str(child_key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str) and value.strip():
            normalized = key.lower()
            if normalized in _SENSITIVE_KEYS or any(part in normalized for part in _PATH_KEY_PARTS):
                values.add(value)

    walk(config)
    return tuple(sorted(values, key=len, reverse=True))


def _hash_placeholder(value: str, label: str = "redacted") -> str:
    import hashlib

    return f"<{label}:sha256={hashlib.sha256(value.encode('utf-8')).hexdigest()}>"


def _redact_text(value: str, secrets: tuple[str, ...], *, field: str = "") -> str:
    text = value.replace("\\", "/")
    for secret in secrets:
        if secret:
            text = text.replace(secret.replace("\\", "/"), _hash_placeholder(secret))
    normalized_field = field.lower()
    if normalized_field in _SENSITIVE_TEXT_KEYS:
        return _hash_placeholder(text, "sensitive") if text else text
    if any(part in normalized_field for part in _PATH_KEY_PARTS):
        text = re.sub(r"(?<![\w.-])(?:[A-Za-z]:/|/)[^\s'\"`,;\]}]+", lambda match: _hash_placeholder(match.group(0), "remote-path"), text)
        text = re.sub(r"(?<![\w.-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?=[:/\s]|$)", lambda match: _hash_placeholder(match.group(0), "remote-host"), text)
        text = re.sub(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?=[:/\s]|$)", lambda match: _hash_placeholder(match.group(0), "remote-host"), text)
    return text


def _redact_report(value: Any, key: str = "", *, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        result = {}
        for raw_key, child in value.items():
            child_key = str(raw_key)
            normalized = child_key.lower()
            if normalized in _SENSITIVE_KEYS:
                result[child_key] = _hash_placeholder(str(child), "redacted") if child not in (None, "") else "[REDACTED]"
            else:
                result[child_key] = _redact_report(child, child_key, secrets=secrets)
        return result
    if isinstance(value, list):
        return [_redact_report(item, key, secrets=secrets) for item in value]
    if isinstance(value, str):
        return _redact_text(value, secrets, field=key)
    return value
