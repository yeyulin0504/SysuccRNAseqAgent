# 统一多组学项目向导设计规范

- 日期：2026-09-09
- 状态：设计候选版，待用户审阅后进入实施计划
- 适用范围：本地优先 SYSU 多组学分析 Agent 的项目工作台、数据接入、状态模型和首版可执行路线
- 目标：把当前分散的项目登记、counts 上传、FASTQ 样本扫描、对话动作、计划生成和后续 WXS/scRNA 预留位统一为一条清晰的项目向导

## 1. 背景与问题

现有 MVP 已经具备 bulk RNA-seq 的部分工程能力：项目、对话、连接设置、counts 直入、远程 FASTQ 扫描、计划与契约冻结、HPC 执行入口。问题在于这些能力没有形成用户能自然理解的一条流程。

当前最明显的问题是项目注册状态和分析会话状态分裂：项目列表可显示 `drafting`，但真实 `session.json` 尚不存在时右侧又显示 `idle`。这会让保存配置、加载演示配置、上传 counts、生成计划等按钮在不同状态判断下互相冲突。counts 入口也仍像工程表单，要求用户理解 sample_id、condition、reference_condition、raw counts 与 normalized expression 的区别；对非生信用户不友好。

本设计把用户路径改成项目向导：先选择组学路线和输入类型，再由系统读取或扫描文件，生成预览和门禁解释，最后生成可审阅计划并执行。首版只要求 bulk counts/表达矩阵和 bulk RNA FASTQ 两条路线可创建分析；WXS 与 scRNA 先作为结构化预留位出现，避免 UI 架构后续重做。

## 2. 设计目标

1. 一个项目只有一套用户可见状态，项目列表、顶部徽标、右侧上下文、按钮权限和对话回复都读同一状态源。
2. 用户从“我有什么数据”开始，不从“我要填哪些底层参数”开始。
3. counts/表达矩阵上传后自动识别样本和矩阵语义，避免把 GEO series matrix 或 normalized expression 错当 DESeq2 raw counts。
4. bulk FASTQ 路线允许用户只提供服务器目录，由后端通过已保存 SSH 配置扫描并生成样本候选表。
5. WXS、scRNA-seq、临床应用和模型中心在界面上有固定位置，但未实现能力必须显示真实不可执行原因。
6. 对话和按钮调用同一套后端动作，不能出现“按钮能做、对话不理解”或“对话回复完成但项目事实未变化”。
7. 首版仍保持本地优先：原始数据、矩阵和 SSH/API 密钥不默认外发给 LLM。

## 3. 非目标

本设计不要求一次性实现 WXS FASTQ 到 VCF、scRNA 下游、PCGR、疗效模型、复杂 bulk 设计、剪接分析或正式多组学联合建模。它只为这些能力预留稳定入口、状态、门禁和产物展示位置。

本设计不改变既有安全边界：LLM 不获得任意 SSH shell 权限，不生成自由命令直接执行；HPC 执行仍通过已注册能力、Adapter 和 Execution Gateway。

## 4. 用户可见状态模型

项目状态统一为以下枚举。旧的 `idle` 不再作为已登记项目的主要用户状态；只有没有项目或后端无法绑定项目时才可显示“未选择项目”。

| 状态 | 含义 | 允许的主要操作 |
|---|---|---|
| `setup` | 项目已创建，但尚未选择路线或输入 | 选择组学路线、加载演示配置、进入设置页 |
| `input_ready` | 已上传矩阵、扫描出 FASTQ 或登记对象，等待样本/设计确认 | 编辑样本分组、重新预览、生成计划 |
| `planned` | 已生成可审阅执行计划 | 修改输入、确认并冻结契约 |
| `confirmed` | 契约已冻结，等待执行或提交 | 执行、查看冻结参数 |
| `running` | 本地或 HPC 作业运行中 | 查看日志、轮询状态 |
| `waiting_user` | 到达人工检查点 | 继续、修改、停止 |
| `completed` | 运行完成且产物通过最低验证 | 查看结果、生成报告、新建修订 |
| `failed` | 执行或输出契约失败 | 查看错误、重试或新建修订 |

