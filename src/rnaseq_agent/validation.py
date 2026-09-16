from __future__ import annotations

import gzip
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .configuration import is_counts_entry_config, is_remote_prestaged_config
from .container import container_errors
from .safety import identifier_error, relative_filename_error


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    checked_files: int
    missing_files: list[Path]
    errors: list[str]


def validate_local_fastqs(
    config: dict[str, Any],
    *,
    check_pipeline: bool = True,
) -> ValidationResult:
    samples = config["samples"]
    local_data_dir = Path(samples["local_data_dir"])
    resolved_data_dir = local_data_dir.resolve()
    paired = config.get("sequencing", {}).get("layout", "paired") == "paired"
    missing: list[Path] = []
    checked = 0
    # counts 直入（DE/CMS 读上传矩阵）没有 FASTQ 输入，跳过本地 FASTQ 校验，
    # 只保留样本表的元数据完整性与 counts_path 可读性检查。
    if is_counts_entry_config(config):
        errors = _counts_entry_errors(config)
        return ValidationResult(
            ok=not missing and not errors,
            checked_files=0,
            missing_files=missing,
            errors=errors,
        )
    errors = _sample_errors(config)

    # remote_path 项目：reads 已经在服务器上（样本里存的是服务器文件名），
    # 本地既没有这些文件、也不该有。只校验样本表元数据与 pipeline/container，
    # 不拿 local_data_dir 去拼路径——否则每个样本都会被误报成「缺少输入文件」。
    if is_remote_prestaged_config(config):
        if check_pipeline:
            errors.extend(_pipeline_errors(config))
            errors.extend(container_errors(config))
        return ValidationResult(
            ok=not errors,
            checked_files=0,
            missing_files=missing,
            errors=errors,
        )

    existing_samples: list[tuple[str, Path, Path | None]] = []
    declared_paths: dict[Path, str] = {}
    for index, sample in enumerate(samples.get("items", []), start=1):
        keys = ("fastq_1", "fastq_2") if paired else ("fastq_1",)
        paths: dict[str, Path] = {}
        for key in keys:
            fastq = sample.get(key)
            if not fastq:
                continue
            checked += 1
            if relative_filename_error(fastq, key):
                continue
            path = (resolved_data_dir / str(fastq)).resolve()
            try:
                path.relative_to(resolved_data_dir)
            except ValueError:
                errors.append(f"FASTQ path escapes samples.local_data_dir: {fastq}")
                continue
            role = f"{sample.get('sample_id') or index}.{key}"
            if path in declared_paths:
                errors.append(
                    f"FASTQ file is reused by {declared_paths[path]} and {role}: {path.name}"
                )
                continue
            declared_paths[path] = role
            if not path.exists():
                missing.append(path)
            elif not path.is_file():
                errors.append(f"FASTQ 路径不是文件：{path}")
            else:
                paths[key] = path
        sample_label = str(sample.get("sample_id") or index)
        if "fastq_1" in paths and (not paired or "fastq_2" in paths):
            existing_samples.append(
                (sample_label, paths["fastq_1"], paths.get("fastq_2"))
            )

    if check_pipeline:
        errors.extend(_pipeline_errors(config))
        errors.extend(container_errors(config))
    if not missing:
        for sample_id, fastq_1, fastq_2 in existing_samples:
            errors.extend(_validate_fastq_sample(sample_id, fastq_1, fastq_2))
    return ValidationResult(ok=not missing and not errors, checked_files=checked, missing_files=missing, errors=errors)


def _validate_fastq_sample(
    sample_id: str,
    fastq_1: Path,
    fastq_2: Path | None,
) -> list[str]:
    if fastq_2 is None:
        return _validate_single_fastq(sample_id, fastq_1)
    return _validate_paired_fastqs(sample_id, fastq_1, fastq_2)


def _validate_single_fastq(sample_id: str, path: Path) -> list[str]:
    count = 0
    try:
        with _open_fastq(path) as handle:
            while True:
                record = _read_record(handle)
                if record is None:
                    break
                count += 1
                error = _record_error(record, count, path.name)
                if error:
                    return [f"样本 {sample_id}：{error}"]
    except (EOFError, gzip.BadGzipFile, OSError, UnicodeError) as exc:
        return [f"样本 {sample_id}：FASTQ 损坏或截断（{path.name}）：{exc}"]
    if count == 0:
        return [f"样本 {sample_id}：FASTQ 为空（{path.name}）"]
    return []


