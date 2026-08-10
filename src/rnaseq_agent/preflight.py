from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .container import container_config, container_errors
from .downstream import downstream_errors
from .configuration import normalize_config
from .remote_transport import RemoteTransport, create_remote_transport
from .shell import shell_quote
from .storage import load_json, save_json


PREFLIGHT_SCHEMA_VERSION = 1
PREFLIGHT_PROBE_VERSION = "read_only_v1"
_PREFIX = "RNASEQ_PREFLIGHT"

_TOOL_COMMANDS: dict[str, tuple[str, str]] = {
    "fastp": ("fastp", "--version"),
    "star": ("STAR", "--version"),
    "arriba": ("arriba", "-v"),
    "featurecounts": ("featureCounts", "-v"),
    "rsem": ("rsem-calculate-expression", "--version"),
}

_SCHEDULER_COMMANDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "slurm": (("sbatch",), ("squeue", "sacct")),
    "pbs": (("qsub",), ("qstat",)),
    "local": (("bash", "nohup"), ()),
}

_STAR_CORE_FILES = (
    "Genome",
    "SA",
    "SAindex",
    "chrLength.txt",
    "chrName.txt",
    "chrNameLength.txt",
    "chrStart.txt",
    "genomeParameters.txt",
)

_RSEM_CORE_SUFFIXES = (".grp", ".ti", ".seq")


class PreflightError(RuntimeError):
    """Raised when a read-only preflight cannot be completed safely."""


