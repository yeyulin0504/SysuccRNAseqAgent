# RNA-seq Agent 模型接入说明

## 推荐：使用独立的 Codex CLI / ChatGPT 登录

已有 ChatGPT Plus/Codex 的用户可以不填写 OpenAI Platform API Key：

1. 启动 GUI，打开“模型”标签页。
2. “模型后端”选择 `codex_cli`。
3. 点击“登录 ChatGPT”，在新窗口中按提示完成官方设备授权。
4. 回到 GUI 点击“检查登录”。
5. 模型名可留空，让 Codex 自动选择默认模型；然后点击“应用设置”和“测试连接”。

该登录保存在用户目录下的 `.sysu_rnaseq_agent/codex_home`，不会覆盖现有
Codex 或 CC Switch 配置。Agent 只让 Codex 返回结构化白名单动作，不允许
它访问项目文件、调用工具或执行命令。

这种方式与 `https://api.openai.com/v1` 的 API Key 计费通道不同。不要把
`https://chatgpt.com/codex` 填进 Base URL，也不要复制 `.codex/auth.json`
中的令牌。每台实验室电脑由对应使用者自行完成一次登录。

## 当前设计

项目现在支持两种对话模式：

1. **本地规则模式**
   - 不需要模型或 API Key。
   - 支持新建/打开项目、校验、运行、状态、报告和结果说明等固定动作。

2. **LLM 增强模式**
   - 支持 OpenAI-compatible `chat/completions` 接口。
   - 模型负责理解自然语言和选择白名单动作。
   - 后端仍由确定性 Python 代码执行。

模型不会直接生成或执行任意 shell 命令。模型触发 `run` 或
`run-nowait` 时，程序还会要求用户再次确认。

## 环境变量

在 PowerShell 中设置：

```powershell
$env:RNASEQ_AGENT_LLM_BASE_URL = "https://服务商地址/v1"
$env:RNASEQ_AGENT_LLM_MODEL = "模型名称"
$env:RNASEQ_AGENT_LLM_API_KEY = "API Key"
$env:PYTHONPATH = "src"
python -m rnaseq_agent chat
```

本地 OpenAI-compatible 服务如果不要求鉴权，可以不设置
`RNASEQ_AGENT_LLM_API_KEY`。

可选超时：

```powershell
$env:RNASEQ_AGENT_LLM_TIMEOUT = "45"
```

## 在对话中检查模型

启动 chat 后输入：

```text
模型状态
```

程序会显示当前模型名称、接口地址和安全执行策略。

## 桌面 GUI 设置

运行：

```powershell
$env:PYTHONPATH = "src"
python -m rnaseq_agent gui
```

在“模型”标签页填写 Base URL、模型名称、API Key 和超时秒数，然后：

- 先点击“拉取模型”，程序会访问 `<Base URL>/models`，并把当前密钥可用的模型放入下拉框；
- “接口类型”通常保持 `auto`；普通兼容服务默认使用 `chat_completions`，本机 CC Switch 自动使用 `responses`；
- 点击“应用设置”使当前程序使用该模型；
- 点击“测试连接”检查接口是否可用；
- 点击“禁用模型”退回本地规则模式。

Base URL、模型名称和超时可以保存为用户默认值。API Key 不会写入
`project.json` 或默认配置，只在当前程序进程中使用。

OpenAI 官方 API 的 Base URL 是：

```text
https://api.openai.com/v1
```

`https://chatgpt.com/`、`https://chatgpt.com/codex` 等是网页地址，不是 API
地址。模型名称必须使用接口返回的完整模型 ID，不能只填写版本数字。
ChatGPT 网页订阅与 OpenAI API 的密钥、权限和计费相互独立。

### 使用 CC Switch 的 Codex 登录通道

如果 Codex 的 `config.toml` 中配置了：

```toml
wire_api = "responses"
base_url = "http://127.0.0.1:15721/v1"
```

桌面 Agent 可以填写：

```text
Base URL: http://127.0.0.1:15721/v1
接口类型: auto
模型名称: Codex 当前显示的模型名称
```

但需要注意：CC Switch 的“OpenAI Official / Codex OAuth”本地路由是为 Codex
客户端接管设计的。Codex 会从自己的安全登录缓存附加 OAuth 凭证，普通桌面应用
不能通过空 Key 或普通 API Key 复用这份登录状态，也不应从 `auth.json` 复制令牌。

因此，本项目虽然支持 Responses API 协议，但不能把个人 ChatGPT/Codex OAuth
当作通用 API 使用。若 CC Switch 中配置的是具有独立 API Key 的第三方供应商，
应优先填写该供应商的真实 Base URL 与 Key；也可以改用独立 OpenAI Platform API
额度或本地模型服务。

部分 CC Switch 版本的 `/models` 只返回空列表，此时模型名称需要手动填写。

## 模型可以选择的动作

- `new`
- `open`
- `validate`
- `run`
- `run-nowait`
- `status`
- `report`
- `explain`
- `summary`
- `where`
- `help`
- `quit`
- `chat`

不在该列表内的动作会退回普通问答，不会交给系统执行。

## 安全边界

- API Key 只通过环境变量读取，不写入 `project.json`。
- LLM 不直接访问 SSH、SCP 或调度器命令。
- 核心参数仍由配置校验和确定性执行引擎控制。
- 模型对结果的说明仅用于辅助理解，不替代生物学或临床结论。
