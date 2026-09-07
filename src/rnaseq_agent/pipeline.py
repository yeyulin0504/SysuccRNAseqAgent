from __future__ import annotations

from typing import Any

from .container import wrap_command
from .shell import shell_quote
from .safety import identifier_error, relative_filename_error

# 说明书 §5 分阶段：stage1 = 真实 reads QC（fastp），stage2 = 比对/定量/融合。
STAGE_QC = "qc"      # fastp（+ MultiQC 汇总，若站点提供）
STAGE_QUANT = "quant"  # STAR -> featureCounts + RSEM + Arriba（比对后 QC 检查点前）
STAGE_DE = "de"      # 条件开放：DESeq2 差异表达 / fgsea 富集
STAGE_CMS = "cms"    # 条件开放：CMScaller CMS 结直肠癌分子分型
STAGE_COUNTS = "counts"  # counts 直入：DESeq2 差异表达 / CMScaller CMS（无 FASTQ 处理）
ALL_STAGES = (STAGE_QC, STAGE_QUANT, STAGE_DE, STAGE_CMS, STAGE_COUNTS)


def pipeline_stages(config: dict[str, Any]) -> list[str]:
    """Deterministic stage list for the frozen pipeline config.

    Stage 1 (QC) runs whenever fastp is enabled. Stage 2 (quant) runs when any
    of STAR / featureCounts / RSEM / Arriba is enabled. The conditional DE
    stage (Stage 3) runs only when ``pipeline.diffexp.enabled`` is true, and
    the conditional CMS stage (Stage 4) only when ``pipeline.cms.enabled`` is
    true.

    Counts 直入（2026-09-08）：当 ``cms.run_mode == "counts"`` 时 diffexp/cms
    不再挂在 FASTQ 主流程后，而是单独由 ``de_cms_counts`` 阶段执行（上游
    featurecounts 产物不再被引用）；此时主流程只剩 qc/quant（若显式禁用
    fastp/star 则主流程为空），diffexp 与 cms 都在 counts 阶段运行。
    """
    pipeline = config.get("pipeline", {})
    # counts 直入会关闭主流程对 featureCounts 的依赖，diffexp/cms 读上传矩阵。
    counts_entry = _counts_entry_enabled(config)
    stages: list[str] = []
    if counts_entry:
        # counts 直入：可选的 FASTQ 主流程与 counts 阶段互斥路径留给上层执行
        # 编排决定；这里只保证 diffexp/cms 不再错误地作为主流程追加阶段出现。
        stages.append(STAGE_COUNTS)
        return stages
    if pipeline.get("fastp", {}).get("enabled"):
        stages.append(STAGE_QC)
    quant_enabled = any(
        pipeline.get(step, {}).get("enabled")
        for step in ("star", "featurecounts", "rsem", "arriba")
    )
    if quant_enabled:
        stages.append(STAGE_QUANT)
    if pipeline.get("diffexp", {}).get("enabled"):
        stages.append(STAGE_DE)
    if pipeline.get("cms", {}).get("enabled"):
        stages.append(STAGE_CMS)
    return stages


def _counts_entry_enabled(config: dict[str, Any]) -> bool:
    """Whether the FASTQ main pipeline is bypassed for a counts 直入 stage."""
    from .cms import COUNTS_ENTRY, cms_run_mode

    if cms_run_mode(config) == COUNTS_ENTRY:
        return True
    return bool(config.get("study", {}).get("input", {}).get("counts_matrix"))


def render_env_setup_script(config: dict[str, Any]) -> str:
    init_commands = config["server"].get("init_commands") or []
    if not init_commands:
        return "#!/usr/bin/env bash\n"
    body = "\n".join(init_commands)
    return f"""#!/usr/bin/env bash
set -euo pipefail

{body}
"""


