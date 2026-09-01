from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .shell import remote_path, shell_quote


@dataclass(frozen=True)
class RemoteCommand:
    description: str
    command: list[str]


def build_directory_scan_command(host: str, user: str, remote_dir: str) -> RemoteCommand:
    target = f"{user}@{host}"
    find_expr = (
        "find "
        f"{shell_quote(remote_dir)} "
        "-maxdepth 2 -type f "
        "\\( -name '*.fastq.gz' -o -name '*.fq.gz' \\) "
        "| sort"
    )
    return RemoteCommand("Scan remote FASTQ files", ["ssh", target, find_expr])


def build_upload_commands(config: dict[str, Any]) -> list[RemoteCommand]:
    server = config["server"]
    samples = config["samples"]
    paired = config.get("sequencing", {}).get("layout", "paired") == "paired"
    target = f"{server['user']}@{server['host']}"
    remote_data_dir = samples["remote_data_dir"]
    local_data_dir = Path(samples["local_data_dir"])

    commands = [
        RemoteCommand(
            "Create remote FASTQ directory",
            ["ssh", target, f"mkdir -p {shell_quote(remote_data_dir)}"],
        )
    ]

    local_files: list[Path] = []
    seen: set[Path] = set()
    for sample in samples.get("items", []):
        keys = ("fastq_1", "fastq_2") if paired else ("fastq_1",)
        for key in keys:
            fastq = sample.get(key)
            if not fastq:
                continue
            local_path = local_data_dir / fastq
            if local_path not in seen:
                seen.add(local_path)
                local_files.append(local_path)

    if local_files:
        sources = [str(path).replace("\\", "/") for path in local_files]
        commands.append(
            RemoteCommand(
                "Upload local FASTQ files to server",
                ["scp", *sources, f"{target}:{remote_path(remote_data_dir)}/"],
            )
        )

    return commands


def collect_local_fastq_paths(config: dict[str, Any]) -> list[Path]:
    samples = config["samples"]
    paired = config.get("sequencing", {}).get("layout", "paired") == "paired"
    local_data_dir = Path(samples["local_data_dir"])
    local_files: list[Path] = []
    seen: set[Path] = set()
    for sample in samples.get("items", []):
        keys = ("fastq_1", "fastq_2") if paired else ("fastq_1",)
        for key in keys:
            fastq = sample.get(key)
            if not fastq:
                continue
            local_path = local_data_dir / fastq
            if local_path not in seen:
                seen.add(local_path)
                local_files.append(local_path)
    return local_files


def project_remote_workdir(remote_base_dir: str, project_id: str) -> str:
    return f"{remote_base_dir.rstrip('/')}/{project_id}"


def local_project_dir(base_dir: Path, project_id: str) -> Path:
    return base_dir / project_id
