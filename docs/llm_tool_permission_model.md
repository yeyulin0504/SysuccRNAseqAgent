# LLM 工具权限模型

本文定义工作台对话 Agent 的权限边界。目标是让模型能推进真实分析，同时保证
模型不能凭一段自然语言直接修改项目、切换服务器或启动计算。

## 核心原则

模型只负责选择已登记的工具并生成结构化参数。代码负责工具白名单、参数校验、
风险分级、人工确认、状态机和实际执行。模型不能生成任意 shell 命令、任意文件
路径操作或未登记的工具名并绕过执行层。

所有工具调用都经过以下链路：

```text
LLM tool_call
  -> 用户级 live tool_mode 过滤 schema
  -> guardrail 重新读取 tool_mode，再做语义校验与参数规范化
  -> confirmation_policy 分组
  -> 人工确认（如需要）
  -> resume / execute 重新读取 tool_mode
  -> durable approval context 与一次性 claim 校验
  -> _run_tool 再次读取 tool_mode 并二次检查
  -> ProjectSession / 确定性服务
  -> tool 结果回灌给 LLM
```

`agent_tools.confirmation_policy()` 是确认粒度的唯一真源。新增入口不能自行复制
一份风险判断。`_run_tool` 是模型与真实系统之间的唯一执行桥，新增工具必须在这里
映射到已有的确定性实现。

## 全局工具权限模式

用户级 LLM 设置包含一个全局 `tool_mode`。它控制的是**模型与规则 fallback 代表
模型发起的工具动作**，不改变结构化工作台按钮、项目状态机或 QC 检查点的权限。
设置页是唯一修改入口，`TOOL_SPECS` 不提供修改该字段的工具，因此模型不能自行
抬高权限。

| 模式 | 向模型暴露的工具 | 运行时行为 |
|---|---|---|
| `disabled` | 无 | 所有模型工具调用和规则工具意图返回结构化 `blocked` |
| `read_only` | 仅 `risk=read` | 只读可自动执行，写入与执行被拒绝 |
| `approved_write` | `read` + `write` | 只读自动执行；写入仍需人工确认；执行被拒绝 |
| `approved_execute` | 全部已登记工具 | 只读自动执行；写入与执行仍按原策略确认 |

旧安装的有效配置中真正不存在 `tool_mode` 字段时默认为 `approved_execute`，保持升级
兼容。显式 JSON `null` 不属于“字段不存在”；它与未知字符串、布尔、数字、数组、
对象、损坏 JSON、非对象配置根和非对象 `llm` 块一样均视为配置错误。保存 API 拒绝，
运行时读取异常则按 `disabled` fail closed。未知工具沿用最高风险
`execute`，除 `approved_execute` 外全部模式先在权限层拒绝；即使处于
`approved_execute`，后续白名单和参数校验仍会拒绝未登记名称。

模式在 provider schema、guardrail、confirmation/resume、execute 和 `_run_tool`
五处读取同一用户级 live 值。确认卡同时绑定发卡时的模式。等待确认时切到更严格
模式后，resume 会消费旧卡并拒绝；即使权限在 confirmation 与 execute 之间改变，
execute 也会在创建一次性 claim 和调用 executor 前停止。直接调用 `_run_tool` 同样
不能绕过开关。

该开关只限制切换之后新发起的 LLM/规则工具动作，不是远程作业急停开关。已经提交
给远程调度器的作业不会因模式切到 `disabled` 而终止；需要取消时必须使用项目或
调度器的作业控制路径，并核对对应的 `run_id` 和远程 job id。

## 模型数据披露范围

`tool_mode` 只控制模型能发起什么动作，不代表模型可以读取项目中的全部精确信息。
系统另设独立的 `data_scope` 边界。默认情况下，provider 只接收去标识项目摘要：
项目状态、路线、输入类型、样本数、分组计数、确定性样本别名、科学门禁结果和流程
状态。默认上下文不得包含源样本名、患者标识、FASTQ 文件名、远程或本地路径、
host/user/job id、报告正文、命令、stdout/stderr、traceback 或凭据。

