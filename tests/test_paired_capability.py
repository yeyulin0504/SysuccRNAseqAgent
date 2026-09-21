from __future__ import annotations

import pytest

from rnaseq_agent.capability import NOT_EVALUABLE, PASS
from rnaseq_agent.differential import (
    deg_gate,
    diffexp_design_of,
    paired_design_checks,
    render_colData,
    render_diffexp_counts_script,
)

PAIRED_TEMPLATE = "deseq2_paired_two_group"
PAIRED_DESIGN_FORMULA = "~ pair_id + condition"


def _paired_config(*, pair_ids=None, conditions=None, formula=None, batch=False, prefilter=0):
    pair_ids = pair_ids or ["p3", "p1", "p2"]
    conditions = conditions or ("untrt", "trt")
    items = []
    for pair in pair_ids:
        for condition in conditions:
            sample = {
                "sample_id": f"{pair}_{condition}",
                "condition": condition,
                "pair_id": pair,
                "fastq_1": f"{pair}_{condition}_R1.fastq.gz",
                "fastq_2": f"{pair}_{condition}_R2.fastq.gz",
            }
            if batch:
                sample["batch"] = "B1"
            items.append(sample)
    config = {
        "study": {"design": "paired_two_group"},
        "samples": {"items": items},
        "pipeline": {"diffexp": {"enabled": True}},
        "diffexp": {
            "reference_condition": "untrt",
            "contrast_condition": "trt",
            "min_count_prefilter": prefilter,
        },
    }
    if formula is not None:
        config["diffexp"]["formula"] = formula
    return config


def test_paired_design_is_canonical_and_renders_fixed_formula():
    config = _paired_config()
    descriptor = diffexp_design_of(config)
    assert descriptor["template"] == PAIRED_TEMPLATE
    assert descriptor["formula"] == PAIRED_DESIGN_FORMULA
    assert [row["pair_id"] for row in descriptor["pair_mapping"]] == ["p1", "p2", "p3"]
    assert descriptor["pair_mapping"][0]["reference_sample_id"] == "p1_untrt"
    assert descriptor["pair_mapping"][0]["contrast_sample_id"] == "p1_trt"
    assert deg_gate(config).verdict == PASS
    script = render_diffexp_counts_script(config)
    assert "design = ~ pair_id + condition" in script
    assert "design = ~ pair_id + condition" in script
    assert "pair_id" in render_colData(config).splitlines()[0]


@pytest.mark.parametrize(
    "mutator, expected",
    [
        (lambda c: c["samples"]["items"][0].pop("pair_id"), "pair_id"),
        (lambda c: c["samples"]["items"][1].update({"pair_id": "p1"}), "duplicate"),
    ],
)
def test_paired_design_rejects_invalid_pair_metadata(mutator, expected):
    config = _paired_config()
    if expected == "duplicate":
        config["samples"]["items"][1]["condition"] = "untrt"
    else:
        mutator(config)
    reasons = deg_gate(config).reasons
    assert reasons
    assert any(expected in reason for reason in reasons)


def test_paired_design_rejects_missing_mate_extra_rows_and_too_few_pairs():
    config = _paired_config()
    config["samples"]["items"][1]["pair_id"] = "missing"
    assert any("mate" in reason or "exactly" in reason for reason in deg_gate(config).reasons)

    config = _paired_config(pair_ids=["p1", "p2", "p3", "p4"])
    config["samples"].setdefault("items", []).append(dict(config["samples"]["items"][0], sample_id="extra", pair_id="p1"))
    assert any("exactly" in reason or "extra" in reason for reason in deg_gate(config).reasons)

    config = _paired_config(pair_ids=["p1", "p2"])
    assert any("3" in reason and "pair" in reason for reason in deg_gate(config).reasons)


@pytest.mark.parametrize(
    "change, marker",
    [
        (lambda c: c.update({"study": {"design": "independent_two_group"}}), "paired_two_group"),
        (lambda c: c["diffexp"].update({"formula": "~ condition"}), "formula"),
        (lambda c: c["diffexp"].update({"reference_condition": "trt"}), "reference"),
        (lambda c: c["diffexp"].update({"min_count_prefilter": 1}), "prefilter"),
        (lambda c: c["samples"]["items"][0].update({"batch": "B1"}), "batch"),
    ],
)
def test_paired_design_rejects_unsupported_policy(change, marker):
    config = _paired_config()
    change(config)
    assert any(marker in reason for reason in paired_design_checks(config))
