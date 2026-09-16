# LLM 工具循环 — 交接说明

> 日期：2026-09-16
> 分支：`wizard`（领先 `master` 15 个提交）
> 相关提交：`d63fc50`（本轮）、`c74eac2`（上一轮）
> 交接原因：本轮把「LLM 真正拥有执行工具」做完了主体，剩下 3 个已复现缺陷待修。

---

## 0. 一句话现状

模型现在有 **17 个真实工具**，可以读配置、写配置、启动分析。写盘/执行被
代码强制走人工确认，确认粒度分三级（只读放行 / 配置合并 / 执行单独）。
主体可用、测试全绿；**有 3 个已复现的缺陷需要修**，其中 2 个是安全/协议性的。

---

## 1. 交付状态

| 项 | 状态 |
|---|---|
| 工具数 | 10 → **17** |
| 确认策略 | 布尔 `requires_confirmation` → **三级 `never`/`batch`/`solo`** |
| 全套件 | **528 passed, 1 skipped**（连续两次稳定） |
| 端点级新测试 | `test_webapp_chat_tools.py` 27 个用例 |
| 图层新测试 | `test_chat_graph.py` +8 个用例（`TestConfirmationPolicy`） |

### 改动文件

```
src/rnaseq_agent/agent_tools.py      1351 行  ← 工具契约层（纯声明 + 纯函数）
src/rnaseq_agent/chat_graph.py        630 行  ← 对话控制平面图
src/rnaseq_agent/webapp.py           3507 行  ← 工具到确定性实现的桥
tests/test_chat_graph.py              584 行
tests/test_webapp_chat_tools.py       820 行
```

**建议阅读顺序**：`agent_tools.py` 的工具表与校验 → `chat_graph.py` 的
`node_guardrail` → `webapp.py` 的 `_run_tool`。

---

## 2. 系统怎么工作的

### 2.1 三层责任划分

```
agent_tools.py   纯声明 + 纯函数：工具 schema、风险分级、确认策略、
                 参数校验、卡片文案。不碰磁盘、不碰网络。
                          ↓
chat_graph.py    控制平面：agent → guardrail → execute 三节点循环。
                 唯一允许产生副作用的位置是 execute。
                          ↓
webapp.py        执行器：_run_tool 把每个工具映射到既有的确定性实现
                 （session.edit / session.plan / session.execute …）。
```

**核心原则**：思考交给 LLM，安全交给代码。schema 里的范围约束、风险分级、
确认粒度全部由代码强制；提示词只负责告诉模型有哪些工具、以及分工。

### 2.2 图结构

```
START → agent ──┬─(无工具调用)────────────→ END
                └─(deferred_calls 非空)→ guardrail
                                          │
        guardrail ──┬─(pending_calls 非空)→ execute
                    ├─(deferred_calls 还有)→ guardrail   ← 顺延下一组
                    └─(都没有)───────────→ agent
                                                          │
        execute ────┬─(deferred_calls 还有)→ guardrail
                    └─(没有)─────────────→ agent
```

关键点：

- `node_agent` 把模型这一轮请求的**全部**调用塞进 `deferred_calls`，**不分组**。
  分组只在 `guardrail` 里做，避免两处逻辑漂移。
- `node_guardrail` 每轮只处理**一组**：`batch` 合并成一张卡片，`solo` 逐个成卡片。
  参数校验在弹卡片**之前**——不合法的调用没有让人签字的必要。
- `execute` 之后回 `guardrail`（不是回 `agent`），所以顺延的组会被逐个确认。
- 用户拒绝时**清空整个 `deferred_calls`**，不再弹下一张卡——不逼他对同一批
  动作重复表态。

### 2.3 三级确认策略

唯一真源是 `agent_tools.confirmation_policy(name)`，`requires_confirmation()`
委托给它（避免两套判断漂移）。`ToolSpec.policy` 可覆盖按风险推导的默认值。

