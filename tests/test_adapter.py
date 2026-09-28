"""Tests for the Adapter (inspect/plan/materialize) and the post-run chain.

Framework section 5.2: Adapter.inspect/plan -> Analysis Contract ->
Adapter.materialize -> Execution Gateway -> Output Validator ->
Post-run Gate -> Artifact Registry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rnaseq_agent.adapter import (
    AdapterError,
    adapter_inspect,
    adapter_materialize,
    adapter_plan,
    discover_adapter,
)
from rnaseq_agent.capability import (
    ABSTAIN,
    FAIL_OUTPUT_CONTRACT,
    NOT_EVALUABLE,
    PASS,
    STALE,
    resolve_capability,
)
from rnaseq_agent.output_validator import (
    ValidationFailure,
    mark_stale_downstream,
    post_run_gate,
    register_artifacts,
    validate_output_contract,
)

CAP_ID = "workflow.bulk_rna.grch38_pe_expression_fusion"


def _config(tmp_path: Path) -> dict:
    return {
        "schema_version": 1,
        "project": {"id": "t", "title": "T", "owner": "t"},
        "study": {"cancer_type": "pan_cancer", "design": "independent_two_group"},
        "server": {
            "profile": "mvp_local",
            "host": "localhost",
            "user": "t",
            "remote_base_dir": f"{tmp_path}/remote",
            "remote_workdir": f"{tmp_path}/remote/w",
            "scheduler": "local",
            "threads": 4,
            "memory_gb": 16,
            "shell": "bash",
            "init_commands": [],
        },
        "reference": {
            "name": "GENCODE_R47_GRCh38p14_ALL",
            "remote_gtf_path": f"{tmp_path}/ref.gtf",
            "remote_genome_fasta_path": f"{tmp_path}/ref.fa",
            "star_index_dir": f"{tmp_path}/star_idx",
            "rsem_index_prefix": f"{tmp_path}/rsem",
        },
        "sequencing": {"layout": "paired", "reads_per_sample_million": 20, "strandedness": "auto"},
        "samples": {
            "source": "local_upload",
            "local_data_dir": f"{tmp_path}/fastq",
            "remote_data_dir": "remote",
            "items": [
                {"sample_id": "a", "condition": "control", "fastq_1": "a_R1.fastq.gz", "fastq_2": "a_R2.fastq.gz"},
                {"sample_id": "b", "condition": "treatment", "fastq_1": "b_R1.fastq.gz", "fastq_2": "b_R2.fastq.gz"},
            ],
        },
        "pipeline": {
            "fastp": {"enabled": True, "version": "0.24.1"},
            "star": {"enabled": True, "version": "2.7.11b"},
            "arriba": {"enabled": True, "version": "2.5.0"},
            "featurecounts": {"enabled": True, "version": "Subread 2.1.1"},
            "rsem": {"enabled": True, "version": "1.2.28"},
        },
        "polling": {"interval_seconds": 300, "timeout_hours": 24},
        "notification": {"email_enabled": False},
    }


class TestAdapterInspect:
    def test_inspect_passes_on_valid_config(self, tmp_path: Path) -> None:
        cap = resolve_capability(CAP_ID)
        result = adapter_inspect(cap, _config(tmp_path))
        assert result.ok
        assert result.verdict == PASS

    def test_inspect_fails_on_missing_r2(self, tmp_path: Path) -> None:
        cap = resolve_capability(CAP_ID)
        config = _config(tmp_path)
        config["samples"]["items"][0]["fastq_2"] = ""
        result = adapter_inspect(cap, config)
        assert not result.ok
        assert result.verdict == NOT_EVALUABLE
        assert any("R2" in f for f in result.findings)

    def test_inspect_fails_on_missing_reference(self, tmp_path: Path) -> None:
        cap = resolve_capability(CAP_ID)
        config = _config(tmp_path)
        config["reference"]["star_index_dir"] = ""
        result = adapter_inspect(cap, config)
        assert not result.ok
        assert any("参考设置" in f for f in result.findings)

    def test_discovery_unknown_and_version_mismatch_are_structured(self) -> None:
        unknown = discover_adapter("missing")
        incompatible = discover_adapter(CAP_ID, version="9.0.0")
        assert unknown.status == NOT_EVALUABLE
        assert unknown.code == "UNKNOWN_CAPABILITY"
        assert incompatible.status == NOT_EVALUABLE
        assert incompatible.code == "INCOMPATIBLE_VERSION"


class TestAdapterPlan:
    def test_plan_with_failed_inspection_marks_not_evaluable(self, tmp_path: Path) -> None:
        cap = resolve_capability(CAP_ID)
        config = _config(tmp_path)
        config["reference"]["star_index_dir"] = ""
        inspection = adapter_inspect(cap, config)
        plan = adapter_plan(cap, config, inspection=inspection)
        assert plan.capability_id == CAP_ID
        assert "NOT_EVALUABLE" in "\n".join(plan.steps)
        assert plan.summary.startswith("Adapter 预检未通过")

    def test_plan_embeds_passed_checks(self, tmp_path: Path) -> None:
        cap = resolve_capability(CAP_ID)
        inspection = adapter_inspect(cap, _config(tmp_path))
        plan = adapter_plan(cap, _config(tmp_path), inspection=inspection)
        assert "全部通过" in "\n".join(plan.steps)


class TestAdapterMaterialize:
    def test_materialize_renders_scripts(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir(parents=True, exist_ok=True)
        config_path = project_dir / "project.json"
        config_path.write_text(json.dumps(_config(tmp_path)), encoding="utf-8")
        cap = resolve_capability(CAP_ID)
        paths = adapter_materialize(config_path, _config(tmp_path))
        assert {"env_setup.sh", "run_pipeline.sh", "submit.sh"} == set(paths)
        for path in paths.values():
            assert path.is_file()
            assert path.read_text(encoding="utf-8").strip()


class TestOutputValidator:
    def test_missing_artifact_raises_fail_output_contract(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ValidationFailure) as excinfo:
            validate_output_contract(
                {},
                attempts_dir=project_dir,
                required_artifacts={"featurecounts/gene_counts.txt": "count matrix"},
            )
        assert FAIL_OUTPUT_CONTRACT in str(excinfo.value)

    def test_present_artifact_validated(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        extracted = project_dir / "downloads" / "extracted" / "featurecounts"
        extracted.mkdir(parents=True, exist_ok=True)
        (extracted / "gene_counts.txt").write_text("gene\tcount\nA\t1\n", encoding="utf-8")
        report = validate_output_contract(
            {},
            attempts_dir=project_dir,
            required_artifacts={"featurecounts/gene_counts.txt": "count matrix"},
        )
        assert report["status"] == PASS
        assert "sha256" in report["artifacts"]["featurecounts/gene_counts.txt"]


class TestPostRunGate:
    def test_passes_with_clean_qc(self) -> None:
        result = post_run_gate({}, qc_report={"alignment_rate": 0.9, "duplicate_rate": 0.1})
        assert result["status"] == PASS

    def test_abstains_on_low_alignment(self) -> None:
        result = post_run_gate({}, qc_report={"alignment_rate": 0.3})
        assert result["status"] == ABSTAIN
        assert "qc_low_alignment_rate" in result["reason_codes"]


class TestArtifactRegistry:
    def test_register_and_stale_invalidation(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir(parents=True, exist_ok=True)
        register_artifacts(
            project_dir,
            attempt_id="run_1",
            artifacts={
                "counts": {
                    "path": "counts.tsv",
                    "depends_on": ["project_config"],
                    "sha256": "abc",
                }
            },
        )
        # Re-register from a different attempt -> old becomes STALE.
        register_artifacts(
            project_dir,
            attempt_id="run_2",
            artifacts={
                "counts": {
                    "path": "counts.tsv",
                    "depends_on": ["project_config"],
                    "sha256": "def",
                }
            },
        )
        registry_path = project_dir / "artifact_registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        # Latest registration wins with VALID status.
        assert registry["counts"]["status"] == "VALID"

        stale = mark_stale_downstream(project_dir, "project_config")
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        assert registry["counts"]["status"] == STALE
        assert "counts" in stale
