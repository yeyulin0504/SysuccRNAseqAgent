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
  -> schema 与语义校验，并规范化为实际执行参数
  -> confirmation_policy 分组
  -> 人工确认（如需要）
  -> durable approval context 与一次性 claim 校验
  -> _run_tool 二次权限检查
  -> ProjectSession / 确定性服务
  -> tool 结果回灌给 LLM
```

`agent_tools.confirmation_policy()` 是确认粒度的唯一真源。新增入口不能自行复制
一份风险判断。`_run_tool` 是模型与真实系统之间的唯一执行桥，新增工具必须在这里
映射到已有的确定性实现。

## 三级策略

| 策略 | 适用范围 | 行为 |
|---|---|---|
| `never` | 读取项目、只读扫描、读取状态与报告 | 参数合法后自动执行 |
| `batch` | 样本、参考、分析开关、资源和模型参数等项目配置 | 同一模型轮次合并成一张确认卡 |
| `solo` | 计划、冻结契约、运行，以及服务器连接目标 | 每个动作单独确认 |

连接配置虽然通常只是写配置，但它决定命令发往哪台服务器，因此固定为 `solo`。
密码、私钥和 LLM API Key 不暴露给模型，也不出现在工具参数、确认卡片、对话历史
或日志中；它们只能由设置页写入受保护的用户级存储。

QC 决策不属于普通 LLM 工具。它只能通过运行图已经持久化的 `/graph/resume`
检查点提交，并同时校验当前 `run_id`、attempt 和待恢复 interrupt。这样可避免模型
在没有真实 QC checkpoint 时自行构造“通过/拒绝”决定，或把旧运行的批准重放到新
运行。`record_qc_decision` 不得重新加入 `TOOL_SPECS` 或 `_run_tool`。

`never` 表示不需要人工签字，不等于代码可以做任意副作用。允许的副作用仅限于
刷新本地运行状态或生成确定性的派生报告。它不能提交作业、修改科学参数、改变
连接目标或写入用户决定。

没有 LangGraph checkpointer/resume 能力的规则 fallback 只能自动执行只读动作。
无论是非流式 `/api/chat`、LLM 未配置、模型请求失败还是图构建失败，只要规则意图
会写配置、生成计划、冻结契约或运行分析，就返回 `confirmation_required`，不得退回
旧规则执行器直接写盘，也不得生成无法恢复的伪确认卡。

## 批准语义

批准采用 fail-closed 规则：只有 `{"approved": true}` 中的 JSON 布尔 `true`
才算批准。字段缺失、字符串 `"yes"`、数字、列表、裸布尔值或其它对象全部按拒绝
处理。HTTP 入口和图层都必须执行这一规则，不能只依赖前端校验。

批准还必须绑定用户实际看到的对象。QC 检查点将 durable interrupt 中的
`run_id` 由服务端注入 resume，并在执行前与当前 attempt 精确比较；attempt 漂移、
持久化失败、旧 checkpoint 缺少绑定信息或显式拒绝都终止为 `FAIL`，不能继续下游。
普通工具确认卡也采用同一原则：guardrail 在 durable checkpoint 中保存
`approval_id`、project、thread、tool_call ids、工具名、规范化参数摘要、项目/共享
连接 revision 与 hash、contract/run id、policy version 和有效期。浏览器恢复时必须
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

## 下一轮权限加固门禁

- `browse_remote_samples` 当前免确认读取任意绝对路径。应把默认范围限制在用户批准
  的数据根目录；越界扫描改为 `solo` 确认，并限制超时、文件数、符号链接逃逸和
  输出大小。
- CI 增加工具清单完整性元测试：每个 `TOOL_SPECS` 项都必须有 schema、risk、
  policy、确认卡渲染、executor 映射、图层测试和端点测试；嵌套对象同样要求
  `additionalProperties: false`。
- 用 sentinel 密码、API Key、私钥跑完整链路，断言秘密不出现在确认卡、tool
  result、异常、对话 checkpoint、工具日志和项目日志。
- 同一 project/thread 的重复 resume 已串行化并有 durable claim 兜底。下一步增加
  两个 thread 同改项目、两个项目同改共享连接的并发测试；共享写入采用 revision/CAS
  或项目级写锁，不能依赖最后写入者覆盖。
- 提供禁用全部 LLM 写入/执行工具的 kill switch，并纳入发布测试。
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