def render_remote_pipeline_script(config: dict[str, Any]) -> str:
    server = config["server"]
    reference = config["reference"]
    sequencing = config["sequencing"]
    samples = config["samples"]["items"]
    pipeline = config["pipeline"]
    threads = int(server["threads"])
    paired = sequencing["layout"] == "paired"
    featurecounts_strand = _featurecounts_strand(sequencing.get("strandedness", "auto"))
    fastp_command = wrap_command(config, "fastp")
    star_command = wrap_command(config, "STAR")
    arriba_command = wrap_command(config, "arriba")
    rsem_command = wrap_command(config, "rsem-calculate-expression")
    featurecounts_command = wrap_command(config, "featureCounts")
    sample_blocks = []
    bam_exprs = []
    for sample in samples:
        sample_id = sample["sample_id"]
        sample_id_error = identifier_error(sample_id, "sample_id")
        if sample_id_error:
            raise ValueError(sample_id_error)
        for key in ("fastq_1", "fastq_2") if paired else ("fastq_1",):
            filename_error = relative_filename_error(sample.get(key), key)
            if filename_error:
                raise ValueError(filename_error)
        raw_r1 = f'"$INPUTDIR"/{shell_quote(sample["fastq_1"])}'
        raw_r2 = (
            f'"$INPUTDIR"/{shell_quote(sample.get("fastq_2", ""))}'
            if paired
            else ""
        )

        read1_expr = raw_r1
        read2_expr = raw_r2
        fastp_block = ""
        if pipeline["fastp"]["enabled"]:
            trim_r1_path = f"fastp/{sample_id}.R1.fastq.gz"
            trim_r1 = shell_quote(trim_r1_path)
            if paired:
                trim_r2_path = f"fastp/{sample_id}.R2.fastq.gz"
                trim_r2 = shell_quote(trim_r2_path)
                fastp_block = f"""
mkdir -p fastp
{fastp_command} \\
  -i {raw_r1} \\
  -I {raw_r2} \\
  -o {trim_r1} \\
  -O {trim_r2} \\
  --thread {threads} \\
  --json {shell_quote(f"fastp/{sample_id}.json")} \\
  --html {shell_quote(f"fastp/{sample_id}.html")}
""".strip()
                read2_expr = trim_r2
            else:
                fastp_block = f"""
mkdir -p fastp
{fastp_command} \\
  -i {raw_r1} \\
  -o {trim_r1} \\
  --thread {threads} \\
  --json {shell_quote(f"fastp/{sample_id}.json")} \\
  --html {shell_quote(f"fastp/{sample_id}.html")}
""".strip()
            read1_expr = trim_r1

        star_block = ""
        if pipeline["star"]["enabled"]:
            read_files = read1_expr if not paired else f"{read1_expr} {read2_expr}"
            star_block = f"""
mkdir -p star
read_cmd=""
if [[ {read1_expr} == *.gz ]]; then
  read_cmd="--readFilesCommand zcat"
fi
{star_command} \\
  --runThreadN {threads} \\
  --genomeDir {shell_quote(reference['star_index_dir'])} \\
  --readFilesIn {read_files} \\
  $read_cmd \\
  --twopassMode Basic \\
  --outFileNamePrefix {shell_quote(f"star/{sample_id}.")} \\
  --outSAMtype BAM SortedByCoordinate \\
  --outSAMunmapped Within \\
  --chimOutType Junctions SeparateSAMold WithinBAM HardClip \\
  --chimSegmentMin 10 \\
  --chimJunctionOverhangMin 10 \\
  --chimScoreDropMax 30 \\
  --chimScoreJunctionNonGTAG 0 \\
  --chimScoreSeparation 1 \\
  --chimSegmentReadGapMax 3 \\
  --chimMultimapNmax 50 \\
  --quantMode TranscriptomeSAM GeneCounts
""".strip()
            bam_exprs.append(shell_quote(f"star/{sample_id}.Aligned.sortedByCoord.out.bam"))

        arriba_block = ""
        if pipeline["arriba"]["enabled"]:
            optional_arriba = []
            if reference.get("arriba_blacklist_path"):
                optional_arriba.append(f"-b {shell_quote(reference['arriba_blacklist_path'])}")
            if reference.get("arriba_known_fusions_path"):
                optional_arriba.append(f"-k {shell_quote(reference['arriba_known_fusions_path'])}")
            arriba_cmd_lines = [
                f"{arriba_command} \\",
                f"  -x {shell_quote(f'star/{sample_id}.Aligned.sortedByCoord.out.bam')} \\",
                f"  -c {shell_quote(f'star/{sample_id}.Chimeric.out.sam')} \\",
                f"  -g {shell_quote(reference['remote_gtf_path'])} \\",
                f"  -a {shell_quote(reference['remote_genome_fasta_path'])} \\",
                f"  -o {shell_quote(f'arriba/{sample_id}.fusions.tsv')} \\",
                f"  -O {shell_quote(f'arriba/{sample_id}.fusions.discarded.tsv')}",
            ]
            if optional_arriba:
                arriba_cmd_lines[-1] += " \\"
                arriba_cmd_lines.extend(f"  {arg}" for arg in optional_arriba)
            arriba_block = f"""
mkdir -p arriba
{chr(10).join(arriba_cmd_lines)}
""".strip()

        rsem_block = ""
        if pipeline["rsem"]["enabled"]:
            rsem_lines = [
                f"{rsem_command} \\",
                "  --alignments \\",
            ]
            if paired:
                rsem_lines.append("  --paired-end \\")
            rsem_lines.extend(
                [
                    f"  -p {threads} \\",
                    f"  {shell_quote(f'star/{sample_id}.Aligned.toTranscriptome.out.bam')} \\",
                    f"  {shell_quote(reference['rsem_index_prefix'])} \\",
                    f"  {shell_quote(f'rsem/{sample_id}')}",
                ]
            )
            rsem_block = f"""
mkdir -p rsem
{chr(10).join(rsem_lines)}
""".strip()

        blocks = [fastp_block, star_block, arriba_block, rsem_block]
        rendered = "\n\n".join(block for block in blocks if block)
        sample_blocks.append(f"# Sample {shell_quote(sample_id)}\n{rendered}")

    featurecounts_block = ""
    if pipeline["featurecounts"]["enabled"] and bam_exprs:
        featurecounts_lines = [
            f"{featurecounts_command} \\",
            f"  -T {threads} \\",
            f"  -a {shell_quote(reference['remote_gtf_path'])} \\",
            f"  -o {shell_quote('featurecounts/gene_counts.txt')} \\",
            f"  -s {featurecounts_strand} \\",
        ]
        if paired:
            featurecounts_lines.append("  -p \\")
        featurecounts_lines.append(f"  {' '.join(bam_exprs)}")
        featurecounts_block = f"""
mkdir -p featurecounts
{chr(10).join(featurecounts_lines)}
""".strip()

    # 框架 15.3 条件开放：DESeq2 差异表达（冻结 ~ condition 模板）。
    # counts 落盘后、比对后 QC 人工检查点之前由脚本在同一 attempt 内执行；
    # 设计门禁（单样本/不足重复/混杂）在计划冻结阶段已经拒绝非法设计。
    diffexp_block = ""
    if pipeline.get("diffexp", {}).get("enabled"):
        from .differential import render_diffexp_script, render_colData

        diffexp_script = "scripts/diffexp_deseq2.R"
        col_data = "scripts/colData.tsv"
        diffexp_block = f"""
mkdir -p diffexp
cat > diffexp/colData.tsv <<'RNA_AGENT_COLDATA_EOF'
{render_colData(config)}RNA_AGENT_COLDATA_EOF
if command -v Rscript >/dev/null 2>&1; then
  Rscript {shell_quote(diffexp_script)} featurecounts/gene_counts.txt diffexp/colData.tsv diffexp/deseq2
elif [ -x "$RNASEQ_RSEM_IMAGE" ]; then
  {wrap_command(config, 'Rscript')} {shell_quote(diffexp_script)} featurecounts/gene_counts.txt diffexp/colData.tsv diffexp/deseq2
else
  echo "diffexp requested but Rscript is not available; skipping DE stage." >&2
fi
""".strip()

    # 框架 15.3 条件开放：CMScaller CMS 结直肠癌分子分型（已注册分型模型）。
    # 输入为 featureCounts 原始 counts；癌种/样本数/featureCounts 前置在
    # 计划冻结阶段由 CMS 门禁批准后才启用。
    cms_block = ""
    if pipeline.get("cms", {}).get("enabled"):
        from .cms import render_cms_script

        cms_script = "scripts/cms_cmscaller.R"
        cms_block = f"""
mkdir -p cms
if command -v Rscript >/dev/null 2>&1; then
  Rscript {shell_quote(cms_script)} featurecounts/gene_counts.txt cms/cms
elif [ -x "$RNASEQ_RSEM_IMAGE" ]; then
  {wrap_command(config, 'Rscript')} {shell_quote(cms_script)} featurecounts/gene_counts.txt cms/cms
else
  echo "cms requested but Rscript is not available; skipping CMS stage." >&2
fi
""".strip()

    sample_section = "\n\n".join(sample_blocks)
    env_setup_line = "source scripts/env_setup.sh" if server.get("init_commands") else ""
    prefix = f"{env_setup_line}\n\n" if env_setup_line else ""
    script = f"""#!/usr/bin/env bash
set -euo pipefail
umask 077

WORKDIR="${{RNASEQ_RUN_WORKDIR:-$PWD}}"
INPUTDIR="${{RNASEQ_INPUT_DIR:-$WORKDIR/raw}}"
mkdir -p "$WORKDIR"/{{logs,scripts,status}}
chmod 700 "$WORKDIR" "$WORKDIR"/{{logs,scripts,status}}
cd "$WORKDIR"

rm -f status/completed.flag status/failed.flag

cleanup_failure() {{
  echo "failed" > status/state.txt
  date -Is > status/ended_at.txt
  touch status/failed.flag
}}
trap cleanup_failure ERR

echo "running" > status/state.txt
date -Is > status/started_at.txt

{prefix}{sample_section}

{featurecounts_block}

{diffexp_block}

{cms_block}

echo "completed" > status/state.txt
date -Is > status/ended_at.txt
touch status/completed.flag
"""
    return script


