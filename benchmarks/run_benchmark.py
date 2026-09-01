"""CLI for comparing repeated RNA-seq Agent run manifests."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from metrics import BenchmarkInputError, canonical_sha256, observe_run, summarize_case


MODES = ("free", "skill", "contract")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare run and optional result manifests: execution fingerprints, "
            "contract IDs, outcomes, result validation, and consistency."
        ),
        epilog=(
            "Important: the current free mode is an editable legacy configuration "
            "baseline, not an unconstrained LLM planner."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--case", type=Path, help="Benchmark case JSON file.")
    source.add_argument(
        "--manifest",
        type=Path,
        action="append",
        help="Manifest to compare; repeat this option for multiple attempts.",
    )
    source.add_argument("--self-test", action="store_true", help="Run an offline smoke test.")
    parser.add_argument("--case-id", default="ad-hoc", help="Case label for --manifest input.")
    parser.add_argument("--expected-mode", choices=MODES, help="Expected execution mode.")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path, help="Write report to a file instead of stdout.")
    return parser


def _load_case_file(path: Path) -> tuple[str, list[dict[str, Any]], Path]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkInputError(f"Could not load case file {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise BenchmarkInputError("Case file must be an object with schema_version=1")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise BenchmarkInputError("Case file must contain a non-empty cases array")
    return str(payload.get("benchmark_id", path.stem)), cases, path.resolve().parent


def _evaluate_cases(
    benchmark_id: str,
    cases: list[dict[str, Any]],
    *,
    base_dir: Path,
) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    for case_number, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise BenchmarkInputError(f"Case {case_number} must be an object")
        case_id = str(case.get("case_id") or f"case-{case_number}")
        expected_mode = case.get("expected_mode")
        if expected_mode is not None and expected_mode not in MODES:
            raise BenchmarkInputError(f"{case_id}: unsupported expected_mode {expected_mode!r}")
        runs = case.get("runs")
        if not isinstance(runs, list) or not runs:
            raise BenchmarkInputError(f"{case_id}: runs must be a non-empty array")
        observations = []
        for ordinal, run_spec in enumerate(runs, start=1):
            if not isinstance(run_spec, dict):
                raise BenchmarkInputError(f"{case_id} run {ordinal}: run must be an object")
            observations.append(observe_run(run_spec, base_dir=base_dir, ordinal=ordinal))
        summaries.append(
            summarize_case(
                case_id,
                observations,
                expected_mode=str(expected_mode) if expected_mode is not None else None,
            )
        )
    return {
        "schema_version": 1,
        "benchmark_id": benchmark_id,
        "metric_definitions": {
            "success_rate": "successful runs / runs with known outcomes",
            "outcome_coverage": "runs with known outcomes / all declared runs",
            "exact_modal_consistency_rate": "runs carrying the most common three-fingerprint tuple / comparable manifests",
            "exact_pairwise_consistency_rate": "matching unordered run pairs / all unordered comparable run pairs",
            "contract_id_consistency_rate": "runs carrying the most common non-empty contract ID / runs with a contract ID",
            "result_manifest_coverage": "loaded result manifests / all declared runs",
            "result_validation_success_rate": "validation.ok=true / integrity-valid result manifests with known validation.ok",
            "scientific_files_modal_consistency_rate": "result manifests carrying the most common scientific file-list fingerprint / comparable result manifests",
            "scientific_files_pairwise_consistency_rate": "matching scientific file-list fingerprint pairs / all comparable result-manifest pairs",
        },
        "caveat": (
            "free is the editable legacy-config baseline in the current implementation; "
            "it is not a true LLM tool/parameter-planning condition"
        ),
        "cases": summaries,
    }


def _format_rate(value: Any) -> str:
    return "NA" if value is None else f"{float(value):.1%}"


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Reproducibility benchmark: {report['benchmark_id']}",
        "",
        f"> Caveat: {report['caveat']}.",
        "",
        "| Case | Expected mode | Runs | Comparable | Success rate | Outcome coverage | Exact modal consistency | Pairwise consistency | Contract ID coverage | Contract ID consistency |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for case in report["cases"]:
        lines.append(
            "| {case_id} | {mode} | {runs} | {comparable} | {success} | {coverage} | "
            "{modal} | {pairwise} | {contract_coverage} | {contract_consistency} |".format(
                case_id=case["case_id"],
                mode=case.get("expected_mode") or ", ".join(case["observed_modes"]) or "NA",
                runs=case["run_count"],
                comparable=case["comparable_manifest_count"],
                success=_format_rate(case["success_rate"]),
                coverage=_format_rate(case["outcome_coverage"]),
                modal=_format_rate(case["exact_modal_consistency_rate"]),
                pairwise=_format_rate(case["exact_pairwise_consistency_rate"]),
                contract_coverage=_format_rate(case["contract_id_coverage"]),
                contract_consistency=_format_rate(case["contract_id_consistency_rate"]),
            )
        )

    lines.extend(
        [
            "",
            "## Result reproducibility",
            "",
            "| Case | Result manifest coverage | Result integrity | Validation success | Validation coverage | Scientific results comparable | Scientific modal consistency | Scientific pairwise consistency |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for case in report["cases"]:
        lines.append(
            "| {case_id} | {coverage} | {integrity} | {validation} | {validation_coverage} | "
            "{comparable} | {modal} | {pairwise} |".format(
                case_id=case["case_id"],
                coverage=_format_rate(case["result_manifest_coverage"]),
                integrity=_format_rate(case["result_manifest_integrity_rate"]),
                validation=_format_rate(case["result_validation_success_rate"]),
                validation_coverage=_format_rate(case["result_validation_coverage"]),
                comparable=case["scientific_files_comparable_count"],
                modal=_format_rate(case["scientific_files_modal_consistency_rate"]),
                pairwise=_format_rate(case["scientific_files_pairwise_consistency_rate"]),
            )
        )

    lines.extend(["", "## Per-case details", ""])
    for case in report["cases"]:
        lines.extend(
            [
                f"### {case['case_id']}",
                "",
                f"- Observed modes: {', '.join(case['observed_modes']) or 'none'}",
                f"- Manifest integrity: {_format_rate(case['manifest_integrity_rate'])}",
                f"- Unique full fingerprint tuples: {case['exact_fingerprint_unique_count']}",
                f"- Workflow fingerprint consistency: {_format_rate(case['fingerprint_metrics']['workflow_sha256']['modal_consistency_rate'])}",
                f"- Input fingerprint consistency: {_format_rate(case['fingerprint_metrics']['inputs_sha256']['modal_consistency_rate'])}",
                f"- Script fingerprint consistency: {_format_rate(case['fingerprint_metrics']['scripts_sha256']['modal_consistency_rate'])}",
                f"- Unique non-empty contract IDs: {case['contract_id_unique_count']}",
                f"- Unique scientific file-list fingerprints: {case['scientific_files_unique_count']}",
                f"- Full result file-list consistency: {_format_rate(case['result_files_modal_consistency_rate'])}",
            ]
        )
        if case["mode_mismatch_runs"]:
            lines.append("- Mode mismatches: " + ", ".join(case["mode_mismatch_runs"]))
        errors = [run for run in case["runs"] if run["error"]]
        if errors:
            lines.append("- Input warnings:")
            for run in errors:
                lines.append(f"  - {run['run_label']}: {run['error']}")
        result_errors = [run for run in case["runs"] if run["result_manifest"]["error"]]
        if result_errors:
            lines.append("- Result manifest warnings:")
            for run in result_errors:
                lines.append(f"  - {run['run_label']}: {run['result_manifest']['error']}")
        lines.append("")

    lines.extend(
        [
            "## Interpretation boundary",
            "",
            "A run fingerprint match shows that the recorded workflow, inputs, and rendered scripts match. It does not by itself prove identical software binaries, references, or scheduler environments.",
            "",
            "A scientific_files_sha256 match is strict byte-level identity of the recorded scientific file inventory (paths, sizes, and file hashes). It is not a semantic comparison of count matrices or biological conclusions.",
            "",
            "Unknown outcomes are excluded from the success-rate denominator and are exposed through outcome coverage.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_demo_manifest(path: Path, mode: str, suffix: str = "same") -> None:
    body = {
        "run_id": path.stem,
        "created_at": "2026-01-01T00:00:00+00:00",
        "execution": {
            "mode": mode,
            "skill_id": "bulk_rnaseq_expression_v1" if mode == "skill" else "",
            "contract_id": "sha256:contract" if mode == "contract" else "",
        },
        "fingerprints": {
            "project_snapshot_sha256": "snapshot",
            "workflow_sha256": "workflow-" + suffix,
            "inputs_sha256": "inputs-same",
            "scripts_sha256": "scripts-" + suffix,
        },
    }
    payload = {
        "schema_version": 1,
        "manifest_id": f"sha256:{canonical_sha256(body)}",
        "body": body,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_demo_result_manifest(path: Path, suffix: str = "same", validation_ok: bool = True) -> None:
    body = {
        "run_id": path.stem,
        "project_id": "self-test",
        "execution_mode": "contract",
        "validation": {"ok": validation_ok, "errors": [] if validation_ok else ["demo error"]},
        "fingerprints": {
            "files_sha256": "all-files-" + suffix,
            "scientific_files_sha256": "scientific-files-" + suffix,
        },
        "files": [],
    }
    payload = {
        "schema_version": 1,
        "manifest_id": f"sha256:{canonical_sha256(body)}",
        "created_at": "2026-01-01T00:00:00+00:00",
        "body": body,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _self_test() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="rnaseq-benchmark-") as temp:
        root = Path(temp)
        first = root / "run-1.json"
        second = root / "run-2.json"
        first_result = root / "result-1.json"
        second_result = root / "result-2.json"
        tampered_result = root / "result-tampered.json"
        _write_demo_manifest(first, "contract")
        _write_demo_manifest(second, "contract")
        _write_demo_result_manifest(first_result)
        _write_demo_result_manifest(second_result)
        _write_demo_result_manifest(tampered_result)
        tampered_payload = json.loads(tampered_result.read_text(encoding="utf-8"))
        tampered_payload["body"]["validation"]["errors"].append("changed after signing")
        tampered_result.write_text(json.dumps(tampered_payload, indent=2), encoding="utf-8")
        cases = [
            {
                "case_id": "self-test-contract",
                "expected_mode": "contract",
                "runs": [
                    {
                        "manifest": str(first),
                        "result_manifest": str(first_result),
                        "success": True,
                        "outcome": "completed",
                    },
                    {
                        "manifest": str(second),
                        "result_manifest": str(second_result),
                        "success": True,
                        "outcome": "completed",
                    },
                ],
            },
            {
                "case_id": "self-test-legacy-without-results",
                "expected_mode": "contract",
                "runs": [{"manifest": str(first), "success": True}],
            },
            {
                "case_id": "self-test-tampered-result",
                "expected_mode": "contract",
                "runs": [
                    {
                        "manifest": str(first),
                        "result_manifest": str(tampered_result),
                        "success": True,
                    }
                ],
            },
        ]
        report = _evaluate_cases("self-test", cases, base_dir=root)
        summary = report["cases"][0]
        assert summary["exact_modal_consistency_rate"] == 1.0
        assert summary["exact_pairwise_consistency_rate"] == 1.0
        assert summary["success_rate"] == 1.0
        assert summary["contract_id_consistency_rate"] == 1.0
        assert summary["result_manifest_coverage"] == 1.0
        assert summary["result_manifest_integrity_rate"] == 1.0
        assert summary["result_validation_success_rate"] == 1.0
        assert summary["scientific_files_modal_consistency_rate"] == 1.0
        assert summary["scientific_files_pairwise_consistency_rate"] == 1.0
        legacy = report["cases"][1]
        assert legacy["result_manifest_coverage"] == 0.0
        assert legacy["scientific_files_modal_consistency_rate"] is None
        tampered = report["cases"][2]
        assert tampered["result_manifest_integrity_rate"] == 0.0
        assert tampered["result_validation_success_rate"] is None
        assert tampered["scientific_files_modal_consistency_rate"] is None
        return report


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.self_test:
            report = _self_test()
        elif args.case:
            benchmark_id, cases, base_dir = _load_case_file(args.case.resolve())
            report = _evaluate_cases(benchmark_id, cases, base_dir=base_dir)
        else:
            runs = [{"manifest": str(path.resolve())} for path in args.manifest]
            cases = [
                {
                    "case_id": args.case_id,
                    "expected_mode": args.expected_mode,
                    "runs": runs,
                }
            ]
            report = _evaluate_cases(args.case_id, cases, base_dir=Path.cwd())
    except BenchmarkInputError as exc:
        print(f"benchmark input error: {exc}", file=sys.stderr)
        return 2

    output = json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else _markdown(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + ("" if output.endswith("\n") else "\n"), encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