| 策略 | 语义 | 工具 |
|---|---|---|
| `never` | 直接放行 | `read_project_state` `browse_remote_samples` `refresh_project_status` `get_project_report` |
| `batch` | 配置类合并成一张卡 | `write_project_config` `configure_pipeline` `set_run_resources` `set_diffexp_reference` `rollback_changes` `edit_samples` `edit_reference` `set_cms_options` |
| `solo` | 各自独占一张卡 | `generate_plan` `confirm_contract` `run_analysis` `record_qc_decision` + **`edit_connection`**（连接配置钉死 solo） |

用户 2026-09-16 明确的两条边界，不要改动：
- 「配置合并、执行单独」
- 连接配置「含，但每次必确认」

### 2.4 确认卡片

`describe_call(name, arguments, current)` 渲染卡片文案。写盘类走
`field_changes()` 产出 `[{label, old, new}]`，渲染成「旧值 → 新值」。
`current` 由 `build_chat_graph(config_reader=...)` 注入，webapp 传
`_read_project_config`。

---

## 3. 必须遵守的设计约束

改代码前请先确认这几条不破：

1. **`node_guardrail` 必须无副作用。** LangGraph 在 `Command(resume=...)` 时
   会**从头重跑当前节点**，任何写盘都会在用户每确认一次时多写一次。
2. **跨 resume 的判定必须进 state。** 局部变量在重跑时丢失。`rejected` 就是
   为此加的字段。
3. **`execute` 是唯一允许产生副作用的节点。**
4. **`_run_tool` 是对话图与真实系统之间唯一的桥。** 每个工具必须映射到既有的
   确定性实现，不允许模型自己拼路径、拼命令、绕过门禁。
5. **`risk_of(name) != RISK_READ and not approved` 时 `_run_tool` 一律拒绝。**
   这是纵深防御：即使图的守卫被绕过，这里仍然拦得住。
6. **确认策略的唯一真源是 `confirmation_policy()`。** 不要在别处再写一遍
   风险到粒度的映射。
7. **`interrupt()` 的恢复必须有 checkpointer。**

---

## 4. 待修缺陷（已复现，按优先级）

### P0-1 拒绝分支留下悬空 `tool_call`（协议违规）

**位置**：`src/rnaseq_agent/chat_graph.py:522-544`（`node_guardrail` 的拒绝分支）

**现象**：一轮里模型请求了多个组（例如 `batch: set_run_resources` +
`solo: generate_plan`）。用户拒绝了第一组（batch），代码只为 `valid` 里的
调用追加了 tool 消息，`rest`（顺延队列里的 `generate_plan`）被丢弃且**没有
tool 回复**。但 assistant 消息里已经声明了这个 `tool_call_id`。

**复现**：

```python
# 模型一轮返回两个调用，拒绝第一组
turns = [{"tool_calls": [
    {"id": "call_a", "name": "set_run_resources", "arguments": '{"threads":16}'},
    {"id": "call_b", "name": "generate_plan", "arguments": "{}"},
]}, {"content": "好"}]
# 第一轮挂起后 Command(resume={"approved": False}) 恢复
```

实测消息序列：

```
user       tool_call_id=None       declares=[]
assistant  tool_call_id=None       declares=['call_a', 'call_b']
tool       tool_call_id=call_a     content={"ok": false, "error": "用户拒绝了这个操作..."}
assistant  tool_call_id=None       declares=[]
```

```
assistant 声明过: ['call_a', 'call_b']
有 tool 回复的 : ['call_a']
>>> 悬空（协议违规）: ['call_b']
```

**影响**：OpenAI 的 tool-calling 协议要求 assistant 声明的每个 `tool_call_id`
都必须有对应的 tool 消息。协议违规是事实；**是否触发 400 取决于端点实现**——
严格校验的端点（部分 Azure / 自建网关 / 未来换的模型供应商）会直接拒绝整个
请求。当前用的端点宽容，所以没暴露。

**修复方向**：拒绝时把 `rest` 里的每个调用也补上 tool 消息，说明「因为上一个
操作被拒绝，这个动作也一并放弃」。不要为了修这个而取消「拒绝即清空队列」的
行为——那个是刻意的产品决策。

**修完请补测试**：断言 `declared == answered`，即 assistant 声明的每个
`tool_call_id` 都有对应 tool 消息。这个断言值得加成不变量，覆盖 approve /
reject / 参数校验失败三条路径。

