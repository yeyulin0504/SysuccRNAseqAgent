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
    assert any(item["id"] == "tool.fastp" for item in envelope["missing"]["required"])
    assert any(item["id"] == "reference.gtf" for item in envelope["missing"]["required"])
    assert any(item["id"] == "scheduler.squeue" for item in envelope["missing"]["preferred"])
    assert not any(item["id"] == "container.docker_daemon" for item in envelope["missing"]["runtime"])
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
            "references": {"container_image": {"state": "readable"}},
        },
    )

    assert report["install_plan"]["default_mode"] == "review_only"
    assert report["install_plan"]["executed"] is False
    assert report["overall"] == "pass"
    assert report["summary"] == {"errors": 0, "warnings": 0}


def test_remote_failure_cannot_be_overridden_by_local_success() -> None:
    config = _config()
    config["container"] = {"enabled": False}
    envelope = build_preflight_envelope(
        config,
        local={"overall": "pass", "tools": {"fastp": {"available": True}}},
        remote={"overall": "fail", "tools": {"fastp": {"available": False}}},
    )

    assert envelope["target"] == "remote"
    assert envelope["tools"]["fastp"]["available"] is False
    assert envelope["ready_to_execute"] is False
    assert any(item["id"] == "remote.preflight" for item in envelope["missing"]["runtime"])


def test_declared_reference_missing_from_remote_report_is_required() -> None:
    config = _config()
    config["container"] = {"enabled": False}
    config["pipeline"]["star"]["enabled"] = True
    envelope = build_preflight_envelope(
        config,
        remote={"overall": "pass", "tools": {"fastp": {"available": True}}},
    )

    missing = {item["id"] for item in envelope["missing"]["required"]}
    assert "reference.star_index_dir" in missing
    assert envelope["ready_to_execute"] is False


def test_missing_container_reference_is_not_ready_to_execute() -> None:
    config = _config()
    envelope = build_preflight_envelope(
        config,
        remote={
            "overall": "pass",
            "tools": {"fastp": {"available": True}},
            "container": {"enabled": True, "engine": "docker"},
        },
    )

    missing = {item["id"] for item in envelope["missing"]["required"]}
    assert "reference.container_image" in missing or "container.image" in missing
    assert envelope["ready_to_execute"] is False


def test_remote_optional_and_runtime_findings_are_preserved_in_their_layers() -> None:
    config = _config()
    config["container"] = {"enabled": False}
    envelope = build_preflight_envelope(
        config,
        remote={
            "overall": "warning",
            "tools": {"fastp": {"available": True}},
            "missing": {
                "optional": [{"id": "reference.blacklist", "reason": "not installed"}],
                "runtime": [{"id": "runtime.r", "reason": "R unavailable"}],
            },
        },
    )

    assert envelope["missing"]["optional"] == [
        {"id": "reference.blacklist", "level": "optional", "reason": "not installed"}
    ]
    assert envelope["missing"]["runtime"] == [
        {"id": "runtime.r", "level": "runtime", "reason": "R unavailable"}
    ]
    assert envelope["ready_to_execute"] is False


def test_sensitive_remote_fields_are_redacted_with_diagnostic_hashes() -> None:
    config = _config()
    config["server"]["host"] = "secret-hpc.internal"
    config["server"]["remote_workdir"] = "/srv/private/project"
    envelope = build_preflight_envelope(
        config,
        remote={
            "overall": "pass",
            "tools": {"fastp": {"available": True}},
            "diagnostics": {
                "hostname": "secret-hpc.internal",
                "remote_host": "secret-hpc.internal:/srv/private/project",
                "source": "/srv/private/project/input.fastq.gz",
                "message": "failed on secret-hpc.internal:/srv/private/project/input.fastq.gz",
                "reason": "ssh://private-user@secret-hpc.internal/srv/private/project",
                "path": "/srv/private/project/input.fastq.gz",
            },
        },
    )

    serialized = json.dumps(envelope, sort_keys=True)
    for secret in ("secret-hpc.internal", "/srv/private/project", "private-user@secret-hpc.internal"):
        assert secret not in serialized
    assert "sha256" in serialized


def test_redaction_removes_credentials_and_bare_host_paths_from_diagnostics() -> None:
    config = _config()
    envelope = build_preflight_envelope(
        config,
        remote={
            "overall": "pass",
            "tools": {"fastp": {"available": True}},
            "diagnostics": {
                "detail": "ssh://alice:password@compute01:/data/private/run.fastq",
                "authorization": "Bearer super-secret-token",
                "remote_host": "compute01",
                "remote_path": "C:\\Users\\alice\\private\\run.fastq",
            },
        },
    )

    serialized = json.dumps(envelope, sort_keys=True)
    for secret in ("alice", "password", "compute01", "/data/private", "super-secret-token"):
        assert secret not in serialized
