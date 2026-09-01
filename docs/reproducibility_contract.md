# RNA-seq Agent 可复现执行模式

当前执行层支持三种可比较模式。三种模式最终仍只能调用仓库中受支持的 RNA-seq 工具，不向 LLM 暴露任意远程 shell。

## 三种模式

| 模式 | 配置含义 | 适合用途 | 运行前约束 |
| --- | --- | --- | --- |
| `free` | 项目配置可继续编辑，运行时重新渲染脚本 | 作为“可调整工作流”基线 | 常规 FASTQ 和配置校验 |
| `skill` | 使用仓库内版本化的固定工作流 Profile | 实验室 SOP、固定 Skill 基线 | 步骤开关和声明版本必须与 Profile 完全一致 |
| `contract` | 将配置、输入 FASTQ 和渲染脚本冻结为合同 | 审批后执行、重放和漂移检测 | SHA-256 全部匹配，否则在 SSH 连接前拒绝运行 |

这里的 `free` 不表示允许模型生成并执行任意命令，只表示分析配置尚未被 Skill 或合同锁定。

## 使用方法

列出当前固定 Skill：

```powershell
$env:PYTHONPATH = "src"
python -m rnaseq_agent profiles
```

将项目切换到固定表达定量 Skill：

```powershell
python -m rnaseq_agent apply-profile runs/<project_id>/project.json bulk_rnaseq_expression_v1
```

当前提供两个 Profile：

- `bulk_rnaseq_expression_v1`：fastp、STAR、featureCounts；
- `bulk_rnaseq_full_v1`：fastp、STAR、Arriba、featureCounts、RSEM。

FASTQ、样本表和参考配置校验通过后，冻结合同：

```powershell
python -m rnaseq_agent contract create runs/<project_id>/project.json
```

这会生成：

```text
runs/<project_id>/analysis_contract.json
```

同时把 `project.json` 的 `execution.mode` 改为 `contract`。冻结动作视为一次显式用户批准。

在提交服务器前单独验合同：

```powershell
python -m rnaseq_agent contract verify runs/<project_id>/project.json
```

随后正常运行：

```powershell
python -m rnaseq_agent run runs/<project_id>/project.json --no-wait
```

每次提交都会创建新的 `run_id`，不会复用旧运行目录：

```text
runs/<project_id>/attempts/<run_id>/
├── project.snapshot.json
├── run_manifest.json
├── generated_scripts/
├── agent_logs/
├── downloads/extracted/
└── result_manifest.json       # 下载成功后生成
```

远端工作目录对应为 `<project_workdir>/attempts/<run_id>/`。渲染脚本在启动时还会清除本次 attempt 内可能存在的旧状态 flag，形成双重防护。
每个远端 attempt 默认使用 `umask 077`，目录权限收紧为 `700`，上传的 FASTQ 与支持文件收紧为 `600`；正式院内部署仍需结合机构 ACL、用户组和备份策略核验。

合同模式下，`run` 会在建立 SSH 连接前重新计算指纹。任意一项发生变化都会拒绝提交，包括：

- FASTQ 文件内容或大小；
- 样本 ID、分组、R1/R2 文件名；
- 测序布局或链特异性；
- pipeline 步骤、声明版本；
- 线程、内存、调度器、初始化命令；
- 参考路径；
- 渲染后的环境、分析或提交脚本；
- 合同正文自身。

## 合同中保存什么

合同保存以下可验证信息：

- 规范化工作流快照；
- 每个 FASTQ 的逻辑文件名、大小和 SHA-256；
- 每个渲染脚本的大小和 SHA-256；
- 工作流、输入、脚本三个分项指纹；
- 总合同 ID；
- 显式批准时间和批准方式。

合同不保存 FASTQ 内容、SSH 密码、API key 或 SMTP 密码。SSH 临时密码仍只存在于当前进程内存。

## 当前边界

第一版合同已经能检出本地输入、配置和脚本漂移，并已具备独立 attempt、运行清单、安全解压、结果逐文件 SHA-256 和关键产物存在性检查。但这些能力仍不能单独证明完整的计算可复现性。后续仍需补充：

1. 服务器实际软件版本、module/Conda lock 或 Apptainer digest；
2. FASTA、GTF、STAR/RSEM index 的远程 checksum；
3. 上传前后 FASTQ checksum 对照；
4. 通用 count matrix、DEG 和生物学指标的语义一致性指标（当前仅已加入
   HBR/UHR 数据的 ERCC CPM 真值诊断）；
5. Slurm `squeue/sacct` 状态和资源使用；
6. 追加式 hash-chain 审计日志（当前只读 HPC 预检已输出脱敏报告，但尚未
   与每次执行形成追加式审计链）。

因此，当前可以表述为“具备本地输入—配置—脚本合同验证”，还不能表述为已经完成院内生产级合规认证。
