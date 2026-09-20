# Task 6 independent review

审查对象：`d9888d3`（`feat: route every remote browse through approved roots`）。

结论：**BLOCKER，不能通过 Task 6 review**。这次提交把四个入口的大部分调用路径集中到 `_execute_browse_attempt`，但仍有几个会直接破坏 Task 6 安全契约的绕过点。尤其是 disabled 模式没有进入统一 wrapper/audit、旧的 `find` 扫描器仍然是独立生产实现、扫描存储与审计协议还没有达到计划要求的严格校验和故障原子性。

## 验证证据

- `python -m compileall -q src tests`：通过。
- 使用仓库 `.venv` 运行 Task 6 相关回归：`275 passed, 6 failed, 1 warning`。
- 失败项为 `test_browse_samples_surfaces_readable_failure`、`test_remote_scan_returns_paired_preview`、`test_chat_browses_remote_samples_before_calling_llm`、`test_command_scan_failure_skips_history_and_reports_error`、`test_command_scan_success_records_fastq_scan_history`、`test_browse_remote_samples_runs_without_a_card_and_returns_pairs`。
- 失败用例包含旧的 `find`/无 approved-root 假设，但提交没有替换这些断言，也没有补上计划中要求的四入口、disabled audit、故障注入和 four-channel 契约测试；因此不能把报告中的局部 `106 passed` 当作 Task 6 GREEN 证据。

## Blocker 1：disabled 模式绕过统一 wrapper 和审计

计划要求 disabled 的 rule/LLM browse 在不访问 policy、credential、transport 或 inner executor 的前提下，仍通过 `_execute_browse_attempt` 产生一次 denied audit。当前代码在进入 wrapper 之前就返回：

- `src/rnaseq_agent/webapp.py:2378-2380` 的 `_run_tool` 先调用 `tool_mode_block`，因此 disabled 的 `browse_remote_samples` 返回普通 dict，不会调用 `_execute_browse_attempt`。
- `src/rnaseq_agent/webapp.py:2973-2978` 的 `/api/chat` 先调用 `_rule_intent_mode_block`；
- 流式 rule 路径同样在 `src/rnaseq_agent/webapp.py` 的 browse 分支之前做 mode block。

结果是 disabled 失败没有统一 `ToolExecutionResult`，也没有 `_record_browse_audit` 事件，直接违反 primary cases `37-41,43` 和 Task 6 Step 4/5/7。

## Blocker 2：旧的独立远程扫描实现仍在生产代码中

`src/rnaseq_agent/webapp.py:610-631` 的 `_scan_remote_samples` 仍然拼接 `find`，调用通用 transport，并直接使用 `detect_fastq_pairs`。这不是 thin adapter，而是完整的第二套远程发现逻辑。Task 6 Step 11 明确要求删除或改成只调用 `_execute_browse_attempt` 的兼容层；仓库搜索仍能命中该实现。保留它会继续提供绕过 approved-root、bounded scanner 和 authoritative audit 的潜在出口，也使现有旧测试继续误导未来维护。

## Blocker 3：scan store 的 alias 和 record 校验不严格，source_ref 可被错配

`src/rnaseq_agent/remote_scan_store.py:262-274` 的 `load_remote_scan` 直接读取 alias JSON，没有先验证 alias 的 private path、字段集合、`source_ref` 是否等于查询值，也没有验证 alias 指向的 record 的 `source_ref` 是否仍然匹配。可将 `src_A` 的 alias 改成合法 `scan_B`，随后 `load_remote_scan(source_ref=src_A)` 返回 B 的完整扫描结果。这个行为违反计划中“source lookup validates the alias and re-checks the full record binding”。

`_record_from_json`（`remote_scan_store.py:143-160`）也不是严格 schema：它用 `str()`/`bool()` 强制转换，允许缺省 `apply_state`，过滤掉非 dict sample，而不拒绝 malformed record；`_cleanup`（`remote_scan_store.py:202-213`）遇到 malformed record 直接忽略。因此损坏的 record 既不会 quarantine，也可能在容量统计和恢复流程中被静默跳过。

## Blocker 4：scan store 写入协议没有短写和 alias 发布故障的完整处理

`remote_scan_store.py:175-194` 对 record/alias 使用单次 `handle.write(raw)`，不是短写安全循环。注入短写可能把截断 JSON 原子发布。`store_remote_scan`（约 `216-258`）在 alias 写入失败后直接对两个路径调用 `unlink`，没有按同一 verified helper 做清理、没有对“replace 已发布但 flush 报错”的状态做 reconcile，也没有保证 cleanup 本身成功。计划要求的 crash/short-write/replace/fsync 矩阵不能由当前实现满足。

## Blocker 5：authoritative audit 没有实现计划规定的 reconcile/损坏记录协议

