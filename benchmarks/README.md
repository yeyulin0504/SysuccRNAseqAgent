# Free / Skill / Contract 可复现性基准

这个目录提供一个仅使用 Python 标准库的最小基准工具，用于比较重复运行生成的 `run_manifest.json` 和可选的 `result_manifest.json`。它不会连接服务器、启动任务或读取 FASTQ 内容。

## 当前三种模式的准确含义

- `free`：当前项目中的“配置仍可编辑”的 legacy baseline。它仍然运行仓库内受支持的固定管线，不是让 LLM 自主选择任意工具和参数的 planner。因此，现阶段不能用这个基准声称“自由智能体规划 vs Skill”的优劣。
- `skill`：配置必须匹配一个版本化 workflow profile，例如 `bulk_rnaseq_expression_v1`。
- `contract`：运行前验证已批准合同；合同锁定本地输入、工作流配置和渲染脚本的 SHA-256。

三组比较时应保持样本、目标工作流、服务器资源和参考资源相同。否则测到的是实验条件差异，不是执行模式差异。

## 固定的公开数据集

公开数据只用于可复现性基准，不纳入源码仓库。当前下载目标放在已由 `.gitignore` 排除的 `runs/public_benchmark_*` 目录；`dataset.lock.json` 会记录数据规格和每个已验证文件的 SHA-256。下载器先写入 `.part`，只有大小和 SHA-256 均匹配后才进入最终路径，也不会覆盖已有但校验失败的文件。下载器不会自动解包归档。

当前采用两层数据设计：

1. `nfcore_gse110004_3x3.json`：酵母 3 对 3 的 nf-core 微型测试 reads，并包含小型参考及预建索引。它主要用于快速验证下载、FASTQ 配对、调度、管线连通性、运行隔离和三模式重复执行；它是缩小的测试衍生数据，不代表真实测序深度，也不适合支持完整生物学结论。
2. `griffith_hbr_uhr_ercc_3x3.json`：HBR ERCC Mix2 与 UHR ERCC Mix1 的 3 对 3 人源商业参考 RNA 数据，主要用于 ERCC 方向性、重复一致性、定量和差异表达稳定性的第二层验证。内源 chr22 基因没有完整金标准；FASTQ 名称中的 Build37 与当前教程提供的 GRCh38 chr22+ERCC 参考存在版本提示差异，必须把比对兼容性作为结果报告的一部分。

第二层数据属于“公开、人源、商业混合参考 RNA”，不是本项目采集的患者队列，也不包含本规格声明的个体临床标签；但它仍是人源遗传数据。使用时应继续遵守所在单位的数据分类、存储和访问控制要求，不应因为“公开”就按普通无敏感性文件处理。原始 reads、参考文件、解压内容及本地 lock 均不提交到 Git；论文或报告引用来源和固定规格即可。

下载并校验微型酵母数据：

```powershell
python benchmarks/download_dataset.py `
  --spec benchmarks/datasets/nfcore_gse110004_3x3.json `
  --destination runs/public_benchmark_gse110004_3x3
```

下载并校验人源 ERCC 数据：

```powershell
python benchmarks/download_dataset.py `
  --spec benchmarks/datasets/griffith_hbr_uhr_ercc_3x3.json `
  --destination runs/public_benchmark_hbr_uhr_ercc_3x3
```

已有文件的纯离线复核使用 `--verify-only`；该模式不会打开任何下载 URL，缺失或不匹配时直接失败：

```powershell
python benchmarks/download_dataset.py `
  --spec benchmarks/datasets/nfcore_gse110004_3x3.json `
  --destination runs/public_benchmark_gse110004_3x3 `
  --verify-only