def _validate_paired_fastqs(
    sample_id: str,
    fastq_1: Path,
    fastq_2: Path,
) -> list[str]:
    count = 0
    try:
        with _open_fastq(fastq_1) as left, _open_fastq(fastq_2) as right:
            while True:
                record_1 = _read_record(left)
                record_2 = _read_record(right)
                if record_1 is None and record_2 is None:
                    break
                if record_1 is None or record_2 is None:
                    return [
                        f"样本 {sample_id}：R1/R2 的 reads 数量不一致，"
                        f"差异出现在第 {count + 1} 条附近。"
                    ]
                count += 1
                for record, filename in (
                    (record_1, fastq_1.name),
                    (record_2, fastq_2.name),
                ):
                    error = _record_error(record, count, filename)
                    if error:
                        return [f"样本 {sample_id}：{error}"]
                if _read_id(record_1[0]) != _read_id(record_2[0]):
                    return [
                        f"样本 {sample_id}：R1/R2 的 read ID 在第 {count} 条不一致。"
                    ]
    except (EOFError, gzip.BadGzipFile, OSError, UnicodeError) as exc:
        return [
            f"样本 {sample_id}：双端 FASTQ 损坏或截断，"
            f"问题出现在第 {count + 1} 对 reads 附近：{exc}"
        ]
    if count == 0:
        return [f"样本 {sample_id}：双端 FASTQ 文件为空。"]
    return []


def _open_fastq(path: Path) -> TextIO:
    if path.name.lower().endswith(".gz"):
        return gzip.open(path, "rt", encoding="ascii", errors="strict")
    return path.open("r", encoding="ascii", errors="strict")


def _read_record(handle: TextIO) -> tuple[str, str, str, str] | None:
    header = handle.readline()
    if not header:
        return None
    sequence = handle.readline()
    plus = handle.readline()
    quality = handle.readline()
    if not sequence or not plus or not quality:
        raise EOFError("FASTQ ended inside a four-line record.")
    return header.rstrip(), sequence.rstrip(), plus.rstrip(), quality.rstrip()


def _record_error(
    record: tuple[str, str, str, str],
    record_number: int,
    filename: str,
) -> str:
    header, sequence, plus, quality = record
    if not header.startswith("@"):
        return f"{filename} 第 {record_number} 条记录的 header 格式错误。"
    if not plus.startswith("+"):
        return f"{filename} 第 {record_number} 条记录缺少 '+' 行。"
    if len(sequence) != len(quality):
        return (
            f"{filename} 第 {record_number} 条记录的序列和质量值长度不一致。"
        )
    return ""


def _read_id(header: str) -> str:
    read_id = header.split(maxsplit=1)[0].removeprefix("@")
    if read_id.endswith(("/1", "/2")):
        read_id = read_id[:-2]
    return read_id


def _counts_entry_errors(config: dict[str, Any]) -> list[str]:
    """Metadata checks for a counts 直入 project (no FASTQ input).

    Keeps the sample-table and server checks that still apply (project id,
    at least one sample, valid sample ids, remote workdir) plus the presence
    of the uploaded matrix path recorded under ``samples.counts_path``.
    """
    errors: list[str] = []
    samples = config.get("samples", {})
    items = samples.get("items", [])

    project_id_error = identifier_error(config.get("project", {}).get("id"), "project.id")
    if project_id_error:
        errors.append(project_id_error)

    if not items:
        errors.append("At least one sample is required.")

    counts_path = str(samples.get("counts_path") or "").strip()
    if counts_path and not Path(counts_path).is_file():
        errors.append(f"counts 直入需要本地上传矩阵文件：{counts_path}")
    elif not counts_path:
        errors.append("counts 直入缺少 samples.counts_path（本地上传矩阵）。")

    seen_ids: set[str] = set()
    for index, sample in enumerate(items, start=1):
        sample_id = sample.get("sample_id")
        if not sample_id:
            errors.append(f"Sample {index} is missing sample_id.")
        elif sample_id in seen_ids:
            errors.append(f"Duplicate sample_id: {sample_id}")
        else:
            seen_ids.add(sample_id)
        sample_id_error = identifier_error(sample_id, f"samples.items[{index - 1}].sample_id")
        if sample_id_error:
            errors.append(sample_id_error)
        if not str(sample.get("condition", "")).strip():
            errors.append(f"Sample {sample_id or index} is missing condition.")

    server = config.get("server", {})
    if not server.get("remote_workdir"):
        errors.append("Missing server.remote_workdir.")

    return errors