需要精确信息时，必须使用独立的数据披露确认，按 `sample_ids`、
`fastq_filenames`、`remote_paths` 和 `report_excerpt` 分开授权。授权绑定 project、
thread、provider identity、字段类别、项目与数据 revision；只供一轮 provider 请求使用，
十分钟过期，发送结果不确定时也视为已消费。普通工具确认卡不能顺带扩大
`data_scope`，而数据披露授权也不能增加写入或执行权限。

原始披露值只在获批的单次 provider 请求中临时组装，不写入持久化对话、tool log
或 checkpoint。持久记录只保存授权元数据、类别、数量、字节数和结果码。所有
provider 请求最终都由同一 `ModelContextBuilder` 通过显式 allowlist 构造，并在发送
前检查已知凭据和私钥标记。详细设计见
`docs/superpowers/specs/2026-09-17-llm-data-disclosure-design.md`。

## 三个相互独立的权限轴

远程数据根、工具模式和模型数据披露回答的是三个不同问题，不能由模型调用一个
工具互相提升：

| 权限轴 | 要回答的问题 | 唯一授权方 |
|---|---|---|
| Approved remote root | 当前 SSH 身份是否可以扫描这个远程目录？ | Settings 中的 approved-root 生命周期 |
| `tool_mode` | 规则/LLM 逻辑是否可以调用这类工具？ | 用户级 live tool-mode 设置 |
| `data_scope` | provider 这一轮可以收到哪些精确项目数据？ | 独立的数据披露授权 |

Settings 是创建、扩大和撤销 approved root 的唯一入口。项目 JSON、对话确认卡、模型
工具参数和通用连接编辑都不能自动迁移或写入 root。结构化本地工作台可以显示已获批
扫描的精确目录与文件名；provider 默认仍只收到去标识的数量、状态和组计数，除非
另有绑定 project、thread、provider、字段类别与 revision 的一次性披露授权。

远程浏览统一返回四个通道：`local` 仅供当前请求和结构化 UI 使用，`model` 是 provider
安全摘要，`log_projection` 是唯一允许进入通用工具日志的内容，`security_audit` 只交给
独立审计 owner 完成一次权威提交。审计提交失败时 exact scan reference 必须撤销或保留
为可 reconciliation 的 uncertain 状态；History 只是去标识显示投影，不能替代审计记录，
也不能在审计已提交后把一次成功误改成失败。

Workbench 的 remote scan apply 只接受服务器构造的
`BrowseContext(project_id, None, "workbench")`。浏览器提交的 `scan_id`、
`result_revision`、`group_id` 和截断确认会在 private scan store 中重新验证；目录、样本
行、当前身份、approved root 与 policy revision 都不以浏览器 JSON 为 authority。整个
bounded scan 的 `source_ref` 指向全部目录组，不能通过客户端挑选子集重写结果。一次
apply 先持久化 claim，再在 project state 中原子写入 filename-only FASTQ 和 opaque receipt，
最后把 scan 标记为 consumed；崩溃重试复用同一 claim，避免重复写入。

## 三级策略

| 策略 | 适用范围 | 行为 |
|---|---|---|
| `never` | 读取已有项目配置、只读扫描远程目录 | 参数合法后自动执行 |
| `batch` | 样本、参考、分析开关、资源、刷新状态和生成报告等会落盘的动作 | 同一模型轮次合并成一张确认卡 |
| `solo` | 计划、冻结契约、运行，以及服务器连接目标 | 每个动作单独确认 |

连接配置虽然通常只是写配置，但它决定命令发往哪台服务器，因此固定为 `solo`。
密码、私钥和 LLM API Key 不暴露给模型，也不出现在工具参数、确认卡片、对话历史
或日志中；它们只能由设置页写入受保护的用户级存储。

QC 决策不属于普通 LLM 工具。它只能通过运行图已经持久化的 `/graph/resume`
检查点提交，并同时校验当前 `run_id`、attempt 和待恢复 interrupt。这样可避免模型
在没有真实 QC checkpoint 时自行构造“通过/拒绝”决定，或把旧运行的批准重放到新
运行。`record_qc_decision` 不得重新加入 `TOOL_SPECS` 或 `_run_tool`。

