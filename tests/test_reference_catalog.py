from __future__ import annotations

import unittest

from rnaseq_agent.reference_catalog import (
    catalog_reference_fields,
    infer_reference_candidates,
    normalize_explicit_species,
)


class ReferenceCatalogTests(unittest.TestCase):
    def test_normalizes_controlled_species_answers(self) -> None:
        self.assertEqual(normalize_explicit_species("人类"), "human")
        self.assertEqual(normalize_explicit_species("Mus musculus"), "mouse")
        self.assertIsNone(normalize_explicit_species("大鼠"))

    def test_assembly_alias_selects_matching_catalog_entry(self) -> None:
        candidates = infer_reference_candidates("organism: mouse\nreference: mm39")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].catalog_id, "GENCODE_M35_GRCm39_PRIMARY")
        self.assertEqual(candidates[0].confidence, "high")
        self.assertIn("mm39", candidates[0].evidence)

    def test_mixed_species_metadata_has_no_catalog_candidate(self) -> None:
        candidates = infer_reference_candidates("human xenograft with mouse stroma")

        self.assertEqual(candidates, ())

    def test_unknown_metadata_has_no_catalog_candidate(self) -> None:
        self.assertEqual(infer_reference_candidates("RNA-seq experiment"), ())

    def test_unsupported_mouse_assembly_has_no_catalog_candidate(self) -> None:
        candidates = infer_reference_candidates("organism: mouse\nreference: mm10")

        self.assertEqual(candidates, ())

    def test_catalog_fields_describe_identity_without_deployed_paths(self) -> None:
        fields = catalog_reference_fields("GENCODE_M35_GRCm39_PRIMARY")

        self.assertEqual(fields["species"], "mouse")
        self.assertEqual(fields["assembly"], "GRCm39")
        self.assertIn("gtf_url", fields)
        self.assertNotIn("remote_gtf_path", fields)
        self.assertNotIn("remote_genome_fasta_path", fields)
        self.assertNotIn("star_index_dir", fields)
        self.assertNotIn("rsem_index_prefix", fields)
        self.assertEqual(fields["index_state"], "unconfigured")
        self.assertEqual(len(fields["catalog_manifest_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
