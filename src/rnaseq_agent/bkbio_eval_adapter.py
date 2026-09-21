"""Real bkbio-eval adapter for the project's counts-entry DESeq2 path.

The adapter deliberately contains no differential-expression implementation.
It creates an isolated :class:`~rnaseq_agent.session.ProjectSession`, freezes
an Analysis Contract, executes the existing ``counts`` stage, and translates
the downloaded ``diffexp/deseq2_results.tsv`` artifact to
``bkbio-eval/analyzer-result@1``.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib import metadata
from pathlib import Path
from threading import Lock
from typing import Any, Mapping
from uuid import uuid4

from . import __version__
from .analysis_contract import canonical_sha256, sha256_file
from .capability import PASS
from .connection_store import SHARED_FIELDS, load_connection
from .container import container_config, wrap_command
from .defaults import DEFAULT_CMS, DEFAULT_CONTAINER, DEFAULT_DIFFEXP, DEFAULT_PIPELINE, DEFAULT_REFERENCE
from .differential import DEG_MIN_REPLICATES_PER_GROUP, PAIRED_DESIGN_FORMULA
from .remote_transport import create_remote_transport
from .session import ProjectSession
from .shell import shell_quote
from .ssh_auth import get_ssh_credential, normalize_auth_mode, set_ssh_credential
from .storage import load_json


RESULT_SCHEMA = "bkbio-eval/analyzer-result@1"
ANALYZER_NAME = "sysu-rnaseq-agent"
PROJECT_TEMPLATE_ENV = "RNASEQ_AGENT_EVAL_PROJECT_TEMPLATE"
_CREDENTIAL_LOCKS: dict[tuple[str, str], Lock] = {}
_CREDENTIAL_LOCKS_GUARD = Lock()
_CONTAINER_TEMPLATE_FIELDS = {
    "enabled",
    "engine",
    "image_uri",
    "image_path",
    "bind_paths",
    "digest",
    "image_digest",
}
_EVALUATION_TEMPLATE_FIELDS = {"release_mode"}


class AdapterError(RuntimeError):
    """Base error for the bkbio-eval integration boundary."""


class AdapterInputError(AdapterError):
    """The evaluator input cannot be represented by the real product path."""


class AdapterNotEvaluableError(AdapterInputError):
    """A valid evaluator case is outside the frozen product capability."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class AdapterUnavailableError(AdapterError):
    """The configured execution environment is unavailable."""


class AdapterExecutionError(AdapterError):
    """The real project execution path did not produce a valid artifact."""


@dataclass(frozen=True)
class EvalInputs:
    counts_path: Path
    sample_ids: tuple[str, ...]
    gene_ids: tuple[str, ...]
    library_sizes: dict[str, int]
    samples: tuple[dict[str, str], ...]

    @property
    def n_genes(self) -> int:
        return len(self.gene_ids)

    @property
    def n_samples(self) -> int:
        return len(self.sample_ids)


def read_eval_inputs(
    inputs_dir: Path,
    *,
    condition_column: str,
    pair_column: str = "",
) -> EvalInputs:
    """Read and validate the standard bkbio-eval counts/coldata boundary."""
    inputs_dir = Path(inputs_dir).resolve()
    counts_path = inputs_dir / "counts.tsv"
    coldata_path = inputs_dir / "coldata.tsv"
    if not counts_path.is_file():
        raise AdapterInputError(f"counts.tsv not found: {counts_path}")
    if not coldata_path.is_file():
        raise AdapterInputError(f"coldata.tsv not found: {coldata_path}")

    sample_ids, gene_ids, library_sizes = _scan_counts(counts_path)
    samples_by_id = _read_coldata(
        coldata_path,
        condition_column=condition_column,
        pair_column=pair_column,
    )
    count_set = set(sample_ids)
    coldata_set = set(samples_by_id)
    if count_set != coldata_set:
        missing = sorted(count_set - coldata_set)
        extra = sorted(coldata_set - count_set)
        raise AdapterInputError(
            "coldata.tsv sample names must exactly match counts.tsv columns; "
            f"missing={missing}, extra={extra}"
        )
    ordered_samples = tuple(samples_by_id[sample_id] for sample_id in sample_ids)
    return EvalInputs(
        counts_path=counts_path,
        sample_ids=tuple(sample_ids),
        gene_ids=tuple(gene_ids),
        library_sizes=library_sizes,
        samples=ordered_samples,
    )


