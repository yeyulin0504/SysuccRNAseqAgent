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
    finalize_run_manifest,
    validate_run_manifest,
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


def test_finalization_preserves_original_fingerprints_and_inputs(tmp_path: Path) -> None:
    path = write_run_manifest(
        tmp_path,
        project_id="project-1",
        revision_id="revision-1",
        run_id="run-1",
        attempt_id="attempt-1",
        contract_id="sha256:contract",
        parameters={"threads": 4},
        config={"server": {"host": "hpc.example"}},
        tool_versions={"fastp": "0.24.1"},
        environment={"platform": "test"},
        validation={"status": "pass"},
        preflight={"overall": "pass"},
        final_status="prepared",
        extra_body={"fingerprints": {"inputs_sha256": "sha256:inputs"}, "inputs": [{"logical_name": "reads"}], "scripts": [{"name": "run.sh"}]},
    )

    finalized = finalize_run_manifest(tmp_path, final_status="download_failed", error="network down")
    payload = validate_run_manifest(finalized)

    assert payload["body"]["fingerprints"]["inputs_sha256"] == "sha256:inputs"
    assert payload["body"]["inputs"] == [{"logical_name": "reads"}]
    assert payload["body"]["scripts"] == [{"name": "run.sh"}]
    assert payload["body"]["final_status"] == "download_failed"
    assert payload["body"]["audit"]["final_status"] == "download_failed"
    assert payload["body"]["audit"]["error"] == "network down"
    assert payload["manifest_id"] == "sha256:" + canonical_sha256(payload["body"])


def test_legacy_manifest_without_audit_can_be_finalized(tmp_path: Path) -> None:
    body = {"run_id": "run-1", "inputs": [{"logical_name": "reads"}], "fingerprints": {"inputs_sha256": "sha256:inputs"}}
    legacy = {"schema_version": 1, "manifest_id": "sha256:" + canonical_sha256(body), "body": body}
    (tmp_path / "run_manifest.json").write_text(json.dumps(legacy), encoding="utf-8")

    payload = validate_run_manifest(finalize_run_manifest(tmp_path, final_status="timeout"))

    assert payload["body"]["inputs"] == [{"logical_name": "reads"}]
    assert payload["body"]["audit"]["final_status"] == "timeout"


def test_flat_legacy_manifest_is_migrated_without_losing_fields(tmp_path: Path) -> None:
    body = {"run_id": "run-legacy", "final_status": "prepared", "custom": {"keep": True}}
    legacy = {
        "schema_version": 0,
        **body,
        "manifest_id": "sha256:" + canonical_sha256(body),
    }
    (tmp_path / "run_manifest.json").write_text(json.dumps(legacy), encoding="utf-8")

    payload = validate_run_manifest(finalize_run_manifest(tmp_path, final_status="failed", error="boom"))

    assert payload["body"]["custom"] == {"keep": True}
    assert payload["body"]["final_status"] == "failed"
    assert payload["manifest_id"] == "sha256:" + canonical_sha256(payload["body"])


def test_redacts_common_secrets_and_url_userinfo(tmp_path: Path) -> None:
    path = write_run_manifest(
        tmp_path,
        project_id="project-1",
        revision_id="revision-1",
        run_id="run-1",
        attempt_id="attempt-1",
        contract_id="sha256:contract",
        parameters={"api_key": "key", "passphrase": "phrase", "url": "https://user:password@example.org/data"},
        config={},
        tool_versions={},
        environment={},
        validation={},
        preflight={},
        final_status="failed",
    )

    text = path.read_text(encoding="utf-8")
    assert '"api_key": "[REDACTED]"' in text
    assert '"passphrase": "[REDACTED]"' in text
    assert "user:password@" not in text
    assert "https://[REDACTED]@example.org/data" in text


def test_lineage_records_metadata_and_source_inputs(tmp_path: Path) -> None:
    input_path = tmp_path / "raw" / "reads.fastq.gz"
    input_path.parent.mkdir()
    input_path.write_bytes(b"reads")

    write_io_lineage(
        tmp_path,
        inputs=[{"logical_name": "reads", "path": "raw/reads.fastq.gz", "source_inputs": []}],
        outputs=[{"logical_name": "counts", "path": "counts.tsv", "source_inputs": ["reads"]}],
    )

    lines = [json.loads(line) for line in (tmp_path / "io_lineage.jsonl").read_text(encoding="utf-8").splitlines()]
    output = next(item for item in lines if item["kind"] == "output")
    assert output["logical_name"] == "counts"
    assert output["source_inputs"] == ["reads"]
    assert output["exists"] is False
    assert output["sha256"] is None
    assert output["sha256_skipped_reason"] == "file_missing"
    input_record = next(item for item in lines if item["kind"] == "input")
    assert input_record["exists"] is True
    assert input_record["size_bytes"] == 5
    assert input_record["sha256"]


def test_lineage_preserves_explicit_empty_source_inputs(tmp_path: Path) -> None:
    write_io_lineage(
        tmp_path,
        inputs=[{"logical_name": "reads", "path": "reads.fastq.gz", "source_inputs": []}],
        outputs=[],
        source_inputs=["unexpected-default"],
    )

    record = json.loads((tmp_path / "io_lineage.jsonl").read_text(encoding="utf-8"))
    assert record["source_inputs"] == []
