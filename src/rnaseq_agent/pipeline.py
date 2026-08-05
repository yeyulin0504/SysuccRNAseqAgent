from __future__ import annotations

from typing import Any

from .container import wrap_command
from .shell import shell_quote
from .safety import identifier_error, relative_filename_error


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

echo "completed" > status/state.txt
date -Is > status/ended_at.txt
touch status/completed.flag
"""
    return script


def render_submit_script(config: dict[str, Any]) -> str:
    scheduler = config["server"]["scheduler"]
    threads = int(config["server"]["threads"])
    memory_gb = int(config["server"]["memory_gb"])
    project_id = config["project"]["id"]
    project_id_error = identifier_error(project_id, "project.id")
    if project_id_error:
        raise ValueError(project_id_error)

    if scheduler == "slurm":
        return f"""#!/usr/bin/env bash
#SBATCH -J {project_id}
#SBATCH -c {threads}
#SBATCH --mem={memory_gb}G
#SBATCH -o logs/slurm-%j.out
#SBATCH -e logs/slurm-%j.err

bash scripts/run_pipeline.sh
"""
    if scheduler == "pbs":
        return f"""#!/usr/bin/env bash
#PBS -N {project_id}
#PBS -l select=1:ncpus={threads}:mem={memory_gb}gb
#PBS -o logs/pbs.out
#PBS -e logs/pbs.err

cd "$PBS_O_WORKDIR"
bash scripts/run_pipeline.sh
"""
    if scheduler == "local":
        return """#!/usr/bin/env bash
bash scripts/run_pipeline.sh
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
