"""Zero-dependency ERCC truth diagnostics for featureCounts matrices.

This module intentionally performs a small, descriptive CPM comparison only.  It
does not estimate dispersion, fit a differential-expression model, or calculate
p-values, and therefore does not replace DESeq2, edgeR, or a comparable method.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


CONTRAST = {
    "treatment": "UHR_Mix1",
    "control": "HBR_Mix2",
    "direction": "UHR_Mix1_over_HBR_Mix2",
}
DISCLAIMER = (
    "This is a small-sample CPM truth diagnostic, not a differential-expression "
    "test. It does not replace DESeq2/edgeR, and it computes no p-values."
)
_ERCC_RE = re.compile(r"(?i)(?<![A-Z0-9])ERCC[-_:]?(\d{5})(?!\d)")
_FEATURECOUNTS_METADATA = {
    "geneid",
    "chr",
    "start",
    "end",
    "strand",
    "length",
}


class ERCCMetricsError(ValueError):
    """Raised when a matrix, truth table, or sample mapping is unsafe to use."""


@dataclass(frozen=True)
class CountMatrix:
    samples: tuple[str, ...]
    library_sizes: dict[str, float]
    ercc_counts: dict[str, dict[str, float]]


@dataclass(frozen=True)
class TruthRecord:
    ercc_id: str
    subgroup: str
    expected_log2_mix1_over_mix2: float


def _normalized_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.strip().casefold())


def _parse_ercc_id(value: str) -> str | None:
    match = _ERCC_RE.search(value.strip())
    return f"ERCC-{match.group(1)}" if match else None


def _read_tsv_rows(path: Path, *, comments: bool) -> list[list[str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = []
            for row in csv.reader(handle, delimiter="\t"):
                if not row or not any(cell.strip() for cell in row):
                    continue
                if comments and row[0].lstrip().startswith("#"):
                    continue
                rows.append(row)
    except OSError as exc:
        raise ERCCMetricsError(f"Cannot read {path}: {exc}") from exc
    if not rows:
        raise ERCCMetricsError(f"TSV file is empty: {path}")
    return rows


def read_featurecounts(path: str | Path) -> CountMatrix:
    """Read a standard featureCounts ``gene_counts.txt`` matrix."""

    source = Path(path)
    rows = _read_tsv_rows(source, comments=True)
    header = [cell.strip() for cell in rows[0]]
    normalized = [_normalized_header(cell) for cell in header]
    if not header or normalized[0] != "geneid":
        raise ERCCMetricsError(
            "featureCounts header must start with 'Geneid'; received "
            f"{header[0]!r} in {source}"
        )

    sample_indexes = [
        index
        for index, name in enumerate(normalized)
        if index > 0 and name not in _FEATURECOUNTS_METADATA
    ]
    if not sample_indexes:
        raise ERCCMetricsError("featureCounts matrix contains no sample columns")
    samples = tuple(header[index] for index in sample_indexes)
    if any(not name for name in samples) or len(set(samples)) != len(samples):
        raise ERCCMetricsError("featureCounts sample column names must be non-empty and unique")

    library_sizes = {sample: 0.0 for sample in samples}
    ercc_counts: dict[str, dict[str, float]] = {}
    for line_number, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            raise ERCCMetricsError(
                f"featureCounts row {line_number} has {len(row)} fields; "
                f"expected {len(header)}"
            )
        values: dict[str, float] = {}
        for sample, index in zip(samples, sample_indexes):
            try:
                value = float(row[index].strip())
            except ValueError as exc:
                raise ERCCMetricsError(
                    f"Invalid count at row {line_number}, sample {sample!r}: "
                    f"{row[index]!r}"
                ) from exc
            if not math.isfinite(value) or value < 0:
                raise ERCCMetricsError(
                    f"Counts must be finite and non-negative; row {line_number}, "
                    f"sample {sample!r} is {value!r}"
                )
            library_sizes[sample] += value
            values[sample] = value

        ercc_id = _parse_ercc_id(row[0])
        if ercc_id:
            accumulated = ercc_counts.setdefault(
                ercc_id, {sample: 0.0 for sample in samples}
            )
            for sample, value in values.items():
                accumulated[sample] += value

    empty_libraries = [sample for sample, size in library_sizes.items() if size <= 0]
    if empty_libraries:
        raise ERCCMetricsError(
            "Cannot calculate CPM for zero-size libraries: " + ", ".join(empty_libraries)
        )
    return CountMatrix(samples, library_sizes, ercc_counts)


def _find_truth_columns(header: Sequence[str]) -> tuple[int, int | None, int]:
    normalized = [_normalized_header(value) for value in header]

    ercc_index = next(
        (i for i, value in enumerate(normalized) if value == "erccid"), None
    )
    subgroup_index = next(
        (i for i, value in enumerate(normalized) if "subgroup" in value), None
    )
    log2_index = next(
        (
            i
            for i, value in enumerate(normalized)
            if "log2" in value
            and (
                ("mix1" in value and "mix2" in value)
                or "expected" in value
                or "truth" in value
            )
        ),
        None,
    )
    fold_index = next(
        (
            i
            for i, value in enumerate(normalized)
            if "foldchange" in value and ("expected" in value or "mix1" in value)
        ),
        None,
    )

    # The official Griffith/Thermo Fisher table has seven columns in this order.
    if ercc_index is None and len(header) >= 7:
        ercc_index = 1
    if subgroup_index is None and len(header) >= 7:
        subgroup_index = 2
    if log2_index is None and fold_index is None and len(header) >= 7:
        log2_index = 6

    if ercc_index is None:
        raise ERCCMetricsError("Truth TSV has no recognizable ERCC ID column")
    if log2_index is not None:
        return ercc_index, subgroup_index, log2_index
    if fold_index is not None:
        return ercc_index, subgroup_index, -fold_index - 1
    raise ERCCMetricsError(
        "Truth TSV needs expected log2(Mix 1/Mix 2) or expected fold-change ratio"
    )


def read_ercc_truth(path: str | Path) -> list[TruthRecord]:
    """Read the Griffith ERCC truth TSV (or a header-compatible subset)."""

    source = Path(path)
    rows = _read_tsv_rows(source, comments=False)
    header = [cell.strip() for cell in rows[0]]
    ercc_index, subgroup_index, value_marker = _find_truth_columns(header)
    is_fold_change = value_marker < 0
    value_index = -value_marker - 1 if is_fold_change else value_marker
    required_index = max(
        ercc_index,
        value_index,
        subgroup_index if subgroup_index is not None else 0,
    )

    records: list[TruthRecord] = []
    seen: set[str] = set()
    for line_number, row in enumerate(rows[1:], start=2):
        if row[0].lstrip().startswith("#"):
            continue
        if len(row) <= required_index:
            raise ERCCMetricsError(
                f"Truth row {line_number} has too few fields for the selected columns"
            )
        ercc_id = _parse_ercc_id(row[ercc_index])
        if ercc_id is None:
            raise ERCCMetricsError(
                f"Truth row {line_number} has an invalid ERCC ID: {row[ercc_index]!r}"
            )
        if ercc_id in seen:
            raise ERCCMetricsError(f"Duplicate truth ERCC ID: {ercc_id}")
        try:
            raw_value = float(row[value_index].strip())
            expected_log2 = math.log2(raw_value) if is_fold_change else raw_value
        except (ValueError, OverflowError) as exc:
            raise ERCCMetricsError(
                f"Truth row {line_number} has an invalid expected ratio: "
                f"{row[value_index]!r}"
            ) from exc
        if not math.isfinite(expected_log2) or (is_fold_change and raw_value <= 0):
            raise ERCCMetricsError(
                f"Truth row {line_number} expected ratio must be finite and positive"
            )
        subgroup = (
            row[subgroup_index].strip() if subgroup_index is not None else "unassigned"
        ) or "unassigned"
        records.append(TruthRecord(ercc_id, subgroup, expected_log2))
        seen.add(ercc_id)

    if not records:
        raise ERCCMetricsError("Truth TSV contains no ERCC records")
    return records


def _expand_prefixes(values: Iterable[str] | None, default: str) -> tuple[str, ...]:
    prefixes: list[str] = []
    for value in values or (default,):
        prefixes.extend(part.strip() for part in value.split(",") if part.strip())
    if not prefixes:
        raise ERCCMetricsError("Sample prefix list cannot be empty")
    return tuple(prefixes)


def _matches_prefix(sample: str, prefixes: Sequence[str]) -> bool:
    normalized = sample.replace("\\", "/")
    components = [part.casefold() for part in normalized.split("/") if part]
    full = normalized.casefold()
    for prefix in prefixes:
        candidate = prefix.casefold()
        if full.startswith(candidate) or any(part.startswith(candidate) for part in components):
            return True
    return False


def map_sample_groups(
    samples: Sequence[str],
    control_prefixes: Iterable[str] | None = None,
    treatment_prefixes: Iterable[str] | None = None,
) -> tuple[list[str], list[str], tuple[str, ...], tuple[str, ...]]:
    """Map all matrix columns to HBR/control or UHR/treatment, without guessing."""

    control_rules = _expand_prefixes(control_prefixes, "HBR")
    treatment_rules = _expand_prefixes(treatment_prefixes, "UHR")
    control: list[str] = []
    treatment: list[str] = []
    unmapped: list[str] = []
    overlap: list[str] = []
    for sample in samples:
        is_control = _matches_prefix(sample, control_rules)
        is_treatment = _matches_prefix(sample, treatment_rules)
        if is_control and is_treatment:
            overlap.append(sample)
        elif is_control:
            control.append(sample)
        elif is_treatment:
            treatment.append(sample)
        else:
            unmapped.append(sample)

    if overlap:
        raise ERCCMetricsError(
            "Sample columns match both groups: " + ", ".join(overlap)
        )
    if unmapped:
        raise ERCCMetricsError(
            "Unmapped sample columns: "
            + ", ".join(unmapped)
            + ". Supply explicit --control-prefix/--treatment-prefix values."
        )
    if not control or not treatment:
        raise ERCCMetricsError(
            "Both groups require at least one sample; mapped control="
            f"{control!r}, treatment={treatment!r}"
        )
    return control, treatment, control_rules, treatment_rules


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    mean_left = statistics.fmean(left)
    mean_right = statistics.fmean(right)
    centered_left = [value - mean_left for value in left]
    centered_right = [value - mean_right for value in right]
    denominator = math.sqrt(
        sum(value * value for value in centered_left)
        * sum(value * value for value in centered_right)
    )
    if denominator == 0:
        return None
    return sum(a * b for a, b in zip(centered_left, centered_right)) / denominator


def _average_ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        average_rank = ((start + 1) + end) / 2.0
        for position in range(start, end):
            ranks[ordered[position][0]] = average_rank
        start = end
    return ranks


def _metric_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    expected = [float(row["expected_log2_mix1_over_mix2"]) for row in rows]
    observed = [float(row["observed_log2_uhr_over_hbr"]) for row in rows]
    errors = [actual - truth for actual, truth in zip(observed, expected)]
    direction_pairs = [
        (truth, actual)
        for truth, actual in zip(expected, observed)
        if abs(truth) > 1e-12
    ]
    direction_correct = sum(
        1
        for truth, actual in direction_pairs
        if (truth > 0 and actual > 0) or (truth < 0 and actual < 0)
    )
    count = len(rows)
    return {
        "n": count,
        "pearson": _pearson(expected, observed),
        "spearman": _pearson(_average_ranks(expected), _average_ranks(observed))
        if count >= 2
        else None,
        "mae": statistics.fmean(abs(error) for error in errors) if errors else None,
        "rmse": math.sqrt(statistics.fmean(error * error for error in errors))
        if errors
        else None,
        "direction_n": len(direction_pairs),
        "direction_correct": direction_correct,
        "direction_accuracy": direction_correct / len(direction_pairs)
        if direction_pairs
        else None,
    }


def _coverage(total: int, matched: int, detected: int) -> dict[str, Any]:
    return {
        "truth_ercc_total": total,
        "matched_in_count_matrix": matched,
        "matrix_match_rate": matched / total if total else None,
        "detected_both_groups": detected,
        "detection_coverage": detected / total if total else None,
    }


def _input_artifact(path: str | Path) -> dict[str, Any]:
    """Return an auditable identity record for one local input file."""

    supplied = Path(path)
    try:
        resolved = supplied.resolve(strict=True)
        digest = hashlib.sha256()
        size_bytes = 0
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size_bytes += len(chunk)
    except OSError as exc:
        raise ERCCMetricsError(f"Cannot fingerprint input {supplied}: {exc}") from exc
    return {
        "file_name": resolved.name,
        "absolute_path_saved": False,
        "size_bytes": size_bytes,
        "sha256": digest.hexdigest(),
    }


def compute_ercc_metrics(
    counts_path: str | Path,
    truth_path: str | Path,
    *,
    control_prefixes: Iterable[str] | None = None,
    treatment_prefixes: Iterable[str] | None = None,
    pseudocount: float = 0.5,
) -> dict[str, Any]:
    """Calculate CPM-based ERCC truth agreement for the fixed UHR/HBR contrast."""

    if not math.isfinite(pseudocount) or pseudocount < 0:
        raise ERCCMetricsError("pseudocount must be finite and non-negative")
    matrix = read_featurecounts(counts_path)
    truth = read_ercc_truth(truth_path)
    control, treatment, control_rules, treatment_rules = map_sample_groups(
        matrix.samples, control_prefixes, treatment_prefixes
    )

    result_rows: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    for record in truth:
        raw_counts = matrix.ercc_counts.get(
            record.ercc_id, {sample: 0.0 for sample in matrix.samples}
        )
        sample_cpm = {
            sample: raw_counts.get(sample, 0.0) / matrix.library_sizes[sample] * 1_000_000.0
            for sample in matrix.samples
        }
        control_mean = statistics.fmean(sample_cpm[sample] for sample in control)
        treatment_mean = statistics.fmean(sample_cpm[sample] for sample in treatment)
        detected_control = control_mean > 0
        detected_treatment = treatment_mean > 0
        detected_both = detected_control and detected_treatment
        if detected_both:
            observed = math.log2(
                (treatment_mean + pseudocount) / (control_mean + pseudocount)
            )
        else:
            observed = None
        row = {
            "ercc_id": record.ercc_id,
            "subgroup": record.subgroup,
            "expected_log2_mix1_over_mix2": record.expected_log2_mix1_over_mix2,
            "sample_cpm": sample_cpm,
            "group_mean_cpm": {
                "HBR_Mix2": control_mean,
                "UHR_Mix1": treatment_mean,
            },
            "present_in_count_matrix": record.ercc_id in matrix.ercc_counts,
            "detected_control": detected_control,
            "detected_treatment": detected_treatment,
            "detected_both_groups": detected_both,
            "observed_log2_uhr_over_hbr": observed,
        }
        result_rows.append(row)
        if detected_both:
            metric_rows.append(row)

    subgroup_reports: dict[str, Any] = {}
    for subgroup in sorted({record.subgroup for record in truth}):
        subgroup_all = [row for row in result_rows if row["subgroup"] == subgroup]
        subgroup_metric = [row for row in subgroup_all if row["detected_both_groups"]]
        subgroup_reports[subgroup] = {
            "coverage": _coverage(
                len(subgroup_all),
                sum(bool(row["present_in_count_matrix"]) for row in subgroup_all),
                len(subgroup_metric),
            ),
            "metrics": _metric_summary(subgroup_metric),
        }

    return {
        "schema_version": 1,
        "analysis_type": "ercc_cpm_truth_diagnostic",
        "contrast": {
            **CONTRAST,
            "expected_definition": "log2(ERCC Mix 1 / ERCC Mix 2)",
            "observed_definition": (
                "log2((mean UHR CPM + pseudocount) / "
                "(mean HBR CPM + pseudocount))"
            ),
        },
        "method": {
            "normalization": "CPM from each sample's total featureCounts library size",
            "pseudocount_cpm": pseudocount,
            "detection_rule": "group mean CPM > 0 in both groups",
            "direction_rule": "sign agreement for non-zero expected log2 ratios",
            "disclaimer": DISCLAIMER,
        },
        "inputs": {
            "featurecounts": _input_artifact(counts_path),
            "truth_tsv": _input_artifact(truth_path),
        },
        "samples": {
            "control": control,
            "treatment": treatment,
            "control_prefixes": list(control_rules),
            "treatment_prefixes": list(treatment_rules),
            "library_sizes": matrix.library_sizes,
        },
        "coverage": _coverage(
            len(result_rows),
            sum(bool(row["present_in_count_matrix"]) for row in result_rows),
            len(metric_rows),
        ),
        "metrics": _metric_summary(metric_rows),
        "subgroups": subgroup_reports,
        "ercc": result_rows,
    }


def _display(value: Any, digits: int = 6) -> str:
    if value is None:
        return "NA"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    """Render a complete human-readable diagnostic without external packages."""

    coverage = report["coverage"]
    metrics = report["metrics"]
    samples = report["samples"]
    lines = [
        "# ERCC CPM truth diagnostic",
        "",
        f"> **Scope:** {report['method']['disclaimer']}",
        "",
        "## Input audit",
        "",
        "| Input | File name | Size (bytes) | SHA-256 |",
        "|---|---|---:|---|",
        "| featureCounts | "
        + report["inputs"]["featurecounts"]["file_name"]
        + " | "
        + str(report["inputs"]["featurecounts"]["size_bytes"])
        + " | `"
        + report["inputs"]["featurecounts"]["sha256"]
        + "` |",
        "| ERCC truth TSV | "
        + report["inputs"]["truth_tsv"]["file_name"]
        + " | "
        + str(report["inputs"]["truth_tsv"]["size_bytes"])
        + " | `"
        + report["inputs"]["truth_tsv"]["sha256"]
        + "` |",
        "",
        "## Contrast and sample mapping",
        "",
        "The fixed contrast is **UHR + ERCC Mix 1 / HBR + ERCC Mix 2**.",
        "",
        "| Role | Samples | Prefix rules |",
        "|---|---|---|",
        "| Control (HBR Mix 2) | "
        + ", ".join(samples["control"])
        + " | "
        + ", ".join(samples["control_prefixes"])
        + " |",
        "| Treatment (UHR Mix 1) | "
        + ", ".join(samples["treatment"])
        + " | "
        + ", ".join(samples["treatment_prefixes"])
        + " |",
        "",
        "## Overall results",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Truth ERCCs | {coverage['truth_ercc_total']} |",
        f"| Present in matrix | {coverage['matched_in_count_matrix']} |",
        f"| Detected in both groups | {coverage['detected_both_groups']} |",
        f"| Detection coverage | {_display(coverage['detection_coverage'])} |",
        f"| Pearson r | {_display(metrics['pearson'])} |",
        f"| Spearman rho | {_display(metrics['spearman'])} |",
        f"| MAE (log2 ratio) | {_display(metrics['mae'])} |",
        f"| RMSE (log2 ratio) | {_display(metrics['rmse'])} |",
        f"| Direction accuracy | {_display(metrics['direction_accuracy'])} |",
        f"| Direction comparisons | {metrics['direction_n']} |",
        "",
        "Only ERCCs detected in both groups contribute to agreement metrics. "
        "Expected 1:1 controls are excluded from the up/down direction metric.",
        "",
        "## Per-subgroup results",
        "",
        "| Subgroup | Truth | Both detected | Coverage | Pearson | Spearman | MAE | RMSE | Direction accuracy |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for subgroup, subgroup_report in report["subgroups"].items():
        subcoverage = subgroup_report["coverage"]
        submetrics = subgroup_report["metrics"]
        lines.append(
            f"| {subgroup} | {subcoverage['truth_ercc_total']} | "
            f"{subcoverage['detected_both_groups']} | "
            f"{_display(subcoverage['detection_coverage'])} | "
            f"{_display(submetrics['pearson'])} | {_display(submetrics['spearman'])} | "
            f"{_display(submetrics['mae'])} | {_display(submetrics['rmse'])} | "
            f"{_display(submetrics['direction_accuracy'])} |"
        )

    all_samples = list(samples["control"]) + list(samples["treatment"])
    lines.extend(
        [
            "",
            "## Per-ERCC CPM values",
            "",
            "| ERCC | Subgroup | Expected log2 | Observed log2 | HBR mean CPM | UHR mean CPM | "
            + " | ".join(all_samples)
            + " | Both detected |",
            "|---|---|---:|---:|---:|---:|"
            + "---:|" * len(all_samples)
            + "---|",
        ]
    )
    for row in report["ercc"]:
        sample_values = " | ".join(
            _display(row["sample_cpm"].get(sample)) for sample in all_samples
        )
        lines.append(
            f"| {row['ercc_id']} | {row['subgroup']} | "
            f"{_display(row['expected_log2_mix1_over_mix2'])} | "
            f"{_display(row['observed_log2_uhr_over_hbr'])} | "
            f"{_display(row['group_mean_cpm']['HBR_Mix2'])} | "
            f"{_display(row['group_mean_cpm']['UHR_Mix1'])} | {sample_values} | "
            f"{_display(row['detected_both_groups'])} |"
        )
    lines.extend(
        [
            "",
            "## Method boundary",
            "",
            f"- CPM denominator: all featureCounts rows in each sample; pseudocount: {report['method']['pseudocount_cpm']} CPM.",
            "- No dispersion model, multiple-testing correction, confidence interval, or p-value is calculated.",
            "- Use DESeq2, edgeR, or another validated count model for inferential differential expression.",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare featureCounts ERCC CPM ratios with the fixed "
            "UHR Mix1 / HBR Mix2 truth contrast."
        )
    )
    parser.add_argument("--counts", required=True, help="featureCounts gene_counts.txt")
    parser.add_argument("--truth", required=True, help="Griffith ERCC truth TSV")
    parser.add_argument(
        "--control-prefix",
        action="append",
        help="HBR/control sample prefix; repeat or comma-separate (default: HBR)",
    )
    parser.add_argument(
        "--treatment-prefix",
        action="append",
        help="UHR/treatment sample prefix; repeat or comma-separate (default: UHR)",
    )
    parser.add_argument(
        "--pseudocount",
        type=float,
        default=0.5,
        help="CPM pseudocount used only for the observed log2 ratio (default: 0.5)",
    )
    parser.add_argument(
        "--format", choices=("json", "markdown"), default="json"
    )
    parser.add_argument("--output", help="Write report to this path instead of stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = compute_ercc_metrics(
            args.counts,
            args.truth,
            control_prefixes=args.control_prefix,
            treatment_prefixes=args.treatment_prefix,
            pseudocount=args.pseudocount,
        )
    except ERCCMetricsError as exc:
        parser.error(str(exc))
    rendered = (
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.format == "json"
        else render_markdown(report)
    )
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
