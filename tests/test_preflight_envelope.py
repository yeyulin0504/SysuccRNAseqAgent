from __future__ import annotations

import json
from pathlib import Path

from rnaseq_agent.preflight_envelope import build_preflight_envelope
from rnaseq_agent.portable_preflight import _build_portable_report


def _config() -> dict:
    return {
        "project": {"id": "envelope-test"},
        "server": {"scheduler": "slurm", "remote_workdir": "/home/secret/work"},
        "pipeline": {
            "fastp": {"enabled": True, "version": "0.24.1"},
            "star": {"enabled": False},
            "arriba": {"enabled": False},
            "featurecounts": {"enabled": False},
            "rsem": {"enabled": False},
            "diffexp": {"enabled": False},
            "cms": {"enabled": False},
        },
        "container": {"enabled": True, "engine": "docker", "image_uri": "rnaseq:latest"},
    }


def test_build_preflight_envelope_layers_missing_and_is_ready_only_when_required_runtime_pass() -> None:
    envelope = build_preflight_envelope(
        _config(),
        local={
            "overall": "warning",
            "tools": {"fastp": {"available": True, "path": "/home/secret/bin/fastp"}},
            "container": {"enabled": True, "engine": "docker", "daemon_available": False},
            "permissions": {"data_exfiltration": {"allowed": False}},
        },
        remote={
            "overall": "pass",
            "tools": {"fastp": {"available": False, "path": "/home/secret/bin/fastp"}},
            "scheduler": {
                "configured": "slurm",
                "commands": {
                    "sbatch": {"requirement": "required", "available": True},
                    "squeue": {"requirement": "recommended", "available": False},
                },
            },
            "container": {"enabled": True, "engine": "docker", "daemon_available": True},
            "references": {"gtf": {"state": "missing", "path": "/home/secret/ref/genes.gtf"}},
        },
    )

    assert set(
        (
            "overall",
            "ready_to_execute",
            "missing",
            "tools",
            "references",
            "scheduler",
            "container",
            "permissions",
            "install_plan",
        )
    ) <= envelope.keys()
    assert "fastp" not in {item["id"] for item in envelope["missing"]["required"]}
    assert any(item["id"] == "reference.gtf" for item in envelope["missing"]["required"])
    assert any(item["id"] == "scheduler.squeue" for item in envelope["missing"]["preferred"])
    assert any(item["id"] == "container.docker_daemon" for item in envelope["missing"]["runtime"])
    assert envelope["ready_to_execute"] is False
    assert envelope["overall"] == "blocked"
    assert envelope["install_plan"]["default_mode"] == "review_only"
    assert envelope["install_plan"]["executed"] is False


def test_envelope_redacts_remote_paths_and_denies_data_exfiltration() -> None:
    envelope = build_preflight_envelope(
        _config(),
        remote={
            "overall": "pass",
            "references": {"gtf": {"state": "readable", "path": "/home/secret/ref/genes.gtf"}},
            "tools": {"fastp": {"available": True, "path": "/home/secret/bin/fastp"}},
            "remote_identity": {"hostname": "secret-hpc.internal", "hostname_sha256": "abc"},
        },
    )

    serialized = json.dumps(envelope, sort_keys=True)
    assert "/home/secret" not in serialized
    assert "secret-hpc.internal" not in serialized
    assert envelope["permissions"]["data_exfiltration"]["allowed"] is False
    assert envelope["permissions"]["data_exfiltration"]["policy"] == "deny"


def test_portable_report_wraps_existing_remote_preflight_without_running_install() -> None:
    report = _build_portable_report(
        _config(),
        {
            "schema_version": 1,
            "generated_at": "2026-09-28T00:00:00+00:00",
            "overall": "pass",
            "tools": {"fastp": {"available": True, "reported_version": "0.24.1"}},
            "scheduler": {
                "configured": "slurm",
                "commands": {
                    "sbatch": {"requirement": "required", "available": True},
                    "squeue": {"requirement": "recommended", "available": True},
                    "sacct": {"requirement": "recommended", "available": True},
                },
            },
            "container": {
                "enabled": True,
                "engine": "docker",
                "engine_available": True,
                "daemon_available": True,
                "image_state": "readable",
            },
        },
    )

    assert report["install_plan"]["default_mode"] == "review_only"
    assert report["install_plan"]["executed"] is False
    assert report["overall"] == "pass"