# -- staged rendering (说明书 §5：真实分阶段) -----------------------------


def _script_header(config: dict[str, Any], stage_name: str) -> str:
    server = config["server"]
    env_setup_line = "source scripts/env_setup.sh" if server.get("init_commands") else ""
    prefix = f"{env_setup_line}\n\n" if env_setup_line else ""
    return f"""#!/usr/bin/env bash
set -euo pipefail
umask 077

WORKDIR="${{RNASEQ_RUN_WORKDIR:-$PWD}}"
INPUTDIR="${{RNASEQ_INPUT_DIR:-$WORKDIR/raw}}"
mkdir -p "$WORKDIR"/{{logs,scripts,status}}
chmod 700 "$WORKDIR" "$WORKDIR"/{{logs,scripts,status}}
cd "$WORKDIR"

rm -f status/completed.flag status/failed.flag status/{stage_name}.completed.flag status/{stage_name}.failed.flag

cleanup_failure() {{
  echo "failed" > status/state.txt
  echo "failed" > status/{stage_name}.flag
  date -Is > status/ended_at.txt
  touch status/failed.flag status/{stage_name}.failed.flag
}}
trap cleanup_failure ERR

echo "running" > status/state.txt
date -Is > status/started_at.txt

"""


def _script_footer(config: dict[str, Any], stage_name: str) -> str:
    return f"""
echo "completed" > status/state.txt
date -Is > status/ended_at.txt
touch status/completed.flag status/{stage_name}.completed.flag
"""


