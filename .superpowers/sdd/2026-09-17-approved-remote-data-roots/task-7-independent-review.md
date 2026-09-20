# Task 7 implementation report

Commit: `3b0a4d5`

Task 7 now applies an approved remote scan group through a server-verified,
workbench-only endpoint. The browser submits only `scan_id`,
`result_revision`, `group_id`, and the explicit truncated-result acknowledgment;
the canonical directory and FASTQ rows are read from the private scan record.

The scan store validates its complete fixed schema and alias binding, checks the
current identity, policy revision, active root, and target containment, then
durably claims a pending record before invoking the idempotent project writer.
Retries reuse the same claim id after a callback or final record replace
failure. Project state stores basenames only and a bounded receipt ledger with
opaque identifiers. Replays classify as `REMOTE_SCAN_REFERENCE_USED` and
expired records as `REMOTE_SCAN_REFERENCE_EXPIRED`.

The project endpoint creates the normal remote-prestaged draft/session and
intake projection. The workbench renders separate directory groups, requires an
explicit acknowledgment for truncated inventories, and clears the reference
after successful application. Audit publication is authoritative; a failure to
append the de-identified History projection does not discard a published scan.

Validation run on Windows:

```text
python -m compileall -q src
pytest tests/test_remote_scan_store.py tests/test_webapp_project_wizard.py::test_remote_scan_apply_persists_filename_only_group_and_rejects_replay tests/test_webapp_project_wizard.py::test_fastq_session_from_detected_samples_enters_input_ready -q
8 passed
git diff --check
```

The broader legacy browse tests that still expect unapproved directories to be
scannable remain compatibility cases for Task 8; they are intentionally not
re-enabled by this commit.

## Independent scoped re-review: final Task 7 commit set（2026-09-21）

审查对象为 `3b0a4d5` 及其后续修复 `61643ea`、`a39e9e0`、`67d2dde`、`80c83b8`、`0afb935` 的联合快照。结论：**BLOCKER，不能通过 Task 7 release gate**。

验证证据：

- `pytest tests/test_remote_browse.py tests/test_remote_scan_store.py tests/test_webapp_project_wizard.py tests/test_webapp_workbench.py tests/test_validation.py tests/test_sample_detection.py -k "group or scan_store or reference or expiry or replay or context or claim or receipt or crash or tombstone or basename or duplicate or truncated or remote_prestaged or remote_scan_apply or fastq_session_from_detected" -q`：`17 passed, 69 deselected`。
- Task 7 专项命令（scan store、apply endpoint、remote-prestaged session）：`8 passed`。
- `compileall -q src` 和 `git diff --check` 通过。
- 更宽的 `tests/test_remote_scan_store.py tests/test_webapp_project_wizard.py tests/test_webapp_workbench.py`：`34 passed, 2 failed`；两项失败是 Task 6 旧测试，仍 monkeypatch 独立 scanner 或要求未批准 root 也能扫描，不属于 Task 7 行为证据。

已关闭的部分：

1. **Context/policy/root revalidation：基本 PASS。** endpoint 在 `locked_browse_policy()` 内构造固定的 `BrowseContext(project_id, None, "workbench")`；`consume_remote_scan` 在 remote-scan lock 下重验项目、thread、source、result revision、expiry、当前 identity digest、policy revision、active root 和 target containment。rule-chat/LLM/project-command/non-null-thread 记录不能用于 workbench apply。
2. **Lock order：PASS。** apply 路径保持 connection → remote-scan → project 的顺序；project callback 完成后才释放 scan/connection lock，History/intake projection 在锁外执行。竞争 consumer 不能生成第二个 claim。
3. **Strict record/alias schema：大部分 PASS。** record/group/sample 固定字段、重复 JSON key、类型、路径、时间戳、source/state/digest 格式以及 alias 的 `source_ref`/`scan_id` 绑定均已 fail closed；`discard_remote_scan` 后续也验证了完整 reference 绑定。
4. **Atomic project draft：PASS（正常路径）。** `61643ea` 改为临时文件 + flush/fsync + replace；project receipt 与 filename-only samples 在同一次 project-state lock 下落盘。成功 apply 的 `remote_data_dir` 来自 server group canonical directory，样本只保存 basename；receipt 只含 claim/scan/revision/group/timestamp 等 opaque 字段。`0afb935` 也让 apply History 失败不再把已消费结果伪装成失败。
5. **Grouped UI/truncation：PASS（正常路径）。** 多目录保持独立 radio 选择，单目录可自动选择；截断结果显示持久警告并要求 checkbox；成功后清空 reference、groups 和 truncation 状态。浏览器只提交 `scan_id`、`result_revision`、`group_id`、`accept_truncated`。