`never` 表示不需要人工签字，也表示实现必须没有持久化副作用。当前只有读取已有
项目配置和只读 SSH 目录扫描属于这一类。`refresh_project_status` 会保存会话状态，
`get_project_report` 会生成报告文件，两者都属于 `write`，不能在 `read_only` 下运行。

没有 LangGraph checkpointer/resume 能力的规则 fallback 同样先读取 `tool_mode`。
模式允许的只读动作可以自动执行；模式不允许的动作返回结构化 `blocked`。模式允许
但需要确认的写入或执行返回 `confirmation_required`。无论是非流式 `/api/chat`、
LLM 未配置、模型请求失败还是图构建失败，都不得退回旧规则执行器直接写盘，也不得
生成无法恢复的伪确认卡。

## 批准语义

批准采用 fail-closed 规则：只有 `{"approved": true}` 中的 JSON 布尔 `true`
才算批准。字段缺失、字符串 `"yes"`、数字、列表、裸布尔值或其它对象全部按拒绝
处理。HTTP 入口和图层都必须执行这一规则，不能只依赖前端校验。

批准还必须绑定用户实际看到的对象。QC 检查点将 durable interrupt 中的
`run_id` 由服务端注入 resume，并在执行前与当前 attempt 精确比较；attempt 漂移、
持久化失败、旧 checkpoint 缺少绑定信息或显式拒绝都终止为 `FAIL`，不能继续下游。
普通工具确认卡也采用同一原则：guardrail 在 durable checkpoint 中保存
`approval_id`、project、thread、tool_call ids、工具名、规范化参数摘要、项目/共享
连接 revision 与 hash、contract/run id、policy version、tool mode 和有效期。浏览器恢复时必须
回传实际看到的 `approval_id`；Web 层和图层都会校验。旧卡重放、跨项目/线程 resume、
等待期间参数、连接、契约或 run 变化都 fail closed。项目或绑定文件不可读时不展示
可批准卡片，执行前也会再次拒绝。

签名、卡片和 executor 使用同一份规范化参数。规范化发生在发卡前，因此循环补齐的
样本分组、默认输入模式、数值类型和去除空字段等真实执行语义都属于用户批准对象；
确认后不得再把另一份参数交给执行器。卡片必须展示全部关键语义，包括样本 condition、
R1/R2、删除项和 CMS 的 enabled、n_perm、fdr、run_mode。

拒绝一张卡片时，同一模型轮次里尚未展示的动作全部取消。系统必须为 assistant
声明过的每个 `tool_call_id` 生成对应的 tool 消息，包括：

- 已批准并执行的调用；
- 用户拒绝的调用；
- 因前一个调用被拒绝而一并取消的调用；
- 参数校验失败的调用；
- 执行时抛异常的调用。

这既是 OpenAI tool-calling 协议要求，也是保证下一轮模型能理解真实状态的条件。

## 确认卡片

卡片描述的是即将执行的具体动作，不是抽象工具名。写配置时必须展示真实生效的
`旧值 -> 新值`。服务器连接是用户级共享配置，因此旧值要先合并全局连接，再与
新值比较；直接读取项目内的 `server` 块会误导用户。

确认卡片的 guardrail 节点必须无副作用。LangGraph 在 resume 时会重跑 interrupt 节点，
任何写盘、远程调用或状态刷新都可能造成重复执行。只有 execute 节点可调用
`_run_tool`。

execute 节点不能只信 guardrail 之前的结果。执行前应再次调用参数校验、重新读取
确认策略并验证 approval context；批准状态必须使用字面量布尔判断。对于可能在
“副作用已完成、checkpoint 尚未写回”之间崩溃的动作，Web 层先按 project/thread
串行化 stream 与 resume；execute 在调用 executor 前，还会在项目目录的
`.approval_claims/` 里用 `O_EXCL` 原子创建以 `sha256(approval_id)` 命名的 claim，
写入 `started` 后 flush 和 fsync。普通成功或失败会更新为 `consumed`；若进程在
executor 后异常退出，`started` 保留，任何重放都拒绝再次执行。claim 只保存绑定
摘要和结果布尔摘要，不保存原始参数或秘密。该策略选择 fail closed：不确定是否已
产生副作用时，由用户核对项目/远端状态后重新发起新卡，系统不自动重试。