def _sample_reads(sample: dict[str, Any], paired: bool) -> tuple[str, str]:
    """Input read expressions (raw under $INPUTDIR)."""
    raw_r1 = f'"$INPUTDIR"/{shell_quote(sample["fastq_1"])}'
    raw_r2 = (
        f'"$INPUTDIR"/{shell_quote(sample.get("fastq_2", ""))}'
        if paired
        else ""
    )
    return raw_r1, raw_r2


def _fastp_block(
    config: dict[str, Any],
    sample: dict[str, Any],
    *,
    paired: bool,
    threads: int,
    read1_expr: str,
    read2_expr: str,
) -> tuple[str, str, str]:
    """fastp block for one sample. Returns (block, clean_r1, clean_r2)."""
    fastp_command = wrap_command(config, "fastp")
    sample_id = sample["sample_id"]
    trim_r1_path = f"fastp/{sample_id}.R1.fastq.gz"
    trim_r1 = shell_quote(trim_r1_path)
    read2_expr = read2_expr
    if paired:
        trim_r2_path = f"fastp/{sample_id}.R2.fastq.gz"
        trim_r2 = shell_quote(trim_r2_path)
        block = f"""
mkdir -p fastp
{fastp_command} \\
  -i {read1_expr} \\
  -I {read2_expr} \\
  -o {trim_r1} \\
  -O {trim_r2} \\
  --thread {threads} \\
  --json {shell_quote(f"fastp/{sample_id}.json")} \\
  --html {shell_quote(f"fastp/{sample_id}.html")}
""".strip()
        return block, trim_r1_path, trim_r2_path
    block = f"""
mkdir -p fastp
{fastp_command} \\
  -i {read1_expr} \\
  -o {trim_r1} \\
  --thread {threads} \\
  --json {shell_quote(f"fastp/{sample_id}.json")} \\
  --html {shell_quote(f"fastp/{sample_id}.html")}
""".strip()
    return block, trim_r1_path, ""


