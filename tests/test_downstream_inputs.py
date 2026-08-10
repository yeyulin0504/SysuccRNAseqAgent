from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rnaseq_agent.downstream import downstream_errors
from rnaseq_agent.downstream_inputs import StandaloneInputError, inspect_standalone_inputs


class StandaloneInputTests(unittest.TestCase):
    def _config(self, root: Path) -> dict:
        return {
            "reference": {"species": "human", "catalog_id": "catalog", "index_state": "existing_confirmed"},
            "downstream": {
                "enabled": True,
                "source_mode": "standalone_count_matrix",
                "profile_id": "bulk_rnaseq_deseq2_v1",
                "input_root": str(root),
                "input": {"source_filename": "counts.tsv", "source_path": str(root / "counts.tsv")},
                "metadata_input": {"source_filename": "metadata.tsv", "source_path": str(root / "metadata.tsv")},
                "design": {"condition_column": "condition", "batch_column": "", "formula": "~ condition"},
                "contrasts": [{"id": "treated_vs_control", "factor": "condition", "numerator": "treated", "denominator": "control"}],
                "filtering": {"min_count": 10, "min_samples": 2},
                "differential_expression": {"padj_threshold": 0.05, "abs_log2_fold_change": 1.0},
                "enrichment": {"enabled": False, "gmt": {"enabled": False}},
                "runtime": {"environment_kind": "apptainer", "image_path": "/containers/deseq2.sif", "image_sha256": "a" * 64, "rscript_path": "Rscript"},
            },
        }

    def _write_valid(self, root: Path) -> None:
        (root / "counts.tsv").write_text(
            "gene_id\tcontrol_1\tcontrol_2\ttreated_1\ttreated_2\n"
            "ENSG000001.1\t10\t11\t20\t21\n"
            "ENSG000002\t1\t2\t3\t4\n",
            encoding="utf-8",
        )
        (root / "metadata.tsv").write_text(
            "sample_id\tcondition\n"
            "control_1\tcontrol\ncontrol_2\tcontrol\n"
            "treated_1\ttreated\ntreated_2\ttreated\n",
            encoding="utf-8",
        )

    def test_inspects_valid_standalone_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._write_valid(root)

            summary = inspect_standalone_inputs(self._config(root))

            self.assertEqual(summary.gene_count, 2)
            self.assertEqual(summary.sample_count, 4)
            self.assertEqual(summary.condition_counts, {"control": 2, "treated": 2})
            self.assertEqual(downstream_errors(self._config(root)), [])

    def test_rejects_noninteger_counts(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._write_valid(root)
            (root / "counts.tsv").write_text("gene_id\ta\tb\nGENE1\t1\t1.5\n", encoding="utf-8")

            with self.assertRaisesRegex(StandaloneInputError, "nonnegative integer"):
                inspect_standalone_inputs(self._config(root))

    def test_rejects_metadata_sample_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._write_valid(root)
            (root / "metadata.tsv").write_text("sample_id\tcondition\nother\tcontrol\n", encoding="utf-8")

            with self.assertRaisesRegex(StandaloneInputError, "exactly match"):
                inspect_standalone_inputs(self._config(root))


if __name__ == "__main__":
    unittest.main()
