from __future__ import annotations

import json
from pathlib import Path
import time
import tomllib

import pytest

from rnaseq_agent.analysis_contract import verify_project_contract
from rnaseq_agent.bkbio_eval_adapter import (
    AdapterInputError,
    AdapterUnavailableError,
    EvalInputs,
    _ensure_runtime_available,
    _install_runtime_credential,
    _restore_runtime_credential,
    _validate_groups,
    read_eval_inputs,
    run_case,
)
from rnaseq_agent.execution import CommandResult
from rnaseq_agent.pipeline import render_stage_script
from rnaseq_agent.run_agent import RunOutcome
from rnaseq_agent.ssh_auth import clear_ssh_credential, get_ssh_credential
from rnaseq_agent.storage import load_json, save_json


COUNTS = """gene\tc2\tt2\tc1\tt1\tc3\tt3
G1\t11\t44\t10\t40\t9\t36
G2\t21\t6\t20\t5\t19\t4
G3\t100\t100\t100\t100\t100\t100
G4\t0\t0\t0\t0\t0\t0
"""

COLDATA = """sample\tcondition\tpatient
c1\tcontrol\tp1
c2\tcontrol\tp2
c3\tcontrol\tp3
t1\ttreated\tp1
t2\ttreated\tp2
t3\ttreated\tp3
"""

DESEQ2_RESULTS = """gene\tbaseMean\tlog2FoldChange\tlfcSE\tstat\tpvalue\tpadj
G1\t25\t2.0\t0.2\t10\t0.001\t0.01
G2\t12.5\t-1.5\t0.3\t-5\t0.004\t0.02
G3\t100\t0.5\t0.1\t5\t0.01\t0.03
G4\t0\tNA\tNA\tNA\tNA\tNA
"""


def test_package_exposes_the_adapter_as_a_console_command() -> None:
    """Removing the installable analyzer command must fail this contract test."""
    root = Path(__file__).resolve().parents[1]
    payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert payload["project"]["scripts"]["rnaseq-agent-bkbio-eval"] == (
        "rnaseq_agent.bkbio_eval_adapter:main"
    )


def test_runtime_credential_uses_template_key_path_and_restores_previous() -> None:
    host = "key-target.example.test"
    user = "key-user"
    config = {
        "server": {
            "host": host,
            "user": user,
            "auth_mode": "key",
            "key_path": "C:/keys/eval_ed25519",
        }
    }
    clear_ssh_credential(host, user)

    installed = _install_runtime_credential(config, {})
    try:
        credential = get_ssh_credential(host, user)
        assert credential.mode == "key"
        assert Path(credential.key_path) == Path("C:/keys/eval_ed25519")
    finally:
        _restore_runtime_credential(config, installed)

    restored = get_ssh_credential(host, user)
    assert restored.mode == "key"
    assert restored.key_path == ""


def test_real_counts_stage_loads_init_commands_and_uses_configured_container() -> None:
    """Discarding env setup or relying on an undeclared image variable must fail."""
    samples = [
        {"sample_id": f"c{i}", "condition": "control"} for i in range(1, 4)
    ] + [{"sample_id": f"t{i}", "condition": "treated"} for i in range(1, 4)]
    config = {
        "server": {"threads": 2, "init_commands": ["module load R/4.3"]},
        "reference": {},
        "sequencing": {"layout": "paired"},
        "samples": {"items": samples},
        "pipeline": {"diffexp": {"enabled": True}, "cms": {"enabled": False}},
        "diffexp": {"formula": "~ condition", "reference_condition": "control"},
        "container": {
            "enabled": True,
            "engine": "apptainer",
            "image_path": "/containers/rnaseq-downstream.sif",
            "bind_paths": [],
        },
    }

    script = render_stage_script(config, "counts")

    assert "source scripts/env_setup.sh" in script
    assert "requireNamespace('DESeq2', quietly=TRUE)" in script
    assert "requireNamespace('jsonlite', quietly=TRUE)" in script
    assert "RNASEQ_RSEM_IMAGE" not in script
    assert (
        "apptainer exec --cleanenv /containers/rnaseq-downstream.sif Rscript "
        "scripts/diffexp_counts_deseq2.R"
    ) in script
    assert 'echo "diffexp requested but Rscript is not available" >&2\n  exit 127' in script


def test_runtime_probe_applies_server_init_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[str] = []

    class ProbeTransport:
        def execute(self, command: str) -> CommandResult:
            commands.append(command)
            return CommandResult(["ssh"], 0, "native=1 container=0\n", "")

    monkeypatch.setattr(
        "rnaseq_agent.bkbio_eval_adapter.create_remote_transport",
        lambda config: ProbeTransport(),
    )
    _ensure_runtime_available(
        {
            "server": {"init_commands": ["module load R/4.5", "source /opt/conda.sh"]},
            "container": {"enabled": False},
        }
    )

    assert commands
    assert commands[0].index("module load R/4.5") < commands[0].index("command -v Rscript")
    assert commands[0].index("source /opt/conda.sh") < commands[0].index("command -v Rscript")


