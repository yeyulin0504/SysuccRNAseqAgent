# 测试与评测推进路线

本文把主项目与独立 `bkbio-eval` 的职责、测试层级和推进顺序固定下来。两者解决
的问题不同，不应合并成一个包。

## 职责边界

主项目 `SysuccRNAseqAgent` 负责工作台、项目状态、LLM 工具循环、科学门禁、
Analysis Contract、远程执行和产物管理。它的测试回答“动作是否被正确路由、授权
和执行”。

`bkbio-eval` 是独立的数值回归脚手架。它从 counts 或 FASTQ 用例出发，对被测
系统的真实结果做结构、方向、效应量和内部一致性检查。它不评测 LLM 文案，也不
把某个参考实现的输出当作唯一金标准。

Adapter 是两者唯一的连接面。它负责调用主项目的真实执行路径并把真实产物转换为
`bkbio-eval/analyzer-result@1`，不能在 Adapter 中另写一套 CPM+t 检验或复制
DESeq2 逻辑。否则评测通过只说明 Adapter 正确，与主项目无关。

## 四层验证

| 层级 | 触发频率 | 运行环境 | 主要发现的问题 |
|---|---|---|---|
| 产品单元/端点测试 | 每次提交 | Windows 本地 Python | 状态机、权限、参数、写盘、UI API |
| L0 合成 counts | 每次提交或 PR | 固定下游容器 | 方向、阈值、字段映射、输出契约 |
| L1 真实 counts | 定时或发布前 | 固定 DESeq2 环境 | 真实规模、归一化、设计、效应量 |
| L2 FASTQ 到矩阵 | 里程碑/夜间 | 服务器或固定镜像 | FASTQ 配对、比对、定量、版本组合 |

LLM 行为测试使用脚本化 fake model，验证工具协议和权限，不依赖线上模型的随机
输出。线上模型只做人工 smoke test，不作为回归套件的唯一判据。

## 第一阶段：权限与协议稳定

完成以下条件后，才继续增加模型工具：

- 拒绝任一确认组时，所有已声明 `tool_call_id` 都有 tool 回复；
- 只有字面量布尔 `true` 能批准，所有其它 resume 值 fail closed；
- 确认卡片读取全局连接覆盖后的真实旧值；
- 每个工具至少有图层契约测试；关键工具有 web 端点测试；
- `set_diffexp_reference` 与 `set_cms_options` 覆盖真实落盘和科学门禁；
- `browse_remote_samples`、`rollback_changes`、`record_qc_decision` 覆盖真实路由；
- 全套 pytest 通过，偶发 Windows 临时目录竞态需重跑确认后单独记录。

任务 #139（分组名按样本顺序循环展开）和 #140（链特异性 `unknown`）已经有实现
与自动测试，应在外部任务表中核实并关闭。任务 #138 是否关闭，应以全量工具端点
覆盖和本文件第一阶段验收为准。

截至 2026-09-16，本阶段的代码缺陷与覆盖缺口已处理：tool-call 协议覆盖批准、
拒绝、整组非法以及部分非法/部分合法；resume 采用 fail-closed；共享连接旧值按真实
生效配置渲染；交付中列出的五个零覆盖工具均增加了 web 端点测试。主项目验证结果
为 `549 passed, 1 skipped, 6 subtests passed`。

## 第二阶段：建立真实 Adapter

建议把 `bkbio-eval` 放入独立 Git 仓库并固定版本；当前 WorkBuddy 交付目录不是 Git
仓库，不适合作为长期真源。主项目保存兼容版本和调用说明，不复制 evaluator 源码。

Adapter 的最小流程：

1. 接收 `--inputs`、`--params`、`--out`、`--case-id`。
2. 根据 counts/coldata 建立一个隔离的 counts 直入项目。
3. 显式写入 reference/contrast、设计参数和冻结容器版本。
4. 走 `plan -> confirm -> counts stage` 的真实 ProjectSession 路径。
5. 等待并验证 `diffexp/deseq2_results.tsv` 与 summary 产物。
6. 将全部基因结果映射到 `tables.deseq2_results`，并从同一结果生成 up/down 集合。
7. 记录主项目版本、容器 digest、参数、contract id 和产物校验和。

第一版只接 counts，不接 FASTQ。若执行环境缺 R/DESeq2，Adapter 应明确返回
unavailable/skip 原因；不能退回模板统计实现并把结果冒充为主项目输出。

## 第三阶段：修复 evaluator 已知问题

优先处理：

1. 锁住 `library_size_ratio = contrast_level / reference_level` 的语义。当前两个示例
   已按参数索引样本，交换 reference/contrast 的实测比值为 `2.0 -> 0.5`，但变量名
   仍叫 `trt_lib/ctrl_lib`，且 handoff 与 case rationale 仍声称“硬编码”。应先添加
   交换后取倒数的测试，再重命名变量并修正文档，避免维护者误判为未修缺陷。
2. 修改 evaluator 断言后重跑 unit、L0、L1 变异测试；不能通过放宽容差来消除
   失败。
3. 保持 `consistency` 的空集合显式失败和 `$param` 阈值引用。
4. 同步维护 `README.md`、`docs/ADAPTER.md` 和模板，避免契约漂移。
5. 用带 R/DESeq2 的固定容器落地 `L1_airway_deseq2`，补足 BH 校正更容易被观察
   的真实用例。

WorkBuddy 交付目录中的 evaluator 已完成参数方向测试、mutation/CLI 子进程的
Windows UTF-8 修复和多层 `--level unit,L0,L1` 支持。验证结果为
`116 passed, 1 skipped`；变异测试
保持 unit `6/6`、L0 `11/11`、L1 `12/12`。该目录仍需迁入正式 Git 仓库，才能对
这些变更做长期版本追踪。

## 第四阶段：CI 与发布门禁

建议分三条流水线：

- `fast`: 主项目 pytest + evaluator 单元测试 + L0，目标十分钟内；
- `numerical`: L1 counts，固定容器 digest，保存 JSON 报告和失败产物；
- `pipeline`: L2 FASTQ，固定 STAR/Salmon/参考索引版本，夜间或里程碑运行。

CI 必须使用 `--require-confirmed`，并把“全部 skipped”视为失败。期望值更新必须作为
独立审阅变更，说明来源、方法、容差依据和变异测试结果。

## 完成标准

下一阶段可以开始扩展 WXS/scRNA 或更多自主工具的前提是：

- 主项目的权限和协议测试稳定通过；
- counts 直入真实 Adapter 能跑 L0 与至少一个 L1；
- 每次评测报告能追溯到主项目 commit、容器 digest、Analysis Contract 和输入校验
  和；
- 失败能区分产品协议错误、运行环境不可用、数值断言失败和 evaluator 自身错误；
- 任何自动执行都仍受工具白名单、参数校验、人工确认和科学门禁约束。