def _quant_blocks(
    config: dict[str, Any],
    sample: dict[str, Any],
    *,
    paired: bool,
    threads: int,
    read1_expr: str,
    read2_expr: str,
) -> list[str]:
    """STAR/Arriba/RSEM blocks for one sample (uses cleaned reads)."""
    reference = config["reference"]
    pipeline = config["pipeline"]
    sample_id = sample["sample_id"]
    star_command = wrap_command(config, "STAR")
    arriba_command = wrap_command(config, "arriba")
    rsem_command = wrap_command(config, "rsem-calculate-expression")
    blocks: list[str] = []

    if pipeline.get("star", {}).get("enabled"):
        read_files = read1_expr if not paired else f"{read1_expr} {read2_expr}"
        blocks.append(
            f"""
mkdir -p star
read_cmd=""
if [[ {read1_expr} == *.gz ]]; then
  read_cmd="--readFilesCommand zcat"
fi
{star_command} \\
  --runThreadN {threads} \\
  --genomeDir {shell_quote(reference['star_index_dir'])} \\
  --readFilesIn {read_files} \\
  $read_cmd \\
  --twopassMode Basic \\
  --outFileNamePrefix {shell_quote(f"star/{sample_id}.")} \\
  --outSAMtype BAM SortedByCoordinate \\
  --outSAMunmapped Within \\
  --chimOutType Junctions SeparateSAMold WithinBAM HardClip \\
  --chimSegmentMin 10 \\
  --chimJunctionOverhangMin 10 \\
  --chimScoreDropMax 30 \\
  --chimScoreJunctionNonGTAG 0 \\
  --chimScoreSeparation 1 \\
  --chimSegmentReadGapMax 3 \\
  --chimMultimapNmax 50 \\
  --quantMode TranscriptomeSAM GeneCounts
""".strip()
        )

    if pipeline.get("arriba", {}).get("enabled"):
        optional_arriba = []
        if reference.get("arriba_blacklist_path"):
            optional_arriba.append(f"-b {shell_quote(reference['arriba_blacklist_path'])}")
        if reference.get("arriba_known_fusions_path"):
            optional_arriba.append(f"-k {shell_quote(reference['arriba_known_fusions_path'])}")
        arriba_cmd_lines = [
            f"{arriba_command} \\",
            f"  -x {shell_quote(f'star/{sample_id}.Aligned.sortedByCoord.out.bam')} \\",
            f"  -c {shell_quote(f'star/{sample_id}.Chimeric.out.sam')} \\",
            f"  -g {shell_quote(reference['remote_gtf_path'])} \\",
            f"  -a {shell_quote(reference['remote_genome_fasta_path'])} \\",
            f"  -o {shell_quote(f'arriba/{sample_id}.fusions.tsv')} \\",
            f"  -O {shell_quote(f'arriba/{sample_id}.fusions.discarded.tsv')}",
        ]
        if optional_arriba:
            arriba_cmd_lines[-1] += " \\"
            arriba_cmd_lines.extend(f"  {arg}" for arg in optional_arriba)
        blocks.append(
            f"""
mkdir -p arriba
{chr(10).join(arriba_cmd_lines)}
""".strip()
        )

    if pipeline.get("rsem", {}).get("enabled"):
        rsem_lines = [
            f"{rsem_command} \\",
            "  --alignments \\",
        ]
        if paired:
            rsem_lines.append("  --paired-end \\")
        rsem_lines.extend(
            [
                f"  -p {threads} \\",
                f"  {shell_quote(f'star/{sample_id}.Aligned.toTranscriptome.out.bam')} \\",
                f"  {shell_quote(reference['rsem_index_prefix'])} \\",
                f"  {shell_quote(f'rsem/{sample_id}')}",
            ]
        )
        blocks.append(
            f"""
mkdir -p rsem
{chr(10).join(rsem_lines)}
""".strip()
        )
    return blocks