def run_preflight(
    config_path: Path,
    *,
    output_path: Path | None = None,
    transport: RemoteTransport | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Run one read-only remote probe and persist only sanitized findings.

    The probe deliberately does not source ``server.init_commands``.  It calls
    ``execute`` exactly once and never calls a transport's upload or download
    methods.  The saved JSON excludes connection identifiers, raw output, and
    the full remote command.
    """

    config_path = config_path.resolve()
    config = normalize_config(load_json(config_path))
    _validate_config_for_preflight(config)
    command = build_read_only_probe(config)

    try:
        active_transport = transport or create_remote_transport(config)
        result = active_transport.execute(command)
    except Exception as exc:
        raise PreflightError(
            "Read-only server preflight failed "
            f"({_safe_exception_summary(exc, config)}). Check VPN/SSH access, "
            "authentication, and host-key trust."
        ) from None

    if result.returncode != 0:
        raise PreflightError(
            "Read-only server preflight returned a non-zero status "
            f"({int(result.returncode)}). Raw remote output was not saved."
        )

    parsed = _parse_probe_output(result.stdout)
    report = _build_report(config, parsed)
    destination = (output_path or config_path.parent / "preflight.json").resolve()
    save_json(destination, report)
    return destination, report


def build_read_only_probe(config: dict[str, Any]) -> str:
    """Build the single POSIX-shell probe used by :func:`run_preflight`.

    Only discovery and access-test primitives are used.  Paths from the config
    are shell-quoted and are never evaluated as code.
    """

    workdir = _safe_remote_path(config.get("server", {}).get("remote_workdir"), "server.remote_workdir")
    container = container_config(config)
    lines = [
        "probe_emit() { printf '%s\\t%s\\t%s\\t%s\\n' "
        f"{shell_quote(_PREFIX)} \"$1\" \"$2\" \"$3\"; }}",
        "probe_command() { "
        "probe_command_name=$1; "
        "probe_command_path=$(command -v \"$probe_command_name\"); "
        "if [ -n \"$probe_command_path\" ]; then "
        "probe_emit command \"$probe_command_name\" \"$probe_command_path\"; "
        "else probe_emit command \"$probe_command_name\" missing; fi; "
        "}",
        "probe_tool() { "
        "probe_tool_key=$1; probe_tool_command=$2; probe_tool_flag=$3; "
        "probe_tool_path=$(command -v \"$probe_tool_command\"); "
        "if [ -n \"$probe_tool_path\" ]; then "
        "probe_emit tool_path \"$probe_tool_key\" \"$probe_tool_path\"; "
        "probe_tool_version=$(\"$probe_tool_command\" \"$probe_tool_flag\" | head -n 1); "
        "probe_emit tool_version \"$probe_tool_key\" \"$probe_tool_version\"; "
        "else probe_emit tool_path \"$probe_tool_key\" missing; fi; "
        "}",
        "probe_file() { "
        "probe_file_label=$1; probe_file_path=$2; "
        "if [ -f \"$probe_file_path\" ] && [ -r \"$probe_file_path\" ]; then "
        "probe_emit reference \"$probe_file_label\" readable; "
        "elif [ -e \"$probe_file_path\" ]; then "
        "probe_emit reference \"$probe_file_label\" unreadable; "
        "else probe_emit reference \"$probe_file_label\" missing; fi; "
        "}",
        "probe_directory() { "
        "probe_directory_label=$1; probe_directory_path=$2; "
        "if [ -d \"$probe_directory_path\" ] && [ -r \"$probe_directory_path\" ] "
        "&& [ -x \"$probe_directory_path\" ]; then "
        "probe_emit reference \"$probe_directory_label\" accessible; "
        "elif [ -e \"$probe_directory_path\" ]; then "
        "probe_emit reference \"$probe_directory_label\" inaccessible; "
        "else probe_emit reference \"$probe_directory_label\" missing; fi; "
        "}",
        "probe_emit metadata home \"$HOME\"",
        "probe_hostname=$(hostname)",
        "probe_emit metadata hostname \"$probe_hostname\"",
        "probe_mask=$(umask)",
        "probe_emit metadata umask \"$probe_mask\"",
    ]

    if container["enabled"]:
        lines.extend(
            [
                "probe_container_tool() { "
                "probe_tool_key=$1; probe_tool_command=$2; probe_tool_flag=$3; "
                "if [ ! -r \"$probe_container_image\" ]; then "
                "probe_emit tool_path \"$probe_tool_key\" missing; return; fi; "
                "probe_tool_version=$(\"$probe_container_engine\" exec "
                f"{_container_exec_args(container)}"
                " \"$probe_tool_command\" \"$probe_tool_flag\" 2>&1); "
                "probe_tool_status=$?; "
                "probe_tool_version=$(printf '%s\\n' \"$probe_tool_version\" | head -n 1); "
                "if [ \"$probe_tool_status\" -eq 0 ]; then "
                "probe_emit tool_path \"$probe_tool_key\" \"container:$probe_tool_command\"; "
                "probe_emit tool_version \"$probe_tool_key\" \"$probe_tool_version\"; "
                "else probe_emit tool_path \"$probe_tool_key\" missing; fi; "
                "}",
                f"probe_container_engine={shell_quote(container['engine'])}",
                f"probe_container_image={shell_quote(container['image_path'])}",
                "probe_command \"$probe_container_engine\"",
                "probe_file container_image \"$probe_container_image\"",
            ]
        )

    all_scheduler_commands = sorted(
        {
            command
            for required, optional in _SCHEDULER_COMMANDS.values()
            for command in (*required, *optional)
        }
    )
    lines.extend(f"probe_command {shell_quote(command)}" for command in all_scheduler_commands)

    tool_probe = "probe_container_tool" if container["enabled"] else "probe_tool"
    for key, (command, flag) in _TOOL_COMMANDS.items():
        lines.append(f"{tool_probe} {shell_quote(key)} {shell_quote(command)} {shell_quote(flag)}")

    downstream = config.get("downstream", {})
    if downstream.get("enabled", False):
        runtime = downstream["runtime"]
        downstream_image = _safe_remote_path(runtime["image_path"], "downstream.runtime.image_path")
        required_r_packages = "jsonlite DESeq2 ggplot2 pheatmap clusterProfiler enrichplot DOSE AnnotationDbi org.Hs.eg.db org.Mm.eg.db matrixStats"
        lines.extend(
            [
                f"probe_file downstream_runtime_image {shell_quote(downstream_image)}",
                "probe_downstream_r() { "
                "if [ ! -r \"$1\" ]; then probe_emit downstream r_packages missing_image; return; fi; "
                "probe_downstream_result=$(apptainer exec \"$1\" Rscript -e "
                "'required <- strsplit(commandArgs(TRUE)[1], \" \")[[1]]; missing <- required[!vapply(required, requireNamespace, logical(1), quietly=TRUE)]; if (length(missing)) quit(status=1); cat(\"available\")' "
                f"--args {shell_quote(required_r_packages)} 2>&1); "
                "probe_downstream_status=$?; "
                "if [ \"$probe_downstream_status\" -eq 0 ]; then probe_emit downstream r_packages available; "
                "else probe_emit downstream r_packages unavailable; fi; "
                "}",
                f"probe_downstream_r {shell_quote(downstream_image)}",
            ]
        )

    lines.extend(_reference_probe_lines(config))
    lines.extend(
        [
            f"probe_work_target={shell_quote(workdir)}",
            "probe_work_candidate=$probe_work_target",
            "probe_work_exact=yes",
            "while [ ! -e \"$probe_work_candidate\" ]; do "
            "probe_work_exact=no; "
            "case \"$probe_work_candidate\" in "
            "/) break ;; "
            "*/*) probe_work_candidate=${probe_work_candidate%/*}; "
            "if [ -z \"$probe_work_candidate\" ]; then probe_work_candidate=/; fi ;; "
            "*) probe_work_candidate=. ;; "
            "esac; done",
            "if [ -d \"$probe_work_candidate\" ]; then "
            "probe_emit workdir target_kind \"$probe_work_exact\"; "
            "else probe_emit workdir target_kind invalid; fi",
            "if test -w \"$probe_work_candidate\"; then "
            "probe_emit workdir writable yes; else probe_emit workdir writable no; fi",
            "if test -x \"$probe_work_candidate\"; then "
            "probe_emit workdir searchable yes; else probe_emit workdir searchable no; fi",
            "probe_df=$(df -Pk \"$probe_work_candidate\" | "
            "awk 'END {print $2 \"|\" $3 \"|\" $4 \"|\" $5}')",
            "probe_emit storage df_kb \"$probe_df\"",
        ]
    )
    return "\n".join(lines)


def _reference_probe_lines(config: dict[str, Any]) -> list[str]:
    pipeline = config.get("pipeline", {})
    reference = config.get("reference", {})
    lines: list[str] = []

    def add_file(label: str, path: Any) -> None:
        value = _safe_remote_path(path, f"reference.{label}")
        lines.append(f"probe_file {shell_quote(label)} {shell_quote(value)}")

    if pipeline.get("star", {}).get("enabled", False):
        star_dir = _safe_remote_path(reference.get("star_index_dir"), "reference.star_index_dir")
        lines.append(
            f"probe_directory star_index_dir {shell_quote(star_dir)}"
        )
        for filename in _STAR_CORE_FILES:
            lines.append(
                f"probe_file star_core_{filename} {shell_quote(star_dir.rstrip('/') + '/' + filename)}"
            )

    if (
        pipeline.get("featurecounts", {}).get("enabled", False)
        or pipeline.get("arriba", {}).get("enabled", False)
    ):
        add_file("gtf", reference.get("remote_gtf_path"))

    if pipeline.get("arriba", {}).get("enabled", False):
        add_file("genome_fasta", reference.get("remote_genome_fasta_path"))
        if reference.get("arriba_blacklist_path"):
            add_file("arriba_blacklist", reference.get("arriba_blacklist_path"))
        if reference.get("arriba_known_fusions_path"):
            add_file("arriba_known_fusions", reference.get("arriba_known_fusions_path"))

    if pipeline.get("rsem", {}).get("enabled", False):
        prefix = _safe_remote_path(reference.get("rsem_index_prefix"), "reference.rsem_index_prefix")
        for suffix in _RSEM_CORE_SUFFIXES:
            label = f"rsem_prefix{suffix.replace('.', '_')}"
            lines.append(
                f"probe_file {shell_quote(label)} {shell_quote(prefix + suffix)}"
            )
    return lines


def _container_exec_args(container: dict[str, Any]) -> str:
    if not container.get("enabled"):
        return ""
    args = []
    for path in container.get("bind_paths", []):
        args.extend(["--bind", str(path)])
    args.append(str(container["image_path"]))
    return " ".join(shell_quote(arg) for arg in args)


def _safe_remote_path(value: Any, field: str) -> str:
    text = str(value or "")
    if not text:
        raise PreflightError(f"Missing {field}.")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise PreflightError(f"Invalid control character in {field}.")
    return text


def _validate_config_for_preflight(config: dict[str, Any]) -> None:
    server = config.get("server", {})
    for field in ("host", "user", "remote_workdir"):
        if not server.get(field):
            raise PreflightError(f"Missing server.{field}.")
    scheduler = str(server.get("scheduler", ""))
    if scheduler not in _SCHEDULER_COMMANDS:
        raise PreflightError(f"Unsupported scheduler: {scheduler or 'missing'}")

    pipeline = config.get("pipeline", {})
    if not any(
        isinstance(value, dict) and bool(value.get("enabled"))
        for value in pipeline.values()
    ):
        raise PreflightError("At least one pipeline step must be enabled.")
    errors = container_errors(config)
    if errors:
        raise PreflightError("; ".join(errors))
    errors = downstream_errors(config)
    if errors:
        raise PreflightError("; ".join(errors))


def _parse_probe_output(stdout: str) -> dict[str, Any]:
    values: dict[str, dict[str, str]] = {}
    for raw_line in stdout.splitlines():
        parts = raw_line.split("\t", 3)
        if len(parts) != 4 or parts[0] != _PREFIX:
            continue
        _, category, key, value = parts
        if category not in {
            "metadata",
            "command",
            "tool_path",
            "tool_version",
            "reference",
            "downstream",
            "workdir",
            "storage",
        }:
            continue
        values.setdefault(category, {})[key] = value.strip()
    return values


def _safe_exception_summary(exc: Exception, config: dict[str, Any]) -> str:
    """Return a short, sanitized diagnostic hint for portable preflight users."""

    if not isinstance(exc, TypeError):
        return type(exc).__name__

    server = config.get("server", {})
    secrets = {
        str(server.get("host", "")),
        str(server.get("user", "")),
        str(server.get("remote_workdir", "")),
    }
    samples = config.get("samples", {})
    if isinstance(samples, dict):
        secrets.add(str(samples.get("local_data_dir", "")))
        secrets.add(str(samples.get("remote_data_dir", "")))
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)[:240]
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _build_report(config: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    metadata = parsed.get("metadata", {})
    hostname = str(metadata.get("hostname", ""))
    home = str(metadata.get("home", ""))
    umask = _safe_umask(metadata.get("umask"))

    scheduler_name = str(config["server"]["scheduler"])
    container = container_config(config)
    required_commands, recommended_commands = _SCHEDULER_COMMANDS[scheduler_name]
    command_values = parsed.get("command", {})
    scheduler_commands: dict[str, dict[str, Any]] = {}
    for command in (*required_commands, *recommended_commands):
        raw_path = str(command_values.get(command, "missing"))
        available = raw_path not in {"", "missing"}
        requirement = "required" if command in required_commands else "recommended"
        scheduler_commands[command] = {
            "requirement": requirement,
            "available": available,
            **_sanitized_path_details(raw_path, command, home),
        }
        if not available:
            severity = "error" if requirement == "required" else "warning"
            findings.append(
                {
                    "severity": severity,
                    "code": f"scheduler_{command}_missing",
                    "message": f"Scheduler command {command} is not available in the non-initialized login environment.",
                }
            )

    tools: dict[str, dict[str, Any]] = {}
    tool_paths = parsed.get("tool_path", {})
    tool_versions = parsed.get("tool_version", {})
    for key, (command, _) in _TOOL_COMMANDS.items():
        step = config.get("pipeline", {}).get(key, {})
        enabled = bool(step.get("enabled", False))
        raw_path = str(tool_paths.get(key, "missing"))
        available = raw_path not in {"", "missing"}
        actual_version = _extract_version(tool_versions.get(key)) if available else ""
        expected_version = _extract_version(step.get("version")) if enabled else ""
        version_matches = (
            actual_version == expected_version
            if actual_version and expected_version
            else None
        )
        tool_report: dict[str, Any] = {
            "enabled": enabled,
            "command": command,
            "available": available,
            **_sanitized_path_details(raw_path, command, home),
            "reported_version": actual_version or "unreported",
            "expected_version": expected_version or "unspecified",
            "version_matches": version_matches,
        }
        tools[key] = tool_report
        if enabled and not available:
            location = (
                "inside the configured container image"
                if container["enabled"]
                else "without running init_commands"
            )
            findings.append(
                {
                    "severity": "error",
                    "code": f"tool_{key}_missing",
                    "message": f"Enabled tool {command} is not available {location}.",
                }
            )
        elif enabled and not actual_version:
            findings.append(
                {
                    "severity": "warning",
                    "code": f"tool_{key}_version_unreported",
                    "message": f"Enabled tool {command} was found but did not report a parseable version.",
                }
            )
        elif enabled and version_matches is False:
            findings.append(
                {
                    "severity": "warning",
                    "code": f"tool_{key}_version_mismatch",
                    "message": f"Enabled tool {command} does not match the configured version label.",
                }
            )

    references: dict[str, dict[str, str]] = {}
    for label, state in sorted(parsed.get("reference", {}).items()):
        normalized_state = state if state in {"readable", "accessible", "missing", "unreadable", "inaccessible"} else "unknown"
        references[label] = {"state": normalized_state}
        if normalized_state not in {"readable", "accessible"}:
            findings.append(
                {
                    "severity": "error",
                    "code": f"reference_{_safe_code(label)}_{normalized_state}",
                    "message": f"Required reference check {label} is {normalized_state}.",
                }
            )

    expected_reference_labels = _expected_reference_labels(config)
    for label in sorted(expected_reference_labels.difference(references)):
        references[label] = {"state": "not_reported"}
        findings.append(
            {
                "severity": "error",
                "code": f"reference_{_safe_code(label)}_not_reported",
                "message": f"Required reference check {label} was not reported by the probe.",
            }
        )

    work_values = parsed.get("workdir", {})
    target_kind_value = str(work_values.get("target_kind", "unknown"))
    target_kind = {
        "yes": "configured_workdir",
        "no": "nearest_existing_parent",
        "invalid": "invalid_target",
    }.get(target_kind_value, "unknown")
    writable = work_values.get("writable") == "yes"
    searchable = work_values.get("searchable") == "yes"
    if not writable:
        findings.append(
            {
                "severity": "error",
                "code": "workdir_parent_not_writable",
                "message": "The configured work directory or its nearest existing parent is not writable.",
            }
        )
    if not searchable:
        findings.append(
            {
                "severity": "error",
                "code": "workdir_parent_not_searchable",
                "message": "The configured work directory or its nearest existing parent is not searchable.",
            }
        )

    storage = _parse_df(parsed.get("storage", {}).get("df_kb"))
    if storage["available_kb"] is None:
        findings.append(
            {
                "severity": "warning",
                "code": "storage_capacity_unreported",
                "message": "Filesystem capacity could not be parsed from df -Pk.",
            }
        )

    private_umask = _is_private_umask(umask)
    if umask == "unreported":
        findings.append(
            {
                "severity": "warning",
                "code": "umask_unreported",
                "message": "The login-shell umask was not reported.",
            }
        )
    elif not private_umask:
        findings.append(
            {
                "severity": "warning",
                "code": "umask_not_private",
                "message": "The login-shell umask permits group or other access; execution still enforces umask 077.",
            }
        )

    container_details = _container_report(container, parsed, findings)
    downstream_details = _downstream_report(config, parsed, findings)
    errors = sum(item["severity"] == "error" for item in findings)
    warnings = sum(item["severity"] == "warning" for item in findings)
    overall = "fail" if errors else "warning" if warnings else "pass"

    return {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "probe_version": PREFLIGHT_PROBE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_id": str(config.get("project", {}).get("id", "")),
        "overall": overall,
        "summary": {"errors": errors, "warnings": warnings},
        "privacy": {
            "connection_identifiers_saved": False,
            "raw_output_saved": False,
            "full_command_saved": False,
        },
        "environment_setup": {
            "init_commands_executed": False,
            "note": "Checks describe the non-initialized login environment.",
        },
        "remote_identity": {
            "hostname_sha256": hashlib.sha256(hostname.encode("utf-8")).hexdigest()
            if hostname
            else "unreported"
        },
        "scheduler": {
            "configured": scheduler_name,
            "commands": scheduler_commands,
        },
        "container": container_details,
        "downstream": downstream_details,
        "tools": tools,
        "references": references,
        "workdir_access": {
            "tested_target": target_kind,
            "writable": writable,
            "searchable": searchable,
        },
        "storage": storage,
        "security": {
            "login_umask": umask,
            "private_by_default": private_umask,
            "execution_enforces_umask_077": True,
        },
        "findings": findings,
    }


def _expected_reference_labels(config: dict[str, Any]) -> set[str]:
    pipeline = config.get("pipeline", {})
    reference = config.get("reference", {})
    labels: set[str] = set()
    if container_config(config)["enabled"]:
        labels.add("container_image")
    if pipeline.get("star", {}).get("enabled", False):
        labels.add("star_index_dir")
        labels.update(f"star_core_{name}" for name in _STAR_CORE_FILES)
    if pipeline.get("featurecounts", {}).get("enabled", False) or pipeline.get("arriba", {}).get("enabled", False):
        labels.add("gtf")
    if pipeline.get("arriba", {}).get("enabled", False):
        labels.add("genome_fasta")
        if reference.get("arriba_blacklist_path"):
            labels.add("arriba_blacklist")
        if reference.get("arriba_known_fusions_path"):
            labels.add("arriba_known_fusions")
    if pipeline.get("rsem", {}).get("enabled", False):
        labels.update(f"rsem_prefix{suffix.replace('.', '_')}" for suffix in _RSEM_CORE_SUFFIXES)
    return labels


def _sanitized_path_details(raw_path: str, command: str, home: str) -> dict[str, str]:
    if raw_path in {"", "missing"}:
        return {"path": "unresolved", "path_sha256": "unreported"}
    normalized = raw_path.strip()
    if normalized.startswith("container:"):
        return {
            "path": f"container/{command}",
            "path_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        }
    if home and (normalized == home or normalized.startswith(home.rstrip("/") + "/")):
        hint = f"<private-root>/{command}"
    elif normalized in {f"/bin/{command}", f"/usr/bin/{command}", f"/usr/local/bin/{command}"}:
        hint = normalized
    else:
        hint = f"<remote-path>/{command}"
    return {
        "path": hint,
        "path_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    }


def _container_report(
    container: dict[str, Any],
    parsed: dict[str, Any],
    findings: list[dict[str, str]],
) -> dict[str, Any]:
    if not container["enabled"]:
        return {"enabled": False}
    command_values = parsed.get("command", {})
    engine_path = str(command_values.get(container["engine"], "missing"))
    engine_available = engine_path not in {"", "missing"}
    image_state = str(parsed.get("reference", {}).get("container_image", "not_reported"))
    if not engine_available:
        findings.append(
            {
                "severity": "error",
                "code": "container_engine_missing",
                "message": f"Container engine {container['engine']} is not available.",
            }
        )
    return {
        "enabled": True,
        "engine": container["engine"],
        "engine_available": engine_available,
        "image_state": image_state,
        "bind_paths_count": len(container.get("bind_paths", [])),
    }


def _downstream_report(
    config: dict[str, Any],
    parsed: dict[str, Any],
    findings: list[dict[str, str]],
) -> dict[str, Any]:
    downstream = config.get("downstream", {})
    if not downstream.get("enabled", False):
        return {"enabled": False}
    state = str(parsed.get("reference", {}).get("downstream_runtime_image", "not_reported"))
    packages = str(parsed.get("downstream", {}).get("r_packages", "not_reported"))
    if state != "readable":
        findings.append({"severity": "error", "code": "downstream_runtime_image_unreadable", "message": "Downstream Apptainer image is not readable."})
    if packages != "available":
        findings.append({"severity": "error", "code": "downstream_r_packages_unavailable", "message": "Downstream R/Bioconductor runtime does not provide the required packages."})
    return {
        "enabled": True,
        "environment_kind": downstream.get("runtime", {}).get("environment_kind", ""),
        "image_state": state,
        "required_packages": packages,
    }


def _extract_version(value: Any) -> str:
    text = str(value or "")[:256]
    match = re.search(r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+)+(?:[-+._]?[A-Za-z0-9]+)*)", text, re.IGNORECASE)
    return match.group(1) if match else ""


def _safe_umask(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[0-7]{3,4}", text):
        return text.zfill(4)
    return "unreported"


def _is_private_umask(value: str) -> bool:
    if not re.fullmatch(r"[0-7]{4}", value):
        return False
    numeric = int(value, 8)
    return bool(numeric & 0o077 == 0o077)


def _parse_df(value: Any) -> dict[str, int | str | None]:
    text = str(value or "").strip()
    fields = text.split("|")
    if len(fields) != 4:
        return {
            "unit": "KiB",
            "total_kb": None,
            "used_kb": None,
            "available_kb": None,
            "capacity_percent": None,
        }
    try:
        total, used, available = (int(fields[index]) for index in range(3))
        capacity = int(fields[3].rstrip("%"))
    except (TypeError, ValueError):
        return {
            "unit": "KiB",
            "total_kb": None,
            "used_kb": None,
            "available_kb": None,
            "capacity_percent": None,
        }
    return {
        "unit": "KiB",
        "total_kb": total,
        "used_kb": used,
        "available_kb": available,
        "capacity_percent": capacity,
    }


def _safe_code(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_") or "unknown"
