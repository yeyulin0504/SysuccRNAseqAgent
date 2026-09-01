from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.ercc_metrics import (
    ERCCMetricsError,
    compute_ercc_metrics,
    main,
    render_markdown,
)


FEATURECOUNTS_HEADER = (
    "Geneid\tChr\tStart\tEnd\tStrand\tLength\t"
    "HBR_Rep1.bam\tHBR_Rep2.bam\tUHR_Rep1.bam\tUHR_Rep2.bam\n"
)
TRUTH_HEADER = (
    "Re-sort ID\tERCC ID\tsubgroup\tconcentration in Mix 1\t"
    "concentration in Mix 2\texpected fold-change ratio\t"
    "log2(Mix 1/Mix 2)\n"
)


def _count_row(gene: str, values: tuple[int, int, int, int]) -> str:
    return (
        f"{gene}\tchr22\t1\t100\t+\t100\t"
        + "\t".join(str(value) for value in values)
        + "\n"
    )


def _truth_row(
    index: int, ercc_id: str, subgroup: str, fold: float, expected_log2: float
) -> str:
    return (
        f"{index}\t{ercc_id}\t{subgroup}\t1\t1\t{fold}\t{expected_log2}\n"
    )


class ERCCMetricsTests(unittest.TestCase):
    def _write_fixture(self, root: Path, *, missing_fourth: bool = False) -> tuple[Path, Path]:
        counts = root / "gene_counts.txt"
        counts_text = FEATURECOUNTS_HEADER
        counts_text += _count_row("ERCC-00001", (100, 100, 200, 200))
        counts_text += _count_row("ERCC-00002", (200, 200, 100, 100))
        counts_text += _count_row("ERCC-00003", (100, 100, 100, 100))
        if not missing_fourth:
            counts_text += _count_row("ERCC-00004", (50, 50, 100, 100))
            background = (550, 550, 500, 500)
        else:
            background = (600, 600, 600, 600)
        counts_text += _count_row("ENSG000001", background)
        counts.write_text(counts_text, encoding="utf-8")

        truth = root / "ERCC_Controls_Analysis.txt"
        truth.write_text(
            TRUTH_HEADER
            + _truth_row(1, "ERCC-00001", "A", 2, 1)
            + _truth_row(2, "ERCC-00002", "A", 0.5, -1)
            + _truth_row(3, "ERCC-00003", "B", 1, 0)
            + _truth_row(4, "ERCC-00004", "B", 2, 1),
            encoding="utf-8",
        )
        return counts, truth

    def test_perfect_ratios_and_subgroup_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            counts, truth = self._write_fixture(Path(temp_dir))
            report = compute_ercc_metrics(counts, truth, pseudocount=0)
            expected_inputs = {
                "featurecounts": (counts.name, counts.read_bytes()),
                "truth_tsv": (truth.name, truth.read_bytes()),
            }

        self.assertEqual(report["contrast"]["direction"], "UHR_Mix1_over_HBR_Mix2")
        self.assertEqual(report["metrics"]["n"], 4)
        self.assertAlmostEqual(report["metrics"]["pearson"], 1.0)
        self.assertAlmostEqual(report["metrics"]["spearman"], 1.0)
        self.assertAlmostEqual(report["metrics"]["mae"], 0.0)
        self.assertAlmostEqual(report["metrics"]["rmse"], 0.0)
        self.assertAlmostEqual(report["metrics"]["direction_accuracy"], 1.0)
        self.assertEqual(report["metrics"]["direction_n"], 3)
        self.assertAlmostEqual(report["coverage"]["detection_coverage"], 1.0)
        self.assertAlmostEqual(report["subgroups"]["A"]["metrics"]["pearson"], 1.0)
        self.assertIn("HBR_Rep1.bam", report["ercc"][0]["sample_cpm"])
        for key, (file_name, payload) in expected_inputs.items():
            artifact = report["inputs"][key]
            self.assertEqual(artifact["file_name"], file_name)
            self.assertFalse(artifact["absolute_path_saved"])
            self.assertEqual(artifact["size_bytes"], len(payload))
            self.assertEqual(artifact["sha256"], hashlib.sha256(payload).hexdigest())

        markdown = render_markdown(report)
        self.assertIn("does not replace DESeq2/edgeR", markdown)
        self.assertIn("No dispersion model", markdown)
        self.assertNotIn("p-value |", markdown)
        self.assertIn(report["inputs"]["featurecounts"]["sha256"], markdown)
        self.assertIn(report["inputs"]["truth_tsv"]["file_name"], markdown)

    def test_missing_ercc_reduces_detection_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            counts, truth = self._write_fixture(Path(temp_dir), missing_fourth=True)
            report = compute_ercc_metrics(counts, truth, pseudocount=0)

        self.assertEqual(report["coverage"]["truth_ercc_total"], 4)
        self.assertEqual(report["coverage"]["matched_in_count_matrix"], 3)
        self.assertEqual(report["coverage"]["detected_both_groups"], 3)
        self.assertAlmostEqual(report["coverage"]["detection_coverage"], 0.75)
        missing = next(row for row in report["ercc"] if row["ercc_id"] == "ERCC-00004")
        self.assertFalse(missing["present_in_count_matrix"])
        self.assertIsNone(missing["observed_log2_uhr_over_hbr"])

    def test_bad_default_mapping_fails_and_explicit_prefixes_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            counts, truth = self._write_fixture(root)
            text = counts.read_text(encoding="utf-8")
            text = text.replace("HBR_Rep", "CTRL_").replace("UHR_Rep", "CASE_")
            counts.write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(ERCCMetricsError, "Unmapped sample columns"):
                compute_ercc_metrics(counts, truth)

            report = compute_ercc_metrics(
                counts,
                truth,
                control_prefixes=["CTRL"],
                treatment_prefixes=["CASE"],
                pseudocount=0,
            )
            self.assertEqual(len(report["samples"]["control"]), 2)
            self.assertEqual(len(report["samples"]["treatment"]), 2)

            output = root / "report.json"
            self.assertEqual(
                main(
                    [
                        "--counts",
                        str(counts),
                        "--truth",
                        str(truth),
                        "--control-prefix",
                        "CTRL",
                        "--treatment-prefix",
                        "CASE",
                        "--pseudocount",
                        "0",
                        "--output",
                        str(output),
                    ]
                ),
                0,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["analysis_type"], "ercc_cpm_truth_diagnostic")


if __name__ == "__main__":
    unittest.main()
