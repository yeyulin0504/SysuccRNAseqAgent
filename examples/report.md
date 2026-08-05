# RNA-seq demo RNA-seq Run Report

## Project summary

- Project ID: rnaseq_demo_001
- Project title: RNA-seq demo
- Owner: local_user
- Status: unknown
- Message: No status message available.
- Config path: examples\project.demo.json
- Project directory: examples
- Runtime estimate: 预计耗时约 2.2 小时。 估算依据：2 个样本，约 40M reads/样本，16 线程，已启用步骤权重 1.75。真实耗时会受服务器排队和 I/O 影响。

## Input data

- Source: local_upload
- Local FASTQ directory: D:/data/rnaseq/raw_fastq
- Remote FASTQ directory: /data/users/username/rnaseq_projects/rnaseq_demo_001/raw
- Sample count: 2
- Layout: paired
- Strandedness: auto

| Sample ID | Condition | FASTQ R1 | FASTQ R2 |
| --- | --- | --- | --- |
| Ctrl_1 | control | Ctrl_1_R1.fastq.gz | Ctrl_1_R2.fastq.gz |
| Treat_1 | treatment | Treat_1_R1.fastq.gz | Treat_1_R2.fastq.gz |

## Reference and run settings

- Reference name: GENCODE_R47_GRCh38p14_ALL
- Species: human
- Release: GENCODE v47
- Assembly: GRCh38.p14
- Regions: ALL
- Scheduler: slurm
- Threads: 16
- Memory GB: 64
- Remote workdir: /data/users/username/rnaseq_projects/rnaseq_demo_001
- Poll interval seconds: 300
- Poll timeout hours: 168
- Email notification enabled: True

## Enabled pipeline steps

- fastp: enabled=True, version=0.24.1
- star: enabled=True, version=2.7.11b
- arriba: enabled=True, version=2.5.0
- featurecounts: enabled=True, version=Subread 2.1.1
- rsem: enabled=True, version=1.2.28

## Run timeline

- No event log found.

## Command summary

- No command log found.

## Result files

- No downloaded results found locally.

## How to read these outputs

- fastp: quality control and trimming reports.
- star: alignment logs and mapping-related outputs.
- featurecounts: gene-level count matrix for downstream differential expression.
- rsem: gene and transcript expression quantification results.
- arriba: candidate fusion calls when this step is enabled.
- status/logs: execution state and audit trail for this run.

## Warnings and gaps

- This report summarizes run configuration, logs, and output artifacts.
- It does not provide biological conclusions or replace manual expert review.
- Event timeline is incomplete because no event log was found.
- Command audit is incomplete because no command log was found.
- Result inventory is incomplete because no local extracted download directory was found.
