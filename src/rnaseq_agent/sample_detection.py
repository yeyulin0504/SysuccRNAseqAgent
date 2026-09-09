"""Read-only sample detection for FASTQ listings and count matrices."""

from __future__ import annotations

import math
import re
from pathlib import PurePosixPath
from typing import Any

_FASTQ_RE = re.compile(r"^(?P<sample>.+?)(?:_R|_)(?P<read>[12])(?:_\d+)?\.(?:fastq|fq)(?:\.gz)?$", re.I)

_MAX_SCAN_ROWS = 100


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


def preview_expression_matrix(content: bytes, filename: str = "") -> dict[str, Any]:
    """Preview a counts / expression matrix uploaded as bytes.

    Returns a dict with at least: ok, matrix_type, gene_id_column, samples,
    can_run_deseq2, warnings, sample_count, gene_count.
    """
    warnings: list[str] = []
    text = content.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()

    if not text.strip():
        return _unknown_preview(warnings + ["文件内容为空。"], samples=[], gene_count=0)

    # --- GEO series matrix detection -----------------------------
    begin_i = end_i = None
    for i, line in enumerate(lines):
        if "!series_matrix_table_begin" in line:
            begin_i = i
        elif begin_i is not None and "!series_matrix_table_end" in line:
            end_i = i
            break
    geo_flagged = begin_i is not None
    if not geo_flagged:
        # Filename heuristic: series_matrix / GSE names with several "!" header
        # lines hint at GEO even when the begin marker is absent. Content wins
        # when the markers above are present, so this only fills the gap.
        bang_lines = sum(1 for line in lines if line.strip().startswith("!"))
        lowered = filename.lower()
        if ("series_matrix" in lowered or "gse" in lowered) and bang_lines >= 2:
            geo_flagged = True
            warnings.append("未找到 !series_matrix_table_begin，已按 GEO 系列矩阵尝试解析。")

    region_lines = lines[begin_i + 1:end_i] if begin_i is not None else lines

    # --- Locate header / data rows within the region ---------------
    header_line: str | None = None
    data_lines: list[str] = []
    for line in region_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("!"):
            continue
        if header_line is None:
            header_line = stripped
        else:
            data_lines.append(line)
            if len(data_lines) >= _MAX_SCAN_ROWS:
                break

    if header_line is None:
        return _unknown_preview(warnings + ["未找到矩阵表头行。"], samples=[], gene_count=len(data_lines))

    # --- Delimiter + columns ---------------------------------------
    tabs = header_line.count("\t")
    commas = header_line.count(",")
    delim = "\t" if tabs > commas else ","
    columns = [cell.strip() for cell in header_line.split(delim)]
    while columns and columns[-1] == "":
        columns.pop()
    if not columns:
        return _unknown_preview(warnings + ["矩阵表头为空。"], samples=[], gene_count=len(data_lines))

    gene_id_column = columns[0]
    # Drop all empty cells, not just trailing ones: a matrix with an empty
    # column in the middle must not leak an "" sample downstream.
    samples = [cell for cell in columns[1:] if cell.strip()]

    # --- Scan data cells ---------------------------------------------
    stats = {"integer": 0, "decimal": 0, "negative": 0, "non_numeric": 0, "empty": 0}
    gene_count = 0
    ncols = len(columns)
    for line in data_lines:
        if not line.strip():
            continue
        gene_count += 1
        row = [cell.strip() for cell in line.split(delim)]
        if len(row) < ncols:
            row.extend([""] * (ncols - len(row)))
        elif len(row) > ncols:
            row = row[:ncols]
        for cell in row[1:]:
            _tally_numeric(cell, stats)

    numeric_cells = stats["integer"] + stats["decimal"] + stats["negative"]
    if gene_count == 0:
        return _unknown_preview(warnings + ["未解析到数据行（矩阵只有表头？）。"],
                                samples=samples, gene_count=0)
    if numeric_cells == 0:
        return _unknown_preview(warnings + ["未检测到数值型表达数据。"],
                                samples=samples, gene_count=gene_count)

    if stats["decimal"] == 0 and stats["negative"] == 0:
        matrix_type = "raw_counts"
        can_run_deseq2 = True
    elif geo_flagged:
        matrix_type = "geo_series_matrix_like"
        can_run_deseq2 = False
        warnings.append("检测到浮点表达值，不是 raw counts，无法运行 DESeq2。")
    else:
        matrix_type = "normalized_expression"
        can_run_deseq2 = False
        reason = "检测到小数，不是 raw counts，无法运行 DESeq2。" if stats["decimal"] else \
            "检测到负数，不是 raw counts，无法运行 DESeq2。"
        warnings.append(reason)

    return {
        "ok": True,
        "matrix_type": matrix_type,
        "gene_id_column": gene_id_column,
        "samples": samples,
        "can_run_deseq2": can_run_deseq2,
        "warnings": warnings,
        "sample_count": len(samples),
        "gene_count": gene_count,
    }


def read_counts_samples(content: bytes) -> list[str]:
    preview = preview_expression_matrix(content)
    samples = preview["samples"]
    if not preview["ok"] and not samples:
        # No sample columns at all (empty table / no header): nothing to return.
        message = "；".join(preview["warnings"]) or "无法识别 counts 矩阵。"
        raise ValueError(message)
    # Header-only matrices (ok=False but samples parsed) still return the sample
    # names, keeping the old "read header only" semantics for counts-preview.
    if not samples or len(samples) != len(set(samples)):
        raise ValueError("counts 矩阵表头缺少样本或包含重复样本名。")
    return samples


def _tally_numeric(cell: str, stats: dict[str, int]) -> None:
    """Classify one data cell into integer / decimal / negative / non-numeric."""
    if not cell:
        stats["empty"] += 1
        return
    try:
        value = float(cell)
    except ValueError:
        stats["non_numeric"] += 1
        return
    if not math.isfinite(value):
        stats["non_numeric"] += 1
        return
    if value < 0:
        stats["negative"] += 1
    elif "." in cell or "e" in cell or "E" in cell:
        stats["decimal"] += 1
    else:
        stats["integer"] += 1


def _unknown_preview(warnings: list[str], samples: list[str], gene_count: int) -> dict[str, Any]:
    return {
        "ok": False,
        "matrix_type": "unknown",
        "gene_id_column": None,
        "samples": samples,
        "can_run_deseq2": False,
        "warnings": warnings,
        "sample_count": len(samples),
        "gene_count": gene_count,
    }