```

人源规格中的主 reads 是一个固定 TAR 包。第一次运行上述下载命令只校验 TAR 本身；随后应使用经过路径穿越和链接检查的安全解包流程，把成员放到规格声明的 `archive_members_root`（当前为目标目录下的 `fastq/`）。下载器本身不会执行解包。仓库内安全解包器的固定调用为：

```powershell
$env:PYTHONPATH = "src"
python -c "from pathlib import Path; from rnaseq_agent.archive import safe_extract_tar; safe_extract_tar(Path(r'runs/public_benchmark_hbr_uhr_ercc_3x3/source/HBR_UHR_ERCC_ds_5pc.tar'), Path(r'runs/public_benchmark_hbr_uhr_ercc_3x3/fastq'))"
```

完成后运行：

```powershell
python benchmarks/download_dataset.py `
  --spec benchmarks/datasets/griffith_hbr_uhr_ercc_3x3.json `
  --destination runs/public_benchmark_hbr_uhr_ercc_3x3 `
  --verify-only `
  --verify-archive-members
```

该命令先复核所有已下载源文件，再逐项验证 `archive_members` 的大小和 SHA-256，并把通过校验的成员及 `archive_members_root` 写入 `dataset.lock.json`。缺失或不匹配会直接失败；不能只凭 TAR 下载成功就视为 FASTQ 输入已经就绪。

对已经下载的 FASTQ 做完整 gzip、四行结构、序列/质量长度、R1/R2 read ID
和记录数检查：

```powershell
python benchmarks/fastq_integrity.py `
  runs/public_benchmark_gse110004_3x3/project.json `
  --output runs/public_benchmark_gse110004_3x3/fastq_integrity.json

python benchmarks/fastq_integrity.py `
  runs/public_benchmark_hbr_uhr_ercc_3x3/project.json `
  --output runs/public_benchmark_hbr_uhr_ercc_3x3/fastq_integrity.json
```

完整性报告只保留项目相对文件名、大小、SHA-256、记录数和标准错误码，
不保存绝对路径、read ID、序列、质量字符串或底层异常文本。

## ERCC 科学真值诊断

人源 benchmark 完成 `featureCounts` 后，用固定的 **UHR + Mix1 / HBR + Mix2** 方向生成诊断报告：

```powershell
python benchmarks/ercc_metrics.py `
  --counts runs/<project_id>/attempts/<run_id>/downloads/extracted/featurecounts/gene_counts.txt `
  --truth runs/public_benchmark_hbr_uhr_ercc_3x3/source/ERCC_Controls_Analysis.txt `
  --format markdown `
  --output runs/<project_id>/attempts/<run_id>/ercc_metrics.md