状态来源优先级为：

1. 真实分析 session、graph state、run attempt；
2. 项目 intake draft；
3. workspace registry 的静态元数据。

workspace registry 不再长期保存会和 session 冲突的状态。列表接口返回项目前必须重新推导状态，或把 registry 中的 state 当缓存并在 session/intake 变化时同步更新。

## 5. 统一项目向导

工作台中间区域改为四步向导，而不是同时展示所有表单。

### 5.1 第一步：选择路线

路线分为三类：

| 路线 | 首版状态 | 页面说明 |
|---|---|---|
| bulk RNA-seq | 可配置 | 支持 counts/表达矩阵直入、本地 FASTQ、服务器 FASTQ 扫描 |
| WXS | 预留 | 显示 tumor-normal FASTQ、SNV/small InDel、CNV 后续；按钮为“查看输入要求” |
| scRNA-seq | 预留 | 显示 10x filtered matrix、h5ad、Seurat RDS；按钮为“查看输入要求” |

WXS 和 scRNA 的预留卡片不能创建假 session，不能显示“已支持”。它们返回结构化 `NOT_IMPLEMENTED` 或 `NOT_AVAILABLE_YET`，并说明计划接入的能力 ID。

### 5.2 第二步：选择输入类型

bulk RNA-seq 下提供三个输入：

| 输入类型 | 首版行为 |
|---|---|
| counts/表达矩阵上传 | 上传本地 `.tsv/.csv/.txt`，自动读取表头，识别样本列和矩阵类型 |
| 本地 FASTQ | 允许登记本地 FASTQ 目录和样本文件；可后续增加本地目录扫描 |
| 服务器 FASTQ 自动扫描 | 用户填服务器目录或在对话里请求浏览目录；后端通过 SSH 扫描 R1/R2 并生成候选样本表 |

输入类型变更必须生成新的 intake revision，不能静默覆盖已冻结契约。若项目已有运行产物，页面提示“更换输入会创建新修订，旧结果保留”。

### 5.3 第三步：样本与设计确认

系统自动生成样本预览。用户主要通过批量粘贴或表格快速编辑分组，而不是逐个单元格从零开始输入。

bulk counts/表达矩阵预览至少包含：

- 原始文件名、大小、SHA-256；
- 检测到的样本列；
- 基因 ID 列候选；
- 矩阵语义：`raw_counts`、`normalized_expression`、`geo_series_matrix_like` 或 `unknown`;
- 是否存在非整数、负数、重复基因、重复样本、空列；
- 推荐可运行能力和不可运行原因。

bulk FASTQ 预览至少包含：

- 扫描路径和数据来源：本地或服务器；
- R1/R2 配对样本候选；
- 未配对文件和命名异常；
- 默认样本 ID；
- 待补充字段：condition、batch、癌种、设计。

### 5.4 第四步：计划、确认和执行

只有当前路线可执行的动作出现在主按钮上：

- `setup`：显示“选择输入”；
- `input_ready`：显示“生成分析计划”；
- `planned`：显示“确认并冻结契约”；
- `confirmed`：显示“执行测试/提交运行”；
- `waiting_user`：显示当前检查点动作；
- `completed/failed`：显示结果、日志、重试或新建修订。

按钮和对话必须调用相同后端 command，例如 `set_route`、`preview_counts_matrix`、`scan_remote_fastq`、`create_intake_session`、`plan`、`confirm`、`execute`。对话不再只返回规则文本；若识别到已注册动作，必须改变项目状态或返回门禁错误。

## 6. counts/表达矩阵路线

### 6.1 矩阵语义识别

后端新增或扩展 counts preview，读取文件前若发现 GEO series matrix 注释段，应跳过 `!` 开头元信息并定位表达矩阵表头。识别结果分为：