`src/rnaseq_agent/security_audit.py:84-103` 只在 `_write` 抛出 `OSError` 且目标文件存在时抛出 `AuditCommitUncertainError`，没有在同一锁范围内调用 `reconcile_browse_audit`、重试 flush 或区分 `committed/absent/uncertain`。因此目录 fsync 在 `os.replace` 后失败时，只能粗略标记 uncertain，无法完成计划要求的 bounded reconciliation。

`_parse`（`security_audit.py:77-81`）只调用 `json.loads`，没有验证固定字段集合、事件 id、文件名 digest、记录大小和字段类型；malformed 或 digest-mismatched published record 也不会按计划 quarantine 并使 sink 进入明确不可用状态。`reconcile_browse_audit` 对这类错误统一返回 `uncertain`，没有实现计划的恢复语义。

## Blocker 6（已部分缓解）：four-channel contract 只对 browse 生效，非 browse 工具仍返回普通 dict

虽然 `chat_graph.py:114-123` 声明了 typed `ToolExecutionResult`，但 `_run_tool` 的所有非 browse 分支仍直接返回 dict（`webapp.py:2382` 之后大量路径）。fix round 已在 `node_execute` 的 legacy dict 分支加入 provider/log allowlist 和路径/FASTQ 脱敏，新的 sentinel 测试证明 exact sample/path 不再进入 provider messages 或 generic `tool_log`；因此原先的直接泄露 blocker 已缓解。它仍不是 Task 6 Step 9 要求的完整四通道返回类型，后续应把这些 internal dict 包装为 `ToolExecutionResult(local, model, log_projection, None)`，但可作为残余兼容债务跟踪。

## Blocker 7：严格 explicit project binding 没有覆盖所有 browse 入口

`/api/samples/scan-remote` 增加了 unknown explicit project 的 404 检查，但它在 `webapp.py:1800-1802` 直接返回，完全没有进入 `_execute_browse_attempt`，所以该拒绝没有按 Step 5 产生 authoritative denied audit。与此同时，`/api/chat` 和 `/api/chat/stream` 仍经由 `_legacy_dir_for`/`_bound_project_id`；未知的显式 project 会退回 legacy project。这样 rule-chat browse 可能在请求了不存在项目时读取 legacy 项目，违背 Task 6 的 strict browse project resolver 和“unknown explicit id never falls back”约束。

## 非 blocker 观察

- `consume_remote_scan` 留作 Task 7 seam 是计划允许的；但 Task 7 必须基于严格修复后的 record/alias 协议实现。
- Task 2 已知的 Windows ancestor reparse race 仍是后续 hardening 限制，不改变本次 Task 6 的 blocker 结论。
- `_finalize_tool_execution_result` 是目前唯一的 `_record_browse_audit` 调用点，这个方向正确；需要先修复前面的绕过路径和 sink failure semantics。

## 复审前必须补的测试

1. disabled 的 LLM tool 和 rule-chat browse：断言不触碰 policy/credential/transport/inner executor，但恰好产生一个 denied audit，且没有 confirmation card。
2. 四入口 convergence spy：workbench、project command、rule chat、LLM 均只进入 `_execute_browse_attempt`，且显式未知 project 不回退 legacy。
3. alias tamper：`source_ref A -> scan B`、缺字段、未知字段、错误类型、record/source_ref 不一致都必须 fail closed。
4. scan-store 注入短写、prefix write、temp fsync、replace、目录 flush、进程退出；验证没有 partial record、孤立 alias 或错误暴露。
5. audit 注入 duplicate/conflict、malformed record、filename digest mismatch、replace 前后失败、目录 fsync 失败，验证 `committed/absent/uncertain` 恢复矩阵。
6. typed-result contract：所有工具都返回四通道结构；provider 只收到 `model`，generic log 只收到 `log_projection`，exact local 不进入 state/checkpointer。
7. 静态门禁：`rg` 不应再找到独立的 `find`/`detect_fastq_pairs` 远程扫描实现，`_scan_remote_samples` 只能是 thin adapter。

修复以上 blocker 后，再运行计划中的 Task 6 全套测试和独立 scoped re-review；在此之前不建议进入 Task 8 或把 LLM disclosure 当作已具备安全前提。

## Fix round evidence

本轮新增/修复了：disabled LLM browse 进入统一 wrapper 并保留一次 `security_audit`、rule-chat disabled 分支不再提前短路、旧 `_scan_remote_samples` 改为统一 wrapper 的 compatibility shim、scan alias/source_ref 重新绑定检查、严格 scan record schema、scan short-write loop、audit fixed-schema parser/reconciliation、legacy dict provider/log projection，以及 chat/chat-stream 的显式未知项目 404。

Fix round focused evidence：`8 passed`（新增 alias/schema/disabled/sentinel tests），并通过 `compileall` 与 `git diff --check`。Task7 的 `consume_remote_scan`/apply 未纳入本轮提交。

## Scoped re-review: `34dfe38`（2026-09-21）

