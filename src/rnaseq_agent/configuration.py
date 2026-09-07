from __future__ import annotations

from copy import deepcopy
from typing import Any

from .defaults import (
    DEFAULT_CMS,
    DEFAULT_CONTAINER,
    DEFAULT_DIFFEXP,
    DEFAULT_PIPELINE,
    DEFAULT_REFERENCE,
)


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(config)

    reference = {**DEFAULT_REFERENCE, **normalized.get("reference", {})}
    normalized["reference"] = reference

    server = normalized.setdefault("server", {})
    server.setdefault("port", 22)
    server.setdefault("shell", "bash")
    server.setdefault("init_commands", [])

    normalized.setdefault(
        "polling",
        {
            "interval_seconds": 300,
            "timeout_hours": 168,
        },
    )

    pipeline = normalized.setdefault("pipeline", {})
    for step, defaults in DEFAULT_PIPELINE.items():
        pipeline.setdefault(step, defaults.copy())
        pipeline[step].setdefault("enabled", defaults["enabled"])
        pipeline[step].setdefault("version", defaults["version"])

    container = {**DEFAULT_CONTAINER, **normalized.get("container", {})}
    container.setdefault("bind_paths", [])
    normalized["container"] = container

    diffexp = {**DEFAULT_DIFFEXP, **normalized.get("diffexp", {})}
    diffexp.setdefault("formula", DEFAULT_DIFFEXP["formula"])
    diffexp.setdefault("min_replicates_per_group", DEFAULT_DIFFEXP["min_replicates_per_group"])
    normalized["diffexp"] = diffexp

    cms = {**DEFAULT_CMS, **normalized.get("cms", {})}
    cms.setdefault("n_perm", DEFAULT_CMS["n_perm"])
    cms.setdefault("fdr", DEFAULT_CMS["fdr"])
    cms.setdefault("seed", DEFAULT_CMS["seed"])
    cms.setdefault("do_plot", DEFAULT_CMS["do_plot"])
    cms.setdefault("min_samples", DEFAULT_CMS["min_samples"])
    normalized["cms"] = cms

    normalized.setdefault("notification", {"email_enabled": False})
    normalized.setdefault("sequencing", {"layout": "paired", "strandedness": "auto"})
    normalized.setdefault("samples", {"items": []})

    execution = normalized.setdefault("execution", {})
    execution.setdefault("mode", "free")
    execution.setdefault("skill_id", "")
    execution.setdefault("contract_file", "analysis_contract.json")
    return normalized