---

### P0-2 `approved` 判定 fail-open

**位置**：`src/rnaseq_agent/chat_graph.py:513`

```python
approved = bool(decision) and decision.get("approved") is not False
```

这行的语义是「**除非明确说 False，否则算批准**」。实测：

| resume 值 | 实际行为 |
|---|---|
| `{"approved": True}` | 执行 ✅ |
| `{"approved": False, "note": "不要"}` | 不执行 ✅ |
| `{}` | 不执行 ✅（被 `bool(decision)` 挡住） |
| `{"note": "我只是随手写了句备注"}` | **执行** ❌ |
| `{"approved": "yes"}` | **执行** ❌ |
| `{"approved": 0}` | **执行** ❌ |

**影响**：`webapp` 的 `/api/chat/resume` 有 `isinstance(approved, bool)` 前置
校验（`webapp.py:2576`），所以**当前唯一的生产入口是安全的**。但
`build_chat_graph` 是公开 API，其它调用方（CLI、测试工具、未来新增入口）没有
这层保护。安全默认应该是 fail-closed：只有明确 `True` 才算批准。

**修复方向**：

```python
approved = isinstance(decision, dict) and decision.get("approved") is True
```

与 webapp 的校验保持一致。注意 `is True` 而非 `== True`——后者会让 `1` 通过。

**修完请补测试**：把这 6 种输入写成参数化测试，锁住 fail-closed 语义。

---

### P1-1 确认卡片的旧值不等于真实生效值

**位置**：`src/rnaseq_agent/webapp.py:1642`（`_read_project_config`）
对比 `src/rnaseq_agent/webapp.py:109`（`_session_for`）

**现象**：连接配置（host / user / port / scheduler / threads / memory_gb /
remote_base_dir / remote_workdir / shell / auth_mode）是**用户级共享**的。
`_session_for` 读完 `project.json` 后会调 `apply_connection_to_config()`
用全局连接覆盖 `server` 块；但 `_read_project_config` 直接读 `project.json`，
**没有做同样的合并**。

实测：

```
project.json 里 host   = old.example
全局连接里 host        = global.example

>>> 卡片显示的旧值 host = old.example   ← 直接读 project.json
>>> 真实生效的 host     = global.example ← 全局连接覆盖后
>>> 真实生效的 threads  = 64
```

**影响**：用户在为一个具体动作签字，而卡片上的旧值是错的。他说「主机从
old.example 换到 new.example」，实际是从 `global.example` 换到 `new.example`。
这正是用户本轮特别要求「连接配置必须明写旧值 → 新值」要解决的场景。

**为什么测试没发现**：`test_webapp_chat_tools.py` 的 `_seed_project()` 只写
项目配置，**没有调 `save_connection()`**，所以全局连接为空，
`apply_connection_to_config` 不覆盖任何字段。测试掩盖了这个差异。

**修复方向**：`_read_project_config` 复用 `_session_for` 的合并逻辑——读
`project.json` 后过一遍 `apply_connection_to_config(payload, load_connection())`。
**注意保持只读**：不能用 `_session_for`（它会构造 `ProjectSession` 并可能
触发 `load_session`），要单独写一个只读的合并函数。这个函数跑在 `guardrail`
里，无副作用是硬约束。

**修完请补测试**：`_seed_project()` 之后调 `save_connection({"host": "..."})`，
断言卡片上的旧值是全局值。同时保留现有「全局为空时显示项目值」的用例。

---

## 5. 测试覆盖缺口

以下 5 个工具在 **webapp 端点层面零覆盖**（`_run_tool` 分支没有被任何
`/api/chat/stream` 测试走到）：

| 工具 | webapp 分支 | 是否有测试 |
|---|---|---|
| `browse_remote_samples` | `webapp.py:1745` | ❌ 仅图层出现名字 |
| `rollback_changes` | `webapp.py:1921` | ❌ 仅图层出现名字 |
| `set_diffexp_reference` | `webapp.py:1853` | ❌ 完全没有 |
| `set_cms_options` | `webapp.py:1900` | ❌ 完全没有 |
| `record_qc_decision` | `webapp.py:1903` | ❌ 仅 `test_session.py` 层 |

