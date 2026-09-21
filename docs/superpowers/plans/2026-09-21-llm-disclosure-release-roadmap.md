# LLM 数据披露发布路线图

日期：2026-09-21  
分支：`wizard`

这份路线图把“模型能做什么”和“模型能看到什么”分开推进。当前产品默认采用：

> 模型默认只收到去标识摘要；样本名、FASTQ 文件名、远程目录和报告片段必须单独确认。

## 当前基线

已经落地并有测试覆盖的部分：

- provider 默认上下文通过 `ModelContextBuilder` 构造，项目状态、路线、样本数量、分组计数和科学门禁使用显式 allowlist。
- 样本名、FASTQ 名称、路径、报告正文、凭据和远程主机信息不会进入默认 provider projection。
- 主要生成式 HTTP、Responses、CLI/GUI 和 Codex 入口经过 `ModelProviderGateway`；请求和响应有大小限制、工具调用拒绝和最后一道 secret scanner。
- approved remote root、`tool_mode`、`data_scope` 是三条独立权限轴。
- remote scan 的结构化 UI 可以显示本地精确信息，provider 默认只收到数量、配对统计和不透明 `source_ref`。
- metadata-only grant store 已具备 TTL、project/thread/provider/tool-mode/revision 绑定、单次 claim 和 terminal 状态。
- local `sample_ids` 已有独立的结构化 Web API：创建元数据卡、布尔批准/拒绝、一次性 exact provider send；返回正文只存在当前请求，不写入 History、ChatState 或 grant JSON。

仍然明确关闭或未接通的部分：

- exact grant 已接入独立的结构化 Web API 和真实 provider 请求；自然语言 ChatGraph 已接入只支持 `sample_ids` 的独立 disclosure intent/card，并在批准后通过 Graph → claim → dispatch 返回一次性 transient exact response。前端独立 disclosure card 仍待实现。
- 当前只开放 local `sample_ids`；`fastq_filenames`、`report_excerpt`、`remote_paths` 和 remote `source_ref` 仍拒绝。
- claim、connection snapshot、revision 复核和 exact response collector 已在这条 local API 中形成闭环；approved-root remote exact 仍关闭。
- 因此当前不能声称“用户确认后模型可以读取远程 FASTQ 精确路径”。

## 分阶段推进

### 阶段 A：summary-only release gate

目标是让默认边界稳定，任何异常都停在 summary 或结构化拒绝。

验收条件：

1. 默认 provider payload 不含样本 ID、FASTQ 名称、路径、报告片段、host/user/job id 和秘密。
2. 每个 assistant `tool_call_id` 都有且只有一个 tool 回复。
3. 工具日志只保存版本化参数投影、哈希和结果摘要；raw arguments 只存在有 TTL 的 request-local store。
4. structured tool result 不因安全清洗器扫描 JSON 字段名而被替换成泛化文本。
5. 完整 pytest、`compileall`、`git diff --check`、bkbio-eval 和 unit/L0 mutation 均有新鲜证据。

### 阶段 B：local `sample_ids` vertical slice

第一条 exact 链路只支持本地 `sample_ids`，暂不开放 FASTQ 文件名、报告片段和远程路径。

业务流：

1. 模型或用户发起“需要样本名”的请求，系统生成 data-disclosure confirmation card。
2. 卡片只显示字段类别、记录数、用途、provider identity 摘要、revision 摘要和过期时间，不显示原值。
3. 用户批准后，系统在固定 connection snapshot 下 claim grant。
4. `ModelContextBuilder` 只在 request-local context 中注入 `sample_ids` exact block，并带 manifest；ChatState、checkpoint、History、tool log 和 grant JSON 只保存 manifest。
5. exact 请求强制走 `dispatch_exact`，禁用 tools/tool choice；响应必须完整通过 bounded collector 后才能短时显示。
6. pre-transport failure 标记 `consumed_failed`；transport 已开始但结果不确定标记 `consumed_ambiguous`；任何结果都禁止自动重试。

必须覆盖：拒绝、过期、重复 claim、跨 project/thread、provider 切换、tool mode 收紧、sample revision 变化、secret scanner 命中和 provider tool call。

### 阶段 C：local FASTQ/report scopes

在阶段 B 稳定后分别开放 `fastq_filenames` 和 `report_excerpt`，每个字段类别单独授权，不能从 sample grant 自动扩大。报告只允许 bounded excerpt，不能发送完整 report、stdout、stderr 或 traceback。

### 阶段 D：approved-root remote exact

最后才实现 remote `source_ref` consumer。claim 前必须按固定顺序锁定 connection、remote scan/policy、project/grant，并重新验证：SSH identity、approved root、policy revision、scan/result revision、whole-scan `source_ref` 和未过期状态。`thread_id=None` 的 workbench scan 不能授权给 LLM thread。

## 权限判定顺序

每次 exact provider send 都按以下顺序检查：

1. `tool_mode` 是否允许这类模型动作；
2. data grant 是否绑定当前 project、thread、provider identity、tool mode 和数据 revisions；
3. 若涉及 remote data，approved root 和完整 scan 是否仍有效；
4. 是否已经用 durable single-use claim 抢占；
5. request-local builder 是否只生成被批准字段；
6. gateway 是否在发送前发现 secret、tool call 或超限响应。

任何一项失败都不发送 provider 请求。exact grant 不授予写盘、扫描或执行权限；approved root 也不扩大模型 data scope。

## 当前 local `sample_ids` 结构化入口

阶段 B 的第一条业务入口暂时不让普通 LLM 工具自行扩大数据权限，而是使用独立的
project-scoped API：

1. `POST /api/projects/{project_id}/data-disclosures` 只接受
   `fields: ["sample_ids"]` 和用途文本，返回不含原值的 disclosure card。
2. `POST .../{grant_id}/decision` 只接受 JSON 布尔 `approved`；字符串、数字和缺失值
   都拒绝。
3. `POST .../{grant_id}/send` 在已批准且仍然绑定当前 project/thread/provider/tool mode
   与 revisions 时 claim grant，构造 exact request-local context，并强制调用
   `dispatch_exact`。响应正文作为当前 HTTP 结果返回，不追加到聊天 transcript 或
   History；发送不确定时 grant 进入 `consumed_ambiguous`，不会自动重试。

这条入口先验证权限和传输边界；ChatGraph 的专用 disclosure intent 现在复用同一套
metadata-only grant/card，并在批准后用独立 sender 完成 request-local exact response。
exact 文本只进入当前 SSE `disclosure_result` 事件，不写入模型上下文、History 或日志。
前端独立 disclosure card 仍单独推进。
普通工具确认卡不会顺带扩大 `data_scope`。

## 文档维护规则

- `docs/llm_tool_permission_model.md` 描述长期权限契约；本路线图记录当前实现状态和阶段门槛。
- 每完成一个阶段，更新本路线图的基线、测试命令和明确关闭项，再更新 `.superpowers/sdd/2026-09-21-night-run-hardening/progress.md`。
- 任何把 `remote_paths`、任意文件、凭据、完整报告或 shell 输出加入 exact scope 的改动，都必须另写 spec，不能通过扩大现有 schema 实现。