- `raw_counts`：样本列基本为非负整数，适合 DESeq2；
- `normalized_expression`：大量小数或 log-like 值，不适合 DESeq2 raw counts，可用于表达矩阵审计、部分分类器适用性检查；
- `geo_series_matrix_like`：GEO series matrix 或平台表达矩阵，需要作为 normalized expression 处理；
- `unknown`：无法判断，禁止自动执行 DESeq2，只允许用户查看预览或手动修正。

### 6.2 能力门禁

| 能力 | 可用条件 | 不可用时提示 |
|---|---|---|
| DESeq2 差异表达 | `raw_counts`、两组独立设计、每组不少于 3 个生物学重复、reference condition 存在 | 当前矩阵不是 raw counts、分组不足或参考组缺失 |
| CMS 分型 | CRC/COAD/READ，样本数达到 CMScaller 门槛，输入满足模型适配器要求 | 癌种、样本数、输入尺度或基因覆盖不满足 |
| 表达矩阵审计 | 任意可解析矩阵 | 文件不可解析时失败 |

上传 `GSE13294_series_matrix.txt` 这类 GEO 表达矩阵时，系统应给出“这不是 raw counts，不能直接用于 DESeq2”的明确提示，并允许继续做表达矩阵预览或后续模型适用性检查。

### 6.3 Session 创建

用户确认样本分组和分析能力后，系统创建或更新项目的 analysis session：

- `samples.source = "counts_upload"` 或 `"expression_matrix_upload"`；
- `samples.counts_path` 或 `study.input.expression_matrix` 指向项目 uploads 下的不可变文件副本；
- raw counts 且启用 DESeq2 时写入 `pipeline.diffexp.enabled = true`；
- raw counts 或适配器允许时可写入 `cms.run_mode = "counts"`；
- FASTQ 主流程在 counts 直入 session 中保持关闭；
- `project.json` 和 `session.json` 同步创建，状态进入 `input_ready` 或在自动计划模式下进入 `planned`。

## 7. bulk FASTQ 路线

服务器 FASTQ 自动扫描使用已保存 SSH 配置。用户可从表单填目录，也可在对话中说“浏览我的服务器目录”或给出远程绝对路径。

后端扫描只允许只读文件枚举，不执行任意用户命令。扫描结果转换为样本候选后，用户补充分组和参考资源。确认后创建 bulk FASTQ session，能力 ID 为 `workflow.bulk_rna.grch38_pe_expression_fusion`，后续可进入 expression/fusion 主线。

若 SSH 未配置或连接测试失败，扫描按钮和对话动作都返回同一错误，并引导到设置页。密码/API key 保存策略沿用连接设置设计：测试失败不清空已输入凭据；用户保存后可用于本地连接测试。

## 8. WXS 与 scRNA 预留位

WXS 卡片显示：

- 输入：paired tumor-normal WES FASTQ；
- 首期目标：SNV/small InDel，CNV 后续；
- 后续解释：PCGR research interpretation；
- 当前状态：未接入执行链；
- 可点“查看输入要求”，不可点“开始运行”。

scRNA-seq 卡片显示：

- 输入：10x filtered matrix/H5、h5ad、Seurat RDS；
- 首期目标：对象审计、QC、Leiden/UMAP、marker、人工注释、基因/基因集分析；
- 条件能力：pseudobulk；
- 当前状态：未接入执行链；
- 可点“查看输入要求”，不可点“开始运行”。

这些预留位的目的，是让老师看到架构已经覆盖 molecular pathology 和 clinical application 的完整框架，同时不夸大当前 MVP 能力。

## 9. UI 布局

页面保持白色、轻量、工作台风格，参考 DeepScience 的项目工作区和持续会话，但去掉复杂终端、Connector 市场和深色视觉。

布局为：

- 左栏：项目列表、当前项目状态、对话列表；
- 中栏：统一项目向导和对话；
- 右栏：项目上下文，页签为“流程、输入、运行、结果、模型”。

中栏优先展示当前步骤。高级参数、参考路径、容器设置和连接测试默认折叠或放在设置页。右栏只展示项目事实，不承担主要输入。

## 10. API 与数据结构

建议新增或整理以下后端概念：