def render_stage_script(config: dict[str, Any], stage: str) -> str:
    """Render the remote script for one scientific stage.

    说明书 §5：

    - ``qc``   : fastp per-sample QC JSON/HTML + 清洗后 reads（Stage 1）;
    - ``quant``: STAR -> Arriba -> RSEM，featureCounts 汇总矩阵（Stage 2）;
    - ``de``   : 条件开放 DESeq2（Stage 3）；fgsea 需 MSigDB 资产，缺失时阻断;
    - ``cms``  : 条件开放 CMScaller CMS 结直肠癌分子分型（Stage 4）。

    Stage 2 只使用 Stage 1 产生的 fastp/ 清洗 reads（同一 attempt 目录），
    不再读取原始输入。每阶段写独立的 status/<stage>.completed.flag。
    """
    if stage not in ALL_STAGES:
        raise ValueError(f"Unsupported stage: {stage!r}. Allowed: {ALL_STAGES}")

    server = config["server"]
    reference = config["reference"]
    sequencing = config["sequencing"]
    samples = config["samples"]["items"]
    pipeline = config["pipeline"]
    threads = int(server["threads"])
    paired = sequencing["layout"] == "paired"

    body: list[str] = []

    if stage == STAGE_QC:
        if not pipeline.get("fastp", {}).get("enabled"):
            raise ValueError("Stage qc requested but pipeline.fastp.enabled is False.")
        blocks: list[str] = []
        for sample in samples:
            sample_id = sample["sample_id"]
            error = identifier_error(sample_id, "sample_id")
            if error:
                raise ValueError(error)
            for key in ("fastq_1", "fastq_2") if paired else ("fastq_1",):
                filename_error = relative_filename_error(sample.get(key), key)
                if filename_error:
                    raise ValueError(filename_error)
            raw_r1, raw_r2 = _sample_reads(sample, paired)
            block, _, _ = _fastp_block(
                config,
                sample,
                paired=paired,
                threads=threads,
                read1_expr=raw_r1,
                read2_expr=raw_r2,
            )
            blocks.append(f"# Sample {shell_quote(sample_id)}\n{block}")
        body.append("\n\n".join(blocks))

    elif stage == STAGE_QUANT:
        quant_enabled = any(
            pipeline.get(step, {}).get("enabled")
            for step in ("star", "featurecounts", "rsem", "arriba")
        )
        if not quant_enabled:
            raise ValueError("Stage quant requested but no quant tool is enabled.")
        featurecounts_strand = _featurecounts_strand(sequencing.get("strandedness", "auto"))
        featurecounts_command = wrap_command(config, "featureCounts")
        sample_blocks: list[str] = []
        bam_exprs: list[str] = []
        for sample in samples:
            sample_id = sample["sample_id"]
            error = identifier_error(sample_id, "sample_id")
            if error:
                raise ValueError(error)
            # 输入为 Stage 1 清洗后的 fastp/<sample>.R1/R2.fastq.gz（同目录）。
            read1_expr = shell_quote(f"fastp/{sample_id}.R1.fastq.gz")
            read2_expr = shell_quote(f"fastp/{sample_id}.R2.fastq.gz") if paired else ""
            blocks = _quant_blocks(
                config,
                sample,
                paired=paired,
                threads=threads,
                read1_expr=read1_expr,
                read2_expr=read2_expr,
            )
            if blocks:
                sample_blocks.append(f"# Sample {shell_quote(sample_id)}\n" + "\n\n".join(blocks))
            if pipeline.get("star", {}).get("enabled"):
                bam_exprs.append(shell_quote(f"star/{sample_id}.Aligned.sortedByCoord.out.bam"))

        if sample_blocks:
            body.append("\n\n".join(sample_blocks))

        if pipeline.get("featurecounts", {}).get("enabled") and bam_exprs:
            featurecounts_lines = [
                f"{featurecounts_command} \\",
                f"  -T {threads} \\",
                f"  -a {shell_quote(reference['remote_gtf_path'])} \\",
                f"  -o {shell_quote('featurecounts/gene_counts.txt')} \\",
                f"  -s {featurecounts_strand} \\",
            ]
            if paired:
                featurecounts_lines.append("  -p \\")
            featurecounts_lines.append(f"  {' '.join(bam_exprs)}")
            body.append(
                f"""
mkdir -p featurecounts
{chr(10).join(featurecounts_lines)}
""".strip()
            )

    elif stage == STAGE_DE:
        if not pipeline.get("diffexp", {}).get("enabled"):
            raise ValueError("Stage de requested but pipeline.diffexp.enabled is False.")
        from .differential import render_colData, render_diffexp_script

        diffexp_script = "scripts/diffexp_deseq2.R"
        col_data = "scripts/colData.tsv"
        body.append(
            f"""
mkdir -p diffexp
cat > diffexp/colData.tsv <<'RNA_AGENT_COLDATA_EOF'
{render_colData(config)}RNA_AGENT_COLDATA_EOF
if command -v Rscript >/dev/null 2>&1; then
  Rscript {shell_quote(diffexp_script)} featurecounts/gene_counts.txt diffexp/colData.tsv diffexp/deseq2
elif [ -n "$RNASEQ_RSEM_IMAGE" ] && [ -x "$RNASEQ_RSEM_IMAGE" ]; then
  {wrap_command(config, 'Rscript')} {shell_quote(diffexp_script)} featurecounts/gene_counts.txt diffexp/colData.tsv diffexp/deseq2
else
  echo "diffexp requested but Rscript is not available; skipping DE stage." >&2
fi
""".strip()
        )

    elif stage == STAGE_CMS:
        if not pipeline.get("cms", {}).get("enabled"):
            raise ValueError("Stage cms requested but pipeline.cms.enabled is False.")
        from .cms import render_cms_script

        cms_script = "scripts/cms_cmscaller.R"
        body.append(
            f"""
mkdir -p cms
if command -v Rscript >/dev/null 2>&1; then
  Rscript {shell_quote(cms_script)} featurecounts/gene_counts.txt cms/cms
elif [ -n "$RNASEQ_RSEM_IMAGE" ] && [ -x "$RNASEQ_RSEM_IMAGE" ]; then
  {wrap_command(config, 'Rscript')} {shell_quote(cms_script)} featurecounts/gene_counts.txt cms/cms
else
  echo "cms requested but Rscript is not available; skipping CMS stage." >&2
fi
""".strip()
        )

    elif stage == STAGE_COUNTS:
        # counts 直入（2026-09-08）：DESeq2 差异表达 + CMScaller CMS 在同一
        # 阶段执行，输入为上传的 counts_matrix.tsv（样本设计冻结在 colData）。
        from .differential import render_colData

        blocks: list[str] = [
            f"""
mkdir -p diffexp
cat > diffexp/colData.tsv <<'RNA_AGENT_COLDATA_EOF'
{render_colData(config)}RNA_AGENT_COLDATA_EOF
""".strip()
        ]
        if pipeline.get("diffexp", {}).get("enabled"):
            blocks.append(
                f"""
mkdir -p diffexp
if command -v Rscript >/dev/null 2>&1; then
  Rscript scripts/diffexp_counts_deseq2.R counts_matrix.tsv diffexp/colData.tsv diffexp/deseq2
elif [ -n "$RNASEQ_RSEM_IMAGE" ] && [ -x "$RNASEQ_RSEM_IMAGE" ]; then
  {wrap_command(config, 'Rscript')} scripts/diffexp_counts_deseq2.R counts_matrix.tsv diffexp/colData.tsv diffexp/deseq2
else
  echo "diffexp requested but Rscript is not available; skipping DE stage." >&2
fi
""".strip()
            )
        if pipeline.get("cms", {}).get("enabled"):
            blocks.append(
                f"""
mkdir -p cms
if command -v Rscript >/dev/null 2>&1; then
  Rscript scripts/cms_counts_cmscaller.R counts_matrix.tsv cms/cms
elif [ -n "$RNASEQ_RSEM_IMAGE" ] && [ -x "$RNASEQ_RSEM_IMAGE" ]; then
  {wrap_command(config, 'Rscript')} scripts/cms_counts_cmscaller.R counts_matrix.tsv cms/cms
else
  echo "cms requested but Rscript is not available; skipping CMS stage." >&2
fi
""".strip()
            )
        body.extend(blocks)

    header = _script_header(config, stage)
    footer = _script_footer(config, stage)
    return header + "\n".join(body) + footer + "\n"