def run_case(
    inputs_dir: Path,
    params_path: Path,
    out_path: Path,
    *,
    case_id: str = "",
    connection: Mapping[str, Any] | None = None,
    project_template: Path | None = None,
) -> dict[str, Any]:
    """Run one evaluator case through the real frozen counts-stage workflow."""
    params_path = Path(params_path).resolve()
    out_path = Path(out_path).resolve()
    params = _read_params(params_path)
    _validate_supported_design(params)
    condition_column = str(params.get("condition_column") or "condition").strip()
    pair_column = str(params.get("pair_column") or "pair_id").strip()
    try:
        inputs = read_eval_inputs(
            Path(inputs_dir),
            condition_column=condition_column,
            pair_column=pair_column if _is_paired_request(params) else "",
        )
    except AdapterInputError as exc:
        if _is_paired_request(params) and pair_column and "missing columns" in str(exc):
            raise AdapterNotEvaluableError(
                "unsupported_design",
                f"paired_two_group requires source pair column {pair_column!r}",
            ) from exc
        raise
    reference, contrast = _validate_groups(inputs, params, condition_column)
    if _is_paired_request(params):
        _validate_paired_inputs(inputs, params, reference=reference, contrast=contrast)

    project_id = _project_id(case_id)
    project_dir = out_path.parent / ".rnaseq-agent-eval" / project_id
    uploads_dir = project_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=False)
    staged_counts = uploads_dir / "counts_matrix.tsv"
    shutil.copyfile(inputs.counts_path, staged_counts)

    template = _load_project_template(project_template)
    shared_connection = dict(connection) if connection is not None else load_connection()
    config = _build_project_config(
        project_dir,
        project_id,
        staged_counts,
        inputs,
        params,
        reference=reference,
        connection=shared_connection,
        template=template,
    )

    credential = _install_runtime_credential(config, shared_connection)
    try:
        runtime = _ensure_runtime_available(config)
        session = ProjectSession(project_dir)
        gate = session.new_project(config)
        if gate.verdict != PASS:
            raise AdapterInputError(
                "NOT_EVALUABLE: " + "; ".join(gate.reasons)
            )
        plan = session.plan()
        if plan.summary.startswith("Adapter 预检未通过"):
            raise AdapterInputError(f"NOT_EVALUABLE: {plan.summary}")
        contract = session.confirm()
        outcome = session.execute_stage("counts", wait=True)
    except AdapterError:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary preserves the real failure
        raise AdapterExecutionError(
            f"real counts-stage execution failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        _restore_runtime_credential(config, credential)

    if outcome.get("state") != "stage_completed":
        raise AdapterExecutionError(
            f"counts stage ended in {outcome.get('state')!r}: {outcome.get('message', '')}"
        )

    live_config = load_json(project_dir / "project.json")
    run_id = str(live_config.get("status", {}).get("run_id") or "").strip()
    if not run_id:
        raise AdapterExecutionError("counts stage completed without status.run_id")
    attempt_dir = project_dir / "attempts" / run_id
    de_path = attempt_dir / "downloads" / "extracted" / "diffexp" / "deseq2_results.tsv"
    summary_path = attempt_dir / "downloads" / "extracted" / "diffexp" / "deseq2_summary.json"
    if not de_path.is_file():
        raise AdapterExecutionError(f"real DESeq2 artifact is missing: {de_path}")
    if not summary_path.is_file():
        raise AdapterExecutionError(f"real DESeq2 summary is missing: {summary_path}")

    result = _build_result(
        inputs,
        params,
        de_path,
        summary_path,
        out_path,
        project_dir=project_dir,
        attempt_dir=attempt_dir,
        contract=contract,
        run_id=run_id,
        reference=reference,
        contrast=contrast,
        config=live_config,
        case_id=case_id,
        runtime=runtime,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run bkbio-eval counts cases through SYSU RNA-seq Agent's real DESeq2 path."
    )
    parser.add_argument("--inputs", required=True, help="bkbio-eval input directory")
    parser.add_argument("--params", required=True, help="bkbio-eval params JSON")
    parser.add_argument("--out", required=True, help="analyzer-result@1 JSON output")
    parser.add_argument("--case-id", default="", help="bkbio-eval case id")
    parser.add_argument(
        "--project-template",
        default=os.environ.get(PROJECT_TEMPLATE_ENV, ""),
        help=(
            "Optional project JSON supplying container/polling/runtime settings; "
            f"defaults to ${PROJECT_TEMPLATE_ENV}."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        result = run_case(
            Path(args.inputs),
            Path(args.params),
            Path(args.out),
            case_id=args.case_id,
            project_template=Path(args.project_template) if args.project_template else None,
        )
    except AdapterNotEvaluableError as exc:
        result = {
            "schema": RESULT_SCHEMA,
            "analyzer": ANALYZER_NAME,
            "status": "NOT_EVALUABLE",
            "reason_code": exc.reason_code,
            "message": str(exc),
        }
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except AdapterUnavailableError as exc:
        print(f"Adapter unavailable: {exc}", file=sys.stderr)
        return 3
    except AdapterInputError as exc:
        print(f"Input is not evaluable: {exc}", file=sys.stderr)
        return 4
    except AdapterExecutionError as exc:
        print(f"Analysis failed: {exc}", file=sys.stderr)
        return 5
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Adapter failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 6
    print(
        f"Completed {args.case_id or 'case'}: "
        f"{result['metrics']['n_significant']} significant genes -> {args.out}",
        file=sys.stderr,
    )
    return 0


def _scan_counts(counts_path: Path) -> tuple[list[str], list[str], dict[str, int]]:
    with counts_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        try:
            header = next(reader)
        except StopIteration as exc:
            raise AdapterInputError("counts.tsv is empty") from exc
        if len(header) < 2 or not header[0].strip():
            raise AdapterInputError("counts.tsv must contain a gene column and sample columns")
        sample_ids = [value.strip() for value in header[1:]]
        if any(not sample_id for sample_id in sample_ids):
            raise AdapterInputError("counts.tsv contains an empty sample name")
        if len(set(sample_ids)) != len(sample_ids):
            raise AdapterInputError("counts.tsv contains duplicate sample names")

        library_sizes = {sample_id: 0 for sample_id in sample_ids}
        gene_ids: list[str] = []
        seen_genes: set[str] = set()
        for line_no, row in enumerate(reader, start=2):
            if len(row) != len(header):
                raise AdapterInputError(
                    f"counts.tsv line {line_no} has {len(row)} fields; expected {len(header)}"
                )
            gene = row[0].strip()
            if not gene:
                raise AdapterInputError(f"counts.tsv line {line_no} has an empty gene id")
            if gene in seen_genes:
                raise AdapterInputError(f"counts.tsv contains duplicate gene id: {gene}")
            seen_genes.add(gene)
            gene_ids.append(gene)
            for sample_id, raw in zip(sample_ids, row[1:]):
                library_sizes[sample_id] += _raw_count(raw, line_no=line_no, sample_id=sample_id)
    if not gene_ids:
        raise AdapterInputError("counts.tsv contains no genes")
    return sample_ids, gene_ids, library_sizes


def _raw_count(raw: str, *, line_no: int, sample_id: str) -> int:
    text = raw.strip()
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise AdapterInputError(
            f"counts.tsv line {line_no}, sample {sample_id!r} is not numeric: {text!r}"
        ) from exc
    if not value.is_finite() or value < 0 or value != value.to_integral_value():
        raise AdapterInputError(
            f"counts.tsv line {line_no}, sample {sample_id!r} is not a non-negative integer: {text!r}"
        )
    return int(value)


def _read_coldata(
    coldata_path: Path,
    *,
    condition_column: str,
    pair_column: str = "",
) -> dict[str, dict[str, str]]:
    with coldata_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fields = reader.fieldnames or []
        required = {"sample", condition_column}
        if pair_column:
            required.add(pair_column)
        missing = sorted(required - set(fields))
        if missing:
            raise AdapterInputError(f"coldata.tsv is missing columns: {missing}")
        samples: dict[str, dict[str, str]] = {}
        for line_no, row in enumerate(reader, start=2):
            sample_id = str(row.get("sample") or "").strip()
            condition = str(row.get(condition_column) or "").strip()
            if not sample_id or not condition:
                raise AdapterInputError(
                    f"coldata.tsv line {line_no} requires non-empty sample and {condition_column}"
                )
            if sample_id in samples:
                raise AdapterInputError(f"coldata.tsv contains duplicate sample: {sample_id}")
            sample = {"sample_id": sample_id, "condition": condition}
            if pair_column:
                pair_id = str(row.get(pair_column) or "").strip()
                if pair_id:
                    sample["pair_id"] = pair_id
            batch = str(row.get("batch") or "").strip()
            if batch:
                sample["batch"] = batch
            samples[sample_id] = sample
    if not samples:
        raise AdapterInputError("coldata.tsv contains no samples")
    return samples


def _read_params(params_path: Path) -> dict[str, Any]:
    if not params_path.is_file():
        raise AdapterInputError(f"params JSON not found: {params_path}")
    try:
        payload = json.loads(params_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterInputError(f"could not read params JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AdapterInputError("params JSON must be an object")
    return payload


def _is_paired_request(params: Mapping[str, Any]) -> bool:
    return bool(params.get("paired", False)) or str(params.get("design_template") or "").strip() == "paired_two_group"


def _validate_supported_design(params: Mapping[str, Any]) -> None:
    requested = str(params.get("design") or "~ condition").strip()
    paired = params.get("paired", False)
    if not isinstance(paired, bool):
        raise AdapterInputError("params.paired must be a JSON boolean")
    paired_request = _is_paired_request(params)
    if paired_request:
        template = str(params.get("design_template") or "").strip()
        if template and template != "paired_two_group":
            raise AdapterNotEvaluableError("unsupported_design", "unknown paired design template")
        if not paired:
            raise AdapterNotEvaluableError("unsupported_design", "paired_two_group requires params.paired=true")
        if re.sub(r"\s+", "", requested).lower() != "~pair_id+condition":
            raise AdapterNotEvaluableError(
                "unsupported_design",
                f"paired_two_group generates {PAIRED_DESIGN_FORMULA}; arbitrary formulas are forbidden (independent fallback ~ condition is also forbidden)",
            )
    elif re.sub(r"\s+", "", requested).lower() != "~condition":
        raise AdapterNotEvaluableError(
            "unsupported_design",
            "the real SYSU RNA-seq Agent DESeq2 gate currently supports only "
            "independent, unpaired design '~ condition'; paired or multifactor designs are not "
            "silently downgraded"
        )
    min_count = params.get("min_count_prefilter", 0)
    if min_count is None:
        min_count = 0
    if isinstance(min_count, bool) or not isinstance(min_count, (int, float)):
        raise AdapterInputError("params.min_count_prefilter must be numeric")
    try:
        min_count_value = Decimal(str(min_count))
    except InvalidOperation as exc:
        raise AdapterInputError("params.min_count_prefilter must be numeric") from exc
    if not min_count_value.is_finite():
        raise AdapterInputError("params.min_count_prefilter must be finite")
    if min_count_value != 0:
        raise AdapterNotEvaluableError(
            "unsupported_design",
            "the current real counts-stage contract does not expose "
            "min_count_prefilter; refusing to ignore the requested value"
        )


def _validate_groups(
    inputs: EvalInputs,
    params: Mapping[str, Any],
    condition_column: str,
) -> tuple[str, str]:
    del condition_column  # samples are normalized to the canonical 'condition' key
    reference = str(params.get("reference_level") or "").strip()
    contrast = str(params.get("contrast_level") or "").strip()
    if not reference or not contrast or reference == contrast:
        raise AdapterInputError(
            "params.reference_level and params.contrast_level must be distinct non-empty values"
        )
    conditions = {sample["condition"] for sample in inputs.samples}
    if conditions != {reference, contrast}:
        raise AdapterInputError(
            f"coldata conditions must be exactly reference/contrast {sorted([reference, contrast])}; "
            f"found {sorted(conditions)}"
        )
    group_counts = {
        condition: sum(sample["condition"] == condition for sample in inputs.samples)
        for condition in conditions
    }
    undersized = {
        condition: count
        for condition, count in group_counts.items()
        if count < DEG_MIN_REPLICATES_PER_GROUP
    }
    if undersized:
        raise AdapterInputError(
            f"each condition requires at least {DEG_MIN_REPLICATES_PER_GROUP} biological replicates; "
            f"found {undersized}"
        )
    _threshold(params, "fdr_threshold", 0.05, minimum=0.0, maximum=1.0)
    _threshold(params, "log2fc_threshold", 1.0, minimum=0.0)
    return reference, contrast


def _validate_paired_inputs(
    inputs: EvalInputs,
    params: Mapping[str, Any],
    *,
    reference: str,
    contrast: str,
) -> None:
    samples = inputs.samples
    pair_groups: dict[str, list[dict[str, str]]] = {}
    for sample in samples:
        pair_id = str(sample.get("pair_id") or "").strip()
        if not pair_id:
            raise AdapterNotEvaluableError("unsupported_design", "paired_two_group requires a non-empty pair column")
        pair_groups.setdefault(pair_id, []).append(sample)
    mapping_count = 0
    for pair_id, rows in sorted(pair_groups.items()):
        by_condition: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            by_condition.setdefault(row["condition"], []).append(row)
        if len(rows) != 2 or len(by_condition.get(reference, [])) != 1 or len(by_condition.get(contrast, [])) != 1:
            raise AdapterNotEvaluableError(
                "unsupported_design",
                f"pair {pair_id!r} must contain exactly one reference and one contrast sample",
            )
        mapping_count += 1
    if mapping_count < 3:
        raise AdapterNotEvaluableError("unsupported_design", "paired_two_group requires at least 3 complete pairs")
    if any(str(sample.get("batch") or "").strip() for sample in samples):
        raise AdapterNotEvaluableError("unsupported_design", "paired_two_group does not support a declared batch")
    min_count = params.get("min_count_prefilter", 0)
    if min_count not in (0, 0.0, None):
        raise AdapterNotEvaluableError("unsupported_design", "paired_two_group requires min_count_prefilter=0")


def _threshold(
    params: Mapping[str, Any],
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    try:
        value = float(params.get(key, default))
    except (TypeError, ValueError) as exc:
        raise AdapterInputError(f"params.{key} must be numeric") from exc
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        suffix = f" and <= {maximum}" if maximum is not None else ""
        raise AdapterInputError(f"params.{key} must be >= {minimum}{suffix}")
    return value


def _project_id(case_id: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(case_id or "case")).strip("._-") or "case"
    stem = stem[:90]
    return f"eval_{stem}_{uuid4().hex[:8]}"


def _load_project_template(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise AdapterUnavailableError(f"project template not found: {resolved}")
    payload = load_json(resolved)
    if not isinstance(payload, dict):
        raise AdapterUnavailableError("project template must contain a JSON object")
    return payload


def _build_project_config(
    project_dir: Path,
    project_id: str,
    staged_counts: Path,
    inputs: EvalInputs,
    params: Mapping[str, Any],
    *,
    reference: str,
    connection: Mapping[str, Any],
    template: Mapping[str, Any],
) -> dict[str, Any]:
    raw_server = template.get("server", {}) if isinstance(template.get("server"), dict) else {}
    allowed_server_fields = set(SHARED_FIELDS) | {"init_commands", "key_path"}
    server = {key: deepcopy(value) for key, value in raw_server.items() if key in allowed_server_fields}
    for key in SHARED_FIELDS:
        value = connection.get(key)
        if value not in (None, "", []):
            server[key] = value
    missing_connection = [key for key in ("host", "user") if not str(server.get(key) or "").strip()]
    remote_root = str(server.get("remote_base_dir") or server.get("remote_workdir") or "").rstrip("/")
    if not remote_root:
        missing_connection.append("remote_base_dir")
    if missing_connection:
        raise AdapterUnavailableError(
            "saved server connection is incomplete (missing "
            + ", ".join(missing_connection)
            + "); configure ~/.rnaseq_agent/connection.json or pass --project-template"
        )
    server.update(
        {
            "profile": server.get("profile") or "bkbio_eval",
            "port": int(server.get("port") or 22),
            "remote_base_dir": remote_root,
            "remote_workdir": f"{remote_root}/bkbio-eval/{project_id}",
            "scheduler": server.get("scheduler") or "local",
            "threads": int(server.get("threads") or 8),
            "memory_gb": int(server.get("memory_gb") or 32),
            "shell": server.get("shell") or "bash",
            "init_commands": list(server.get("init_commands") or []),
        }
    )

    pipeline = deepcopy(DEFAULT_PIPELINE)
    for step in pipeline.values():
        step["enabled"] = False
    pipeline["diffexp"] = {
        "enabled": True,
        "version": DEFAULT_PIPELINE["diffexp"]["version"],
    }
    paired = _is_paired_request(params)
    diffexp = {
        **deepcopy(DEFAULT_DIFFEXP),
        "formula": PAIRED_DESIGN_FORMULA if paired else "~ condition",
        "reference_condition": reference,
        **({"contrast_condition": str(params.get("contrast_level") or "").strip(), "min_count_prefilter": 0} if paired else {}),
        "padj_cutoff": _threshold(params, "fdr_threshold", 0.05, minimum=0.0, maximum=1.0),
        "lfc_cutoff": _threshold(params, "log2fc_threshold", 1.0, minimum=0.0),
    }
    cms = {**deepcopy(DEFAULT_CMS), "run_mode": "counts"}
    raw_container = template.get("container")
    container = (
        {
            key: deepcopy(value)
            for key, value in raw_container.items()
            if key in _CONTAINER_TEMPLATE_FIELDS
        }
        if isinstance(raw_container, Mapping) and raw_container
        else deepcopy(DEFAULT_CONTAINER)
    )
    raw_evaluation = template.get("evaluation")
    evaluation: dict[str, Any] = {"release_mode": False}
    if isinstance(raw_evaluation, Mapping):
        evaluation.update(
            {
                key: deepcopy(value)
                for key, value in raw_evaluation.items()
                if key in _EVALUATION_TEMPLATE_FIELDS
            }
        )

    config: dict[str, Any] = {
        "schema_version": 1,
        "project": {"id": project_id, "title": f"bkbio-eval {project_id}", "owner": "bkbio-eval"},
        "study": {
            "cancer_type": "pan_cancer",
            "design": "paired_two_group" if paired else "independent_two_group",
        },
        "server": server,
        "reference": deepcopy(template.get("reference") or DEFAULT_REFERENCE),
        "sequencing": {"layout": "paired", "reads_per_sample_million": 0, "strandedness": "unknown"},
        "samples": {
            "source": "counts_upload",
            "local_data_dir": str(staged_counts.parent),
            "remote_data_dir": "AUTO",
            "counts_path": str(staged_counts),
            "items": [dict(sample) for sample in inputs.samples],
        },
        "pipeline": pipeline,
        "diffexp": diffexp,
        "cms": cms,
        "container": container,
        "execution": {"mode": "free", "skill_id": "", "contract_file": "analysis_contract.json"},
        "polling": deepcopy(template.get("polling") or {"interval_seconds": 5, "timeout_hours": 4}),
        "notification": {"email_enabled": False},
        "evaluation": evaluation,
    }
    return config


def _install_runtime_credential(
    config: Mapping[str, Any],
    connection: Mapping[str, Any],
) -> tuple[str, str, Any, Lock] | None:
    server = config["server"]
    host = str(server.get("host") or "")
    user = str(server.get("user") or "")
    mode = normalize_auth_mode(
        str(connection.get("auth_mode") or server.get("auth_mode") or "key")
    )
    identity = (host.strip(), user.strip())
    with _CREDENTIAL_LOCKS_GUARD:
        credential_lock = _CREDENTIAL_LOCKS.setdefault(identity, Lock())
    credential_lock.acquire()
    try:
        previous = get_ssh_credential(host, user)
        password = str(connection.get("password") or "")
        if mode == "password":
            if not password:
                raise AdapterUnavailableError(
                    f"password authentication is configured for {user}@{host}, "
                    "but no decrypted password is available"
                )
            set_ssh_credential(host, user, mode="password", password=password)
            return host, user, previous, credential_lock

        key_path = str(connection.get("key_path") or server.get("key_path") or "")
        set_ssh_credential(host, user, mode=mode, key_path=key_path)
        return host, user, previous, credential_lock
    except Exception:
        credential_lock.release()
        raise


def _restore_runtime_credential(
    config: Mapping[str, Any],
    credential: tuple[str, str, Any, Lock] | None,
) -> None:
    del config
    if credential is None:
        return
    host, user, previous, credential_lock = credential
    try:
        set_ssh_credential(
            host,
            user,
            mode=previous.mode,
            password=previous.password,
            key_path=previous.key_path,
        )
    finally:
        credential_lock.release()


def _ensure_runtime_available(config: Mapping[str, Any]) -> dict[str, str]:
    """Select and identify the one configured remote DESeq2/jsonlite runtime."""
    config_dict = dict(config)
    container = container_config(config_dict)
    release_mode = bool(config.get("evaluation", {}).get("release_mode", False))
    raw_container = config.get("container", {})
    declared_digest = ""
    if isinstance(raw_container, Mapping):
        declared_digest = str(
            raw_container.get("digest") or raw_container.get("image_digest") or ""
        ).strip()

    if release_mode:
        if not container["enabled"]:
            raise AdapterUnavailableError(
                "release mode requires an enabled container with a verifiable digest"
            )
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", declared_digest):
            raise AdapterUnavailableError(
                "release mode requires container.digest in sha256:<64 hex> form"
            )

    init_commands = [
        str(command).strip()
        for command in config.get("server", {}).get("init_commands", [])
        if str(command).strip()
    ]
    try:
        transport = create_remote_transport(config_dict)
        if release_mode:
            image = shell_quote(container["image_path"])
            digest_result = transport.execute(
                "; ".join(["set -e", *init_commands, f"sha256sum {image}"])
            )
            actual_digest = digest_result.stdout.strip().split(maxsplit=1)[0].lower()
            if (
                digest_result.returncode != 0
                or actual_digest != declared_digest.removeprefix("sha256:").lower()
            ):
                raise AdapterUnavailableError(
                    "configured container digest could not be verified against the remote image"
                )

        package_check = (
            "requireNamespace('DESeq2', quietly=TRUE) && "
            "requireNamespace('jsonlite', quietly=TRUE)"
        )
        expected_mode = "container" if container["enabled"] else "native"
        r_expression = (
            f"if (!({package_check})) quit(status=1); "
            f'cat(paste0("mode={expected_mode}|R=", as.character(getRversion()), '
            '"|DESeq2=", as.character(packageVersion("DESeq2")), '
            '"|jsonlite=", as.character(packageVersion("jsonlite")), "\\n"))'
        )
        if container["enabled"]:
            engine = shell_quote(container["engine"])
            image = shell_quote(container["image_path"])
            probe = (
                f"command -v {engine} >/dev/null 2>&1 && [ -r {image} ] && "
                f"{wrap_command(config_dict, 'Rscript')} -e {shell_quote(r_expression)}"
            )
        else:
            probe = (
                "command -v Rscript >/dev/null 2>&1 && "
                f"Rscript -e {shell_quote(r_expression)}"
            )
        result = transport.execute("; ".join(["set -e", *init_commands, probe]))
    except AdapterUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize transport failures for the adapter CLI
        raise AdapterUnavailableError(
            f"could not probe the configured remote DESeq2 runtime: {type(exc).__name__}: {exc}"
        ) from exc

    match = re.search(
        r"^mode=(native|container)\|R=([^|\r\n]+)\|DESeq2=([^|\r\n]+)\|jsonlite=([^|\r\n]+)$",
        result.stdout,
        flags=re.MULTILINE,
    )
    if result.returncode != 0 or match is None or match.group(1) != expected_mode:
        detail = (
            result.stdout.strip()
            or result.stderr.strip()
            or f"mode={expected_mode} unavailable"
        )
        raise AdapterUnavailableError(
            "remote DESeq2/jsonlite runtime is unavailable or unverifiable "
            f"({detail})"
        )
    return {
        "mode": match.group(1),
        "r_version": match.group(2),
        "deseq2_version": match.group(3),
        "jsonlite_version": match.group(4),
    }


def _build_result(
    inputs: EvalInputs,
    params: Mapping[str, Any],
    de_path: Path,
    summary_path: Path,
    out_path: Path,
    *,
    project_dir: Path,
    attempt_dir: Path,
    contract: Mapping[str, Any],
    run_id: str,
    reference: str,
    contrast: str,
    config: Mapping[str, Any],
    case_id: str,
    runtime: Mapping[str, str],
) -> dict[str, Any]:
    try:
        summary = load_json(summary_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterExecutionError(f"could not read DESeq2 summary: {exc}") from exc
    if not isinstance(summary, dict):
        raise AdapterExecutionError("DESeq2 summary must contain a JSON object")
    _verify_summary(summary, config, reference=reference, contrast=contrast)
    staged_counts = Path(str(config.get("samples", {}).get("counts_path") or "")).resolve()
    if not staged_counts.is_file():
        raise AdapterExecutionError(f"staged counts input is missing: {staged_counts}")
    run_manifest, result_manifest, input_counts_sha256 = _verify_manifests(
        attempt_dir,
        run_id,
        de_path,
        staged_counts=staged_counts,
        contract=contract,
    )

    table, row_order = _read_deseq2_results(de_path)
    expected_genes = set(inputs.gene_ids)
    actual_genes = set(table)
    if actual_genes != expected_genes:
        raise AdapterExecutionError(
            "DESeq2 artifact gene set differs from counts input; "
            f"missing={len(expected_genes - actual_genes)}, extra={len(actual_genes - expected_genes)}"
        )

    fdr = _threshold(params, "fdr_threshold", 0.05, minimum=0.0, maximum=1.0)
    lfc = _threshold(params, "log2fc_threshold", 1.0, minimum=0.0)
    significant: list[str] = []
    up: list[str] = []
    down: list[str] = []
    for gene in row_order:
        row = table[gene]
        padj = row["padj"]
        log2fc = row["log2FoldChange"]
        if padj is None or log2fc is None or padj >= fdr or abs(log2fc) < lfc:
            continue
        significant.append(gene)
        if log2fc > 0:
            up.append(gene)
        elif log2fc < 0:
            down.append(gene)
    significant.sort()
    up.sort()
    down.sort()

    by_condition: dict[str, list[str]] = {}
    for sample in inputs.samples:
        by_condition.setdefault(sample["condition"], []).append(sample["sample_id"])
    reference_mean = sum(inputs.library_sizes[s] for s in by_condition[reference]) / len(
        by_condition[reference]
    )
    contrast_mean = sum(inputs.library_sizes[s] for s in by_condition[contrast]) / len(
        by_condition[contrast]
    )

    top_gene = next((gene for gene in row_order if table[gene]["padj"] is not None), None)
    artifact_rel = de_path.resolve().relative_to(out_path.parent.resolve()).as_posix()
    container = dict(config.get("container", {}))
    container_provenance = {
        "enabled": bool(container.get("enabled")),
        "engine": container.get("engine", ""),
        "image_uri": container.get("image_uri", ""),
        "image_path": container.get("image_path", ""),
        "digest": container.get("digest") or container.get("image_digest") or None,
        "config_sha256": canonical_sha256(container),
    }
    requested_design = str(
        params.get("design") or (PAIRED_DESIGN_FORMULA if _is_paired_request(params) else "~ condition")
    ).strip()
    created_at = str(contract.get("created_at") or "")
    release_mode = bool(config.get("evaluation", {}).get("release_mode", False))

    return {
        "schema": RESULT_SCHEMA,
        "analyzer": ANALYZER_NAME,
        "params": {
            "condition_column": str(params.get("condition_column") or "condition"),
            "reference_level": reference,
            "contrast_level": contrast,
            "fdr_threshold": fdr,
            "log2fc_threshold": lfc,
            "design": requested_design,
            "paired": _is_paired_request(params),
        },
        "metrics": {
            "n_genes": inputs.n_genes,
            "n_samples": inputs.n_samples,
            "n_significant": len(significant),
            "n_up": len(up),
            "n_down": len(down),
            "library_size_min": float(min(inputs.library_sizes.values())),
            "library_size_max": float(max(inputs.library_sizes.values())),
            "library_size_ratio": contrast_mean / reference_mean if reference_mean else None,
        },
        "de_results": {
            "significant_genes": significant,
            "up_genes": up,
            "down_genes": down,
            "top_gene_by_padj": top_gene,
        },
        "tables": {"deseq2_results": table},
        "artifacts": {"deseq2_results": artifact_rel},
        "params_history": [
            {"step": "bkbio_eval_input", "params": dict(params), "at": created_at},
            {
                "step": "analysis_contract",
                "params": {
                    "contract_id": contract.get("contract_id", ""),
                    "formula": summary.get("formula", "~ condition"),
                    "contrast": summary.get("contrast", f"{contrast}_vs_{reference}"),
                },
                "at": created_at,
            },
        ],
        "provenance": {
            "case_id": case_id,
            "project_dir": str(project_dir.resolve()),
            "run_id": run_id,
            "contract_id": contract.get("contract_id", ""),
            "agent_version": _agent_version(),
            "git_commit": _git_commit(),
            "container": container_provenance,
            "runtime": dict(runtime),
            "requested_design": requested_design,
            "executed_design": summary.get("formula", "~ condition"),
            "input_counts_sha256": input_counts_sha256,
            "deseq2_results_sha256": sha256_file(de_path),
            "run_manifest_id": run_manifest.get("manifest_id", ""),
            "result_manifest_id": result_manifest.get("manifest_id", ""),
            "limitations": [] if release_mode else ["smoke/non_release"],
        },
    }


def _read_deseq2_results(de_path: Path) -> tuple[dict[str, dict[str, float | None]], list[str]]:
    required = ("gene", "baseMean", "log2FoldChange", "lfcSE", "stat", "pvalue", "padj")
    table: dict[str, dict[str, float | None]] = {}
    order: list[str] = []
    with de_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        fields = reader.fieldnames or []
        missing = [column for column in required if column not in fields]
        if missing:
            raise AdapterExecutionError(f"DESeq2 artifact is missing columns: {missing}")
        for line_no, raw in enumerate(reader, start=2):
            gene = str(raw.get("gene") or "").strip()
            if not gene:
                raise AdapterExecutionError(f"DESeq2 artifact line {line_no} has an empty gene id")
            if gene in table:
                raise AdapterExecutionError(f"DESeq2 artifact contains duplicate gene id: {gene}")
            table[gene] = {
                column: _optional_float(raw.get(column), column=column, line_no=line_no)
                for column in required[1:]
            }
            order.append(gene)
    if not order:
        raise AdapterExecutionError("DESeq2 artifact contains no result rows")
    return table, order


def _optional_float(raw: Any, *, column: str, line_no: int) -> float | None:
    text = str(raw or "").strip()
    if not text or text.upper() in {"NA", "NAN", "NULL"}:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise AdapterExecutionError(
            f"DESeq2 artifact line {line_no}, column {column} is not numeric: {text!r}"
        ) from exc
    return value if math.isfinite(value) else None


def _verify_summary(
    summary: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    reference: str,
    contrast: str,
) -> None:
    diffexp = config.get("diffexp", {})
    if not isinstance(diffexp, Mapping):
        raise AdapterExecutionError("frozen diffexp configuration is invalid")
    paired = str(config.get("study", {}).get("design", "")).strip() == "paired_two_group"
    expected_formula = PAIRED_DESIGN_FORMULA if paired else "~ condition"
    if diffexp.get("formula") != expected_formula:
        raise AdapterExecutionError(
            f"frozen diffexp formula must be exactly {expected_formula!r}"
        )
    configured_reference = diffexp.get("reference_condition")
    if configured_reference != reference:
        raise AdapterExecutionError(
            "frozen diffexp reference_condition differs from the evaluator request"
        )
    expected = {
        "formula": expected_formula,
        "reference_condition": reference,
        "treatment_condition": contrast,
        "contrast": f"{contrast}_vs_{reference}",
        "padj_cutoff": diffexp.get("padj_cutoff"),
        "log2fc_cutoff": diffexp.get("lfc_cutoff"),
    }
    for field, expected_value in expected.items():
        if field not in summary or summary[field] != expected_value:
            raise AdapterExecutionError(
                f"DESeq2 summary {field} does not match the frozen analysis contract: "
                f"expected {expected_value!r}, found {summary.get(field)!r}"
            )
    if paired:
        if summary.get("template") not in (None, "deseq2_paired_two_group"):
            raise AdapterExecutionError("DESeq2 summary template does not match paired_two_group")


def _verify_manifests(
    attempt_dir: Path,
    run_id: str,
    de_path: Path,
    *,
    staged_counts: Path,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    run_manifest = _load_required_manifest(attempt_dir / "run_manifest.json")
    result_manifest = _load_required_manifest(attempt_dir / "result_manifest.json")
    run_body = run_manifest["body"]
    result_body = result_manifest["body"]
    for filename, body in (
        ("run_manifest.json", run_body),
        ("result_manifest.json", result_body),
    ):
        if body.get("run_id") != run_id:
            raise AdapterExecutionError(
                f"{filename} run_id does not match completed attempt {run_id!r}"
            )

    snapshot_path = attempt_dir / "project.snapshot.json"
    if not snapshot_path.is_file():
        raise AdapterExecutionError(f"project snapshot is missing: {snapshot_path}")
    run_fingerprints = run_body.get("fingerprints")
    if not isinstance(run_fingerprints, dict):
        raise AdapterExecutionError("run_manifest.json fingerprints must be an object")
    if run_fingerprints.get("project_snapshot_sha256") != sha256_file(snapshot_path):
        raise AdapterExecutionError(
            "run_manifest.json project_snapshot_sha256 does not match project.snapshot.json"
        )
    input_counts_sha256 = _verify_input_evidence(
        staged_counts=staged_counts,
        contract=contract,
        run_body=run_body,
    )

    files = result_body.get("files")
    result_fingerprints = result_body.get("fingerprints")
    if not isinstance(files, list) or not isinstance(result_fingerprints, dict):
        raise AdapterExecutionError(
            "result_manifest.json requires files and fingerprints"
        )
    if result_fingerprints.get("files_sha256") != canonical_sha256(files):
        raise AdapterExecutionError(
            "result_manifest.json files_sha256 does not match its files inventory"
        )
    de_entries = [
        item
        for item in files
        if isinstance(item, dict) and item.get("path") == "diffexp/deseq2_results.tsv"
    ]
    if len(de_entries) != 1:
        raise AdapterExecutionError(
            "result_manifest.json must inventory diffexp/deseq2_results.tsv exactly once"
        )
    if de_entries[0].get("sha256") != sha256_file(de_path):
        raise AdapterExecutionError(
            "result_manifest.json DESeq2 result hash does not match the downloaded artifact"
        )
    return run_manifest, result_manifest, input_counts_sha256


def _verify_input_evidence(
    *,
    staged_counts: Path,
    contract: Mapping[str, Any],
    run_body: Mapping[str, Any],
) -> str:
    contract_body = contract.get("body")
    if contract.get("schema_version") != 1 or not isinstance(contract_body, dict):
        raise AdapterExecutionError("Analysis Contract schema/body is invalid")
    if contract.get("contract_id") != f"sha256:{canonical_sha256(contract_body)}":
        raise AdapterExecutionError("Analysis Contract ID is not recomputable")

    contract_inputs = contract_body.get("inputs")
    contract_fingerprints = contract_body.get("fingerprints")
    run_inputs = run_body.get("inputs")
    run_fingerprints = run_body.get("fingerprints")
    if not isinstance(contract_inputs, list) or not isinstance(contract_fingerprints, dict):
        raise AdapterExecutionError("Analysis Contract inputs evidence is invalid")
    if not isinstance(run_inputs, list) or not isinstance(run_fingerprints, dict):
        raise AdapterExecutionError("run_manifest.json inputs evidence is invalid")
    if contract_fingerprints.get("inputs_sha256") != canonical_sha256(contract_inputs):
        raise AdapterExecutionError("Analysis Contract inputs_sha256 is not recomputable")
    if run_fingerprints.get("inputs_sha256") != canonical_sha256(run_inputs):
        raise AdapterExecutionError("run_manifest.json inputs_sha256 is not recomputable")
    if run_inputs != contract_inputs:
        raise AdapterExecutionError(
            "run_manifest.json inputs differ from the approved Analysis Contract inputs"
        )

    counts_entries = [
        item
        for item in contract_inputs
        if isinstance(item, dict) and item.get("role") == "counts"
    ]
    if len(counts_entries) != 1:
        raise AdapterExecutionError(
            "Analysis Contract must contain exactly one counts input artifact"
        )
    staged_hash = sha256_file(staged_counts)
    counts_entry = counts_entries[0]
    if counts_entry.get("sha256") != staged_hash:
        raise AdapterExecutionError(
            "staged counts hash differs from the Analysis Contract and run manifest"
        )
    if counts_entry.get("size_bytes") != staged_counts.stat().st_size:
        raise AdapterExecutionError(
            "staged counts size differs from the Analysis Contract and run manifest"
        )
    if counts_entry.get("logical_name") != staged_counts.name:
        raise AdapterExecutionError(
            "staged counts name differs from the Analysis Contract and run manifest"
        )
    return staged_hash


def _load_required_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AdapterExecutionError(f"required manifest is missing: {path.name}")
    try:
        payload = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterExecutionError(f"could not read {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AdapterExecutionError(f"{path.name} must contain a JSON object")
    if payload.get("schema_version") != 1:
        raise AdapterExecutionError(f"{path.name} has an unsupported schema_version")
    body = payload.get("body")
    if not isinstance(body, dict):
        raise AdapterExecutionError(f"{path.name} body must be an object")
    expected_id = f"sha256:{canonical_sha256(body)}"
    if payload.get("manifest_id") != expected_id:
        raise AdapterExecutionError(f"{path.name} manifest_id is not recomputable")
    return payload


def _agent_version() -> str:
    try:
        return metadata.version("sysu-rnaseq-agent")
    except metadata.PackageNotFoundError:
        return __version__


def _git_commit() -> str:
    repository = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
