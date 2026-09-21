from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from .model_disclosure import (
    ClaimedDataGrant,
    DisclosureManifest,
    ModelContext,
    ModelDataRevisions,
    ProviderIdentity,
)
from .model_provider import ProviderConfig, provider_config_revision, provider_identity
from .storage import load_json

MODEL_PROJECTION_VERSION = 1
TOOL_PROJECTION_VERSION = 1
MAX_ENTRIES = 128
MAX_ENTRY_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_ARGUMENT_FRAGMENT_BYTES = 16 * 1024
MAX_ARGUMENT_FRAGMENTS = 128
MAX_NESTING_DEPTH = 16
MAX_COLLECTION_ITEMS = 256
MAX_SCALAR_STRING_BYTES = 16 * 1024
_SUMMARY_SAFE_ENUMS = {
    "project_state": {"setup", "drafting", "input_ready", "planned", "running", "waiting_qc", "completed", "failed", "unknown"},
    "route_id": {"bulk_rna", "single_cell", "spatial", "unknown"},
    "capability_id": {"bulk_rna", "single_cell", "spatial", "unknown"},
    "input_kind": {"fastq", "counts", "counts_matrix", "expression_matrix", "unknown"},
    "data_scope": {"summary"},
    "layout": {"single", "paired", "unknown"},
    "strandedness": {"forward", "reverse", "unstranded", "unknown", "auto"},
    "classification": {"raw_counts", "normalized", "normalized_expression", "unknown"},
    "state": {"setup", "drafting", "input_ready", "planned", "running", "waiting_qc", "completed", "failed", "unknown"},
    "category": {"run_failed", "unknown"},
}
_SUMMARY_STAGE_NAMES = {"fastp", "star", "featurecounts", "deseq2", "go", "gsea", "cms", "report", "qc", "align", "quantify"}
_SAFE_CONDITION_LABELS = frozenset({
    "baseline", "case", "control", "disease", "healthy", "ko", "knockout",
    "normal", "treated", "treatment", "tumor", "untreated", "vehicle", "wildtype", "wt",
})
_SOURCE_REF_RE = re.compile(r"^src_[0-9a-f]{32}$")


