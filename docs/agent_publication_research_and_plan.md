# 生物信息 Agent 调研与 SYSU RNA-seq Agent 建设建议

## 1. 当前项目定位

当前仓库已经具备 RNA-seq 流程执行底座，而不是空白项目。它能够通过 GUI、chat 或 CLI 收集配置，生成 `project.json`，校验本地 FASTQ，上传到远程服务器，生成并提交远程脚本，轮询状态，下载结果，并生成 Markdown 报告。

因此，后续重点不应是推倒重写，而应是把它继续包装成面向实验室同学的受控型生信 agent：

- 下层保持确定性流程执行：`fastp`、`STAR`、`Arriba`、`featureCounts`、`RSEM`
- 中层负责项目状态、脚本、日志、报告和结果文件管理
- 上层补自然语言问答、参数引导、结果解释和案例演示

## 2. 别人的 Agent 通常如何发布

结合 BioMedAgent、BioMaster、FlowAgent、BRAD、Agentomics 等项目，生信/科研 agent 的发布方式通常不是单一形态，而是多层发布：

| 发布形态 | 作用 | 对本项目的启发 |
| --- | --- | --- |
| GitHub 源码仓库 | 支持复现、二次开发和同行检查 | 本项目应整理 README、示例配置、运行截图、demo 数据说明 |
| Python 包 / CLI | 便于技术用户安装和自动化调用 | 当前已有 `pyproject.toml` 和 `rnaseq-agent` 命令入口，可继续完善 |
| 文档网站 | 提供安装、配置、示例、FAQ、API 说明 | 当前 docs 以 Markdown 为主，后续可用 ReadTheDocs 或 MkDocs |
| GUI / Web UI | 降低非计算背景用户门槛 | 当前已有 Tkinter GUI，适合实验室本地使用 |
| Docker / Conda / 环境说明 | 解决依赖和复现问题 | 当前项目本地无第三方依赖，但远程服务器工具依赖应补环境说明 |
| 示例数据和可复现 demo | 展示端到端能力 | 当前只有占位 demo，需补最小 FASTQ 或真实服务器运行记录 |
| 论文 / 预印本 | 强调问题、系统设计、评测和案例 | 后续可按“受控型 RNA-seq agent”路线写方法学文章或应用短文 |

代表案例：

- BioMedAgent 在论文中提供 GitHub、Hugging Face benchmark、Zenodo 数据和在线交互记录，强调开放代码、benchmark 和交互过程可查。
- BRAD 同时提供论文、PyPI 包、ReadTheDocs 文档、CLI、GUI 和 programmatic API。
- FlowAgent 提供 GitHub、文档、CLI、Nextflow/Snakemake 生成、HPC/Kubernetes 后端和 citation 信息。
- Agentomics 提供 Python 实现、文档、可复现实验和最终模型/报告产物。

对我们来说，最合适的发布路线是：

1. 先整理 GitHub 仓库：README、usage、flow、example、report、known limitations
2. 保留 Python CLI 和 GUI：让老师、维护者、实验室同学都能使用
3. 增加 demo 数据或 demo 记录：证明它不是只会生成配置
4. 后续再考虑文档网站、Docker、Web UI 或 PyPI

## 3. 别人的 Agent 文章通常怎么写

生信 agent 文章通常不是只写“用了某个大模型”，而是写一个能完成真实任务的系统。常见结构如下：

### 3.1 Introduction

需要说明：

- 高通量组学分析流程复杂
- 非计算背景研究者使用门槛高
- 传统 pipeline 虽可复现，但交互、参数选择、错误处理和结果解释仍依赖专家
- LLM agent 可以承担任务理解、工具调用、状态追踪和结果解释

### 3.2 System Design / Methods

需要说明：

- agent 架构：用户输入、任务规划、参数采集、执行引擎、状态管理、报告输出
- LLM 或规则模块负责什么
- 确定性 pipeline 负责什么
- 如何保存中间产物：配置、脚本、日志、结果、报告
- 如何避免不受控执行和不可复现

### 3.3 Workflow

需要展示：

- 用户如何提出需求
- 系统如何追问必要信息
- 如何生成配置
- 如何提交任务
- 如何查询状态
- 如何下载和解释结果

### 3.4 Evaluation

可以评估：

- 配置完整率
- 本地校验成功率
- 任务提交成功率
- 端到端运行成功率
- 报告生成完整性
- 用户操作步骤是否减少
- 与手工运行 pipeline 的时间和错误率对比

### 3.5 Case Study

至少需要一个真实或半真实案例：

- 输入样本信息
- 生成的 `project.json`
- 生成的远程脚本
- 运行日志和状态变化
- 下载结果目录
- Markdown 报告
- 结果解释示例

### 3.6 Limitations

必须主动写：

- 当前不自动给出生物学结论
- 当前解释结果仍需人工复核
- 真实运行依赖服务器环境和参考基因组
- WES/ChIP-seq 尚未完整实现
- LLM 若接入，需要严格限制命令执行权限

## 4. 生物信息流程 Agent 如何落地

外部案例有一个共同点：真正落地的生信 agent 往往不是替代 workflow，而是把 workflow 包起来。

典型落地分层：