仍然阻断的部分：

1. **Result revision 没有完整完整性校验：BLOCKER。** `_record_from_json` 只检查 `result_revision` 是 `sha256:<64 hex>`，`load_remote_scan`/`consume_remote_scan` 没有按 record 内容重新计算 `_revision` 并比较。`_revision` 本身也没有覆盖 `created_at`/`expires_at` 等所有不可变安全字段。因而一个具备 private store 写权限的损坏/篡改 record 可以保持格式合法并延长 expiry 或改变 group 后继续 apply。`67d2dde` 只补了 audit identity/policy digest 的格式检查，未关闭此问题。
2. **Group 与 approved target 的绑定不完整：BLOCKER。** `_root_for_scan` 只证明 `scan.canonical_target` 在 active root 内；`_validate_group_for_apply` 只检查 group 目录是绝对路径，没有检查每个 `group.canonical_directory` 位于 `scan.canonical_target`/approved root，也没有拒绝重复 `group_id`。一个 schema 合法但 group 目录越界的 record 仍可把越界目录写入 `remote_data_dir`。
3. **Receipt pruning 会丢失未完成 claim：BLOCKER。** `_apply_remote_scan_group` 无条件使用 `receipts[-32:]`。计划要求 unresolved `applying` claims 永远保留 receipt；当一个崩溃留下 `applying` claim，后续超过 32 次成功 apply 会把它剪掉，重试时 callback 找不到 receipt，可能重新写样本而不能证明 exactly-once recovery。
4. **Truncation acknowledgment 类型未严格校验：BLOCKER。** endpoint 使用 `bool(payload.get("accept_truncated", False))`；JSON 字符串 `"false"` 会变成 Python `True`，从而绕过显式确认。必须要求字段缺省/布尔类型，并让非 boolean 值 fail closed。
5. **Apply projection 仍有不可恢复的中间失败：PARTIAL。** scan claim 已 consumed、project draft 已落盘后，`save_intake()` 若失败会直接返回错误；重试只能得到 `REMOTE_SCAN_REFERENCE_USED`，无法自动补齐 intake。计划主要把 project receipt 作为权威状态，因此这不是当前最核心的 secret leak，但需要明确把 intake 视为可重建 projection 或提供 repair 路径。apply History 目前只捕获 `OSError`，注入其他异常类型时仍可能把已消费 apply 变成 500。
6. **Project temp writer 尚未复用 private-file helper：FOLLOW-UP。** `61643ea` 的 `tempfile.mkstemp`/`os.replace` 已具备基本原子性，但没有像 scan store 一样通过 `create_private_temp`、`verify_private_path` 和 Windows replace retry；在受保护工作区之外的路径替换/ACL 故障矩阵还没有同等证据。

因此，44（单目录 basename/canonical dir）、46（多目录分组 UI）以及正常路径下的 45/48 已有实现和 focused tests 支撑；但 45 的 crash-safe exactly-once、47 的 malformed/tampered group fail-closed、48 的严格 acknowledgment 类型仍不能整体判绿。应先补 revision recomputation/字段覆盖、group-target containment、receipt retention 和 boolean validation，再运行 claim/replace/crash/replay 注入测试后重新复审。

## Follow-up closure（2026-09-21）

上述 blocker 已在后续工作区修复：`_stored_revision()` 重新计算所有不可变绑定字段（包括 scan/source ids、identity、policy、target、groups、truncation、created/expiry）；记录中的重复 group id、篡改 expiry/group、越过 scan target 的目录会 fail closed；apply endpoint 要求 `accept_truncated` 是 JSON 布尔值；project callback 记录并精确清理当前 claim，保留 unresolved claim receipt，使用原子 project writer。

新增 tamper、重复 group、string acknowledgment 和 apply/replay 测试；scoped follow-up 为 `11 passed`。旧版本（`3b0a4d5` 之前）尚未消费的 record 使用不同 revision 公式，当前按完整性不确定直接返回 `REMOTE_SCAN_REFERENCE_INVALID`，不会迁移或扩大权限；其 TTL 清理仍可将过期记录转为 bounded tombstone。这是显式 fail-closed 迁移策略。