def _write_case(
    tmp_path: Path,
    *,
    coldata: str = COLDATA,
    design: str = "~ condition",
    paired: bool = False,
) -> tuple[Path, Path, Path]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "counts.tsv").write_text(COUNTS, encoding="utf-8")
    (inputs / "coldata.tsv").write_text(coldata, encoding="utf-8")
    params = tmp_path / "params.json"
    params.write_text(
        json.dumps(
            {
                "condition_column": "condition",
                "reference_level": "control",
                "contrast_level": "treated",
                "fdr_threshold": 0.05,
                "log2fc_threshold": 1.0,
                "design": design,
                "paired": paired,
                "min_count_prefilter": 0,
            }
        ),
        encoding="utf-8",
    )
    return inputs, params, tmp_path / "result.json"


def _connection(tmp_path: Path) -> dict[str, object]:
    return {
        "profile": "eval-test",
        "host": "hpc.example.test",
        "user": "tester",
        "port": 22,
        "scheduler": "local",
        "threads": 2,
        "memory_gb": 8,
        "remote_base_dir": "/remote/eval",
        "remote_workdir": "/remote/eval/default",
        "shell": "bash",
        "auth_mode": "password",
        "password": "secret-that-must-stay-in-memory",
    }


def test_run_case_uses_frozen_counts_session_and_maps_real_deseq2_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the ProjectSession path or remapping thresholds must fail this test."""
    inputs, params, out = _write_case(tmp_path)

    def fake_remote_counts_stage(config_path: Path, stage: str, *, wait: bool = True) -> RunOutcome:
        assert stage == "counts"
        assert wait is True
        assert (config_path.parent / "analysis_contract.json").is_file()
        config = load_json(config_path)
        assert config["execution"]["mode"] == "contract"
        time.sleep(1.05)
        assert verify_project_contract(config_path).ok
        assert "password" not in config["server"]
        assert config["samples"]["counts_path"].endswith("counts_matrix.tsv")
        assert config["diffexp"]["reference_condition"] == "control"
        assert config["diffexp"]["padj_cutoff"] == 0.05
        assert config["diffexp"]["lfc_cutoff"] == 1.0

        run_id = "test-run"
        attempt = config_path.parent / "attempts" / run_id
        diffexp = attempt / "downloads" / "extracted" / "diffexp"
        diffexp.mkdir(parents=True)
        (diffexp / "deseq2_results.tsv").write_text(DESEQ2_RESULTS, encoding="utf-8")
        (diffexp / "deseq2_summary.json").write_text(
            json.dumps(
                {
                    "contrast": "treated_vs_control",
                    "formula": "~ condition",
                    "reference_condition": "control",
                    "treatment_condition": "treated",
                    "tested_genes": 4,
                    "significant_padj_0.05_lfc1": 2,
                }
            ),
            encoding="utf-8",
        )
        save_json(
            attempt / "run_manifest.json",
            {"manifest_id": "sha256:run", "body": {"fingerprints": {"inputs_sha256": "inputs"}}},
        )
        save_json(
            attempt / "result_manifest.json",
            {"manifest_id": "sha256:result", "body": {"fingerprints": {"files_sha256": "files"}}},
        )
        config["status"] = {"state": "stage_completed", "run_id": run_id, "stage": "counts"}
        save_json(config_path, config)
        return RunOutcome("stage_completed", "Stage counts completed.", config_path.parent)

    monkeypatch.setattr("rnaseq_agent.run_agent.run_stage_project", fake_remote_counts_stage)
    monkeypatch.setattr("rnaseq_agent.bkbio_eval_adapter._ensure_runtime_available", lambda config: None)
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps({"server": {"password": "template-password-must-not-persist"}}),
        encoding="utf-8",
    )

    result = run_case(
        inputs,
        params,
        out,
        case_id="L0_adapter_contract",
        connection=_connection(tmp_path),
        project_template=template,
    )

    assert out.is_file()
    assert result["schema"] == "bkbio-eval/analyzer-result@1"
    assert result["analyzer"] == "sysu-rnaseq-agent"
    assert result["metrics"] == {
        "n_genes": 4,
        "n_samples": 6,
        "n_significant": 2,
        "n_up": 1,
        "n_down": 1,
        "library_size_min": 128.0,
        "library_size_max": 150.0,
        "library_size_ratio": pytest.approx(145.0 / 130.0),
    }
    assert result["de_results"] == {
        "significant_genes": ["G1", "G2"],
        "up_genes": ["G1"],
        "down_genes": ["G2"],
        "top_gene_by_padj": "G1",
    }
    assert result["tables"]["deseq2_results"]["G1"] == {
        "baseMean": 25.0,
        "log2FoldChange": 2.0,
        "lfcSE": 0.2,
        "stat": 10.0,
        "pvalue": 0.001,
        "padj": 0.01,
    }
    assert result["tables"]["deseq2_results"]["G4"]["padj"] is None
    artifact = out.parent / result["artifacts"]["deseq2_results"]
    assert artifact.read_text(encoding="utf-8") == DESEQ2_RESULTS
    assert result["provenance"]["contract_id"].startswith("sha256:")
    assert result["provenance"]["run_manifest_id"] == "sha256:run"
    assert result["provenance"]["result_manifest_id"] == "sha256:result"
    assert result["provenance"]["requested_design"] == "~ condition"
    assert result["provenance"]["executed_design"] == "~ condition"
    assert result["provenance"]["limitations"] == []

    project_dir = Path(result["provenance"]["project_dir"])
    assert load_json(project_dir / "session.json")["state"] == "stage_completed"
    assert load_json(project_dir / "project.json")["study"]["design"] == "independent_two_group"


def test_read_eval_inputs_rejects_coldata_that_does_not_cover_every_count_column(
    tmp_path: Path,
) -> None:
    """Dropping exact sample-name alignment must fail before any remote execution."""
    bad_coldata = COLDATA.replace("t3\ttreated\tp3\n", "")
    inputs, _, _ = _write_case(tmp_path, coldata=bad_coldata)

    with pytest.raises(AdapterInputError, match="coldata.tsv.*counts.tsv"):
        read_eval_inputs(inputs, condition_column="condition")


@pytest.mark.parametrize(
    ("design", "paired"),
    [
        ("~ patient + condition", True),
        ("~ cell + condition", False),
    ],
)
def test_run_case_rejects_designs_outside_the_real_condition_only_pipeline(
    tmp_path: Path,
    design: str,
    paired: bool,
) -> None:
    """Silently downgrading paired/multifactor L1 inputs to ~condition must fail."""
    inputs, params, out = _write_case(tmp_path, design=design, paired=paired)

    with pytest.raises(AdapterInputError, match="NOT_EVALUABLE.*~ condition"):
        run_case(
            inputs,
            params,
            out,
            case_id="L1_unsupported_design",
            connection=_connection(tmp_path),
        )

    assert not out.exists()


def test_run_case_reports_missing_remote_deseq2_as_unavailable_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the read-only runtime check would submit a doomed scientific job."""
    inputs, params, out = _write_case(tmp_path)

    def unavailable(config: dict) -> None:
        raise AdapterUnavailableError("remote DESeq2/jsonlite runtime is unavailable")

    monkeypatch.setattr("rnaseq_agent.bkbio_eval_adapter._ensure_runtime_available", unavailable)

    with pytest.raises(AdapterUnavailableError, match="DESeq2/jsonlite"):
        run_case(
            inputs,
            params,
            out,
            case_id="L0_missing_runtime",
            connection=_connection(tmp_path),
        )

    assert not out.exists()
    project_root = out.parent / ".rnaseq-agent-eval"
    project_dirs = list(project_root.iterdir())
    assert len(project_dirs) == 1
    assert not (project_dirs[0] / "session.json").exists()


