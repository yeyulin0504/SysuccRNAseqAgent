from __future__ import annotations

from typing import Any

from .shell import shell_quote


SUPPORTED_CONTAINER_ENGINES = {"apptainer", "singularity", "docker"}


def container_config(config: dict[str, Any]) -> dict[str, Any]:
    container = config.get("container", {})
    if not isinstance(container, dict):
        return {"enabled": False}
    engine = str(container.get("engine", "apptainer") or "apptainer")
    image_path = str(container.get("image_path", "") or "")
    image_uri = str(container.get("image_uri", "") or "")
    bind_paths = container.get("bind_paths", [])
    if not isinstance(bind_paths, list):
        bind_paths = []
    return {
        "enabled": bool(container.get("enabled", False)),
        "engine": engine,
        "image_path": image_path,
        "image_uri": image_uri,
        "bind_paths": [str(path) for path in bind_paths if str(path or "").strip()],
    }


def container_errors(config: dict[str, Any]) -> list[str]:
    container = container_config(config)
    if not container["enabled"]:
        return []
    errors: list[str] = []
    if container["engine"] not in SUPPORTED_CONTAINER_ENGINES:
        errors.append(f"Unsupported container engine: {container['engine']}")
    if container["engine"] == "docker":
        if not container["image_uri"]:
            errors.append("Missing container.image_uri for Docker execution.")
    elif not container["image_path"]:
        errors.append("Missing container.image_path for Apptainer execution.")
    return errors


def wrap_command(config: dict[str, Any], command: str) -> str:
    container = container_config(config)
    if not container["enabled"]:
        return command
    if container["engine"] == "docker":
        bind_args = " ".join(
            f"-v {shell_quote(path)}:{shell_quote(path)}" for path in container["bind_paths"]
        )
        bind_prefix = f"{bind_args} " if bind_args else ""
        return f"docker run --rm {bind_prefix}{shell_quote(container['image_uri'])} {command}"
    bind_args = " ".join(
        f"--bind {shell_quote(path)}" for path in container["bind_paths"]
    )
    bind_prefix = f"{bind_args} " if bind_args else ""
    return (
        f"{shell_quote(container['engine'])} exec --cleanenv {bind_prefix}"
        f"{shell_quote(container['image_path'])} {command}"
    )
