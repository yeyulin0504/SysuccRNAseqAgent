from __future__ import annotations

import json
from pathlib import Path

import pytest

from rnaseq_agent.run_audit import (
    canonical_sha256,
    flatten_declared_paths,
    sha256_file,
    write_artifact_index,
    write_io_lineage,
    write_run_manifest,
)
from rnaseq_agent.run_agent import _best_effort_write_audit_manifest


def test_canonical_sha256_is_stable_for_mapping_order() -> None:
    assert canonical_sha256({"b": [2, 1], "a": "值"}) == canonical_sha256(
        {"a": "值", "b": [2, 1]}
    )


def test_flatten_declared_paths_preserves_nested_roles() -> None:
    records = flatten_declared_paths(
        {"samples": [{"fastq_1": "reads/a.fq.gz"}], "reference": {"gtf": "ref.gtf"}}
    )

    assert records == [
        {"path": "reads/a.fq.gz", "declared_at": "samples[0].fastq_1"},
        {"path": "ref.gtf", "declared_at": "reference.gtf"},
    ]


def test_sha256_file_skips_content_hash_above_limit(tmp_path: Path) -> None:
    path = tmp_path / "large.bin"
    path.write_bytes(b"0123456789")

    result = sha256_file(path, max_bytes=4)

    assert result["size_bytes"] == 10
    assert "sha256" not in result
    assert result["sha256_skipped_reason"] == "size_exceeds_max_bytes"


def test_lineage_and_artifact_index_are_written(tmp_path: Path) -> None:
    lineage_path = write_io_lineage(
        tmp_path,
        inputs={"reads": "raw/sample_R1.fastq.gz"},
        outputs=[{"path": "counts/gene_counts.tsv", "artifact_id": "counts-1"}],
    )
    index_path = write_artifact_index(
        tmp_path,
        [{"path": "counts/gene_counts.tsv", "status": "VALID"}],
    )

    assert lineage_path.name == "io_lineage.jsonl"
    assert index_path.name == "artifact_index.json"
    assert lineage_path.read_text(encoding="utf-8").count("\n") == 2
    assert json.loads(index_path.read_text(encoding="utf-8"))["artifacts"][0]["status"] == "VALID"


def test_run_manifest_rejects_id_body_mismatch(tmp_path: Path) -> None:
    path = write_run_manifest(
        tmp_path,
        project_id="project-1",
        revision_id="revision-1",
        run_id="run-1",
        attempt_id="attempt-1",
        contract_id="sha256:contract",
        parameters={"threads": 4},
        config={"server": {"password": "do-not-write"}},
        tool_versions={"fastp": "0.24.1"},
        environment={"platform": "test"},
        validation={"status": "pass"},
        preflight={"overall": "pass"},
        final_status="failed",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["body"]["final_status"] = "completed"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest ID"):
        write_run_manifest(
            tmp_path,
            project_id="project-1",
            revision_id="revision-1",
            run_id="run-1",
            attempt_id="attempt-1",
            contract_id="sha256:contract",
            parameters={"threads": 4},
            config={"server": {"password": "do-not-write"}},
            tool_versions={"fastp": "0.24.1"},
            environment={"platform": "test"},
            validation={"status": "pass"},
            preflight={"overall": "pass"},
            final_status="completed",
        )

    assert "do-not-write" not in path.read_text(encoding="utf-8")


def test_failed_attempt_can_write_audit_manifest_without_secrets(tmp_path: Path) -> None:
    _best_effort_write_audit_manifest(
        tmp_path,
        config={
            "project": {"id": "project-1"},
            "execution": {"verified_contract_id": "sha256:contract"},
            "server": {"host": "hpc.example", "password": "super-secret"},
        },
        run_id="run-1",
        attempt_id="attempt-1",
        final_status="failed",
        error="pre-submit validation failed",
    )

    payload = json.loads((tmp_path / "run_manifest.json").read_text(encoding="utf-8"))
    assert payload["body"]["final_status"] == "failed"
    assert "super-secret" not in (tmp_path / "run_manifest.json").read_text(encoding="utf-8")
