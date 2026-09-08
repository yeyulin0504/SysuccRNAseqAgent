"""Read-only sample detection for FASTQ listings and count matrices."""

from __future__ import annotations

import csv
import io
import re
from pathlib import PurePosixPath


_FASTQ_RE = re.compile(r"^(?P<sample>.+?)(?:_R|_)(?P<read>[12])(?:_\d+)?\.(?:fastq|fq)(?:\.gz)?$", re.I)


def detect_fastq_pairs(paths: list[str]) -> dict[str, list]:
    grouped: dict[str, dict[str, str]] = {}
    unmatched: list[str] = []
    for path in paths:
        match = _FASTQ_RE.match(PurePosixPath(path).name)
        if not match:
            unmatched.append(path)
            continue
        sample = match.group("sample")
        read = match.group("read")
        if read in grouped.setdefault(sample, {}):
            unmatched.extend([grouped[sample].pop(read), path])
            continue
        grouped[sample][read] = path
    samples: list[dict[str, str]] = []
    for sample, reads in sorted(grouped.items()):
        if set(reads) == {"1", "2"}:
            samples.append({"sample_id": sample, "fastq_1": reads["1"], "fastq_2": reads["2"]})
        else:
            unmatched.extend(reads.values())
    return {"samples": samples, "unmatched": sorted(unmatched)}


def read_counts_samples(content: bytes) -> list[str]:
    text = content.decode("utf-8-sig")
    first = text.splitlines()[0] if text.splitlines() else ""
    dialect = csv.excel_tab if "\t" in first else csv.excel
    header = next(csv.reader(io.StringIO(first), dialect=dialect), [])
    samples = [value.strip() for value in header[1:] if value.strip()]
    if not samples or len(samples) != len(set(samples)):
        raise ValueError("counts 矩阵表头缺少样本或包含重复样本名。")
    return samples
