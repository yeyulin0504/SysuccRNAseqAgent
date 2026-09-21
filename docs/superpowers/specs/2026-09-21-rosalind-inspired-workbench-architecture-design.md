# Rosalind-inspired RNA-seq Workbench 学习与架构规范

日期：2026-09-21  
状态：设计规范，待用户评审  
适用项目：SYSU RNA-seq Agent

## 1. 目标

本规范定义我们如何学习 Rosalind Workbench 一类科研工作台的公开产品范式，并将其转化为本项目自己的架构能力。

目标不是复刻 Rosalind，而是把当前“对话助手 + RNA-seq 执行引擎 + HPC 连接 + 可复现审计”发展为面向科研人员的可扩展工作台。

成功标准：用户可以围绕一个项目完成数据接入、分析意图表达、方案确认、受控执行、过程观察、结果解释和可复现交付；每一步都有明确状态、证据和失败边界。

## 2. 借鉴边界

### 2.1 可以学习的内容

只学习公开可观察的产品原则、信息组织方式和通用技术模式：

- 以“项目/实验”为中心，而不是以孤立命令为中心；
- 用自然语言帮助用户表达研究问题，再转换为结构化分析计划；
- 将复杂流程封装为可发现、可组合、可验证的分析能力；
- 在同一工作台中管理输入数据、运行过程、结果资产和协作上下文；
- 提供结果浏览、解释、比较和追溯入口；
- 记录参数、软件环境、输入文件、运行日志和输出文件之间的 provenance；
- 对不适合自动判断的情况明确提示不确定性，并把决策交还用户。

### 2.2 不可以直接复制的内容

在没有明确授权或开源许可证的情况下，不复制：

- 源代码、内部接口、提示词、模型权重和专有算法；
- 页面源码、完全相同的布局、图标、动画和视觉品牌；
- 产品文案、教程、示例数据、数据库内容和专有流程定义；
- 通过抓包、逆向、绕过权限得到的非公开信息；
- Rosalind 商标、产品名称或容易造成来源混淆的命名。

所有实现必须基于本项目已有代码、公开标准和独立设计，并保留自己的命名、数据模型、测试和审计记录。

## 3. 当前项目基础

当前项目已经具备以下可复用基础：

- `ProjectSession` 管理 drafting、planned、confirmed、executing、completed、failed 等状态；
- capability registry、Gate-A、adapter inspect/plan/materialize；
- analysis contract 对工作流、输入和渲染脚本做冻结与指纹校验；
- execution gateway 支持本地、Slurm、PBS 和远程 shell；
- 远程 preflight、SSH/SCP、上传、轮询、下载和结果归档；
- output validator、Post-run Gate、Artifact Registry 和 STALE 失效机制；
- GUI、CLI、chat 和 localhost web workbench 共用执行核心；
- 输入输出校验、run manifest、result manifest 和审计日志。

因此，主要工作是把已有的“安全执行内核”组织成更完整的“科研工作台产品层”，而不是另起一个执行系统。

## 4. 目标产品模型

工作台围绕五类一等对象组织：

| 对象 | 作用 | 核心内容 |
|---|---|---|
| Project | 一个研究项目或实验任务 | 研究目标、样本、数据源、权限、成员 |
| Capability | 一个受控分析能力 | 输入契约、适用范围、计划器、执行器、输出契约 |
| Analysis Plan | 用户确认前的分析方案 | 参数、样本分组、资源、预期产物、风险提示 |
| Run | 一次不可混淆的执行尝试 | contract、run_id、状态、日志、资源、错误 |
| Artifact | 可追溯的输入或输出资产 | 文件、表格、图、报告、哈希、来源和状态 |

用户体验应形成以下闭环：

```text
研究问题 / 数据
        ↓
项目上下文与能力匹配
        ↓
Suitability Gate + 数据检查
        ↓
分析计划与风险说明
        ↓
用户确认 / 修改 / 回滚
        ↓
Analysis Contract 冻结
        ↓
受控执行与实时状态
        ↓
输出验证与科学后处理
        ↓
结果浏览、解释、导出与复现
```

## 5. 目标架构

采用分层、端口-适配器风格。用户界面和模型服务只能通过应用服务访问领域能力，不能绕过 contract 和 execution gateway 直接执行命令。

