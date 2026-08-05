# SYSU RNA-seq Agent Demo 演示清单

## 1. Demo 目标

当前 demo 的目标不是证明已经完成真实 RNA-seq 全流程分析，而是证明项目已经具备一个受控型 agent 的基础闭环：

- 能读取项目配置
- 能估算运行时间
- 能校验本地 FASTQ
- 能生成标准化报告
- 能查看项目状态
- 能说明如果接入真实服务器后如何继续运行

## 2. 演示前准备

进入项目目录：

```powershell
cd D:\文件\研究生\Agent\SysuccRNAseqAgent-master
```

如果本机已经安装 Python：

```powershell
$env:PYTHONPATH = "src"
python -m rnaseq_agent --help
```

如果本机没有 `python` 命令，但在 Codex 桌面环境中，可以使用 Codex 自带 Python：

```powershell
$env:PYTHONPATH = "src"
& "C:\Users\12249\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m rnaseq_agent --help
```

## 3. 演示步骤

### 3.1 查看示例配置

示例配置文件：

```text
examples\project.demo.json
```

可以向老师说明：

- 项目 ID 是 `rnaseq_demo_001`
- 包含服务器配置、参考基因组配置、样本信息、pipeline 步骤、轮询和邮件设置
- 当前 FASTQ 路径是占位路径，因此校验会提示文件缺失

### 3.2 估算运行时间

```powershell
$env:PYTHONPATH = "src"
python -m rnaseq_agent estimate examples\project.demo.json
```

预期结果：

```text
预计耗时约 2.2 小时。估算依据：2 个样本，约 40M reads/样本，16 线程，已启用步骤权重 1.75。
```

讲解重点：

- agent 不只是保存配置，还会根据样本数、reads 数、线程数和启用步骤给出粗略耗时估计
- 真实耗时仍取决于服务器排队和 I/O

### 3.3 校验本地 FASTQ

```powershell
$env:PYTHONPATH = "src"
python -m rnaseq_agent validate-local examples\project.demo.json
```

当前 demo 的预期结果是校验失败：

```text
Checked FASTQ files: 4
当前配置还没有通过本地 FASTQ 校验。
- 缺失文件：D:\data\rnaseq\raw_fastq\Ctrl_1_R1.fastq.gz
- 缺失文件：D:\data\rnaseq\raw_fastq\Ctrl_1_R2.fastq.gz
- 缺失文件：D:\data\rnaseq\raw_fastq\Treat_1_R1.fastq.gz
- 缺失文件：D:\data\rnaseq\raw_fastq\Treat_1_R2.fastq.gz
```

讲解重点：

- 这不是程序错误，而是示例配置故意使用了占位 FASTQ 路径
- 它证明 agent 会在真正上传和提交前先做本地输入检查
- 下一步只要把 `samples.local_data_dir` 改成真实 FASTQ 目录，并保证文件名匹配，就可以进入真实运行准备

### 3.4 查看上传命令预览

```powershell
$env:PYTHONPATH = "src"
python -m rnaseq_agent upload-command examples\project.demo.json
```

讲解重点：

- agent 会根据配置生成 `ssh` 和 `scp` 命令
- 用户可以在真正执行前检查远程目录和待上传文件

### 3.5 生成 Markdown 报告

```powershell
$env:PYTHONPATH = "src"
python -m rnaseq_agent report examples\project.demo.json --output runs\report_demo.md
```

预期结果：

```text
Report written to: runs\report_demo.md
```

讲解重点：

- 报告会总结项目配置、样本、参考基因组、pipeline、运行状态、命令日志和结果文件
- 当前没有真实下载结果，因此报告会提示结果目录不完整
- 这正好说明报告是可追踪的，不会假装已有结果

### 3.6 查看缓存状态

```powershell
$env:PYTHONPATH = "src"
python -m rnaseq_agent status examples\project.demo.json --cached
```

预期结果：

```text
状态：unknown
```

讲解重点：

- `--cached` 不连接服务器，只读 `project.json`
- 适合在 demo 阶段展示状态入口
- 真实运行时可以去掉 `--cached`，让 agent 通过 SSH 读取远程状态

## 4. 如果要跑真实例子，需要补齐什么

真实运行前至少需要：

1. 一组真实或公开小 FASTQ 文件
2. 本地 `samples.local_data_dir` 指向真实 FASTQ 目录
3. `samples.items` 中的 fastq 文件名与本地文件一致
4. 可登录的服务器地址、用户名、远程工作目录
5. 服务器上已经安装或可加载：
   - `fastp`
   - `STAR`
   - `arriba`
   - `featureCounts`
   - `rsem-calculate-expression`
   - `tar`
   - `bash`
6. 服务器上已经准备好：
   - GTF
   - genome FASTA
   - STAR index
   - RSEM index
7. 根据服务器情况设置 `server.scheduler`：
   - `slurm`
   - `pbs`
   - `local`

真实运行命令：

```powershell
$env:PYTHONPATH = "src"
python -m rnaseq_agent run runs\<project_id>\project.json
```

只提交不等待：

```powershell
$env:PYTHONPATH = "src"
python -m rnaseq_agent run runs\<project_id>\project.json --no-wait
python -m rnaseq_agent status runs\<project_id>\project.json
```

## 5. 向老师汇报时可以这样讲

可以简洁表述为：

> 当前项目已经具备 RNA-seq agent 的基础执行闭环。现在的 demo 可以展示配置读取、耗时估算、本地 FASTQ 校验、上传命令预览、报告生成和状态查看。由于示例 FASTQ 路径是占位路径，所以当前演示属于 dry-run；下一步需要准备真实小数据和服务器路径，完成一次真实端到端运行。

## 6. 下一步建议

优先级从高到低：

1. 找一组小型公开 RNA-seq FASTQ 或老师提供的测试 FASTQ
2. 修改 demo 配置，让 `validate-local` 通过
3. 用真实服务器执行一次 `run --no-wait`
4. 任务结束后下载结果并重新生成报告
5. 增强报告解析 fastp、STAR、featureCounts、RSEM 的关键指标