`batch` 表示一次确认覆盖卡片中的多个配置动作。当前执行仍按工具逐项记录
ChangeSet；运行时若中途失败，前面已完成的变更不会自动回滚。卡片和工具结果应
逐项展示成功或失败。若未来需要真正的事务语义，应先实现配置快照和原子提交，
不能仅靠 UI 文案声称“全部一起生效”。

## 科学门禁与权限门禁

人工批准只表示用户同意尝试这个动作，不表示科学设计合格。两类门禁必须同时
存在：

- 权限门禁决定动作是否可以执行；
- 科学门禁决定设计是否适用，例如 DESeq2 分组、CMS 癌种与样本量要求。

模型不能用用户批准绕过科学门禁。配置 CMS 后即使写盘成功，也必须把当前不满足
的癌种、样本量或输入依赖原因回灌给模型和用户。

## 新增工具检查表

新增或扩大工具能力时必须完成以下检查：

1. 在 `TOOL_SPECS` 中声明最小参数面、`additionalProperties: false` 和风险级别。
2. 在 `validate_call()` 中补足 schema 无法表达的路径、标识符和跨字段约束。
3. 由 `confirmation_policy()` 决定粒度；连接、外部发布、删除和真正执行默认为
   `solo`。
4. 确认卡片展示动作对象、关键参数和真实旧值；秘密字段始终省略。
5. `_run_tool` 只调用确定性服务，不接收模型拼接的 shell、SQL 或任意 URL。
6. 执行层再次检查批准状态；未知工具按最高风险处理并拒绝执行。
7. 添加图层测试：批准、拒绝、非法参数和 tool-call 协议不变量。
8. 添加 web 端点测试：真实 checkpointer、真实项目目录、确认前不写、批准后只写
   一次、拒绝后不写；并覆盖并发双击和崩溃重放。
9. 对科学工具添加适用性门禁测试，不能只测配置字段落盘。
10. 为副作用工具绑定 durable approval context，并在 executor 前创建一次性 claim。
11. 日志记录工具名、风险、参数摘要和结果，不记录密码、API Key 或私钥。
12. 在四种 `tool_mode` 下测试 schema 暴露、guardrail 和 executor；等待确认期间
    收紧权限必须使旧卡失效。

## 下一轮权限加固门禁

- `browse_remote_samples` 必须限制在 Settings 中为当前 SSH 身份明确批准的数据根。
  越界扫描直接结构化拒绝，不能转换成普通 `solo` 工具确认；批准根的预览、批准和
  撤销只允许由 Settings 完成。所有结构化和对话入口统一使用中央授权服务，并限制
  超时、文件数、符号链接逃逸和输出大小。详细设计见
  `docs/superpowers/specs/2026-09-17-approved-remote-data-roots-design.md`。
- CI 增加工具清单完整性元测试：每个 `TOOL_SPECS` 项都必须有 schema、risk、
  policy、确认卡渲染、executor 映射、图层测试和端点测试；嵌套对象同样要求
  `additionalProperties: false`。
- 用 sentinel 密码、API Key、私钥跑完整链路，断言秘密不出现在确认卡、tool
  result、异常、对话 checkpoint、工具日志和项目日志。
- 同一 project/thread 的重复 resume 已串行化并有 durable claim 兜底。下一步增加
  两个 thread 同改项目、两个项目同改共享连接的并发测试；共享写入采用 revision/CAS
  或项目级写锁，不能依赖最后写入者覆盖。
- 当前威胁模型仅覆盖单用户 localhost 与随机 session token。若开放远程或多用户，
  必须先加入身份、角色、审批人绑定和可追溯审计主体。

## 当前明确不授予模型的能力

- 任意 shell/PowerShell/SSH 命令；
- 任意本地文件读写或目录删除；
- 密码、私钥、API Key 的读取或设置；
- 绕过 Analysis Contract 直接启动未冻结流程；
- 修改未列入 schema 的服务器或流水线字段；
- 将报告、消息或数据发送到外部服务；
- 自动合并代码、发布版本或删除项目。

这些能力若未来确有需求，应作为新工具逐项设计和测试，不能扩大现有工具参数来
间接获得。