- `ProjectIntake`：项目的路线、输入类型、上传文件、扫描结果、样本预览、设计草稿；
- `MatrixPreview`：counts/表达矩阵的语义审计结果；
- `RouteStatus`：每条组学路线的可用性、能力 ID、不可执行原因；
- `ProjectVisibleState`：统一用户可见状态。

建议接口：

| 接口 | 作用 |
|---|---|
| `GET /api/projects` | 返回动态推导后的项目状态和路线摘要 |
| `GET /api/projects/{id}/intake` | 读取项目向导草稿 |
| `POST /api/projects/{id}/route` | 设置 bulk/WXS/scRNA 路线 |
| `POST /api/projects/{id}/counts/preview` | 上传或预览矩阵并返回 MatrixPreview |
| `POST /api/projects/{id}/counts/session` | 基于确认后的矩阵和分组创建 session |
| `POST /api/projects/{id}/fastq/scan-remote` | SSH 只读扫描服务器 FASTQ |
| `POST /api/projects/{id}/fastq/session` | 基于 FASTQ 候选和分组创建 session |
| `POST /api/projects/{id}/command` | 对话和按钮共享的结构化动作入口 |

已有 legacy 接口可保留，但 UI 新代码应逐步迁移到项目级接口，减少 `?project=` 参数和会话绑定混乱。

## 11. 错误处理

所有错误按用户能理解的层级展示：

- 配置错误：SSH/API key/参考路径缺失；
- 输入错误：文件不可解析、样本重复、分组缺失；
- 科学门禁：不是 raw counts、样本数不足、癌种不匹配、设计混杂；
- 执行错误：HPC 提交失败、容器缺失、作业失败；
- 输出契约错误：产物缺失、schema 或哈希不一致。

门禁失败不等同于系统失败。页面显示 `NOT_EVALUABLE` 时应说明“这个输入不适合当前能力”，而不是显示红色崩溃式错误。

## 12. 测试与验收

自动测试至少覆盖：

1. 新项目默认进入 `setup`，列表、顶部和右栏状态一致；
2. counts raw matrix 上传后识别样本，保存分组后可进入 `input_ready`；
3. raw counts 且分组合格时可 plan 和 confirm；
4. GEO series matrix 或 normalized expression 不允许启用 DESeq2 raw counts；
5. counts 上传失败或门禁失败不会清空用户已保存连接配置；
6. 服务器 FASTQ 扫描返回 R1/R2 候选后可创建 bulk FASTQ session；
7. WXS/scRNA 路线显示预留状态并返回不可执行原因；
8. 对话里的“浏览服务器目录”“上传后生成计划”等动作和按钮使用同一项目状态；
9. 多对话仍只共享项目事实，不自动读取其他对话正文；
10. 切换项目后不显示前一项目的 intake、样本、运行或结果。

手工验收路径：

1. 新建项目；
2. 选择 bulk RNA-seq；
3. 上传 raw counts 小矩阵；
4. 自动识别样本并批量粘贴分组；
5. 启用 DESeq2，选择 reference condition；
6. 生成计划；
7. 确认契约；
8. 查看右栏流程、输入、运行、结果状态同步。

另一路手工验收为上传 `GSE13294_series_matrix.txt`，预期显示 GEO/normalized expression 提示，并拒绝 DESeq2 raw counts。

## 13. 实施顺序建议

1. 统一状态推导，修复 `drafting/idle` 分裂；
2. 引入 `ProjectIntake` 和 route/input draft 持久化；
3. 扩展矩阵 preview，识别 raw counts 与 GEO/normalized expression；
4. 重构 workbench 为四步向导；
5. 让 counts session 创建走新 intake 数据；
6. 把远程 FASTQ 扫描纳入同一向导；
7. 加 WXS/scRNA 预留卡片和不可执行门禁；
8. 统一对话 command 路由；
9. 跑通自动测试和手工浏览器验收。

每一步都应保持旧接口尽量可用，避免一次性大改导致当前可运行 bulk 能力断掉。
