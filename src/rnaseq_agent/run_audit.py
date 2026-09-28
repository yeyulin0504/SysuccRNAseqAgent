from __future__ import annotations

import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .analysis_contract import canonical_sha256
from .analysis_contract import sha256_file as _contract_sha256_file
from .storage import save_json


RUN_AUDIT_SCHEMA_VERSION = 1
DEFAULT_MAX_HASH_BYTES = 128 * 1024 * 1024
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "private_key",
    "privatekey",
    "credential",
    "authorization",
    "api_key",
    "apikey",
    "api-key",
    "passphrase",
    "client_secret",
    "access_token",
)
_PATH_KEY_PARTS = (
    "path",
    "file",
    "dir",
    "fastq",
    "gtf",
    "fasta",
    "index",
    "counts",
    "reference",
)


def sha256_file(path: Path, max_bytes: int | None = None) -> dict[str, Any]:
    """Return file metadata using the contract module's hashing semantics."""

    path = Path(path)
    size_bytes = path.stat().st_size
    result: dict[str, Any] = {"size_bytes": size_bytes}
    limit = DEFAULT_MAX_HASH_BYTES if max_bytes is None else max_bytes
    if limit is not None and size_bytes > limit:
        result["sha256_skipped_reason"] = "size_exceeds_max_bytes"
        result["max_bytes"] = limit
        return result
    result["sha256"] = _contract_sha256_file(path)
    return result


