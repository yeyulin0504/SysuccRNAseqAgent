from __future__ import annotations

import unittest

from rnaseq_agent.actions import explain_project


class ExplainProjectTests(unittest.TestCase):
    def test_explanation_reflects_enabled_pipeline_steps(self) -> None:
        config = {
            "status": {"state": "completed"},
            "pipeline": {
                "featurecounts": {"enabled": True},
                "rsem": {"enabled": False},
                "arriba": {"enabled": True},
            },
        }

        explanation = "\n".join(explain_project(config))

        self.assertIn("featureCounts", explanation)
        self.assertIn("Arriba", explanation)
        self.assertNotIn("RSEM：", explanation)

    def test_incomplete_run_is_explained_cautiously(self) -> None:
        config = {
            "status": {"state": "submitted"},
            "pipeline": {},
        }

        explanation = "\n".join(explain_project(config))

        self.assertIn("completed", explanation)
        self.assertIn("不替代生物学或临床结论", explanation)


if __name__ == "__main__":
    unittest.main()