1. 知识层：文献、工具文档、流程知识、实验室 SOP
2. 任务理解层：判断用户是在问概念、配参数、跑流程，还是看结果
3. 参数采集层：把自然语言需求转成结构化 schema
4. 执行编排层：调用 pipeline、HPC、容器或 workflow manager
5. 监控与纠错层：记录日志、检测失败、给出修复建议
6. 报告解释层：整理结果文件、关键指标和人工复核提示

本项目当前已经有第 3-4 层的一部分，以及第 6 层的初版报告。下一步最值得补的是：

- 知识问答层
- 更自然的任务理解层
- 结果解释增强
- 真实案例评测

## 5. RNA-seq / WES / ChIP-seq 问答 Agent 体系建议

不建议一开始同时深做 RNA-seq、WES、ChIP-seq。原因是三条流程的数据结构、质控指标、结果解释和风险不同。

推荐路线：

### P1：先把 RNA-seq 做成完整闭环

优先完成：

- RNA-seq 基础知识问答
- 样本、分组、测序模式、链特异性、参考基因组参数采集
- 自动生成 `project.json`
- 本地 FASTQ 校验
- 远程提交和状态追踪
- 报告生成
- fastp / STAR / featureCounts / RSEM / Arriba 结果解释
- 一个可复现 demo

### P2：抽象通用项目 schema，再接 ChIP-seq

ChIP-seq 可复用的框架包括：

- 项目信息
- 样本信息
- 远程执行
- 状态追踪
- 报告输出

需要新增：

- peak calling
- input/control 设计
- MACS2 / Bowtie2 / BWA / deepTools 等工具链
- peak 注释、motif、可视化解释

### P3：最后再做 WES

WES 复杂度更高，原因包括：

- tumor-normal 配对
- 变异调用
- 注释数据库
- 临床隐私风险
- 结果解释边界更敏感

因此 WES 更适合在 RNA-seq agent 稳定后作为独立 adapter 逐步接入。

## 6. 当前仓库已经能演示什么

当前可以立刻演示的 dry-run 链路：

```powershell
$env:PYTHONPATH = "src"
python -m rnaseq_agent estimate examples\project.demo.json
python -m rnaseq_agent validate-local examples\project.demo.json
python -m rnaseq_agent report examples\project.demo.json --output runs\report_demo.md
python -m rnaseq_agent status examples\project.demo.json --cached
```

目前验证结果：

- `estimate` 可正常输出预计耗时
- `validate-local` 会正确指出 demo FASTQ 不存在
- `report` 可生成 Markdown 报告
- `status --cached` 可在不连接服务器时查看缓存状态

这说明当前 demo 是可展示的，但还不是完整真实运行案例。下一步需要准备真实 FASTQ 或公共小数据，并配置真实服务器路径。

## 7. 下一阶段最优先任务

建议按下面顺序推进：

1. 整理 README 和 docs，让项目定位更清楚
2. 增加 `docs/demo_checklist.md`，把本地演示步骤写成清单
3. 准备最小 FASTQ 测试数据或真实服务器运行记录
4. 增强 `report.py`，让它读取 fastp JSON、STAR Log.final.out、featureCounts summary
5. 增强 `chat.py`，加入更自然的“我想做 RNA-seq 分析”路由
6. 新增知识库文档：RNA-seq / WES / ChIP-seq 常见问答
7. 后续再考虑接入 LLM/RAG，而不是一开始就让 LLM 自由执行命令

## 8. 可引用外部资料

- BioMedAgent: Nature Biomedical Engineering, 2026. Multi-agent biomedical data analysis framework with GitHub, benchmark and open datasets. https://www.nature.com/articles/s41551-026-01634-6
- BioMaster: Patterns, 2026. Multi-agent system for automated bioinformatics workflows with dual RAG, planning, execution, debugging and validation. https://www.sciencedirect.com/science/article/pii/S2666389926001200
- BioMaster documentation. https://biomaster.readthedocs.io/en/latest/introduction.html
- FlowAgent documentation. Multi-agent framework that plans bioinformatics workflows, generates Nextflow/Snakemake, executes across local/HPC/Kubernetes/workflow backends, recovers from errors and reports results. https://entelobio.github.io/flowagent/
- BRAD paper: Bioinformatics, 2025. Bioinformatics Retrieval Augmented Digital agent for biomarker discovery and enrichment. https://academic.oup.com/bioinformatics/article/doi/10.1093/bioinformatics/btaf159/8125018
- BRAD PyPI package. https://pypi.org/project/BRAD-Agent/
- BRAD documentation. https://brad-bioinformatics-retrieval-augmented-data.readthedocs.io/
- Agentomics: Bioinformatics, 2026. Agentic system for biomedical ML with strict validation checkpoints, containerized runs and report artifacts. https://doi.org/10.1093/bioinformatics/btag250
- BIOGEN: Frontiers in Bioinformatics, 2026. Evidence-grounded multi-agent reasoning for RNA-seq transcriptomic interpretation. https://www.frontiersin.org/journals/bioinformatics/articles/10.3389/fbinf.2026.1846404/full
- Review of biological LLM agents: systematic review of more than 60 systems and shared challenges around grounding, reproducibility and safety. https://pmc.ncbi.nlm.nih.gov/articles/PMC13017847/