def render_submit_script(config: dict[str, Any], *, stage: str | None = None) -> str:
    scheduler = config["server"]["scheduler"]
    threads = int(config["server"]["threads"])
    memory_gb = int(config["server"]["memory_gb"])
    project_id = config["project"]["id"]
    project_id_error = identifier_error(project_id, "project.id")
    if project_id_error:
        raise ValueError(project_id_error)
    # 分阶段提交：stage 名称决定被调用的阶段脚本名。
    run_script = "run_pipeline.sh" if stage is None else f"run_stage_{stage}.sh"
    if scheduler == "slurm":
        return f"""#!/usr/bin/env bash
#SBATCH -J {project_id}
#SBATCH -c {threads}
#SBATCH --mem={memory_gb}G
#SBATCH -o logs/slurm-%j.out
#SBATCH -e logs/slurm-%j.err

bash scripts/{run_script}
"""
    if scheduler == "pbs":
        return f"""#!/usr/bin/env bash
#PBS -N {project_id}
#PBS -l select=1:ncpus={threads}:mem={memory_gb}gb
#PBS -o logs/pbs.out
#PBS -e logs/pbs.err

cd "$PBS_O_WORKDIR"
bash scripts/{run_script}
"""
    if scheduler == "local":
        return f"""#!/usr/bin/env bash
bash scripts/{run_script}
"""
    raise ValueError(f"Unsupported scheduler: {scheduler}")


def _featurecounts_strand(strandedness: str) -> int:
    mapping = {
        "auto": 0,
        "unstranded": 0,
        "forward": 1,
        "reverse": 2,
    }
    return mapping.get(strandedness, 0)