```

报告记录两个输入文件的大小和 SHA-256，并计算 ERCC 检出覆盖率、Pearson、Spearman、log2 比值 MAE/RMSE、方向准确率及 subgroup 指标。它是小样本 CPM 描述性诊断，不估计离散度、不计算 p 值，也不替代 DESeq2、edgeR 等正式差异表达模型。

## 比较什么

每次运行的 `run_manifest.json` 中，`manifest_id` 包含 `run_id` 和时间戳，因此正常情况下每次都不同。基准不会拿它判断复现性，而是校验 manifest 完整性后比较：

- `workflow_sha256`
- `inputs_sha256`
- `scripts_sha256`
- `execution.contract_id`
- 显式记录的成功/失败结局

当 case 的一次 run 填写了 `result_manifest` 时，还会校验该文件的 `manifest_id`，并统计：

- `result_manifest_coverage`：成功读取到 result manifest 的运行占比。
- `result_manifest_integrity_rate`：读取到的 result manifest 中，正文规范哈希与 `manifest_id` 匹配的比例。
- `result_validation_success_rate`：完整性有效、且有布尔型 `validation.ok` 的 result manifest 中，`ok=true` 的比例。
- `result_validation_coverage`：具有可信 `validation.ok` 的运行占全部声明运行的比例。
- `scientific_files_modal_consistency_rate`：最常见 `scientific_files_sha256` 所占比例。
- `scientific_files_pairwise_consistency_rate`：所有可信结果两两配对后，`scientific_files_sha256` 相同的配对比例。

`scientific_files_sha256` 是科学结果文件清单的严格逐字节指纹：它综合了文件相对路径、大小和每个文件的 SHA-256。相同意味着被记录的科学文件集合及内容逐字节相同；不同则说明至少有文件集合、路径或字节内容发生变化。

它不等于 count matrix 的语义一致性。比如行顺序、浮点格式、压缩元数据或无生物学影响的文本差异都可能使逐字节指纹不同；反过来，这个指标也不评估基因 ID 映射、统计等价性、QC 数值容差或差异表达结论是否一致。这些需要后续增加面向文件类型的语义指标。

其他主要指标：

- `exact_modal_consistency_rate`：最常见的 workflow/input/script 三指纹组合所占比例。
- `exact_pairwise_consistency_rate`：所有运行两两配对后，三指纹完全一致的配对比例。
- 单项 workflow/input/script 一致率：定位漂移来自哪一层。
- `contract_id_consistency_rate`：有合同 ID 的运行中，最常见合同 ID 所占比例。
- `success_rate`：成功次数除以“已知结局”的运行数。
- `outcome_coverage`：已知结局数除以全部声明的运行数。未知结局不按失败计算，避免把 `submitted` 误判为失败。

运行指纹只能证明记录到的输入、配置和渲染脚本一致；结果清单指纹只能证明下载并记录到的文件逐字节一致。在服务器工具二进制、容器 digest、FASTA/GTF/index checksum 等尚未纳入锁定前，不能据此宣称完整环境已复现。

## 快速使用

只比较一组 run manifest：

```powershell
python benchmarks/run_benchmark.py `
  --manifest runs/PROJECT/attempts/RUN_01/run_manifest.json `
  --manifest runs/PROJECT/attempts/RUN_02/run_manifest.json `
  --case-id contract-repeat `
  --expected-mode contract
```

这种简写没有结局和 result manifest 信息，因此 `success_rate`、结果验证率和结果一致率均为 `NA`，相应 coverage 为 0%。正式实验建议复制 `case.example.json`，填写真实路径和终态：

```powershell
Copy-Item benchmarks/case.example.json benchmarks/case.local.json
python benchmarks/run_benchmark.py --case benchmarks/case.local.json
python benchmarks/run_benchmark.py --case benchmarks/case.local.json --format json --output benchmarks/results.local.json
```

case 中的 `manifest` 和 `result_manifest` 相对路径均以 case 文件所在目录为基准。`result_manifest` 是可选字段；旧 case 不添加它仍可运行。运行若在生成 run manifest 前失败，可将 `manifest` 写为 `null`，同时明确填写 `success: false` 和 `outcome`；它会计入成功率，但不会计入指纹一致率。

`success` 是人工确认或外部执行记录，优先级高于 `outcome`。未填 `success` 时，工具只把 `completed/success/succeeded` 识别为成功，把 `failed/run_failed/download_failed/result_validation_failed/validation_failed/policy_failed/upload_failed/timeout` 识别为失败；`submitted/running/queued` 保持未知。

## 建议的最小实验

1. 固定同一份小型公开或合成 paired-end 数据、同一参考和资源参数。
2. 每种模式独立重复至少 5 次；推荐 10 次，用独立 `run_id` 保存 attempt。
3. 运行结束后把每次 run manifest、result manifest 和最终状态登记到本地 case 文件。
4. 首先看输入指纹是否一致；不一致时该组实验不可直接比较。
5. 再看 workflow/script 指纹、合同 ID、成功率、结果验证和结果文件指纹。
6. 后续加入 count matrix 数值/语义一致性、关键 QC 容差和生物学结论稳定性。

`case.schema.json` 描述了 case v1 格式。工具自身执行最小结构校验，不依赖额外 JSON Schema 库。

## 离线自测

```powershell
python benchmarks/run_benchmark.py --help
python benchmarks/run_benchmark.py --self-test
```