def flatten_declared_paths(value: Any, prefix: str = "") -> list[dict[str, str]]:
    records: list[dict[str, str]] = []

    def visit(item: Any, location: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                child_location = f"{location}.{key}" if location else str(key)
                visit(child, child_location)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{location}[{index}]")
        elif isinstance(item, str) and _looks_like_declared_path(location, item):
            records.append({"path": item.replace("\\", "/"), "declared_at": location})

    visit(value, prefix)
    return records


def write_io_lineage(
    attempt_dir: Path,
    inputs: Any,
    outputs: Any,
    *,
    source_inputs: list[str] | None = None,
) -> Path:
    path = Path(attempt_dir) / "io_lineage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for kind, values in (("input", inputs), ("output", outputs)):
        if isinstance(values, list) and all(isinstance(item, dict) for item in values):
            declared = [dict(item) for item in values]
        else:
            declared = flatten_declared_paths(values)
        for record in declared:
            lines.append(
                json.dumps(
                    {"kind": kind, **_lineage_record(Path(attempt_dir), record, kind, source_inputs)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def write_artifact_index(attempt_dir: Path, records: list[dict[str, Any]]) -> Path:
    artifacts = [_redact(record) for record in records]
    body = {"schema_version": RUN_AUDIT_SCHEMA_VERSION, "artifacts": artifacts}
    payload = {**body, "index_id": f"sha256:{canonical_sha256(body)}"}
    path = Path(attempt_dir) / "artifact_index.json"
    save_json(path, payload)
    return path


def write_run_manifest(
    attempt_dir: Path,
    *,
    project_id: str,
    revision_id: str,
    run_id: str,
    attempt_id: str,
    contract_id: str,
    parameters: Any,
    config: Any,
    tool_versions: Any,
    environment: Any,
    validation: Any,
    preflight: Any,
    final_status: str,
    extra_body: dict[str, Any] | None = None,
) -> Path:
    path = Path(attempt_dir) / "run_manifest.json"
    if path.is_file():
        _validate_manifest_payload(json.loads(path.read_text(encoding="utf-8")))

    body: dict[str, Any] = {
        "schema_version": RUN_AUDIT_SCHEMA_VERSION,
        "project_id": project_id,
        "revision_id": revision_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "contract_id": contract_id,
        "parameter_sha256": canonical_sha256(parameters),
        "config_sha256": canonical_sha256(_redact(config)),
        "parameters": _redact(parameters),
        "config": _redact(config),
        "tool_versions": _redact(tool_versions),
        "environment": _redact(environment),
        "validation": _redact(validation),
        "preflight": _redact(preflight),
        "final_status": final_status,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if extra_body:
        body.update(_redact(extra_body))
    payload = {"schema_version": RUN_AUDIT_SCHEMA_VERSION, "manifest_id": f"sha256:{canonical_sha256(body)}", "body": body}
    save_json(path, payload)
    return path


def finalize_run_manifest(
    attempt_dir: Path,
    *,
    final_status: str,
    error: str | None = None,
    validation: Any | None = None,
    preflight: Any | None = None,
) -> Path:
    """Update terminal audit state without replacing the original envelope."""

    path = Path(attempt_dir) / "run_manifest.json"
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        body = _load_manifest_body(payload)
    else:
        payload = {}
        body = {"schema_version": RUN_AUDIT_SCHEMA_VERSION}
    audit = dict(body.get("audit") or {})
    audit.update({"final_status": final_status})
    if error:
        audit["error"] = error
    if validation is not None:
        audit["validation"] = _redact(validation)
    if preflight is not None:
        audit["preflight"] = _redact(preflight)
    body["audit"] = _redact(audit)
    body["final_status"] = final_status
    result = {
        "schema_version": payload.get("schema_version", RUN_AUDIT_SCHEMA_VERSION) if path.is_file() else RUN_AUDIT_SCHEMA_VERSION,
        "manifest_id": f"sha256:{canonical_sha256(body)}",
        "body": body,
    }
    save_json(path, result)
    return path


def validate_run_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate_manifest_payload(payload)
    return payload


def _validate_manifest_payload(payload: dict[str, Any]) -> None:
    body = payload.get("body")
    if not isinstance(body, dict) and "manifest_id" in payload:
        body = {key: value for key, value in payload.items() if key not in {"manifest_id", "schema_version"}}
    expected = f"sha256:{canonical_sha256(body)}" if isinstance(body, dict) else ""
    if payload.get("manifest_id") != expected:
        raise ValueError("manifest ID does not match manifest body")


def _load_manifest_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Load both the current envelope and the pre-envelope flat schema."""

    _validate_manifest_payload(payload)
    body = payload.get("body")
    if isinstance(body, dict):
        return dict(body)
    return {
        key: value
        for key, value in payload.items()
        if key not in {"manifest_id", "schema_version"}
    }


def _looks_like_declared_path(location: str, value: str) -> bool:
    key = location.rsplit(".", 1)[-1].split("[", 1)[0].lower()
    if any(part in key for part in _PATH_KEY_PARTS):
        return True
    return "/" in value or "\\" in value or Path(value).suffix.lower() in {".fq", ".gz", ".gtf", ".fa", ".fasta", ".tsv"}


def _redact(value: Any, key: str = "") -> Any:
    if any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, str):
        return re.sub(r"(://)([^/@\s]+)@", r"\1[REDACTED]@", value)
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    return value


def default_environment() -> dict[str, str]:
    return {"python": sys.version.split()[0], "platform": platform.platform(), "cwd": os.getcwd()}


def _lineage_record(
    attempt_dir: Path,
    record: dict[str, Any],
    kind: str,
    default_source_inputs: list[str] | None,
) -> dict[str, Any]:
    raw_path = str(record.get("path") or record.get("logical_name") or record.get("name") or "")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = attempt_dir / candidate
    exists = candidate.is_file()
    metadata = sha256_file(candidate) if exists else {
        "size_bytes": None,
        "sha256": None,
        "sha256_skipped_reason": "file_missing",
    }
    result = dict(record)
    result.setdefault("logical_name", record.get("name") or raw_path)
    result["path"] = raw_path.replace("\\", "/")
    result["exists"] = exists
    result["size_bytes"] = record.get("size_bytes", metadata.get("size_bytes"))
    result["sha256"] = record.get("sha256", metadata.get("sha256"))
    if "sha256_skipped_reason" in metadata:
        result["sha256_skipped_reason"] = metadata["sha256_skipped_reason"]
    if "source_inputs" not in result:
        result["source_inputs"] = list(default_source_inputs or [])
    result.pop("declared_at", None)
    return _redact(result)