这 5 个里 `set_cms_options` 风险最高：它有分支逻辑（`CMS_RUN_MODES` 校验、
`cms_design_checks` 门禁提示），而且历史上 `set_cms_options` 和
`set_diffexp_reference` 的辅助函数就是因为 `ChatIntent` 导入缺失而静默失败的
（见下节教训）。**建议优先补这两个。**

---

## 6. 其它观察（不一定要改）

### 6.1 `iterations` 只统计模型往返次数

`MAX_TOOL_ITERATIONS = 8` 只在 `node_agent` 里递增。一轮里如果有 1 个 batch +
3 个 solo，会走 5 次 `guardrail`/`execute` 但不增加 `iterations`。这是设计意图
（限流的是模型往返，不是用户签字次数），但名字容易误读，值得在 docstring 里
点明。

### 6.2 兜底消息必须带异常类型

本轮修的两个真实 bug 都是靠这个才定位到的：

- `ChatIntent` 只在 `_run_tool` 内局部导入，三个兄弟辅助函数
  （`_edit_samples_tool` / `_edit_connection_tool` / `_set_cms_options_tool`）
  运行时全部 `NameError`，被吞成「工具执行失败」。**如果兜底消息只写
  「工具执行失败」，这个 bug 会一直静默。**
- `_read_project_config` 从 `.io` 导 `load_json`（`.io` 是空壳），异常被
  `except` 兜住后卡片上所有旧值都显示「（未设置）」，看起来像「项目没配置」。

`chat_graph.py:577` 现在的写法是好的，请保持：

```python
result = {"ok": False, "error": f"工具执行失败（{type(exc).__name__}）：{exc}"}
```

### 6.3 全套件偶发失败是环境问题

Windows 上并发跑 `tests/` 偶发 `pathlib._local.py:808 RuntimeError`
（临时目录竞态），重跑即绿、失败文件单独跑也全绿。不是本轮改动引入的，
基线也存在。**看到 15 个看似无关的失败先重跑一次再判断。**

---

## 7. 挂起事项（非本轮范围）

- **`wizard` → `master` 合并**：`wizard` 领先 15 个提交，一直没合。
- 任务 #138（对话落地配置能力）、#139（样本分组按顺序自动展开）、
  #140（链特异性「未知」选项）在任务表里仍是 pending，但 #139/#140 的
  实现其实已经在本轮之后落地（见 `SYSTEM_PROMPT` 第 4、5 条），**需要核实
  后关掉**。

---

## 8. 验证方式

### 环境

```bash
# 解释器
.venv/Scripts/python.exe

# 跑测试（必须带 PYTHONPATH）
PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/ -q
```

### 当前基线

```
tests/test_webapp_chat_tools.py    27 passed
tests/test_chat_graph.py           40 passed
tests/                             528 passed, 1 skipped
```

### 手动验证分组

```python
PYTHONPATH=src .venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'src')
from rnaseq_agent.agent_tools import TOOL_SPECS, confirmation_policy
for name, spec in TOOL_SPECS.items():
    print(f'{name:28} risk={spec.risk:8} policy={confirmation_policy(name):6} {spec.label}')
"
```

### 手动验证参数校验

模型传这些参数必须被拒（不弹卡片，错误回灌给模型自纠）：
`threads=-5` / `threads=999999` / `threads="abc"` / `threads=True` /
`memory_gb=-1` / `port=99999` / `fdr=5` / `stage='nope'`。

---

## 9. 验收标准

改完后：

1. `PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/ -q` 全绿
   （允许那 1 个 skip）。
2. 三个 P0/P1 缺陷都有对应的回归测试，且测试在修复前是红的。
3. 「assistant 声明的每个 `tool_call_id` 都有 tool 回复」被加成不变量，
   覆盖 approve / reject / 参数校验失败三条路径。
4. `approved` 的 fail-closed 语义有参数化测试锁住。
5. 第 5 节里 `set_cms_options` 与 `set_diffexp_reference` 的端点测试补齐。
