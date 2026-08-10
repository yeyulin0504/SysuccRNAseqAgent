from __future__ import annotations

import unittest

from rnaseq_agent.llm_policy import (
    AgentStep,
    ProposalError,
    apply_onboarding_proposals,
    apply_proposals,
    build_llm_context,
    build_onboarding_context,
    build_reference_search_query,
    validate_onboarding_proposals,
    validate_agent_plan,
    validate_proposals,
)


class LLMPolicyTests(unittest.TestCase):
    def test_agent_plan_accepts_only_fixed_steps(self) -> None:
        steps = validate_agent_plan(
            [{"kind": "validate"}, {"kind": "run", "wait": False}, {"kind": "report"}]
        )

        self.assertEqual(steps, (AgentStep("validate"), AgentStep("run", False), AgentStep("report")))

    def test_agent_plan_rejects_injected_fields_and_invalid_wait(self) -> None:
        invalid = (
            [],
            [{"kind": "shell"}],
            [{"kind": "status", "host": "other-hpc"}],
            [{"kind": "run", "wait": "false"}],
            [{"kind": "run", "wait": False, "command": "sbatch job.sh"}],
            [{"kind": "validate"}] * 7,
        )
        for plan in invalid:
            with self.subTest(plan=plan):
                with self.assertRaises(ProposalError):
                    validate_agent_plan(plan)

    def test_context_includes_only_allowlisted_workflow_fields(self) -> None:
        config = {
            "project": {"id": "private-project", "owner": "private-owner"},
            "server": {"host": "secret-hpc", "user": "private-user"},
            "samples": {"items": [{"sample_id": "patient-001", "fastq_1": "raw.fastq.gz"}]},
            "notification": {"smtp_host": "smtp.private", "password_env": "SMTP_PASSWORD"},
            "sequencing": {"layout": "paired", "strandedness": "auto", "reads_per_sample_million": 40},
            "polling": {"interval_seconds": 300, "timeout_hours": 24},
            "pipeline": {"fastp": {"enabled": True}, "star": {"enabled": False}},
        }

        context = build_llm_context(has_project=True, config=config)

        self.assertEqual(
            context,
            {
                "has_project": True,
                "workflow": {
                    "layout": "paired",
                    "strandedness": "auto",
                    "reads_per_sample_million": 40,
                    "enabled_steps": ["fastp"],
                    "polling_interval_seconds": 300,
                    "polling_timeout_hours": 24,
                },
            },
        )

    def test_accepts_allowlisted_typed_proposals(self) -> None:
        patches = validate_proposals(
            [
                {"op": "replace", "path": "/sequencing/layout", "value": "single"},
                {"op": "replace", "path": "/pipeline/arriba/enabled", "value": False, "reason": "不需要融合检测"},
            ]
        )

        self.assertEqual(patches[0].value, "single")
        self.assertFalse(patches[1].value)

    def test_rejects_sensitive_or_invalid_proposals(self) -> None:
        for proposal in (
            {"op": "replace", "path": "/server/host", "value": "other-host"},
            {"op": "replace", "path": "/samples/items", "value": []},
            {"op": "replace", "path": "/pipeline/star/enabled", "value": "false"},
            {"op": "add", "path": "/sequencing/layout", "value": "single"},
        ):
            with self.subTest(proposal=proposal):
                with self.assertRaises(ProposalError):
                    validate_proposals([proposal])

    def test_apply_proposals_does_not_mutate_source_configuration(self) -> None:
        config = {
            "sequencing": {"layout": "paired", "strandedness": "auto", "reads_per_sample_million": 40},
            "pipeline": {"fastp": {"enabled": True}},
        }
        patches = validate_proposals(
            [{"op": "replace", "path": "/sequencing/layout", "value": "single"}]
        )

        candidate = apply_proposals(config, patches)

        self.assertEqual(config["sequencing"]["layout"], "paired")
        self.assertEqual(candidate["sequencing"]["layout"], "single")
    def test_onboarding_context_only_identifies_current_field(self) -> None:
        self.assertEqual(build_onboarding_context(current_field="server.host"), {"current_field": "server.host"})
        with self.assertRaises(ProposalError):
            build_onboarding_context(current_field="server.password")

    def test_onboarding_accepts_one_safe_field_and_does_not_mutate_source(self) -> None:
        patches = validate_onboarding_proposals(
            [{"field": "sequencing.layout", "value": "single", "reason": "用户说明单端"}]
        )
        source = {"sequencing": {"layout": "paired"}}
        candidate = apply_onboarding_proposals(source, patches)
        self.assertEqual(source["sequencing"]["layout"], "paired")
        self.assertEqual(candidate["sequencing"]["layout"], "single")

    def test_onboarding_normalizes_common_layout_labels(self) -> None:
        cases = {
            "双端": "paired",
            "双端测序": "paired",
            "PE": "paired",
            "paired-end": "paired",
            "单端": "single",
            "单端测序": "single",
            "SE": "single",
            "single-end": "single",
        }
        for raw_value, expected in cases.items():
            with self.subTest(raw_value=raw_value):
                patches = validate_onboarding_proposals(
                    [{"field": "sequencing.layout", "value": raw_value}]
                )
                self.assertEqual(patches[0].value, expected)

        with self.assertRaises(ProposalError):
            validate_onboarding_proposals(
                [{"field": "sequencing.layout", "value": "双端或单端"}]
            )

    def test_onboarding_rejects_secrets_commands_and_duplicate_fields(self) -> None:
        invalid = (
            [{"field": "server.password", "value": "secret"}],
            [{"field": "server.host", "value": "host; rm -rf /"}],
            [{"field": "samples.local_data_dir", "value": "~/secret"}],
            [
                {"field": "sequencing.layout", "value": "paired"},
                {"field": "sequencing.layout", "value": "single"},
            ],
        )
        for proposals in invalid:
            with self.subTest(proposals=proposals):
                with self.assertRaises(ProposalError):
                    validate_onboarding_proposals(proposals)
    def test_builds_public_reference_search_query_without_local_context(self) -> None:
        query = build_reference_search_query(
            species="mouse",
            assembly="GRCm39",
            annotation_release="GENCODE M35",
            layout="paired",
            enabled_tools=("star", "rsem"),
        )

        self.assertEqual(query["species"], "mouse")
        self.assertEqual(query["enabled_tools"], ("star", "rsem"))
        self.assertNotIn("path", repr(query))

    def test_reference_search_requires_confirmed_public_fields(self) -> None:
        with self.assertRaises(ProposalError):
            build_reference_search_query(
                species="mouse",
                assembly="",
                annotation_release="GENCODE M35",
                layout="paired",
                enabled_tools=("star",),
            )


if __name__ == "__main__":
    unittest.main()
