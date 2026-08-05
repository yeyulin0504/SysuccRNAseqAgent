from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence


def export_preflight_config(source: Path, destination: Path) -> dict[str, Any]:
    config = json.loads(source.read_text(encoding="utf-8"))
    server = config.get("server", {})
    container = config.get("container", {})
    reference = config.get("reference", {})
    pipeline = config.get("pipeline", {})

    payload = {
        "schema_version": 1,
        "project": {
            "id": "portable_readonly_hpc_preflight",
            "title": "Portable read-only HPC environment preflight",
            "data_classification": "no-fastq-no-sample-metadata",
        },
        "server": {
            key: server.get(key)
            for key in (
                "host",
                "user",
                "remote_workdir",
                "scheduler",
                "threads",
                "memory_gb",
            )
        },
        "reference": {
            key: reference.get(key, "")
            for key in (
                "name",
                "species",
                "release",
                "assembly",
                "regions",
                "remote_gtf_path",
                "remote_genome_fasta_path",
                "star_index_dir",
                "rsem_index_prefix",
                "arriba_blacklist_path",
                "arriba_known_fusions_path",
            )
        },
        "container": {
            "enabled": bool(container.get("enabled", True)),
            "engine": container.get("engine", "apptainer"),
            "image_path": container.get(
                "image_path",
                "/data/containers/rnaseq-agent-star-rsem.sif",
            ),
            "bind_paths": container.get("bind_paths", []),
        },
        "pipeline": pipeline,
        "server_policy": {
            "init_commands_included": False,
            "uploads_allowed": False,
            "job_submission_allowed": False,
        },
    }
    payload["server"]["port"] = server.get("port") or 22

    required = (
        payload["server"].get("host"),
        payload["server"].get("user"),
        payload["server"].get("remote_workdir"),
        payload["server"].get("scheduler"),
    )
    if not all(required):
        raise ValueError("Source project is missing required server preflight fields.")
    if not isinstance(pipeline, dict) or not pipeline:
        raise ValueError("Source project is missing pipeline settings.")

    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    for forbidden in ("fastq_1", "fastq_2", "local_data_dir", "sample_id"):
        if forbidden in serialized:
            raise ValueError(f"Forbidden data field leaked into export: {forbidden}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(serialized, encoding="utf-8")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    export_preflight_config(args.source.resolve(), args.destination.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