审查对象是提交 `34dfe38 fix: close remote browse audit and disclosure boundaries` 的提交快照；当前工作区中的 `src/rnaseq_agent/remote_scan_store.py`、`tests/test_remote_scan_store.py` 和 `src/rnaseq_agent/webapp.py` 未提交部分属于 Task 7/后续修复，不计入本次结论。

结论：**BLOCKER，不能通过 Task 6 scoped review**。

逐项结论如下：

1. **Disabled rule/LLM browse：PASS。** `_run_tool` 在通用 `tool_mode_block` 之前进入 `_execute_browse_attempt`，并传入实时模式；rule-chat 的 `_rule_intent_mode_block` 对 `browse_samples` 让出给 `_browse_samples_reply`。disabled 分支只构造 `TOOL_MODE_DISABLED` 的审计结果，不读取 policy、credential 或 transport；生产路径随后由 `_finalize_tool_execution_result` 写入一次 authoritative audit。现有新增测试也确认返回 `ToolExecutionResult` 和审计对象。
2. **旧 scanner：PASS。** `webapp.py` 中的 `_scan_remote_samples` 已退化为调用共享 wrapper 的兼容 shim；workbench、project command、rule chat 和 LLM executor 的生产路径均直接进入 `_execute_browse_attempt`，不再保留第二套 `find`/`detect_fastq_pairs` 扫描实现。
3. **Scan alias/record schema：BLOCKER。** `34dfe38` 没有修改 `remote_scan_store.py`。提交快照中的 `_record_from_json` 仍以 `str()`/`bool()` 强制转换、允许缺省字段并过滤 malformed sample；`load_remote_scan(source_ref=...)` 只读取 alias 的 `scan_id`，没有校验 alias 的字段集合、`source_ref` 与查询值相等，也没有重新验证 record 的 `source_ref` 绑定。因此 `src_A -> scan_B` 的篡改仍可返回 B 的 exact scan。当前工作区虽有未提交的严格 schema/alias 修复，但不能作为本提交证据。
4. **Scan short-write/publication recovery：BLOCKER。** 提交快照的 `_write_atomic` 对 record/alias 只调用一次 `handle.write(raw)`，不能处理 prefix/short write；alias 发布失败后的回滚仍直接 `unlink` 两个路径，也没有 verified cleanup/reconciliation 矩阵。相关短写和发布故障修复仍在未提交的 scan-store diff 中。
5. **Audit parser/reconcile：PARTIAL，按发布门禁仍为 BLOCKER。** 固定字段集合、重复 JSON key、事件 id、文件名 digest、类型/数量边界、canonical bytes 和 post-publish file/directory fsync reconciliation 已实现，故原先的“裸 `json.loads`/无 reconcile”缺口已关闭。可是计划要求的 malformed published record 的 bounded quarantine/recovery 尚未实现；`reconcile_browse_audit` 将这类记录压成 `uncertain`，没有显式 quarantine/repair 状态。此外 `_finalize_tool_execution_result` 把 `_record_browse_audit` 与 `append_history` 放在同一个 `try` 中：audit 已成功而 History 写入失败时仍返回 `REMOTE_SECURITY_AUDIT_FAILED` 并可能 discard scan，违反“History 失败不重试/不回滚已提交 audit”的契约。相关故障注入测试也未在本提交中加入。
6. **Non-browse four-channel contract：PARTIAL。** `_legacy_model_projection` 已阻断样本名、FASTQ 路径、报告路径等进入 provider message 和 generic `tool_log`，新增 sentinel 测试覆盖了这一点。可是 `_run_tool` 的非 browse 分支仍返回普通 dict，尚未统一为 `ToolExecutionResult(local, model, log_projection, security_audit)`；这仍是 Task 6 Step 9 的残余契约缺口。作为泄露修复它是有效缓解，作为完整四通道门禁不能标 PASS。
7. **Explicit project binding：BLOCKER。** `/api/chat`、`/api/samples/scan-remote` 和 project command 已对显式未知项目返回 `PROJECT_NOT_FOUND`，但 `34dfe38` 的 `/api/chat/stream` 没有同样的早期检查。它仍先调用 `_legacy_dir_for`；`_bound_project_id` 对未知 id 返回 `None`，随后回退到 legacy project。带有未知 `project_id` 的流式 rule/LLM browse 因此可能在 legacy 项目上继续执行，违反“unknown explicit id never falls back”。当前工作区未提交 diff 才补上了 stream 检查。

本次复审的阻断项是 3、4、5、7；6 是必须继续收敛的契约债务。应先提交严格 scan-store 协议、stream project resolver、audit/History failure separation 和相应故障注入测试，再重新运行完整 Task 6 入口/审计门禁；不能把当前工作区中的未提交修复或 `150 passed` 的排除旧测试结果当作 `34dfe38` 的 GREEN 证据。
