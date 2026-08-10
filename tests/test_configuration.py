from __future__ import annotations

import unittest

from rnaseq_agent.configuration import normalize_config
from rnaseq_agent.defaults import DEFAULT_REFERENCE
from rnaseq_agent.llm_policy import build_llm_context


class ConfigurationPrivacyTests(unittest.TestCase):
    def test_blank_reference_stays_blank_after_normalization(self) -> None:
        config = normalize_config({"reference": {}})

        self.assertEqual(config["reference"], {})
        self.assertNotIn("star_index_dir", config["reference"])

    def test_existing_reference_settings_are_preserved(self) -> None:
        reference = {
            "name": "custom_mouse_reference",
            "star_index_dir": "/approved/mouse/star",
        }

        config = normalize_config({"reference": reference})

        self.assertEqual(config["reference"], reference)
        self.assertNotEqual(config["reference"], DEFAULT_REFERENCE)

    def test_llm_context_excludes_attachment_like_sensitive_data(self) -> None:
        secret = "metadata-content-must-not-leave-local-machine"
        config = {
            "reference": {"attachment": secret, "attachment_path": "/private/meta.tsv"},
            "samples": {"metadata_preview": secret},
            "sequencing": {"layout": "paired"},
            "pipeline": {},
        }

        context = build_llm_context(has_project=True, config=config)

        self.assertNotIn(secret, repr(context))
        self.assertNotIn("attachment", repr(context))
        self.assertNotIn("/private/meta.tsv", repr(context))


if __name__ == "__main__":
    unittest.main()