@pytest.mark.parametrize("min_count_prefilter", [0.5, -0.5, 1e-9])
def test_run_case_rejects_nonzero_fractional_prefilter(
    tmp_path: Path,
    min_count_prefilter: float,
) -> None:
    inputs, params, out = _write_case(tmp_path)
    payload = json.loads(params.read_text(encoding="utf-8"))
    payload["min_count_prefilter"] = min_count_prefilter
    params.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AdapterInputError, match="min_count_prefilter"):
        run_case(inputs, params, out, connection=_connection(tmp_path))


def test_group_validation_requires_three_replicates_per_condition(tmp_path: Path) -> None:
    counts = tmp_path / "counts.tsv"
    counts.write_text("gene\tc1\tc2\tt1\tt2\tt3\nG1\t1\t2\t3\t4\t5\n", encoding="utf-8")
    inputs = EvalInputs(
        counts_path=counts,
        sample_ids=("c1", "c2", "t1", "t2", "t3"),
        gene_ids=("G1",),
        library_sizes={"c1": 1, "c2": 2, "t1": 3, "t2": 4, "t3": 5},
        samples=(
            {"sample_id": "c1", "condition": "control"},
            {"sample_id": "c2", "condition": "control"},
            {"sample_id": "t1", "condition": "treated"},
            {"sample_id": "t2", "condition": "treated"},
            {"sample_id": "t3", "condition": "treated"},
        ),
    )

    with pytest.raises(AdapterInputError, match="at least 3"):
        _validate_groups(
            inputs,
            {"reference_level": "control", "contrast_level": "treated"},
            "condition",
        )
