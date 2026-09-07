# bulk RNA-seq Agent 现状-缺口-修改位置-验收证据（M0 交付物）

日期：2026-09-07。范围：说明书 docs/bulk_rnaseq_ai_build_instructions.md + UI 设计 docs/superpowers/specs/2026-09-07-bulk-rna-workbench-ui-design.md。

基线测试：148 passed / 15 skipped / 1 failed（唯一失败 = test_dataset_downloader.py 的 Windows 8.3 短路径大小写，环境性）。

## 已核实的六个工程缺口

| # | 缺口 | 现状位置 | 违反条款 | 修改位置 | 验收证据 |
|---|------|---------|---------|---------|---------|
| 1 | QC 检查点在 execute 前且是空决定 | agent_graph.py node_wait_qc / node_confirm_contract | §5.1 / §10.2 | agent_graph 增加 QC 产物核验；会话绑定 QC artifact | resume 前无 QC 文件则拒绝；有才放行 |
| 2 | execute 不等待真实终态 | agent_graph.py:155 execute(wait=False) + execute→validate_output 直连 | §3.2 / Stage"排队不误验收" | execute 改真实 poll；webapp 后台任务状态 API | running 时不触发 validate；completed 才进入 |
| 3 | 图 compile 无持久化 checkpointer、web 不驱动图 | build_bulk_rna_graph() compile()；webapp 直调 session | §6 持久化恢复 | SqliteSaver/磁盘 checkpointer，thread_id=project_id；webapp 增加图 invoke 入口 | 等待/排队/运行/完成 四状态重启恢复 |
| 4 | 输出校验/post-run 空字典默认通过 | agent_graph.py:167-179 | §7 | 两节点读取 attempt 真实 manifest | 缺/损文件下游不可消费 |
| 5 | DE 参考组默认选取未要求确认 | differential.py _reference_condition | §5 Stage3 | reference 必填冻结；比较方向确认 | 未显式对比方向被拒 |
| 6 | fgsea/MultiQC 链未实现 | pipeline.py / differential.py 无 fgsea | §5 Stage2/3 | 审计结论：本次阻断并说明缺口，不做伪实现 | 无 MSigDB 时富集阻断、DE 保留 |

## UI 结构性缺口

| # | 缺口 | 现状 | 要求 | 修改位置 | 验收 |
|---|------|------|------|---------|------|
| A | 单项目固定目录 | webapp create_app(project_dir 固定) | 项目首页+多项目 | 新增 workspace 存储 | 多项目并存切换 |
| B | 无对话层 | 无 Thread/Message | Project→Thread→Message | 新增 threads 存储 | 3 对话、刷新保留、隔离 |
| C | 单页滚动布局 | 一个 index.html | 三栏工作台+设置页 | 模板重写 | UI 验收点 |
| D | 无 LangGraph 恢复 | session.json 只存 plan | 稳定 thread_id + checkpointer | agent_graph | 四状态重启 |

## 范围决策（用户已确认）
- fgsea/MultiQC：仓库无 MSigDB 资产 → 本次阻断并说明缺口，不做伪实现。
- 推进顺序：M1（QC 闭环 + 多对话 + 三栏可用结构）先行，M2–M5 后续逐项推进。

## M1 拆分
1. 多项目/多对话/消息存储层（Project→Thread→Message + Spec→Run→Artifact）
2. webapp 三页路由（项目首页/工作台/设置）
3. LangGraph 磁盘 checkpointer + QC 检查点真实化
4. execute 真实等待终态（wait=True/后台轮询）
5. 输出校验/post-run 接入 attempt 真实 manifest
6. 三栏工作台 UI 模板
7. DEG reference 显式确认
8. 测试：多对话隔离/幂等/安全回归

## M2-M5 后续
- M2 完整 bulk 上游阶段化（STAR/定量/融合/QC Viewer）
- M3 队列统计（fgsea 需 MSigDB 资产才做）
- M4 修改与恢复（ChangeSet 依赖传播全链）
- M5 产品交付（统一网页/对话表单同路/报告安装）
