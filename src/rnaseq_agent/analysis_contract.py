from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .configuration import normalize_config
from .pipeline import render_env_setup_script, render_remote_pipeline_script, render_submit_script
from .storage import load_json, save_json
from .validation import validate_local_fastqs
from .workflow_profiles import workflow_profile_errors


CONTRACT_SCHEMA_VERSION = 1
PIPELINE_RENDERER_ID = "rnaseq_agent.shell_pipeline.v1"
SUPPORTED_EXECUTION_MODES = {"free", "skill", "contract"}


@dataclass(frozen=True)
class ContractVerification:
    ok: bool
    contract_id: str
    current_contract_id: str
    errors: list[str]


class ContractError(RuntimeError):
    pass


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_input_artifacts(config: dict[str, Any]) -> list[dict[str, Any]]:
    return _input_artifacts(normalize_config(config))


def build_workflow_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    return _workflow_snapshot(normalize_config(config))


def build_analysis_contract(
    config: dict[str, Any],
    *,
    created_at: str | None = None,
    approval_method: str = "explicit_user_action",
) -> dict[str, Any]:
    normalized = normalize_config(config)
    body = _build_contract_body(normalized)
    contract_id = f"sha256:{canonical_sha256(body)}"
    timestamp = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_id": contract_id,
        "created_at": timestamp,
        "approval": {
            "status": "approved",
            "method": approval_method,
            "approved_at": timestamp,
        },
        "body": body,
    }


def verify_analysis_contract(
    config: dict[str, Any],
    contract: dict[str, Any],
) -> ContractVerification:
    errors: list[str] = []
    stored_body = contract.get("body")
    if not isinstance(stored_body, dict):
        return ContractVerification(False, str(contract.get("contract_id", "")), "", ["Contract body is missing or invalid."])

    contract_id = str(contract.get("contract_id", ""))
    expected_stored_id = f"sha256:{canonical_sha256(stored_body)}"
    if contract_id != expected_stored_id:
        errors.append("Stored contract ID does not match the contract body; the contract may have been modified.")

    if contract.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        errors.append(
            f"Unsupported contract schema version: {contract.get('schema_version')}; "
            f"expected {CONTRACT_SCHEMA_VERSION}."
        )

    approval = contract.get("approval", {})
    if approval.get("status") != "approved":
        errors.append("Contract is not approved.")

    try:
        current_body = _build_contract_body(normalize_config(config))
    except (ContractError, OSError, ValueError) as exc:
        errors.append(f"Could not rebuild the current analysis fingerprint: {exc}")
        return ContractVerification(
            ok=False,
            contract_id=contract_id,
            current_contract_id="",
            errors=errors,
        )
    current_contract_id = f"sha256:{canonical_sha256(current_body)}"

    for section, label in (
        ("workflow", "Workflow configuration"),
        ("inputs", "Input artifacts"),
        ("scripts", "Rendered scripts"),
    ):
        if stored_body.get(section) != current_body.get(section):
            errors.append(f"{label} differs from the approved contract.")

    if contract_id != current_contract_id:
        errors.append("Current analysis fingerprint does not match the approved contract ID.")

    return ContractVerification(
        ok=not errors,
        contract_id=contract_id,
        current_contract_id=current_contract_id,
        errors=errors,
    )