```text
┌──────────────────────────────────────────────────────────┐
│ Presentation Layer                                       │
│ Web Workbench │ Desktop GUI │ CLI │ Chat                 │
└───────────────────────┬──────────────────────────────────┘
                        │ Application API
┌───────────────────────▼──────────────────────────────────┐
│ Workbench Application Layer                              │
│ Project Workspace │ Plan/Confirm │ Run Monitor           │
│ Artifact Browser  │ Report/Explain │ Collaboration       │
└───────────────────────┬──────────────────────────────────┘
                        │ Domain ports
┌───────────────────────▼──────────────────────────────────┐
│ Scientific Control Plane                                 │
│ Capability Registry │ Gate-A │ Adapter                  │
│ Analysis Contract   │ Policy/Consent │ Post-run Gate    │
└───────────────────────┬──────────────────────────────────┘
                        │ Execution ports
┌───────────────────────▼──────────────────────────────────┐
│ Execution Plane                                          │
│ Local │ SSH/SCP │ Slurm │ PBS │ Container/Apptainer      │
│ Upload │ Submit │ Poll │ Cancel │ Download               │
└───────────────────────┬──────────────────────────────────┘
                        │ Evidence and artifacts
┌───────────────────────▼──────────────────────────────────┐
│ Persistence and Provenance                               │
│ Project Store │ Session/Change Log │ Run Manifest        │
│ Artifact Registry │ Result Manifest │ Reports            │
└──────────────────────────────────────────────────────────┘
```

### 5.1 Presentation Layer

统一提供四类入口：

- Web Workbench：主入口，展示项目、计划、运行、结果和审计；
- Desktop GUI：适合本地配置、连接设置和低门槛向导；
- CLI：适合批量、自动化和服务器环境；
- Chat：负责意图理解和解释，但只能调用 allowlist 内的结构化 action。

四类入口不得各自实现 RNA-seq 业务逻辑，必须调用同一组 application service。

### 5.2 Workbench Application Layer

新增或强化以下应用服务边界：

- `ProjectWorkspaceService`：创建项目、加载上下文、管理样本和数据源；
- `PlanService`：将用户意图映射为 capability、生成计划、解释限制；
- `ConfirmationService`：确认、修改、回滚并创建 immutable contract；
- `RunService`：提交、取消、恢复、轮询和查看一次运行；
- `ArtifactService`：浏览、筛选、下载、注册和追踪结果资产；
- `ReportService`：从已验证 artifact 生成报告，不让模型凭空生成结果；
- `AuditService`：统一读取 changesets、contract、manifest 和状态事件。

服务返回结构化对象和状态码，例如 `NOT_EVALUABLE`、`WAITING_USER`、`ABSTAIN`、`FAIL_OUTPUT_CONTRACT`、`STALE`，而不是只返回自然语言。

### 5.3 Scientific Control Plane

这是项目的科学安全边界：


- Registry 负责发现 capability 及其版本；
- Gate-A 判断数据布局、样本数、分组、参考基因组和文件存在性；
- Adapter 负责 inspect、plan、materialize；
- Contract 冻结 workflow、输入哈希、参数和脚本哈希；
- Policy/Consent 控制模型可读数据范围、敏感信息披露和高风险操作确认；
- Output Validator 检查文件、表头、哈希和关键产物；
- Post-run Gate 对 QC、OOD 和科学结论边界进行判断；
- Artifact Registry 建立血缘，并在上游改变时将下游标记为 `STALE`。

模型可以提出意图、选择能力和解释证据，但不能生成任意 shell 命令、修改冻结 contract 或绕过 gate。

### 5.4 Execution Plane

执行层继续沿用当前受控网关：

- 所有远程命令由代码生成器和已注册 workflow 产生；
- 远程连接、上传、调度、轮询和下载均写入 run evidence；
- 每次提交使用独立 `run_id` 和隔离目录；
- 支持取消、失败恢复和结果下载；
- 资源估计、调度器状态和工具版本应进入 manifest；
- 后续支持容器或 Apptainer digest，降低环境漂移。

### 5.5 Persistence and Provenance

第一阶段继续使用项目目录和 JSON/JSONL，以保持可移植性。对象关系应稳定，未来可以替换为数据库而不改变应用服务接口。

最小持久化集合：

- `project.json`：当前项目配置；
- `session.json`：会话状态和当前计划；
- `changesets.jsonl`：追加式变更历史；
- `analysis_contract.json`：冻结的执行契约；
- `run_manifest.json`：一次运行的输入、工作流、脚本和环境指纹；
- `result_manifest.json`：结果文件哈希、类型、验证状态和血缘；
- `events.jsonl`：可选的运行状态事件流。

## 6. Rosalind 范式到本项目的转化

