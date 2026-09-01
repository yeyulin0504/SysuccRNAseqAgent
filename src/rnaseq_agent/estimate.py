from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any


@dataclass(frozen=True)
class RuntimeEstimate:
    hours: float
    summary: str


STEP_WEIGHTS = {
    "fastp": 0.20,
    "star": 0.70,
    "arriba": 0.25,
    "featurecounts": 0.15,
    "rsem": 0.45,
}


def estimate_runtime(config: dict[str, Any]) -> RuntimeEstimate:
    samples = config.get("samples", {}).get("items", [])
    pipeline = config.get("pipeline", {})
    server = config.get("server", {})
    sequencing = config.get("sequencing", {})

    sample_count = max(len(samples), 1)
    reads_million = float(sequencing.get("reads_per_sample_million") or 40)
    threads = max(int(server.get("threads") or 8), 1)

    enabled_weight = 0.0
    for step, weight in STEP_WEIGHTS.items():
        step_cfg = pipeline.get(step, {})
        if step_cfg.get("enabled", True):
            enabled_weight += weight

    thread_factor = max(0.35, 8 / threads)
    hours = sample_count * (reads_million / 40) * enabled_weight * thread_factor
    hours = max(hours, 0.25)
    padded_hours = hours * 1.25

    if padded_hours < 1:
        text = f"预计耗时约 {ceil(padded_hours * 60)} 分钟。"
    else:
        text = f"预计耗时约 {padded_hours:.1f} 小时。"

    detail = (
        f"{text} 估算依据：{sample_count} 个样本，约 {reads_million:g}M reads/样本，"
        f"{threads} 线程，已启用步骤权重 {enabled_weight:.2f}。真实耗时会受服务器排队和 I/O 影响。"
    )
    return RuntimeEstimate(hours=padded_hours, summary=detail)