def create_project_contract(
    config_path: Path,
    *,
    output_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    config = normalize_config(load_json(config_path))
    mode = str(config.get("execution", {}).get("mode", "free"))
    if mode not in SUPPORTED_EXECUTION_MODES:
        raise ContractError(f"Unsupported execution mode: {mode}")

    profile_errors = workflow_profile_errors(config)
    if profile_errors:
        raise ContractError("Workflow profile validation failed: " + "; ".join(profile_errors))

    validation = validate_local_fastqs(config)
    if not validation.ok:
        details = validation.errors + [f"Missing file: {path}" for path in validation.missing_files]
        raise ContractError("Cannot create a contract before local validation passes: " + "; ".join(details))

    destination = output_path or config_path.parent / "analysis_contract.json"
    destination = destination.resolve()
    execution = config.setdefault("execution", {})
    execution["source_mode"] = execution.get("source_mode", mode) if mode == "contract" else mode
    execution["mode"] = "contract"
    try:
        execution["contract_file"] = str(destination.relative_to(config_path.parent.resolve()))
    except ValueError:
        execution["contract_file"] = str(destination)
    execution.pop("verified_contract_id", None)
    execution.pop("verified_at", None)

    contract = build_analysis_contract(config)
    save_json(destination, contract)
    save_json(config_path, config)
    return destination, contract


def project_contract_path(config_path: Path, config: dict[str, Any]) -> Path:
    configured = str(config.get("execution", {}).get("contract_file", "analysis_contract.json")).strip()
    if not configured:
        configured = "analysis_contract.json"
    path = Path(configured)
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def verify_project_contract(
    config_path: Path,
    *,
    contract_path: Path | None = None,
) -> ContractVerification:
    config = normalize_config(load_json(config_path))
    path = (contract_path or project_contract_path(config_path, config)).resolve()
    if not path.is_file():
        return ContractVerification(False, "", "", [f"Contract file not found: {path}"])
    return verify_analysis_contract(config, load_json(path))


def enforce_execution_policy(config_path: Path, config: dict[str, Any]) -> dict[str, str]:
    execution = config.get("execution", {})
    mode = str(execution.get("mode", "free"))
    if mode not in SUPPORTED_EXECUTION_MODES:
        raise ContractError(f"Unsupported execution mode: {mode}")

    if mode == "skill":
        errors = workflow_profile_errors(config)
        if errors:
            raise ContractError("Workflow profile validation failed: " + "; ".join(errors))
        return {"mode": mode, "skill_id": str(execution.get("skill_id", "")), "contract_id": ""}

    if mode == "contract":
        verification = verify_project_contract(config_path)
        if not verification.ok:
            raise ContractError("Approved analysis contract verification failed: " + "; ".join(verification.errors))
        return {
            "mode": mode,
            "skill_id": str(execution.get("skill_id", "")),
            "contract_id": verification.contract_id,
        }

    return {"mode": mode, "skill_id": "", "contract_id": ""}


def _build_contract_body(config: dict[str, Any]) -> dict[str, Any]:
    workflow = _workflow_snapshot(config)
    inputs = _input_artifacts(config)
    scripts = _script_artifacts(config)
    return {
        "renderer": PIPELINE_RENDERER_ID,
        "hash_algorithm": "sha256",
        "workflow": workflow,
        "inputs": inputs,
        "scripts": scripts,
        "fingerprints": {
            "workflow_sha256": canonical_sha256(workflow),
            "inputs_sha256": canonical_sha256(inputs),
            "scripts_sha256": canonical_sha256(scripts),
        },
    }


def _workflow_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    execution = config.get("execution", {})
    server = config.get("server", {})
    reference = config.get("reference", {})
    sample_items = []
    for sample in config.get("samples", {}).get("items", []):
        sample_items.append(
            {
                "sample_id": sample.get("sample_id", ""),
                "condition": sample.get("condition", ""),
                "fastq_1": sample.get("fastq_1", ""),
                "fastq_2": sample.get("fastq_2", ""),
                "batch": str(sample.get("batch", "") or "").strip(),
            }
        )

    reference_keys = (
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
    return {
        "project_id": config.get("project", {}).get("id", ""),
        "execution": {
            "mode": execution.get("mode", "free"),
            "source_mode": execution.get("source_mode", ""),
            "skill_id": execution.get("skill_id", ""),
        },
        "server": {
            "host": server.get("host", ""),
            "port": server.get("port", 22),
            "user": server.get("user", ""),
            "remote_workdir": server.get("remote_workdir", ""),
            "scheduler": server.get("scheduler", ""),
            "threads": server.get("threads"),
            "memory_gb": server.get("memory_gb"),
            "shell": server.get("shell", "bash"),
            "init_commands": deepcopy(server.get("init_commands", [])),
        },
        "sequencing": deepcopy(config.get("sequencing", {})),
        "reference": {key: reference.get(key, "") for key in reference_keys},
        "container": deepcopy(config.get("container", {})),
        "samples": {
            "remote_data_dir": config.get("samples", {}).get("remote_data_dir", ""),
            "items": sample_items,
        },
        "pipeline": deepcopy(config.get("pipeline", {})),
        "diffexp": deepcopy(config.get("diffexp", {})),
    }


def _input_artifacts(config: dict[str, Any]) -> list[dict[str, Any]]:
    samples = config.get("samples", {})
    root = Path(str(samples.get("local_data_dir", ""))).resolve()
    paired = config.get("sequencing", {}).get("layout", "paired") == "paired"
    artifacts: list[dict[str, Any]] = []
    for sample in samples.get("items", []):
        roles = (("R1", "fastq_1"), ("R2", "fastq_2")) if paired else (("R1", "fastq_1"),)
        for role, key in roles:
            logical_name = str(sample.get(key, ""))
            path = (root / logical_name).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ContractError(f"FASTQ path escapes samples.local_data_dir: {logical_name}") from exc
            if not path.is_file():
                raise ContractError(f"FASTQ file not found while building contract: {path}")
            artifacts.append(
                {
                    "sample_id": sample.get("sample_id", ""),
                    "role": role,
                    "logical_name": logical_name.replace("\\", "/"),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return artifacts


def _script_artifacts(config: dict[str, Any]) -> list[dict[str, Any]]:
    scheduler = config.get("server", {}).get("scheduler", "local")
    submit_name = "submit.sbatch" if scheduler == "slurm" else "submit.pbs" if scheduler == "pbs" else "submit.sh"
    rendered = {
        "env_setup.sh": render_env_setup_script(config),
        "run_pipeline.sh": render_remote_pipeline_script(config),
        submit_name: render_submit_script(config),
    }
    return [
        {
            "name": name,
            "size_bytes": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        for name, content in sorted(rendered.items())
    ]