| 学习到的范式 | 本项目的独立实现 |
|---|---|
| 科研工作台 | localhost web workbench + GUI/CLI/chat 共用服务 |
| 对话式分析 | Chat 输出结构化 capability request 和 Analysis Plan |
| 流程封装 | versioned capability registry + adapter |
| 项目中心 | project workspace、样本和数据源上下文 |
| 结果中心 | Artifact Registry、结果浏览和报告服务 |
| 可追溯分析 | contract、manifest、changesets、hash 和 lineage |
| 自动化执行 | execution gateway + HPC adapters |
| 结果解释 | 基于已验证 artifacts 的 report/explain，而非无证据生成 |
| 协作和复用 | 可导出的 project bundle、profile、contract 和报告 |

## 7. 分阶段路线

### Phase 1：工作台骨架

- 统一 project workspace 数据模型；
- 将现有 webapp、GUI、CLI、chat 的入口映射到 application service；
- 提供项目、计划、运行、结果、审计五个主要面板；
- 明确每个页面展示的状态、证据和可用操作。

验收：同一个项目可以从任意入口打开，并看到一致的 plan、run 和 artifact 状态。

### Phase 2：对话到计划

- chat 先生成结构化意图，再调用 capability matching；
- 展示适用性检查、缺失输入、资源估计和风险；
- 用户可以修改、确认、回滚；
- 确认后只能通过 contract 执行。

验收：模型不能产生或执行 allowlist 之外的命令；计划内容可被用户逐项审阅。

### Phase 3：结果与科学解释

- 结果按 artifact 类型、来源、验证状态和 run 组织；
- 报告只引用已注册且验证通过的 artifact；
- 对 QC 不通过、输入不适用和结论不确定情况显示 `ABSTAIN` 或 `WAITING_USER`；
- 支持结果包、报告和 contract 一键导出。

验收：任何结论都能回链到输入、参数、运行、日志和具体结果文件。

### Phase 4：扩展到多组学

- 保留 capability/adapter/contract/artifact 通用接口；
- bulk RNA-seq 作为第一条黄金路径；
- 后续增加单细胞、空间转录组和下游统计模块；
- 不为每种组学复制一套 UI 或执行系统。

验收：新增能力主要增加 capability adapter 和输出契约，不破坏既有工作台。

## 8. 非功能要求

- 可复现：输入、工作流、脚本、工具版本和环境指纹可保存；
- 可审计：用户修改、模型建议、确认、执行和回滚均有记录；
- 可拒绝：数据不适用、证据不足或输出不完整时必须能拒绝下结论；
- 安全：模型不执行任意命令，敏感数据范围由 grant/consent 控制；
- 可移植：本地、HPC 和容器执行通过适配器隔离；
- 可测试：每个 capability 有输入契约、计划测试、执行测试和输出验证测试；
- 可替换：LLM、UI、scheduler 和 persistence 都不是领域核心的硬编码依赖。

## 9. 风险控制

### 产品仿制风险

保持独立命名、视觉设计、文案和数据模型；设计记录中只描述公开范式和我们的独立决策，不保存或引用非公开实现细节。

### 模型越权风险

所有模型操作进入结构化 action allowlist；执行前必须经过 Gate-A、用户确认和 contract 校验。

### 科学误导风险

输出验证、QC gate 和 `ABSTAIN` 是工作台的一等能力；报告生成器必须读取证据对象，不能只根据聊天上下文写结论。

### 架构膨胀风险

优先复用现有 session、contract、adapter、execution 和 artifact 模块。第一阶段不引入多租户、复杂协作、在线数据库或完整 SaaS 计费体系。

## 10. 评审与验收清单

- [ ] 能清楚区分公开范式借鉴与受保护内容复制；
- [ ] UI、CLI、chat 共享同一 application service；
- [ ] chat 无法绕过 capability、gate、contract 和 execution gateway；
- [ ] 用户可查看、修改、确认和回滚分析计划；
- [ ] 每次 run 有独立 manifest、状态和 artifact lineage；
- [ ] 结果页面区分已验证、失败、过期和不确定结果；
- [ ] 报告中的关键结论可以追溯到具体 artifact；
- [ ] 新增组学能力不需要复制整套执行与审计架构；
- [ ] 文档、名称、视觉和代码均保持独立实现。

## 11. 明确不纳入本规范的内容

本规范不决定具体前端框架、云部署方案、商业定价、用户增长、Rosalind 私有功能的复刻，也不授权访问或复制 Rosalind 的受限资料。这些事项需要单独评审。
