from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import tomllib

import pytest

from rnaseq_agent.analysis_contract import canonical_sha256, sha256_file, verify_project_contract
from rnaseq_agent.bkbio_eval_adapter import (
    AdapterExecutionError,
    AdapterInputError,
    AdapterNotEvaluableError,
    AdapterUnavailableError,
    EvalInputs,
    _build_project_config,
    _ensure_runtime_available,
    _install_runtime_credential,
    _restore_runtime_credential,
    _verify_manifests,
    _verify_summary,
    _validate_groups,
    main,
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
    assert "command -v Rscript" not in script
    assert 'echo "diffexp requested but Rscript is not available" >&2\n  exit 127' in script


def test_release_stage_rechecks_container_digest_immediately_before_rscript() -> None:
    digest = "sha256:" + "a" * 64
    samples = [
        {"sample_id": f"c{i}", "condition": "control"} for i in range(1, 4)
    ] + [{"sample_id": f"t{i}", "condition": "treated"} for i in range(1, 4)]
    config = {
        "server": {"threads": 2, "init_commands": []},
        "reference": {},
        "sequencing": {"layout": "paired"},
        "samples": {"items": samples},
        "pipeline": {"diffexp": {"enabled": True}, "cms": {"enabled": False}},
        "diffexp": {"formula": "~ condition", "reference_condition": "control"},
        "container": {
            "enabled": True,
            "engine": "apptainer",
            "image_path": "/containers/release.sif",
            "bind_paths": [],
            "digest": digest,
        },
        "evaluation": {"release_mode": True},
    }

    script = render_stage_script(config, "counts")

    assert "sha256sum /containers/release.sif" in script
    assert "a" * 64 in script
    assert "container digest mismatch before diffexp" in script
    assert script.index("sha256sum /containers/release.sif") < script.index(
        "apptainer exec --cleanenv /containers/release.sif Rscript"
    )


def test_runtime_probe_applies_server_init_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[str] = []

    class ProbeTransport:
        def execute(self, command: str) -> CommandResult:
            commands.append(command)
            return CommandResult(["ssh"], 0, "mode=native|R=4.5.2|DESeq2=1.46.0|jsonlite=2.0.0\n", "")

    monkeypatch.setattr(
        "rnaseq_agent.bkbio_eval_adapter.create_remote_transport",
        lambda config: ProbeTransport(),
    )
    runtime = _ensure_runtime_available(
        {
            "server": {"init_commands": ["module load R/4.5", "source /opt/conda.sh"]},
            "container": {"enabled": False},
        }
    )

    assert commands
    assert commands[0].index("module load R/4.5") < commands[0].index("command -v Rscript")
    assert commands[0].index("source /opt/conda.sh") < commands[0].index("command -v Rscript")
    assert runtime == {
        "mode": "native",
        "r_version": "4.5.2",
        "deseq2_version": "1.46.0",
        "jsonlite_version": "2.0.0",
    }


def test_container_runtime_never_falls_back_to_host_r(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[str] = []

    class ProbeTransport:
        def execute(self, command: str) -> CommandResult:
            commands.append(command)
            return CommandResult(["ssh"], 0, "mode=container|R=4.5.2|DESeq2=1.46.0|jsonlite=2.0.0\n", "")

    monkeypatch.setattr("rnaseq_agent.bkbio_eval_adapter.create_remote_transport", lambda config: ProbeTransport())
    runtime = _ensure_runtime_available(
        {"server": {"init_commands": []}, "container": {"enabled": True, "engine": "apptainer", "image_path": "/x.sif"}}
    )

    assert runtime["mode"] == "container"
    assert "native=1" not in commands[0]


@pytest.mark.parametrize("digest", [None, "", "latest", "sha256:1234"])
def test_release_mode_rejects_missing_or_invalid_container_digest(digest: str | None) -> None:
    with pytest.raises(AdapterUnavailableError, match="digest"):
        _ensure_runtime_available(
            {
                "server": {"init_commands": []},
                "container": {
                    "enabled": True,
                    "engine": "apptainer",
                    "image_path": "/x.sif",
                    "digest": digest,
                },
                "evaluation": {"release_mode": True},
            }
        )


def test_release_mode_rejects_unverifiable_container_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProbeTransport:
        def execute(self, command: str) -> CommandResult:
            return CommandResult(["ssh"], 0, "0" * 64 + "  /x.sif\n", "")

    monkeypatch.setattr(
        "rnaseq_agent.bkbio_eval_adapter.create_remote_transport",
        lambda config: ProbeTransport(),
    )
    with pytest.raises(AdapterUnavailableError, match="digest"):
        _ensure_runtime_available(
            {
                "server": {"init_commands": []},
                "container": {
                    "enabled": True,
                    "engine": "apptainer",
                    "image_path": "/x.sif",
                    "digest": "sha256:" + "a" * 64,
                },
                "evaluation": {"release_mode": True},
            }
        )


def test_same_remote_credential_override_is_serialized() -> None:
    host = "serialized-target.example.test"
    user = "serialized-user"
    config = {"server": {"host": host, "user": user, "auth_mode": "password"}}
    first_connection = {"auth_mode": "password", "password": "first"}
    second_connection = {"auth_mode": "password", "password": "second"}
    clear_ssh_credential(host, user)
    first = _install_runtime_credential(config, first_connection)
    started = threading.Event()
    acquired = threading.Event()
    second: list[object] = []

    def install_second() -> None:
        started.set()
        second.append(_install_runtime_credential(config, second_connection))
        acquired.set()

    worker = threading.Thread(target=install_second, daemon=True)
    worker.start()
    assert started.wait(1)
    assert not acquired.wait(0.1)
    _restore_runtime_credential(config, first)
    assert acquired.wait(1)
    try:
        assert get_ssh_credential(host, user).password == "second"
    finally:
        _restore_runtime_credential(config, second[0])
        worker.join(timeout=1)


def test_project_template_server_uses_a_non_secret_allowlist(tmp_path: Path) -> None:
    staged = tmp_path / "counts.tsv"
    staged.write_text(COUNTS, encoding="utf-8")
    inputs = read_eval_inputs(_case_inputs(tmp_path), condition_column="condition")
    config = _build_project_config(
        tmp_path,
        "eval_case",
        staged,
        inputs,
        {"fdr_threshold": 0.05, "log2fc_threshold": 1.0},
        reference="control",
        connection=_connection(tmp_path),
        template={
            "server": {
                "init_commands": ["module load R"],
                "token": "secret",
                "api_token": "secret",
                "password": "secret",
                "passphrase": "secret",
                "private_key": "secret",
            }
        },
    )
    assert config["server"]["init_commands"] == ["module load R"]
    assert not ({"token", "api_token", "password", "passphrase", "private_key"} & set(config["server"]))


def test_paired_preflight_canonicalizes_pair_column_and_refuses_before_project_or_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = tmp_path / "paired-inputs"
    inputs.mkdir()
    (inputs / "counts.tsv").write_text(
        "gene\tp1_u\tp1_t\tp2_u\tp2_t\tp3_u\tp3_t\nG1\t1\t2\t3\t4\t5\t6\n",
        encoding="utf-8",
    )
    (inputs / "coldata.tsv").write_text(
        "sample\tcondition\tcell\n"
        "p1_u\tuntrt\tcell-b\n"
        "p1_t\ttrt\tcell-b\n"
        "p2_u\tuntrt\tcell-a\n"
        "p2_t\ttrt\tcell-a\n"
        "p3_u\tuntrt\tcell-c\n"
        "p3_t\ttrt\tcell-c\n",
        encoding="utf-8",
    )
    parsed = read_eval_inputs(inputs, condition_column="condition", pair_column="cell")
    assert [sample["pair_id"] for sample in parsed.samples] == ["cell-b", "cell-b", "cell-a", "cell-a", "cell-c", "cell-c"]

    def fail_transport(*args, **kwargs):
        raise AssertionError("transport must not be reached by paired preflight")

    monkeypatch.setattr("rnaseq_agent.bkbio_eval_adapter.create_remote_transport", fail_transport)
    params = tmp_path / "params.json"
    params.write_text(
        json.dumps(
            {
                "condition_column": "condition",
                "pair_column": "cell",
                "design_template": "paired_two_group",
                "design": "~ pair_id + condition",
                "paired": True,
                "reference_level": "untrt",
                "contrast_level": "trt",
                "min_count_prefilter": 0,
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "result.json"
    # Break one pair after parsing: this must refuse before project creation.
    (inputs / "coldata.tsv").write_text(
        (inputs / "coldata.tsv").read_text(encoding="utf-8").replace("p3_t\ttrt\tcell-c", "p3_t\ttrt\tcell-b"),
        encoding="utf-8",
    )
    with pytest.raises(AdapterNotEvaluableError, match="pair"):
        run_case(inputs, params, out, case_id="paired-preflight", connection={})
    assert not (out.parent / ".rnaseq-agent-eval").exists()


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


def _case_inputs(tmp_path: Path) -> Path:
    inputs = tmp_path / "case-inputs"
    inputs.mkdir()
    (inputs / "counts.tsv").write_text(COUNTS, encoding="utf-8")
    (inputs / "coldata.tsv").write_text(COLDATA, encoding="utf-8")
    return inputs


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
    secret_sentinel = "EVAL_TEMPLATE_SECRET_SENTINEL_8f65c8"

    def fake_remote_counts_stage(config_path: Path, stage: str, *, wait: bool = True) -> RunOutcome:
        assert stage == "counts"
        assert wait is True
        assert (config_path.parent / "analysis_contract.json").is_file()
        config = load_json(config_path)
        assert config["execution"]["mode"] == "contract"
        time.sleep(1.05)
        assert verify_project_contract(config_path).ok
        assert "password" not in config["server"]
        assert "token" not in config["server"]
        assert "passphrase" not in config["server"]
        assert "private_key" not in config["server"]
        assert config["samples"]["counts_path"].endswith("counts_matrix.tsv")
        assert config["diffexp"]["reference_condition"] == "control"
        assert config["diffexp"]["padj_cutoff"] == 0.05
        assert config["diffexp"]["lfc_cutoff"] == 1.0
        for audit_name in ("project.json", "session.json", "analysis_contract.json"):
            assert secret_sentinel not in (config_path.parent / audit_name).read_text(
                encoding="utf-8"
            )

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
                    "padj_cutoff": 0.05,
                    "log2fc_cutoff": 1.0,
                    "tested_genes": 4,
                    "significant_padj_0.05_lfc1": 2,
                }
            ),
            encoding="utf-8",
        )
        snapshot = attempt / "project.snapshot.json"
        save_json(snapshot, {"run": {"id": run_id}})
        contract_payload = load_json(config_path.parent / "analysis_contract.json")
        run_inputs = contract_payload["body"]["inputs"]
        run_body = {
            "run_id": run_id,
            "fingerprints": {
                "project_snapshot_sha256": sha256_file(snapshot),
                "inputs_sha256": canonical_sha256(run_inputs),
            },
            "inputs": run_inputs,
        }
        save_json(attempt / "run_manifest.json", {"schema_version": 1, "manifest_id": f"sha256:{canonical_sha256(run_body)}", "body": run_body})
        files = [{"path": "diffexp/deseq2_results.tsv", "sha256": sha256_file(diffexp / "deseq2_results.tsv")}]
        result_body = {"run_id": run_id, "fingerprints": {"files_sha256": canonical_sha256(files)}, "files": files}
        save_json(attempt / "result_manifest.json", {"schema_version": 1, "manifest_id": f"sha256:{canonical_sha256(result_body)}", "body": result_body})
        (inputs / "counts.tsv").write_text("mutated after staging\n", encoding="utf-8")
        config["status"] = {"state": "stage_completed", "run_id": run_id, "stage": "counts"}
        save_json(config_path, config)
        return RunOutcome("stage_completed", "Stage counts completed.", config_path.parent)

    monkeypatch.setattr("rnaseq_agent.run_agent.run_stage_project", fake_remote_counts_stage)
    monkeypatch.setattr(
        "rnaseq_agent.bkbio_eval_adapter._ensure_runtime_available",
        lambda config: {
            "mode": "native",
            "r_version": "4.5.2",
            "deseq2_version": "1.46.0",
            "jsonlite_version": "2.0.0",
        },
    )
    template = tmp_path / "template.json"
    template.write_text(
        json.dumps(
            {
                "server": {
                    "password": secret_sentinel,
                    "token": secret_sentinel,
                    "passphrase": secret_sentinel,
                    "private_key": secret_sentinel,
                },
                "container": {
                    "enabled": False,
                    "engine": "apptainer",
                    "image_path": "/allowed/image.sif",
                    "image_uri": "docker://allowed/image:tag",
                    "bind_paths": [],
                    "registry_token": secret_sentinel,
                    "api_key": secret_sentinel,
                    "passphrase": secret_sentinel,
                    "private_key": secret_sentinel,
                },
                "evaluation": {
                    "release_mode": False,
                    "registry_token": secret_sentinel,
                    "api_key": secret_sentinel,
                    "passphrase": secret_sentinel,
                    "private_key": secret_sentinel,
                },
            }
        ),
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
    assert result["provenance"]["run_manifest_id"].startswith("sha256:")
    assert result["provenance"]["result_manifest_id"].startswith("sha256:")
    assert result["provenance"]["requested_design"] == "~ condition"
    assert result["provenance"]["executed_design"] == "~ condition"
    assert result["provenance"]["limitations"] == ["smoke/non_release"]
    assert result["provenance"]["runtime"]["mode"] == "native"
    project_dir = Path(result["provenance"]["project_dir"])
    assert result["provenance"]["input_counts_sha256"] == sha256_file(
        project_dir / "uploads" / "counts_matrix.tsv"
    )

    assert load_json(project_dir / "session.json")["state"] == "stage_completed"
    assert load_json(project_dir / "project.json")["study"]["design"] == "independent_two_group"
    assert secret_sentinel not in out.read_text(encoding="utf-8")
    for audit_file in project_dir.rglob("*.json"):
        assert secret_sentinel not in audit_file.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("formula", "~ batch + condition"),
        ("reference_condition", "treated"),
        ("treatment_condition", "control"),
        ("contrast", "control_vs_treated"),
        ("padj_cutoff", 0.1),
        ("log2fc_cutoff", 2.0),
    ],
)
def test_summary_must_exactly_match_frozen_analysis_contract(
    field: str,
    bad_value: object,
) -> None:
    summary = {
        "formula": "~ condition",
        "reference_condition": "control",
        "treatment_condition": "treated",
        "contrast": "treated_vs_control",
        "padj_cutoff": 0.05,
        "log2fc_cutoff": 1.0,
    }
    summary[field] = bad_value
    with pytest.raises(AdapterExecutionError, match=field):
        _verify_summary(
            summary,
            {"diffexp": {"formula": "~ condition", "reference_condition": "control", "padj_cutoff": 0.05, "lfc_cutoff": 1.0}},
            reference="control",
            contrast="treated",
        )


def test_summary_rejects_non_condition_formula_even_if_config_matches() -> None:
    formula = "~ batch + condition"
    with pytest.raises(AdapterExecutionError, match="formula"):
        _verify_summary(
            {
                "formula": formula,
                "reference_condition": "control",
                "treatment_condition": "treated",
                "contrast": "treated_vs_control",
                "padj_cutoff": 0.05,
                "log2fc_cutoff": 1.0,
            },
            {
                "diffexp": {
                    "formula": formula,
                    "reference_condition": "control",
                    "padj_cutoff": 0.05,
                    "lfc_cutoff": 1.0,
                }
            },
            reference="control",
            contrast="treated",
        )


def _valid_manifest_fixture(
    root: Path,
    *,
    run_id: str = "run-1",
) -> tuple[Path, Path, Path, dict[str, object]]:
    attempt = root / "attempt"
    staged_counts = root / "uploads" / "counts_matrix.tsv"
    staged_counts.parent.mkdir(parents=True)
    staged_counts.write_text(COUNTS, encoding="utf-8")
    de_path = attempt / "downloads" / "extracted" / "diffexp" / "deseq2_results.tsv"
    de_path.parent.mkdir(parents=True)
    de_path.write_text(DESEQ2_RESULTS, encoding="utf-8")
    snapshot = attempt / "project.snapshot.json"
    save_json(snapshot, {"run": {"id": run_id}})
    inputs = [
        {
            "sample_id": "*",
            "role": "counts",
            "logical_name": staged_counts.name,
            "size_bytes": staged_counts.stat().st_size,
            "sha256": sha256_file(staged_counts),
        }
    ]
    contract_body: dict[str, object] = {
        "inputs": inputs,
        "fingerprints": {"inputs_sha256": canonical_sha256(inputs)},
    }
    contract: dict[str, object] = {
        "schema_version": 1,
        "contract_id": f"sha256:{canonical_sha256(contract_body)}",
        "body": contract_body,
    }
    run_body = {
        "run_id": run_id,
        "fingerprints": {
            "project_snapshot_sha256": sha256_file(snapshot),
            "inputs_sha256": canonical_sha256(inputs),
        },
        "inputs": inputs,
    }
    save_json(
        attempt / "run_manifest.json",
        {
            "schema_version": 1,
            "manifest_id": f"sha256:{canonical_sha256(run_body)}",
            "body": run_body,
        },
    )
    files = [{"path": "diffexp/deseq2_results.tsv", "sha256": sha256_file(de_path)}]
    result_body = {
        "run_id": run_id,
        "fingerprints": {"files_sha256": canonical_sha256(files)},
        "files": files,
    }
    save_json(
        attempt / "result_manifest.json",
        {
            "schema_version": 1,
            "manifest_id": f"sha256:{canonical_sha256(result_body)}",
            "body": result_body,
        },
    )
    return attempt, de_path, staged_counts, contract


@pytest.mark.parametrize("filename", ["run_manifest.json", "result_manifest.json"])
def test_missing_manifests_fail_closed(tmp_path: Path, filename: str) -> None:
    attempt, de_path, staged_counts, contract = _valid_manifest_fixture(tmp_path)
    (attempt / filename).unlink()
    with pytest.raises(AdapterExecutionError, match=filename):
        _verify_manifests(
            attempt,
            "run-1",
            de_path,
            staged_counts=staged_counts,
            contract=contract,
        )


@pytest.mark.parametrize("filename", ["run_manifest.json", "result_manifest.json"])
def test_corrupt_manifest_fails_closed(tmp_path: Path, filename: str) -> None:
    attempt, de_path, staged_counts, contract = _valid_manifest_fixture(tmp_path)
    (attempt / filename).write_text("{broken", encoding="utf-8")
    with pytest.raises(AdapterExecutionError, match=filename):
        _verify_manifests(
            attempt,
            "run-1",
            de_path,
            staged_counts=staged_counts,
            contract=contract,
        )


@pytest.mark.parametrize(
    "forgery",
    [
        "run_schema",
        "run_manifest_id",
        "run_id",
        "snapshot_hash",
        "result_schema",
        "result_manifest_id",
        "files_hash",
        "result_file_hash",
    ],
)
def test_forged_manifests_fail_closed(tmp_path: Path, forgery: str) -> None:
    attempt, de_path, staged_counts, contract = _valid_manifest_fixture(tmp_path)
    run_path = attempt / "run_manifest.json"
    result_path = attempt / "result_manifest.json"
    path = run_path if forgery.startswith("run_") or forgery == "snapshot_hash" else result_path
    payload = load_json(path)
    if forgery.endswith("schema"):
        payload["schema_version"] = 999
    elif forgery.endswith("manifest_id"):
        payload["manifest_id"] = "sha256:" + "0" * 64
    elif forgery == "run_id":
        payload["body"]["run_id"] = "other-run"
        payload["manifest_id"] = f"sha256:{canonical_sha256(payload['body'])}"
    elif forgery == "snapshot_hash":
        payload["body"]["fingerprints"]["project_snapshot_sha256"] = "0" * 64
        payload["manifest_id"] = f"sha256:{canonical_sha256(payload['body'])}"
    elif forgery == "files_hash":
        payload["body"]["fingerprints"]["files_sha256"] = "0" * 64
        payload["manifest_id"] = f"sha256:{canonical_sha256(payload['body'])}"
    elif forgery == "result_file_hash":
        payload["body"]["files"][0]["sha256"] = "0" * 64
        payload["body"]["fingerprints"]["files_sha256"] = canonical_sha256(payload["body"]["files"])
        payload["manifest_id"] = f"sha256:{canonical_sha256(payload['body'])}"
    save_json(path, payload)
    with pytest.raises(AdapterExecutionError):
        _verify_manifests(
            attempt,
            "run-1",
            de_path,
            staged_counts=staged_counts,
            contract=contract,
        )


def test_staged_counts_mutation_after_manifest_creation_fails_closed(tmp_path: Path) -> None:
    attempt, de_path, staged_counts, contract = _valid_manifest_fixture(tmp_path)
    old_hash = sha256_file(staged_counts)
    staged_counts.write_text(COUNTS + "G5\t1\t1\t1\t1\t1\t1\n", encoding="utf-8")
    assert sha256_file(staged_counts) != old_hash

    with pytest.raises(AdapterExecutionError, match="staged counts"):
        _verify_manifests(
            attempt,
            "run-1",
            de_path,
            staged_counts=staged_counts,
            contract=contract,
        )


@pytest.mark.parametrize("source", ["run_inputs_sha256", "contract_inputs_sha256", "contract_counts"])
def test_input_evidence_hashes_must_agree(tmp_path: Path, source: str) -> None:
    attempt, de_path, staged_counts, contract = _valid_manifest_fixture(tmp_path)
    if source == "run_inputs_sha256":
        path = attempt / "run_manifest.json"
        payload = load_json(path)
        payload["body"]["fingerprints"]["inputs_sha256"] = "0" * 64
        payload["manifest_id"] = f"sha256:{canonical_sha256(payload['body'])}"
        save_json(path, payload)
    else:
        body = contract["body"]
        assert isinstance(body, dict)
        if source == "contract_inputs_sha256":
            body["fingerprints"]["inputs_sha256"] = "0" * 64
        else:
            body["inputs"][0]["sha256"] = "0" * 64
            body["fingerprints"]["inputs_sha256"] = canonical_sha256(body["inputs"])
        contract["contract_id"] = f"sha256:{canonical_sha256(body)}"

    with pytest.raises(AdapterExecutionError, match="inputs|counts"):
        _verify_manifests(
            attempt,
            "run-1",
            de_path,
            staged_counts=staged_counts,
            contract=contract,
        )


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

    with pytest.raises(AdapterNotEvaluableError, match="~ condition"):
        run_case(
            inputs,
            params,
            out,
            case_id="L1_unsupported_design",
            connection=_connection(tmp_path),
        )

    assert not out.exists()


def test_cli_writes_strict_not_evaluable_result_for_unsupported_design(tmp_path: Path) -> None:
    inputs, params, out = _write_case(tmp_path, design="~ patient + condition", paired=True)
    exit_code = main(["--inputs", str(inputs), "--params", str(params), "--out", str(out), "--case-id", "tcga"])
    assert exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload) == {"schema", "analyzer", "status", "reason_code", "message"}
    assert payload["status"] == "NOT_EVALUABLE"
    assert payload["reason_code"] == "unsupported_design"
    assert not (tmp_path / ".rnaseq-agent-eval").exists()


