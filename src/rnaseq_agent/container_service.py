"""Validated command builders for downstream Apptainer image management."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from .shell import shell_quote


class ContainerSettingsError(ValueError):
    """Raised when container settings cannot be represented safely."""


def summarize_container_preflight(
    settings: dict[str, Any] | None,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize existing container probe findings; never starts a runtime."""

    settings = settings or {}
    report = report or {}
    enabled = bool(settings.get("enabled", report.get("enabled", False)))
    engine = str(settings.get("engine") or report.get("engine") or "apptainer")
    if not enabled:
        return {"enabled": False, "engine": engine}
    result = {
        "enabled": True,
        "engine": engine,
        "engine_available": report.get("engine_available", report.get("available", True)),
        "image_state": report.get("image_state", report.get("image", "not_reported")),
        "daemon_available": report.get(
            "daemon_available",
            report.get("docker_daemon", report.get("daemon")) if engine == "docker" else True,
        ),
        "bind_paths_count": len(settings.get("bind_paths", []) or []),
    }
    if result["daemon_available"] is None and engine == "docker":
        result["daemon_available"] = False
    return result


def validate_image_settings(raw: dict[str, Any]) -> dict[str, Any]:
    engine = str(raw.get("engine") or "apptainer").strip()
    uri = str(raw.get("image_uri") or "").strip()
    image_path = str(raw.get("image_path") or "").strip()
    binds = [str(value).strip() for value in raw.get("bind_paths", []) if str(value).strip()]
    if engine not in {"apptainer", "singularity", "docker"}:
        raise ContainerSettingsError("容器引擎只支持 Docker、Apptainer 或 Singularity")
    if engine != "docker" and not uri.startswith(("docker://", "oras://")):
        raise ContainerSettingsError("镜像来源只允许 docker:// 或 oras://")
    if engine == "docker" and not uri:
        raise ContainerSettingsError("Docker 必须填写镜像地址")
    if engine != "docker" and (not image_path.startswith("/") or PurePosixPath(image_path).suffix != ".sif"):
        raise ContainerSettingsError("服务器镜像路径必须是绝对 .sif 路径")
    if any(not path.startswith("/") for path in binds):
        raise ContainerSettingsError("挂载目录必须是绝对路径")
    return {
        "enabled": bool(raw.get("enabled", True)),
        "engine": engine,
        "image_uri": uri,
        "image_path": image_path,
        "bind_paths": binds,
    }


def build_pull_command(raw: dict[str, Any]) -> str:
    settings = validate_image_settings(raw)
    if settings["engine"] == "docker":
        return f"docker pull {shell_quote(settings['image_uri'])}"
    parent = str(PurePosixPath(settings["image_path"]).parent)
    return (
        f"mkdir -p {shell_quote(parent)} && "
        f"{shell_quote(settings['engine'])} pull --force "
        f"{shell_quote(settings['image_path'])} {shell_quote(settings['image_uri'])}"
    )


def build_test_command(raw: dict[str, Any]) -> str:
    settings = validate_image_settings(raw)
    binds = " ".join(f"--bind {shell_quote(path)}" for path in settings["bind_paths"])
    bind_part = f" {binds}" if binds else ""
    probe = (
        "stopifnot(loadNamespace('DESeq2', quietly=TRUE));"
        "stopifnot(loadNamespace('CMScaller', quietly=TRUE));"
        "cat('RNASEQ_DOWNSTREAM_OK\\n');sessionInfo()"
    )
    if settings["engine"] == "docker":
        binds = " ".join(
            f"-v {shell_quote(path)}:{shell_quote(path)}" for path in settings["bind_paths"]
        )
        bind_part = f" {binds}" if binds else ""
        return (
            f"docker --version && docker run --rm{bind_part} "
            f"{shell_quote(settings['image_uri'])} Rscript -e {shell_quote(probe)}"
        )
    engine = shell_quote(settings["engine"])
    return (
        f"{engine} --version && {engine} exec --cleanenv{bind_part} "
        f"{shell_quote(settings['image_path'])} Rscript -e {shell_quote(probe)}"
    )
