# LLM 工具权限硬化交付

日期：2026-09-21

本任务只修改了工具模式的缺省/显式 null 语义、ChatGraph 的严格批准判定，并保留既有 tool-call 协议补答、有效连接配置只读合并和五个端点覆盖。

## 变更

- `normalize_tool_mode()` 无参数调用表示旧安装缺失字段，继续返回 `approved_execute`。
- `normalize_tool_mode(None)` 表示显式 JSON `null`，返回 `disabled`；未知类型/未知字符串仍抛错，由读取边界转换为 `disabled`。
- ChatGraph 的 live mode 读取区分缺字段与显式 null；无用户级 LLM 块的 legacy fallback 保持原行为，损坏配置保持 fail closed。
- executor 只接受 `state["confirmed"] is True` 作为副作用批准。
- 未扩大 provider data scope，未启用 remote exact；`batch`/`solo`/`never` 策略保持不变。

## TDD / 验证证据

RED：新增测试 `test_tool_mode_normalization_distinguishes_missing_from_explicit_null` 在实现前失败，原因是 `normalize_tool_mode()` 不支持缺省参数（`TypeError`）。

GREEN：实现 sentinel 与显式 null 语义后，该测试通过；focused 套件通过：

```text
399 passed, 5 skipped, 1 warning
```

覆盖命令：

```text
.venv\\Scripts\\python.exe -m pytest tests/test_chat_graph.py tests/test_connection_store.py tests/test_webapp.py tests/test_webapp_chat_stream.py tests/test_webapp_chat_tools.py -q
```

编译检查通过：`python -m compileall -q src`。

完整主项目套件结果：`1132 passed, 8 skipped, 1 failed`。唯一失败来自同一共享工作区中 paired capability 代理的预存变更：`tests/test_bkbio_eval_adapter.py::test_run_case_rejects_designs_outside_the_real_condition_only_pipeline[~ patient + condition-True]` 仍期待旧错误文本 `~ condition`，而当前 paired 实现返回具名模板错误 `paired_two_group generates ~ pair_id + condition; arbitrary formulas are forbidden`；不涉及本任务文件。

`git diff --check` 的唯一现有告警来自 paired 代理在 `src/rnaseq_agent/differential.py:255` 添加的尾随空格；本任务文件无新增 diff-check 问题。

## 提交

待提交：本报告和本任务改动将由当前 agent 提交；共享工作区中 paired/evaluator 文件不纳入该提交。

## 未解决项

- 主项目全套件需待 paired 代理更新其旧断言或统一错误码后重新跑绿。
- R/DESeq2 airway numerical L1 仍受固定 R/Bioc runtime 缺失阻塞；本任务未改变该状态。