def test_cli_writes_not_evaluable_for_unsupported_prefilter(tmp_path: Path) -> None:
    inputs, params, out = _write_case(tmp_path)
    payload = json.loads(params.read_text(encoding="utf-8"))
    payload["min_count_prefilter"] = 10
    params.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = main(["--inputs", str(inputs), "--params", str(params), "--out", str(out)])

    assert exit_code == 0
    result = json.loads(out.read_text(encoding="utf-8"))
    assert set(result) == {"schema", "analyzer", "status", "reason_code", "message"}
    assert result["status"] == "NOT_EVALUABLE"
    assert result["reason_code"] == "unsupported_design"
    assert not (tmp_path / ".rnaseq-agent-eval").exists()


def test_cli_keeps_malformed_input_nonzero(tmp_path: Path) -> None:
    params = tmp_path / "params.json"
    params.write_text("{}", encoding="utf-8")
    out = tmp_path / "result.json"
    assert main(["--inputs", str(tmp_path / "missing"), "--params", str(params), "--out", str(out)]) != 0
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


@pytest.mark.parametrize("min_count_prefilter", [10, 0.5, -0.5, 1e-9])
def test_run_case_returns_not_evaluable_for_nonzero_prefilter(
    tmp_path: Path,
    min_count_prefilter: float,
) -> None:
    inputs, params, out = _write_case(tmp_path)
    payload = json.loads(params.read_text(encoding="utf-8"))
    payload["min_count_prefilter"] = min_count_prefilter
    params.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AdapterNotEvaluableError, match="min_count_prefilter") as exc_info:
        run_case(inputs, params, out, connection=_connection(tmp_path))
    assert exc_info.value.reason_code == "unsupported_design"


@pytest.mark.parametrize("min_count_prefilter", ["ten", [], {}, True])
def test_run_case_keeps_malformed_prefilter_as_input_error(
    tmp_path: Path,
    min_count_prefilter: object,
) -> None:
    inputs, params, out = _write_case(tmp_path)
    payload = json.loads(params.read_text(encoding="utf-8"))
    payload["min_count_prefilter"] = min_count_prefilter
    params.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AdapterInputError, match="min_count_prefilter"):
        run_case(inputs, params, out, connection=_connection(tmp_path))


def test_cli_keeps_malformed_prefilter_nonzero(tmp_path: Path) -> None:
    inputs, params, out = _write_case(tmp_path)
    payload = json.loads(params.read_text(encoding="utf-8"))
    payload["min_count_prefilter"] = "ten"
    params.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["--inputs", str(inputs), "--params", str(params), "--out", str(out)]) != 0
    assert not out.exists()


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