def _sample_errors(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    samples = config.get("samples", {})
    items = samples.get("items", [])
    paired = config.get("sequencing", {}).get("layout", "paired") == "paired"

    project_id_error = identifier_error(config.get("project", {}).get("id"), "project.id")
    if project_id_error:
        errors.append(project_id_error)

    if not items:
        errors.append("At least one sample is required.")

    if not samples.get("local_data_dir"):
        errors.append("Missing samples.local_data_dir.")
    if not samples.get("remote_data_dir"):
        errors.append("Missing samples.remote_data_dir.")

    seen_ids: set[str] = set()
    for index, sample in enumerate(items, start=1):
        sample_id = sample.get("sample_id")
        if not sample_id:
            errors.append(f"Sample {index} is missing sample_id.")
        elif sample_id in seen_ids:
            errors.append(f"Duplicate sample_id: {sample_id}")
        else:
            seen_ids.add(sample_id)

        sample_id_error = identifier_error(sample_id, f"samples.items[{index - 1}].sample_id")
        if sample_id_error:
            errors.append(sample_id_error)

        if not sample.get("fastq_1"):
            errors.append(f"Sample {sample_id or index} is missing fastq_1.")
        else:
            error = relative_filename_error(
                sample.get("fastq_1"),
                f"samples.items[{index - 1}].fastq_1",
            )
            if error:
                errors.append(error)
        if paired and not sample.get("fastq_2"):
            errors.append(f"Sample {sample_id or index} is missing fastq_2 for paired-end sequencing.")
        elif paired:
            error = relative_filename_error(
                sample.get("fastq_2"),
                f"samples.items[{index - 1}].fastq_2",
            )
            if error:
                errors.append(error)

    server = config.get("server", {})
    if not server.get("remote_workdir"):
        errors.append("Missing server.remote_workdir.")

    return errors


def _pipeline_errors(config: dict[str, Any]) -> list[str]:
    pipeline = config.get("pipeline", {})
    errors: list[str] = []
    if not any(bool(step.get("enabled")) for step in pipeline.values() if isinstance(step, dict)):
        errors.append("At least one pipeline step must be enabled.")
    star_enabled = pipeline.get("star", {}).get("enabled", True)

    dependent_steps = ("arriba", "featurecounts", "rsem")
    for step in dependent_steps:
        if pipeline.get(step, {}).get("enabled", False) and not star_enabled:
            errors.append(f"{step} requires star to be enabled.")

    # 框架 15.3：diffexp（条件开放）需要 STAR 比对与 featureCounts counts。
    if pipeline.get("diffexp", {}).get("enabled"):
        if not star_enabled:
            errors.append("diffexp requires star to be enabled.")
        if not pipeline.get("featurecounts", {}).get("enabled", False):
            errors.append("diffexp requires featurecounts to be enabled.")
        from .differential import diffexp_design_checks

        errors.extend(diffexp_design_checks(config))

    reference = config.get("reference", {})
    required_reference_keys = []
    if pipeline.get("star", {}).get("enabled", True):
        required_reference_keys.append("star_index_dir")
    if pipeline.get("featurecounts", {}).get("enabled", False) or pipeline.get("arriba", {}).get("enabled", False):
        required_reference_keys.append("remote_gtf_path")
    if pipeline.get("arriba", {}).get("enabled", False):
        required_reference_keys.append("remote_genome_fasta_path")
    if pipeline.get("rsem", {}).get("enabled", False):
        required_reference_keys.append("rsem_index_prefix")

    for key in required_reference_keys:
        if not reference.get(key):
            errors.append(f"Missing required reference setting: {key}")

    return errors