class EphemeralArgumentError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _validate_bounded(value: Any, depth: int = 0) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_TOO_DEEP")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_SCALAR_STRING_BYTES:
            raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_TOO_LARGE")
    elif isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_TOO_LARGE")
        for key, item in value.items():
            _validate_bounded(str(key), depth + 1)
            _validate_bounded(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_TOO_LARGE")
        for item in value:
            _validate_bounded(item, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_MALFORMED")


@dataclass(frozen=True, repr=False)
class _EphemeralEntry:
    project_id: str
    thread_id: str
    call_id: str
    name: str
    payload: bytes
    expires_at: float
    fragment_count: int = 0
    finalized: bool = True


class EphemeralToolCallStore:
    """Bounded in-process store for raw tool arguments; values never enter state/logs."""
    def __init__(self, *, ttl_seconds: int = 600):
        self._lock = threading.RLock()
        self._entries: dict[str, _EphemeralEntry] = {}
        self._total_serialized_bytes = 0
        self._ttl_seconds = max(1, int(ttl_seconds))

    @property
    def total_serialized_bytes(self) -> int:
        with self._lock:
            return self._total_serialized_bytes

    @property
    def live_entries(self) -> int:
        with self._lock:
            self._sweep_locked()
            return len(self._entries)

    def _sweep_locked(self) -> None:
        now = time.monotonic()
        for ref, entry in list(self._entries.items())[:MAX_ENTRIES]:
            if entry.expires_at <= now:
                self._total_serialized_bytes -= len(entry.payload)
                self._entries.pop(ref, None)

    def put(self, project_id: str, thread_id: str | None, call_id: str, name: str, arguments: Mapping[str, Any] | None = None) -> str:
        fragment_mode = arguments is None
        arguments = {} if arguments is None else arguments
        _validate_bounded(arguments)
        try:
            payload = _canonical_bytes(dict(arguments))
        except (TypeError, ValueError, OverflowError) as exc:
            raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_MALFORMED") from exc
        if len(payload) > MAX_ENTRY_BYTES:
            raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_TOO_LARGE")
        reserved_len = 0 if fragment_mode else len(payload)
        with self._lock:
            self._sweep_locked()
            if len(self._entries) >= MAX_ENTRIES or self._total_serialized_bytes + reserved_len > MAX_TOTAL_BYTES:
                raise EphemeralArgumentError("EPHEMERAL_STORE_FULL")
            ref = "callref_" + secrets.token_urlsafe(18)
            if fragment_mode:
                payload = b""
            self._entries[ref] = _EphemeralEntry(str(project_id), str(thread_id or ""), str(call_id), str(name), payload, time.monotonic() + self._ttl_seconds, 0, not fragment_mode)
            self._total_serialized_bytes += reserved_len
            return ref

    def get(self, call_ref: str, *, project_id: str | None = None, thread_id: str | None = None,
            call_id: str | None = None, name: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._sweep_locked()
            entry = self._entries.get(str(call_ref))
            if entry is None:
                raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_UNAVAILABLE")
            if not entry.finalized:
                raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_INCOMPLETE")
            if ((project_id is not None and str(project_id) != entry.project_id) or
                (thread_id is not None and str(thread_id or "") != entry.thread_id) or
                (call_id is not None and str(call_id) != entry.call_id) or
                (name is not None and str(name) != entry.name)):
                raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_BINDING_MISMATCH")
            try:
                value = _strict_loads(entry.payload)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_MALFORMED") from exc
            _validate_bounded(value)
            if not isinstance(value, dict):
                raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_MALFORMED")
            return value

    def replace(self, call_ref: str, arguments: Mapping[str, Any]) -> None:
        _validate_bounded(arguments)
        payload = _canonical_bytes(dict(arguments))
        if len(payload) > MAX_ENTRY_BYTES:
            raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_TOO_LARGE")
        with self._lock:
            self._sweep_locked()
            old = self._entries.get(str(call_ref))
            if old is None:
                raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_UNAVAILABLE")
            new_total = self._total_serialized_bytes - len(old.payload) + len(payload)
            if new_total > MAX_TOTAL_BYTES:
                raise EphemeralArgumentError("EPHEMERAL_STORE_FULL")
            self._entries[str(call_ref)] = _EphemeralEntry(old.project_id, old.thread_id, old.call_id, old.name, payload, time.monotonic() + self._ttl_seconds)
            self._total_serialized_bytes = new_total

    def delete(self, call_ref: str) -> None:
        with self._lock:
            entry = self._entries.pop(str(call_ref), None)
            if entry is not None:
                self._total_serialized_bytes -= len(entry.payload)

    def append_fragment(self, call_ref: str, fragment: str | bytes, *, final: bool = False,
                        project_id: str | None = None, thread_id: str | None = None,
                        call_id: str | None = None, name: str | None = None) -> dict[str, Any] | None:
        """Append bounded JSON bytes; partial values are never returned or executable."""
        data = fragment.encode("utf-8") if isinstance(fragment, str) else bytes(fragment)
        if len(data) > MAX_ARGUMENT_FRAGMENT_BYTES:
            raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_TOO_LARGE")
        with self._lock:
            self._sweep_locked()
            entry = self._entries.get(str(call_ref))
            if entry is None:
                raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_UNAVAILABLE")
            if ((project_id is not None and str(project_id) != entry.project_id) or
                (thread_id is not None and str(thread_id or "") != entry.thread_id) or
                (call_id is not None and str(call_id) != entry.call_id) or
                (name is not None and str(name) != entry.name)):
                raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_BINDING_MISMATCH")
            count = entry.fragment_count + 1
            if count > MAX_ARGUMENT_FRAGMENTS:
                raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_TOO_LARGE")
            payload = entry.payload + data
            if len(payload) > MAX_ENTRY_BYTES:
                raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_TOO_LARGE")
            if self._total_serialized_bytes + len(data) > MAX_TOTAL_BYTES:
                raise EphemeralArgumentError("EPHEMERAL_STORE_FULL")
            if final:
                try:
                    parsed = _strict_loads(payload)
                    _validate_bounded(parsed)
                except EphemeralArgumentError:
                    raise
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_MALFORMED") from exc
                if not isinstance(parsed, dict):
                    raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_MALFORMED")
            self._total_serialized_bytes += len(data)
            self._entries[str(call_ref)] = _EphemeralEntry(entry.project_id, entry.thread_id, entry.call_id, entry.name, payload, entry.expires_at, count, final)
            return parsed if final else None


def _strict_loads(payload: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise EphemeralArgumentError("EPHEMERAL_ARGUMENT_MALFORMED")
            result[key] = value
        return result
    return json.loads(payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=lambda _value: (_ for _ in ()).throw(EphemeralArgumentError("EPHEMERAL_ARGUMENT_MALFORMED")))


def canonical_json_sha256(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _short_hash(value: Any) -> str:
    return canonical_json_sha256(value).split(":", 1)[-1][:12]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if result >= 0 else default


def _safe_summary_enum(field: str, value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in _SUMMARY_SAFE_ENUMS.get(field, set()) else "unknown"


def _safe_condition_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text.encode("utf-8")) > 128 or any(ch in text for ch in ("/", "\\", "\n", "\r", "\x00")):
        return "unknown"
    normalized = text.casefold()
    return normalized if normalized in _SAFE_CONDITION_LABELS else ""


def _safe_condition_counts(value: Mapping[Any, Any]) -> dict[str, int]:
    """Project condition counts without copying arbitrary condition labels."""
    result: dict[str, int] = {}
    opaque: dict[str, str] = {}
    for raw_key, raw_count in list(value.items())[:64]:
        count = _safe_int(raw_count)
        if count < 0:
            continue
        raw_text = str(raw_key or "").strip()
        label = _safe_condition_label(raw_text)
        if not label and raw_text:
            label = opaque.setdefault(raw_text, f"group_{len(opaque) + 1:03d}")
        if label:
            result[label] = result.get(label, 0) + count
    return result


def _sample_rows(project: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    samples = project.get("samples")
    if isinstance(samples, Mapping):
        samples = samples.get("items")
    return [item for item in (samples or []) if isinstance(item, Mapping)]


def build_safe_project_summary(project_dir: Path) -> dict[str, Any]:
    """Build the default model context using a fixed de-identified allowlist."""
    project_dir = Path(project_dir)
    project = _load_mapping(project_dir / "project.json")
    session = _load_mapping(project_dir / "session.json")
    intake = _load_mapping(project_dir / "intake.json")
    route = project.get("route") if isinstance(project.get("route"), Mapping) else {}
    study = project.get("study") if isinstance(project.get("study"), Mapping) else {}
    sequencing = project.get("sequencing") if isinstance(project.get("sequencing"), Mapping) else {}
    rows = _sample_rows(project)
    conditions: dict[str, int] = {}
    opaque_conditions: dict[str, str] = {}
    for row in rows:
        condition = _safe_condition_label(row.get("condition"))
        if not condition and str(row.get("condition") or "").strip():
            raw_condition = str(row.get("condition") or "").strip()
            condition = opaque_conditions.setdefault(
                raw_condition, f"group_{len(opaque_conditions) + 1:03d}"
            )
        if condition:
            conditions[condition] = conditions.get(condition, 0) + 1
    status = project.get("status") if isinstance(project.get("status"), Mapping) else {}
    if not status:
        status = session if isinstance(session, Mapping) else {}
    pipeline = project.get("pipeline") if isinstance(project.get("pipeline"), Mapping) else {}
    stages = [{"name": str(name).strip().lower() if str(name).strip().lower() in _SUMMARY_STAGE_NAMES else "unknown",
               "enabled": bool(value.get("enabled"))}
              for name, value in pipeline.items() if isinstance(value, Mapping)]
    input_block = intake.get("input") if isinstance(intake.get("input"), Mapping) else {}
    matrix_class = _safe_summary_enum(
        "classification", input_block.get("classification") or project.get("matrix_classification") or "unknown"
    )
    raw_counts = matrix_class.lower() in {"raw_counts", "raw integer counts", "counts"}
    reference = project.get("reference") if isinstance(project.get("reference"), Mapping) else {}
    contract_path = _contract_path_inside_project(project_dir, project)
    contract_present = bool(contract_path and contract_path.is_file())
    run_id = str(status.get("run_id") or status.get("job_id") or "")
    errors: list[dict[str, str]] = []
    if status.get("error") or status.get("failed"):
        errors.append({"category": "run_failed", "message": "项目运行失败。"})
    return {
        "policy_version": 1,
        "data_scope": "summary",
        "project_state": _safe_summary_enum("project_state", session.get("state") or project.get("state") or status.get("state") or "setup"),
        "route_id": _safe_summary_enum("route_id", route.get("id") or project.get("route_id") or "unknown"),
        "capability_id": _safe_summary_enum("capability_id", route.get("capability_id") or project.get("capability_id") or "bulk_rna"),
        "input_kind": _safe_summary_enum("input_kind", intake.get("input_kind") or input_block.get("kind") or "unknown"),
        "sequencing": {"layout": _safe_summary_enum("layout", sequencing.get("layout") or "unknown"),
                       "strandedness": _safe_summary_enum("strandedness", sequencing.get("strandedness") or "unknown")},
        "sample_count": len(rows),
        "condition_counts": dict(sorted(conditions.items())),
        "sample_aliases": [f"sample_{index:03d}" for index in range(1, len(rows) + 1)],
        "replicate_gate": {"passed": bool(rows) and all(count >= 2 for count in conditions.values()),
                           "minimum_per_condition": 2},
        "pipeline_stages": stages,
        "matrix": {"classification": matrix_class, "deseq2_eligible": raw_counts},
        "references": {
            "gtf_present": bool(str(reference.get("remote_gtf_path") or "").strip()),
            "genome_present": bool(str(reference.get("remote_genome_fasta_path") or "").strip()),
            "star_index_present": bool(str(reference.get("star_index_dir") or "").strip()),
        },
        "contract": {"present": contract_present, "id_hash": _short_hash(_file_sha256(contract_path) if contract_present else "")},
        "run": {"present": bool(run_id), "state": _safe_summary_enum("state", status.get("state") or "unknown"), "id_hash": _short_hash(run_id) if run_id else ""},
        "errors": errors,
    }


def _full_result_mapping(full_result: Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(full_result, Mapping):
        return full_result
    return {}


def project_tool_result_for_model(name: str, full_result: Mapping[str, Any]) -> dict[str, Any]:
    """Project one executor result through a per-tool fixed allowlist."""
    full = _full_result_mapping(full_result)
    ok = bool(full.get("ok"))
    if name not in _registered_tool_names():
        return {"ok": False, "error_code": "MODEL_TOOL_RESULT_UNMAPPED"}
    if name == "read_project_state":
        summary = full.get("summary") if isinstance(full.get("summary"), Mapping) else None
        result = {"ok": ok}
        if summary is not None:
            # Rebuild the provider-facing shape from an allowlist.  A tool result
            # may be assembled by a legacy executor and must not smuggle arbitrary
            # nested values into the model context.
            allowed = {
                "policy_version", "data_scope", "project_state", "route_id",
                "capability_id", "input_kind", "sample_count", "condition_counts",
                "sample_aliases", "replicate_gate", "pipeline_stages", "matrix",
                "references", "contract", "run", "errors", "sequencing",
            }
            safe_summary: dict[str, Any] = {}
            for key in allowed:
                value = summary.get(key)
                if key == "condition_counts" and isinstance(value, Mapping):
                    safe_summary[key] = _safe_condition_counts(value)
                elif key == "sample_aliases" and isinstance(value, list):
                    safe_summary[key] = [f"sample_{index:03d}" for index, _ in enumerate(value[:256], 1)]
                elif key == "pipeline_stages" and isinstance(value, list):
                    safe_summary[key] = [{"name": (stage if stage in _SUMMARY_STAGE_NAMES else "unknown"), "enabled": bool(item.get("enabled"))} for item in value[:64] if isinstance(item, Mapping) for stage in [str(item.get("name") or "").strip().lower()]]
                elif key in {"references", "contract", "run", "replicate_gate", "matrix", "sequencing"} and isinstance(value, Mapping):
                    nested: dict[str, Any] = {}
                    for nested_key, nested_value in value.items():
                        nested_key = str(nested_key)
                        if nested_key in {"layout", "strandedness", "classification", "state"}:
                            nested[nested_key] = _safe_summary_enum(nested_key, nested_value)
                        elif nested_key in {"passed", "deseq2_eligible", "gtf_present", "genome_present", "star_index_present", "present"} and isinstance(nested_value, bool):
                            nested[nested_key] = nested_value
                        elif nested_key == "minimum_per_condition":
                            nested[nested_key] = min(_safe_int(nested_value), 256)
                        elif nested_key == "id_hash" and re.fullmatch(r"[0-9a-f]{8,128}", str(nested_value or "").lower()):
                            nested[nested_key] = str(nested_value).lower()[:128]
                    safe_summary[key] = nested
                elif key in {"policy_version", "sample_count"}:
                    safe_summary[key] = _safe_int(value)
                elif key in {"project_state", "route_id", "capability_id", "input_kind", "data_scope"}:
                    safe_summary[key] = _safe_summary_enum(key, value)
                elif key == "errors" and isinstance(value, list):
                    safe_summary[key] = [
                        {
                            "category": _safe_summary_enum("category", item.get("category")),
                            "message": "项目运行失败。",
                        }
                        for item in value[:16]
                        if isinstance(item, Mapping) and str(item.get("category") or "").strip()
                    ]
            result["summary"] = safe_summary
        else:
            result.update({"state": _safe_summary_enum("state", full.get("state") or "unknown"), "has_session": bool(full.get("has_session"))})
        return result
    if name == "browse_remote_samples":
        nested = full.get("result") if isinstance(full.get("result"), Mapping) else full
        groups = nested.get("groups") if isinstance(nested.get("groups"), list) else []
        sample_count = _safe_int(nested.get("sample_count"), sum(len(g.get("samples") or []) for g in groups if isinstance(g, Mapping)))
        if not sample_count and isinstance(full.get("samples"), list):
            sample_count = len(full.get("samples") or [])
        paired = 0
        for group in groups:
            if isinstance(group, Mapping):
                paired += sum(1 for sample in (group.get("samples") or []) if isinstance(sample, Mapping) and sample.get("fastq_2"))
        paired = paired or _safe_int(nested.get("paired_count"))
        directory_count = len(groups) or _safe_int(nested.get("directory_count")) or (1 if full.get("scanned_path") else 0)
        source_ref = str(full.get("source_ref") or nested.get("source_ref") or "")
        if not _SOURCE_REF_RE.fullmatch(source_ref):
            source_ref = ""
        return {"ok": ok, "sample_count": sample_count, "paired_count": paired,
                "unmatched_count": _safe_int(nested.get("unmatched_count")),
                "directory_count": directory_count, "truncated": bool(nested.get("truncated")),
                "authorization": str(nested.get("authorization") or ("inside_approved_root" if full.get("scanned_path") else "")),
                "source_ref": source_ref}
    if name == "refresh_project_status":
        status = full.get("status") if isinstance(full.get("status"), Mapping) else {}
        safe_status = {"state": _safe_summary_enum("state", status.get("state") or full.get("run_state") or "unknown")}
        message = str(status.get("message") or "")
        if message.startswith("Remote state: "):
            safe_status["message"] = "Remote state: " + safe_status["state"]
        stage = str(status.get("stage") or "").strip().lower()
        result = {"ok": ok, "state": _safe_summary_enum("state", full.get("state") or "unknown"),
                "run_state": _safe_summary_enum("state", full.get("run_state") or status.get("state") or "unknown"),
                "stage": stage if stage in _SUMMARY_STAGE_NAMES else "unknown", "artifact_present": bool(status.get("artifact_present")),
                "status": safe_status}
        return result
    if name == "get_project_report":
        return {"ok": ok, "state": _safe_summary_enum("state", full.get("state") or "unknown"), "generated": ok,
                "present": bool(full.get("report") or full.get("report_path")),
                "validated": bool(full.get("validated")),
                "report_hash": _short_hash(full.get("report") or full.get("report_path") or "")}
    result: dict[str, Any] = {"ok": ok}
    for key in ("state", "run_state", "gate_ok", "confirmation_required"):
        if key in full and isinstance(full.get(key), (str, bool)):
            result[key] = _safe_summary_enum("state", full[key]) if key in {"state", "run_state"} else full[key]
    for key in ("sample_count", "directory_count", "unmatched_count", "record_count"):
        if key in full:
            result[key] = _safe_int(full.get(key))
    if name == "set_cms_options" and isinstance(full.get("gate_warnings"), list):
        safe_warnings: list[str] = []
        for item in full["gate_warnings"][:8]:
            text = str(item) if isinstance(item, str) else ""
            lowered = text.lower()
            if "crc" in lowered:
                safe_warnings.append("CRC cancer type is required for CMS.")
            elif "30" in lowered:
                safe_warnings.append("CMS requires at least 30 samples.")
            elif "sample" in lowered or "样本" in text:
                safe_warnings.append("CMS sample-count gate is not satisfied.")
        if safe_warnings:
            result["gate_warnings"] = list(dict.fromkeys(safe_warnings))
    for key in ("contract_id", "run_id", "config_hash"):
        if full.get(key):
            result[f"{key}_hash"] = _short_hash(full[key])
    if name in {"generate_plan", "confirm_contract", "run_analysis"}:
        result["message"] = "操作已完成。" if ok else "操作未完成。"
    return result


def project_tool_result_for_log(name: str, full_result: Mapping[str, Any]) -> dict[str, Any]:
    projected = project_tool_result_for_model(name, full_result)
    raw_code = str(full_result.get("error_code") or "") if isinstance(full_result, Mapping) else ""
    safe_codes = {"TOOL_MODE_DISABLED", "REMOTE_ROOT_NOT_APPROVED", "REMOTE_SECURITY_AUDIT_FAILED",
                  "MODEL_TOOL_RESULT_UNMAPPED", "REMOTE_SCAN_STORE_FAILED"}
    return {"tool": name, "ok": bool(full_result.get("ok")) if isinstance(full_result, Mapping) else False,
            "error_code": raw_code if raw_code in safe_codes else "",
            "record_counts": {key: _safe_int(projected.get(key)) for key in ("sample_count", "directory_count", "unmatched_count", "record_count") if key in projected},
            "result_hash": canonical_json_sha256(projected)}


def _registered_tool_names() -> tuple[str, ...]:
    from .agent_tools import TOOL_SPECS
    return tuple(TOOL_SPECS)


def project_tool_arguments_for_model(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    from .agent_tools import TOOL_SPECS, PIPELINE_STEPS, RUN_STAGES, SCHEDULERS, CMS_RUN_MODES
    raw = dict(arguments) if isinstance(arguments, Mapping) else {}
    base = {"argument_hash": canonical_json_sha256(raw)}
    if name not in TOOL_SPECS:
        return {**base, "unmapped": True}
    if name == "browse_remote_samples":
        return {**base, "path_present": bool(str(raw.get("path") or "").strip())}
    if name in {"edit_samples"}:
        samples = raw.get("samples") if isinstance(raw.get("samples"), list) else []
        return {**base, "samples_present": bool(samples), "sample_count": min(len(samples), 256),
                "remove_present": bool(raw.get("remove")), "remove_count": min(len(raw.get("remove") or []), 256)}
    if name == "write_project_config":
        samples = raw.get("samples") if isinstance(raw.get("samples"), list) else []
        return {**base, "data_source_present": bool(raw.get("data_source")), "sample_count": min(len(samples), 256),
                "reference_present": any(bool(raw.get(key)) for key in ("gtf", "genome_fasta", "star_index", "rsem_prefix"))}
    if name in {"edit_reference"}:
        return {**base, **{f"{key}_present": bool(raw.get(key)) for key in ("gtf", "genome_fasta", "star_index", "rsem_prefix")}}
    if name == "edit_connection":
        return {**base, **{f"{key}_present": key in raw for key in ("host", "user", "port", "scheduler", "threads", "memory_gb", "remote_base_dir", "remote_workdir")}}
    if name in {"configure_pipeline"}:
        return {**base, "step": raw.get("step") if raw.get("step") in PIPELINE_STEPS else "", "enabled_present": isinstance(raw.get("enabled"), bool)}
    if name == "set_run_resources":
        return {**base, "threads": min(max(_safe_int(raw.get("threads")), 0), 128), "memory_gb": min(max(_safe_int(raw.get("memory_gb")), 0), 2048)}
    if name == "run_analysis":
        return {**base, "stage": raw.get("stage") if raw.get("stage") in RUN_STAGES else ""}
    if name == "set_cms_options":
        return {**base, "enabled_present": isinstance(raw.get("enabled"), bool), "n_perm": min(max(_safe_int(raw.get("n_perm")), 0), 100000), "run_mode": raw.get("run_mode") if raw.get("run_mode") in CMS_RUN_MODES else ""}
    if name in {"set_diffexp_reference"}:
        return {**base, "reference_condition_present": bool(raw.get("reference_condition"))}
    if name in {"generate_plan", "confirm_contract", "rollback_changes", "refresh_project_status", "get_project_report", "read_project_state"}:
        return base
    return base


def project_assistant_tool_call(call_id: str, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    if not str(call_id).strip() or name not in _registered_tool_names():
        raise ValueError("invalid tool call")
    projection = project_tool_arguments_for_model(name, arguments)
    return {"id": str(call_id), "type": "function", "function": {"name": name,
            "arguments": json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))},
            "tool_projection_version": TOOL_PROJECTION_VERSION, "arguments_hash": canonical_json_sha256(dict(arguments))}


def _load_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = load_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
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


def _context_free_revisions(provider: ProviderConfig) -> ModelDataRevisions:
    """Return deterministic revisions for a request with no project context."""
    empty_revision = canonical_json_sha256({})
    return ModelDataRevisions(
        project_revision=empty_revision,
        sample_revision=empty_revision,
        remote_scan_revision=None,
        report_revision=None,
        provider_config_revision=provider_config_revision(provider),
    )


class ModelContextBuilder:
    """Construct provider-safe context without implicitly discovering projects.

    ``project_dir=None`` is the explicit context-free mode used by router and
    connection-test calls.  A real project may add the bounded summary
    projection.  The first exact-data slice accepts only a claimed local
    ``sample_ids`` grant and keeps those values in this request-local object.
    """

    @staticmethod
    def build(
        *,
        project_dir: Path | None,
        project_id: str,
        thread_id: str,
        provider: ProviderConfig,
        system_prompt: str,
        current_user_message: str,
        durable_messages: Any,
        claimed_grant: Any = None,
    ) -> ModelContext:
        messages: list[dict[str, Any]] = []
        revisions = _context_free_revisions(provider)

        # Context-free callers must never probe a project path.  Existing
        # project summaries are still bounded and allowlisted when explicitly
        # supplied by a caller.
        if project_dir is not None and Path(project_dir).is_dir():
            summary = build_safe_project_summary(Path(project_dir))
            messages.append({
                "role": "system",
                "name": "model_summary_v1",
                "content": json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            })
            revisions = read_model_data_revisions(Path(project_dir), provider)

        exact_fields: tuple[str, ...] = ()
        exact_counts: dict[str, int] = {}
        exact_byte_length = 0
        exact_grant_hash: str | None = None
        exact_message: dict[str, Any] | None = None
        if claimed_grant is not None:
            if not isinstance(claimed_grant, ClaimedDataGrant):
                raise ValueError("MODEL_DATA_GRANT_INVALID")
            if project_dir is None or not Path(project_dir).is_dir():
                raise ValueError("MODEL_DATA_SCOPE_UNSUPPORTED")
            if str(claimed_grant.bindings.project_id) != str(project_id) or str(claimed_grant.bindings.thread_id) != str(thread_id):
                raise ValueError("MODEL_DATA_GRANT_INVALID")
            if str(claimed_grant.bindings.provider_identity) != provider_identity(provider).digest:
                raise ValueError("MODEL_PROVIDER_CHANGED")
            if claimed_grant.bindings.remote_scan is not None:
                raise ValueError("MODEL_DATA_SCOPE_UNSUPPORTED")
            if tuple(claimed_grant.fields) != ("sample_ids",):
                raise ValueError("MODEL_DATA_SCOPE_UNSUPPORTED")
            if claimed_grant.bindings.revisions != revisions:
                raise ValueError("MODEL_DATA_REVISION_CHANGED")
            values = claimed_grant.exact_values.get("sample_ids")
            if not isinstance(values, tuple) or not values or any(not isinstance(value, str) or not value for value in values):
                raise ValueError("MODEL_DATA_GRANT_INVALID")
            if set(claimed_grant.exact_values) != {"sample_ids"}:
                raise ValueError("MODEL_DATA_SCOPE_UNSUPPORTED")
            exact_block = {"data_scope": "exact", "sample_ids": list(values)}
            exact_content = json.dumps(exact_block, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            exact_message = {"role": "system", "name": "model_exact_data_v1", "content": exact_content}
            exact_fields = ("sample_ids",)
            exact_counts = {"sample_ids": len(values)}
            exact_byte_length = len(exact_content.encode("utf-8"))
            exact_grant_hash = hashlib.sha256(str(claimed_grant.grant_id).encode("utf-8")).hexdigest()

        if str(system_prompt):
            messages.append({"role": "system", "content": str(system_prompt)})
        if exact_message is not None:
            messages.append(exact_message)

        for item in durable_messages or ():
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "").strip()
            content = item.get("content")
            if role not in {"system", "user", "assistant"} or not isinstance(content, str):
                continue
            messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": str(current_user_message)})
        manifest = DisclosureManifest(
            grant_id_hash=exact_grant_hash,
            fields=exact_fields,
            record_counts=exact_counts,
            byte_length=exact_byte_length,
            revisions=revisions,
        )
        return ModelContext(
            messages=tuple(messages),
            disclosure_manifest=manifest,
            provider_identity=provider_identity(provider),
        )
