from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .model_disclosure import ModelDataRevisions
from .model_provider import ProviderConfig, provider_config_revision


def canonical_json_sha256(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _load_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid project metadata: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid project metadata: {path.name}")
    return value


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ValueError(f"could not read project artifact: {path.name}") from exc
    return "sha256:" + digest.hexdigest()


def _contract_path_inside_project(project_dir: Path, project: dict[str, Any]) -> Path | None:
    configured = str(project.get("execution", {}).get("contract_file", "analysis_contract.json")) if isinstance(project.get("execution"), dict) else "analysis_contract.json"
    candidate = Path(configured)
    if not candidate.is_absolute():
        candidate = project_dir / candidate
    root = project_dir.resolve()
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def read_model_data_revisions(
    project_dir: Path,
    provider: ProviderConfig,
    remote_scan_revision: str | None = None,
) -> ModelDataRevisions:
    project_dir = Path(project_dir)
    project = _load_mapping(project_dir / "project.json")
    session = _load_mapping(project_dir / "session.json")
    intake = _load_mapping(project_dir / "intake.json")
    contract_path = _contract_path_inside_project(project_dir, project)
    project_view = {
        "project": project,
        "session": session,
        "intake": intake,
        "contract_sha256": _file_sha256(contract_path) if contract_path is not None else "outside-project",
    }
    samples = project.get("samples")
    sample_view = {
        "route": project.get("route"),
        "sequencing": project.get("sequencing"),
        "samples": samples,
        "input": intake.get("input"),
        "design": intake.get("design"),
    }
    return ModelDataRevisions(
        project_revision=canonical_json_sha256(project_view),
        sample_revision=canonical_json_sha256(sample_view),
        remote_scan_revision=remote_scan_revision,
        report_revision=_file_sha256(project_dir / "report.md"),
        provider_config_revision=provider_config_revision(provider),
    )
