from __future__ import annotations

from rnaseq_agent.qc_verdict import compute_qc_verdict


def _inputs(**overrides: object) -> dict:
    values = {
        "read_quality": {"q30": 0.92},
        "mapping": {"alignment_rate": 0.94},
        "duplication": {"duplicate_rate": 0.18},
        "strandedness": {"observed": "reverse", "expected": "reverse"},
        "sample_outlier": {"outliers": []},
    }
    values.update(overrides)
    return values


def test_single_dimension_fail_propagates_to_overall_fail() -> None:
    verdict = compute_qc_verdict(
        _inputs(mapping={"alignment_rate": 0.51}),
        thresholds={"mapping": {"fail_below": 0.8}},
    )

    assert verdict["status"] == "fail"
    assert verdict["dimensions"]["mapping"]["status"] == "fail"
    assert verdict["de_readiness"] == "blocked"


def test_warn_dimensions_aggregate_to_warn_and_de_caution() -> None:
    verdict = compute_qc_verdict(
        _inputs(
            read_quality={"q30": 0.78},
            duplication={"duplicate_rate": 0.55},
        ),
        thresholds={
            "read_quality": {"warn_below": 0.8, "fail_below": 0.6},
            "duplication": {"warn_above": 0.5, "fail_above": 0.8},
        },
    )

    assert verdict["status"] == "warn"
    assert verdict["de_readiness"] == "caution"


def test_strandedness_mismatch_is_a_fail() -> None:
    verdict = compute_qc_verdict(
        _inputs(strandedness={"observed": "forward", "expected": "reverse"})
    )

    assert verdict["dimensions"]["strandedness"]["status"] == "fail"
    assert verdict["status"] == "fail"


def test_sample_outlier_is_warn_and_requires_caution() -> None:
    verdict = compute_qc_verdict(
        _inputs(sample_outlier={"outliers": ["sample-7"]})
    )

    assert verdict["dimensions"]["sample_outlier"]["status"] == "warn"
    assert verdict["de_readiness"] == "caution"


def test_missing_key_evidence_abstains_and_blocks_de() -> None:
    verdict = compute_qc_verdict({"read_quality": {"q30": 0.9}})

    assert verdict["status"] == "abstain"
    assert verdict["de_readiness"] == "blocked"
    assert all(
        verdict["dimensions"][name]["status"] == "abstain"
        for name in ("mapping", "duplication", "strandedness", "sample_outlier")
    )


def test_verdict_always_contains_the_five_standard_dimensions() -> None:
    verdict = compute_qc_verdict(_inputs())

    assert set(verdict["dimensions"]) == {
        "read_quality",
        "mapping",
        "duplication",
        "strandedness",
        "sample_outlier",
    }
