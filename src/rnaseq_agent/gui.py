from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, StringVar, Text, Tk, filedialog, messagebox, simpledialog
from tkinter import ttk
from typing import Any

from .configuration import normalize_config
from .actions import (
    explain_project,
    generate_project_report,
    status_summary,
    summarize_project,
    validation_summary,
)
from .defaults import DEFAULT_PIPELINE, DEFAULT_REFERENCE
from .estimate import estimate_runtime
from .llm import (
    CodexCLIClient,
    LLMDecision,
    LLMError,
    OpenAICompatibleClient,
    codex_home_path,
    codex_login_status,
    find_codex_executable,
    launch_codex_device_login,
    load_llm_client_from_env,
)
from .remote import project_remote_workdir
from .remote_transport import probe_server_environment, test_server_connection
from .run_agent import refresh_status, run_project, upload_project_fastqs
from .ssh_auth import (
    clear_ssh_credential,
    get_ssh_credential,
    set_ssh_credential,
)
from .storage import load_defaults, save_defaults, save_json
from .validation import validate_local_fastqs


class ConfigApp(Tk):
    def __init__(self, output_dir: Path) -> None:
        super().__init__()
        self.output_dir = output_dir
        self.title("SYSU RNA-seq Agent")
        self.geometry("1040x740")
        self.minsize(920, 640)
        self.running = False
        self.current_config_path: Path | None = None

        saved = load_defaults() or {}
        self.reference_defaults = saved.get("reference", DEFAULT_REFERENCE.copy())
        saved_llm = saved.get("llm", {})

        self.project_id = StringVar(value=f"rnaseq_{datetime.now():%Y%m%d_%H%M%S}")
        self.project_title = StringVar(value=self.project_id.get())
        self.owner = StringVar(value="local_user")

        self.server_profile = StringVar(value="sysu_hpc")
        self.server_host = StringVar(value="your.server.edu")
        self.server_user = StringVar(value="username")
        self.remote_base_dir = StringVar(value="/data/users/username/rnaseq_projects")
        self.scheduler = StringVar(value="slurm")
        self.threads = StringVar(value="16")
        self.memory_gb = StringVar(value="64")
        self.ssh_auth_mode = StringVar(value="key")
        self.ssh_key_path = StringVar(value="")
        self.ssh_status = StringVar(value="尚未测试服务器连接。")

        self.layout = StringVar(value="paired")
        self.reads_per_sample_million = StringVar(value="40")
        self.strandedness = StringVar(value="auto")
        self.local_data_dir = StringVar(value="D:/data/rnaseq/raw_fastq")
        self.remote_data_dir = StringVar(value="AUTO")

        self.ref_vars = {key: StringVar(value=str(value)) for key, value in self.reference_defaults.items()}
        self.pipeline_vars = {
            step: BooleanVar(value=bool(config["enabled"])) for step, config in DEFAULT_PIPELINE.items()
        }

        self.poll_interval = StringVar(value="300")
        self.poll_timeout = StringVar(value="168")

        self.email_enabled = BooleanVar(value=True)
        self.recipient = StringVar(value="user@example.com")
        self.smtp_host = StringVar(value="smtp.example.com")
        self.smtp_port = StringVar(value="587")
        self.smtp_user = StringVar(value="user@example.com")
        self.smtp_password_env = StringVar(value="RNASEQ_AGENT_SMTP_PASSWORD")

        self.llm_backend = StringVar(
            value=os.environ.get(
                "RNASEQ_AGENT_LLM_BACKEND",
                str(saved_llm.get("backend", "api")),
            )
        )
        self.llm_base_url = StringVar(
            value=os.environ.get(
                "RNASEQ_AGENT_LLM_BASE_URL",
                str(saved_llm.get("base_url", "")),
            )
        )
        self.llm_model = StringVar(
            value=os.environ.get(
                "RNASEQ_AGENT_LLM_MODEL",
                str(saved_llm.get("model", "")),
            )
        )
        self.llm_api_key = StringVar(
            value=os.environ.get("RNASEQ_AGENT_LLM_API_KEY", "")
        )
        self.llm_api_mode = StringVar(
            value=os.environ.get(
                "RNASEQ_AGENT_LLM_API_MODE",
                str(saved_llm.get("api_mode", "auto")),
            )
        )
        self.llm_timeout = StringVar(
            value=os.environ.get(
                "RNASEQ_AGENT_LLM_TIMEOUT",
                str(saved_llm.get("timeout", 45)),
            )
        )
        self.llm_status = StringVar(value="尚未应用模型设置。")
        self.chat_input = StringVar()
        self.chat_text: Text

        self.status_text = StringVar(value="填写配置后，可以保存、校验、直接运行或仅提交。")
        self.sample_text: Text
        self.init_text: Text

        self._build_ui()

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        ttk.Label(
            root,
            text="填写 RNA-seq 分析配置。保存后会生成 project.json，也可以直接点击“保存并运行”。",
        ).pack(anchor="w", pady=(0, 8))

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)
        notebook.add(self._project_tab(notebook), text="项目")
        notebook.add(self._server_tab(notebook), text="服务器")
        notebook.add(self._reference_tab(notebook), text="参考")
        notebook.add(self._samples_tab(notebook), text="样本")
        notebook.add(self._pipeline_tab(notebook), text="流程")
        notebook.add(self._notification_tab(notebook), text="通知")
        notebook.add(self._model_tab(notebook), text="模型")
        notebook.add(self._chat_tab(notebook), text="对话助手")

        footer = ttk.Frame(root)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Label(footer, textvariable=self.status_text).pack(side="left", fill="x", expand=True)
        ttk.Button(footer, text="刷新状态", command=self.refresh_project_status).pack(side="right", padx=(6, 0))
        ttk.Button(footer, text="仅提交", command=lambda: self.start_run(wait=False)).pack(side="right", padx=(6, 0))
        ttk.Button(footer, text="保存并运行", command=lambda: self.start_run(wait=True)).pack(side="right", padx=(6, 0))
        ttk.Button(footer, text="仅上传 FASTQ", command=self.start_upload).pack(side="right", padx=(6, 0))
        ttk.Button(footer, text="估算耗时", command=self.show_estimate).pack(side="right", padx=(6, 0))
        ttk.Button(footer, text="校验配置", command=self.validate_form).pack(side="right", padx=(6, 0))
        ttk.Button(footer, text="保存配置", command=self.save_config).pack(side="right", padx=(6, 0))

    def _project_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        self._entry(frame, "项目 ID", self.project_id, 0, "例如 rnaseq_lung_cancer_001")
        self._entry(frame, "项目名称", self.project_title, 1)
        self._entry(frame, "负责人/用户", self.owner, 2)
        return frame

    def _server_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        self._entry(frame, "服务器配置名", self.server_profile, 0)
        self._entry(frame, "服务器地址 host", self.server_host, 1, "例如 hpc.example.edu")
        self._entry(frame, "服务器用户名", self.server_user, 2)
        self._entry(frame, "服务器项目根目录", self.remote_base_dir, 3)
        self._combo(frame, "调度器", self.scheduler, ["slurm", "pbs", "local"], 4)
        self._entry(frame, "线程数", self.threads, 5)
        self._entry(frame, "内存 GB", self.memory_gb, 6)
        self._combo(
            frame,
            "SSH 登录方式",
            self.ssh_auth_mode,
            ["key", "password", "system"],
            7,
        )
        ttk.Label(
            frame,
            text="key=密钥；password=本次临时密码；system=系统 ssh-agent/统一认证",
        ).grid(row=7, column=2, sticky="w", padx=(8, 0))
        self._entry(frame, "SSH 私钥路径", self.ssh_key_path, 8, "可留空，自动使用 ~/.ssh 或 ssh-agent")
        ttk.Button(frame, text="选择私钥", command=self.choose_ssh_key).grid(
            row=8, column=2, sticky="w", padx=(8, 0)
        )
        auth_buttons = ttk.Frame(frame)
        auth_buttons.grid(row=9, column=1, sticky="w", pady=(6, 8))
        ttk.Button(
            auth_buttons,
            text="输入临时密码",
            command=self.prompt_ssh_password,
        ).pack(side="left")
        ttk.Button(
            auth_buttons,
            text="清除临时凭证",
            command=self.clear_server_credential,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            auth_buttons,
            text="测试连接",
            command=self.test_ssh_connection,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(frame, textvariable=self.ssh_status, wraplength=760).grid(
            row=10, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Label(frame, text="服务器初始化命令").grid(row=11, column=0, sticky="nw", pady=5)
        self.init_text = Text(frame, height=5, width=76)
        self.init_text.grid(row=11, column=1, sticky="nsew", pady=5)
        ttk.Label(frame, text="例如：module load STAR fastp subread rsem arriba").grid(row=12, column=1, sticky="w")
        frame.columnconfigure(1, weight=1)
        return frame

    def _reference_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        fields = [
            ("参考配置名", "name"),
            ("服务器 GTF 路径", "remote_gtf_path"),
            ("服务器 genome FASTA 路径", "remote_genome_fasta_path"),
            ("STAR index 目录", "star_index_dir"),
            ("RSEM index prefix", "rsem_index_prefix"),
            ("Arriba blacklist 路径", "arriba_blacklist_path"),
            ("Arriba known fusions 路径", "arriba_known_fusions_path"),
        ]
        for row, (label, key) in enumerate(fields):
            self._entry(frame, label, self.ref_vars[key], row)
        ttk.Button(frame, text="保存为默认参考配置", command=self.save_reference_defaults).grid(
            row=len(fields), column=1, sticky="e", pady=(10, 0)
        )
        return frame

    def _samples_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        self._combo(frame, "测序类型", self.layout, ["paired", "single"], 0)
        self._entry(frame, "reads/样本，单位 M", self.reads_per_sample_million, 1)
        self._combo(frame, "链特异性", self.strandedness, ["auto", "unstranded", "forward", "reverse"], 2)
        self._entry(frame, "本地 FASTQ 目录", self.local_data_dir, 3)
        ttk.Button(frame, text="选择目录", command=self.choose_fastq_dir).grid(row=3, column=2, padx=(6, 0))
        self._entry(frame, "服务器接收 FASTQ 目录", self.remote_data_dir, 4, "填 AUTO 则使用 <项目目录>/raw")

        ttk.Label(frame, text="样本表：sample_id, condition, fastq_1, fastq_2").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(12, 4)
        )
        self.sample_text = Text(frame, height=12, width=90)
        self.sample_text.grid(row=6, column=0, columnspan=3, sticky="nsew")
        self.sample_text.insert(
            "1.0",
            "Ctrl_1,control,Ctrl_1_R1.fastq.gz,Ctrl_1_R2.fastq.gz\n"
            "Treat_1,treatment,Treat_1_R1.fastq.gz,Treat_1_R2.fastq.gz\n",
        )
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(6, weight=1)
        return frame

    def _pipeline_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        for row, (step, config) in enumerate(DEFAULT_PIPELINE.items()):
            ttk.Checkbutton(frame, text=f"{step} ({config['version']})", variable=self.pipeline_vars[step]).grid(
                row=row, column=0, sticky="w", pady=4
            )
        self._entry(frame, "轮询间隔秒数", self.poll_interval, len(DEFAULT_PIPELINE) + 1)
        self._entry(frame, "最长等待小时数", self.poll_timeout, len(DEFAULT_PIPELINE) + 2)
        return frame

    def _notification_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        ttk.Checkbutton(frame, text="启用邮件通知", variable=self.email_enabled).grid(row=0, column=1, sticky="w", pady=5)
        self._entry(frame, "接收邮箱", self.recipient, 1)
        self._entry(frame, "SMTP 服务器", self.smtp_host, 2)
        self._entry(frame, "SMTP 端口", self.smtp_port, 3)
        self._entry(frame, "SMTP 用户名", self.smtp_user, 4)
        self._entry(frame, "SMTP 密码环境变量名", self.smtp_password_env, 5)
        ttk.Label(frame, text="密码/授权码不写入配置文件，请放在环境变量中。").grid(row=6, column=1, sticky="w")
        return frame

    def _model_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        self._combo(
            frame,
            "模型后端",
            self.llm_backend,
            ["api", "codex_cli"],
            0,
        )
        ttk.Label(
            frame,
            text="codex_cli 复用本机已登录的 Codex，不读取 OAuth Token",
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))
        self._entry(
            frame,
            "Base URL",
            self.llm_base_url,
            1,
            "OpenAI 官方：https://api.openai.com/v1；其他服务请填其 API 地址",
        )

        self._combo(
            frame,
            "接口类型",
            self.llm_api_mode,
            ["auto", "responses", "chat_completions"],
            2,
        )
        ttk.Label(
            frame,
            text="auto 会为本机 CC Switch 自动选择 Responses API",
        ).grid(row=2, column=2, sticky="w", padx=(8, 0))

        ttk.Label(frame, text="模型名称").grid(row=3, column=0, sticky="w", pady=5)
        self.llm_model_combo = ttk.Combobox(
            frame,
            textvariable=self.llm_model,
            values=[],
            state="normal",
            width=69,
        )
        self.llm_model_combo.grid(row=3, column=1, sticky="ew", pady=5)
        ttk.Button(
            frame,
            text="拉取模型",
            command=self.fetch_available_models,
        ).grid(row=3, column=2, sticky="w", padx=(8, 0))

        ttk.Label(frame, text="API Key").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Entry(
            frame,
            textvariable=self.llm_api_key,
            width=72,
            show="*",
        ).grid(row=4, column=1, sticky="ew", pady=5)
        ttk.Label(
            frame,
            text="普通服务填写 API Key；Codex 官方 OAuth 不能在此复用。",
        ).grid(row=4, column=2, sticky="w", padx=(8, 0))

        self._entry(frame, "超时秒数", self.llm_timeout, 5, "API 默认 45 秒；Codex CLI 建议 90 秒")

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=1, sticky="w", pady=(12, 4))
        ttk.Button(
            buttons,
            text="应用设置",
            command=self.apply_model_settings,
        ).pack(side="left")
        ttk.Button(
            buttons,
            text="测试连接",
            command=self.test_model_connection,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons,
            text="登录 ChatGPT",
            command=self.login_codex_chatgpt,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons,
            text="检查登录",
            command=self.check_codex_login,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons,
            text="禁用模型",
            command=self.disable_model,
        ).pack(side="left", padx=(8, 0))

        ttk.Label(
            frame,
            textvariable=self.llm_status,
            wraplength=760,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Label(
            frame,
            text=(
                "安全策略：模型只负责理解自然语言并选择白名单动作；"
                "上传、SSH、任务提交和报告生成仍由确定性后端执行。"
            ),
            wraplength=760,
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))
        frame.columnconfigure(1, weight=1)
        return frame

    def _chat_tab(self, parent: ttk.Notebook) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        ttk.Label(
            frame,
            text=(
                "可以输入：项目摘要、校验配置、生成报告、看看状态、"
                "解释结果、开始运行，或直接提出 RNA-seq 问题。"
            ),
            wraplength=850,
        ).pack(anchor="w", pady=(0, 8))

        self.chat_text = Text(frame, height=24, width=100, state="disabled", wrap="word")
        self.chat_text.pack(fill="both", expand=True)

        input_row = ttk.Frame(frame)
        input_row.pack(fill="x", pady=(8, 0))
        entry = ttk.Entry(input_row, textvariable=self.chat_input)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _event: self.send_chat_message())
        ttk.Button(input_row, text="发送", command=self.send_chat_message).pack(side="left", padx=(8, 0))
        ttk.Button(input_row, text="清空", command=self.clear_chat).pack(side="left", padx=(8, 0))

        self._append_chat(
            "Agent",
            "你好，我可以读取当前表单配置，帮助校验、运行、查看状态、生成报告和解释结果。",
        )
        return frame

    def _entry(self, frame: ttk.Frame, label: str, variable: StringVar, row: int, hint: str = "") -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=variable, width=72).grid(row=row, column=1, sticky="ew", pady=5)
        if hint:
            ttk.Label(frame, text=hint).grid(row=row, column=2, sticky="w", padx=(8, 0))
        frame.columnconfigure(1, weight=1)

    def _combo(self, frame: ttk.Frame, label: str, variable: StringVar, values: list[str], row: int) -> None:
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Combobox(frame, textvariable=variable, values=values, state="readonly", width=20).grid(
            row=row, column=1, sticky="w", pady=5
        )

    def choose_fastq_dir(self) -> None:
        directory = filedialog.askdirectory(title="选择本地 FASTQ 目录")
        if directory:
            self.local_data_dir.set(directory.replace("\\", "/"))

    def build_config(self) -> dict[str, Any]:
        project_id = self.project_id.get().strip()
        if not project_id:
            raise ValueError("项目 ID 不能为空。")

        remote_workdir = project_remote_workdir(self.remote_base_dir.get().strip(), project_id)
        remote_data_dir = self.remote_data_dir.get().strip()
        if remote_data_dir == "AUTO":
            remote_data_dir = f"{remote_workdir}/raw"

        config = {
            "schema_version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "project": {
                "id": project_id,
                "title": self.project_title.get().strip() or project_id,
                "owner": self.owner.get().strip() or "local_user",
            },
            "server": {
                "profile": self.server_profile.get().strip(),
                "host": self.server_host.get().strip(),
                "user": self.server_user.get().strip(),
                "remote_base_dir": self.remote_base_dir.get().strip(),
                "remote_workdir": remote_workdir,
                "scheduler": self.scheduler.get(),
                "threads": int(self.threads.get()),
                "memory_gb": int(self.memory_gb.get()),
                "auth_mode": self.ssh_auth_mode.get(),
                "key_path": self.ssh_key_path.get().strip(),
                "shell": "bash",
                "init_commands": self._parse_init_commands(),
            },
            "reference": self.collect_reference(),
            "sequencing": {
                "layout": self.layout.get(),
                "reads_per_sample_million": int(self.reads_per_sample_million.get()),
                "strandedness": self.strandedness.get(),
            },
            "samples": {
                "source": "local_upload",
                "local_data_dir": self.local_data_dir.get().strip(),
                "remote_data_dir": remote_data_dir,
                "items": self._parse_samples(),
            },
            "pipeline": {
                step: {"enabled": bool(var.get()), "version": DEFAULT_PIPELINE[step]["version"]}
                for step, var in self.pipeline_vars.items()
            },
            "polling": {"interval_seconds": int(self.poll_interval.get()), "timeout_hours": int(self.poll_timeout.get())},
            "notification": self._notification_config(),
            "status": {"state": "configured", "message": "Project config created by GUI mode."},
        }
        config = normalize_config(config)
        runtime = estimate_runtime(config)
        config["runtime_estimate"] = {"hours": runtime.hours, "summary": runtime.summary}
        return config

    def collect_reference(self) -> dict[str, Any]:
        reference = DEFAULT_REFERENCE.copy()
        for key, variable in self.ref_vars.items():
            reference[key] = variable.get().strip()
        return reference

    def _parse_init_commands(self) -> list[str]:
        text = self.init_text.get("1.0", "end").strip()
        if not text:
            return []
        commands: list[str] = []
        for line in text.splitlines():
            commands.extend(part.strip() for part in line.split(";") if part.strip())
        return commands

    def _parse_samples(self) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        paired = self.layout.get() == "paired"
        for raw_line in self.sample_text.get("1.0", "end").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 3:
                raise ValueError(f"样本行至少需要 3 列：{line}")
            items.append(
                {
                    "sample_id": parts[0],
                    "condition": parts[1],
                    "fastq_1": parts[2],
                    "fastq_2": parts[3] if paired and len(parts) > 3 else "",
                }
            )
        return items

    def _notification_config(self) -> dict[str, Any]:
        if not self.email_enabled.get():
            return {"email_enabled": False}
        return {
            "email_enabled": True,
            "recipient": self.recipient.get().strip(),
            "smtp_host": self.smtp_host.get().strip(),
            "smtp_port": int(self.smtp_port.get()),
            "smtp_user": self.smtp_user.get().strip(),
            "password_env": self.smtp_password_env.get().strip(),
            "notify_on": ["completed", "failed", "download_failed", "run_failed"],
        }

    def save_reference_defaults(self) -> None:
        payload = load_defaults() or {}
        payload["reference"] = self.collect_reference()
        save_defaults(payload)
        messagebox.showinfo("已保存", "默认参考配置已保存。")

    def send_chat_message(self) -> None:
        message = self.chat_input.get().strip()
        if not message:
            return
        self.chat_input.set("")
        self._append_chat("你", message)
        if self._handle_local_chat_action(message):
            return

        client = load_llm_client_from_env()
        if client is None:
            self._append_chat(
                "Agent",
                "当前未启用模型。我可以执行固定动作；如需自由问答，请先在“模型”标签页应用模型设置。",
            )
            return
        self._append_chat("Agent", "正在理解你的问题……")
        threading.Thread(
            target=self._chat_llm_worker,
            args=(client, message),
            daemon=True,
        ).start()

    def _handle_local_chat_action(self, message: str) -> bool:
        lower = message.lower().strip()
        if lower in {"帮助", "help", "?"}:
            self._append_chat(
                "Agent",
                "可用动作：项目摘要、校验配置、生成报告、看看状态、解释结果、开始运行、后台运行、模型状态。",
            )
            return True
        if lower in {"项目摘要", "摘要", "summary", "总结一下"}:
            try:
                lines = summarize_project(self.build_config())
            except Exception as exc:
                self._append_chat("Agent", f"读取表单失败：{exc}")
            else:
                self._append_chat("Agent", "\n".join(lines))
            return True
        if lower in {"校验", "校验配置", "检查配置", "validate"}:
            try:
                result = validate_local_fastqs(self.build_config())
            except Exception as exc:
                self._append_chat("Agent", f"校验失败：{exc}")
            else:
                self._append_chat("Agent", "\n".join(validation_summary(result)))
            return True
        if lower in {"报告", "生成报告", "导出报告", "report"}:
            try:
                config_path = self.save_config_silent()
                output = generate_project_report(config_path)
            except Exception as exc:
                self._append_chat("Agent", f"生成报告失败：{exc}")
            else:
                self._append_chat("Agent", f"报告已生成：{output}")
            return True
        if lower in {"解释", "解释结果", "怎么看结果", "explain"}:
            try:
                lines = explain_project(self.build_config())
            except Exception as exc:
                self._append_chat("Agent", f"读取配置失败：{exc}")
            else:
                self._append_chat("Agent", "\n".join(lines))
            return True
        if lower in {"状态", "看看状态", "刷新状态", "status"}:
            self._chat_refresh_status()
            return True
        if lower in {"运行", "开始运行", "run"}:
            if messagebox.askyesno("确认运行", "是否保存配置并运行当前项目？"):
                self._append_chat("Agent", "已开始运行，状态会显示在窗口底部。")
                self.start_run(wait=True)
            else:
                self._append_chat("Agent", "已取消运行。")
            return True
        if lower in {"后台运行", "仅提交", "run-nowait"}:
            if messagebox.askyesno("确认提交", "是否保存配置并提交当前项目？"):
                self._append_chat("Agent", "已开始提交任务。")
                self.start_run(wait=False)
            else:
                self._append_chat("Agent", "已取消提交。")
            return True
        if lower in {"模型", "模型状态", "model", "llm"}:
            client = load_llm_client_from_env()
            if client is None:
                self._append_chat("Agent", "当前未启用模型，正在使用本地规则路由。")
            else:
                self._append_chat(
                    "Agent",
                    f"当前模型：{client.model}\nBase URL：{client.base_url}\n"
                    f"接口类型：{client.resolved_api_mode}\n模型不能直接执行 shell 命令。",
                )
            return True
        return False

    def _chat_llm_worker(
        self,
        client: OpenAICompatibleClient | CodexCLIClient,
        message: str,
    ) -> None:
        try:
            decision = client.decide(
                message,
                has_project=self.current_config_path is not None,
            )
        except Exception as exc:
            self.after(
                0,
                lambda error=str(exc): self._append_chat(
                    "Agent",
                    f"模型理解失败：{error}",
                ),
            )
            return
        self.after(0, lambda result=decision: self._execute_gui_decision(result))

    def _execute_gui_decision(self, decision: LLMDecision) -> None:
        if decision.action == "chat":
            self._append_chat(
                "Agent",
                decision.message or "我可以帮助你处理当前 RNA-seq 项目。",
            )
            return
        if decision.action in {"new", "open"}:
            self._append_chat(
                "Agent",
                "请在上方“项目/服务器/样本”等标签页新建或编辑项目配置。",
            )
            return
        action_phrases = {
            "validate": "校验配置",
            "status": "看看状态",
            "report": "生成报告",
            "explain": "解释结果",
            "summary": "项目摘要",
            "where": "项目摘要",
            "help": "帮助",
            "run": "开始运行",
            "run-nowait": "后台运行",
        }
        phrase = action_phrases.get(decision.action)
        if phrase is None:
            self._append_chat("Agent", decision.message or "该动作当前未在桌面端开放。")
            return
        self._handle_local_chat_action(phrase)

    def _chat_refresh_status(self) -> None:
        config_path = self.current_config_path
        if config_path is None:
            try:
                config_path = self.save_config_silent()
            except Exception as exc:
                self._append_chat("Agent", f"保存当前配置失败：{exc}")
                return
        self._append_chat("Agent", "正在刷新远程状态……")

        def worker() -> None:
            try:
                status = refresh_status(config_path)
                text = "\n".join(status_summary(status))
            except Exception as exc:
                text = f"刷新状态失败：{exc}"
            self.after(0, lambda value=text: self._append_chat("Agent", value))

        threading.Thread(target=worker, daemon=True).start()

    def _append_chat(self, role: str, message: str) -> None:
        self.chat_text.configure(state="normal")
        self.chat_text.insert("end", f"{role}：{message}\n\n")
        self.chat_text.see("end")
        self.chat_text.configure(state="disabled")

    def clear_chat(self) -> None:
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")
        self.chat_text.configure(state="disabled")

    def fetch_available_models(self) -> None:
        if self.llm_backend.get() == "codex_cli":
            executable = find_codex_executable()
            if executable is None:
                self.llm_status.set("没有找到 Codex CLI，请先安装并登录 Codex。")
                return
            self.llm_status.set(
                f"已找到 Codex CLI：{executable}。模型名可以留空，由 Codex 自动选择默认模型。"
            )
            return

        base_url = self.llm_base_url.get().strip()
        api_key = self.llm_api_key.get().strip()
        timeout_text = self.llm_timeout.get().strip() or "45"

        if not base_url:
            messagebox.showwarning("缺少 Base URL", "请先填写模型服务的 API Base URL。")
            return
        if "chatgpt.com" in base_url.lower():
            messagebox.showerror(
                "Base URL 不正确",
                "chatgpt.com 是网页地址，不是 API 地址。\n"
                "OpenAI 官方 API 请填写：https://api.openai.com/v1",
            )
            return
        try:
            timeout = max(int(timeout_text), 1)
        except ValueError:
            messagebox.showerror("模型设置错误", "超时秒数必须是正整数。")
            return

        self.llm_status.set("正在从服务端拉取模型列表……")
        client = OpenAICompatibleClient(
            base_url=base_url,
            model="",
            api_key=api_key,
            timeout_seconds=timeout,
            api_mode=self.llm_api_mode.get(),
        )

        def worker() -> None:
            try:
                models = client.list_models()
            except Exception as exc:
                self.after(
                    0,
                    lambda error=str(exc): self.llm_status.set(f"拉取模型失败：{error}"),
                )
                return
            self.after(0, lambda values=models: self._set_available_models(values))

        threading.Thread(target=worker, daemon=True).start()

    def _set_available_models(self, models: list[str]) -> None:
        self.llm_model_combo.configure(values=models)
        current = self.llm_model.get().strip()
        if current not in models:
            self.llm_model.set(models[0])
        self.llm_status.set(
            f"已拉取 {len(models)} 个模型。请选择模型后点击“应用设置”或“测试连接”。"
        )

    def apply_model_settings(
        self,
        *,
        show_message: bool = True,
    ) -> OpenAICompatibleClient | CodexCLIClient | None:
        backend = self.llm_backend.get().strip() or "api"
        base_url = self.llm_base_url.get().strip()
        model = self.llm_model.get().strip()
        api_key = self.llm_api_key.get().strip()
        api_mode = self.llm_api_mode.get().strip() or "auto"
        timeout_text = self.llm_timeout.get().strip() or "45"

        if backend == "codex_cli":
            try:
                timeout = max(int(timeout_text), 1)
            except ValueError:
                if show_message:
                    messagebox.showerror("模型设置错误", "超时秒数必须是正整数。")
                return None
            executable = find_codex_executable()
            if executable is None:
                if show_message:
                    messagebox.showerror("Codex CLI 未找到", "请先安装并登录 Codex。")
                return None

            os.environ["RNASEQ_AGENT_LLM_BACKEND"] = "codex_cli"
            os.environ["RNASEQ_AGENT_LLM_MODEL"] = model
            os.environ["RNASEQ_AGENT_LLM_TIMEOUT"] = str(timeout)
            payload = load_defaults() or {}
            payload["llm"] = {
                "backend": "codex_cli",
                "model": model,
                "timeout": timeout,
            }
            save_defaults(payload)
            client = CodexCLIClient(
                model=model,
                executable=executable,
                timeout_seconds=timeout,
            )
            display_model = model or "Codex 默认模型"
            self.llm_status.set(f"模型已应用：{display_model}；后端：Codex CLI")
            if show_message:
                messagebox.showinfo(
                    "模型设置已应用",
                    "将通过已登录的 Codex CLI 调用模型，不读取或复制 OAuth Token。",
                )
            return client

        if "chatgpt.com" in base_url.lower():
            if show_message:
                messagebox.showerror(
                    "Base URL 不正确",
                    "chatgpt.com 是网页地址，不是 API 地址。\n"
                    "OpenAI 官方 API 请填写：https://api.openai.com/v1",
                )
            return None
        if not base_url or not model:
            if show_message:
                messagebox.showwarning(
                    "模型设置不完整",
                    "请填写 Base URL，并点击“拉取模型”选择模型名称。",
                )
            return None
        try:
            timeout = max(int(timeout_text), 1)
        except ValueError:
            if show_message:
                messagebox.showerror("模型设置错误", "超时秒数必须是正整数。")
            return None

        os.environ["RNASEQ_AGENT_LLM_BACKEND"] = "api"
        os.environ["RNASEQ_AGENT_LLM_BASE_URL"] = base_url
        os.environ["RNASEQ_AGENT_LLM_MODEL"] = model
        os.environ["RNASEQ_AGENT_LLM_API_MODE"] = api_mode
        os.environ["RNASEQ_AGENT_LLM_TIMEOUT"] = str(timeout)
        if api_key:
            os.environ["RNASEQ_AGENT_LLM_API_KEY"] = api_key
        else:
            os.environ.pop("RNASEQ_AGENT_LLM_API_KEY", None)

        payload = load_defaults() or {}
        payload["llm"] = {
            "backend": "api",
            "base_url": base_url,
            "model": model,
            "api_mode": api_mode,
            "timeout": timeout,
        }
        save_defaults(payload)

        client = OpenAICompatibleClient(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout,
            api_mode=api_mode,
        )
        self.llm_status.set(
            f"模型已应用：{model} @ {base_url}；接口：{client.resolved_api_mode}"
        )
        if show_message:
            messagebox.showinfo(
                "模型设置已应用",
                "Base URL、模型名、接口类型和超时已保存；API Key 仅用于当前程序运行。",
            )
        return client

    def test_model_connection(self) -> None:
        client = self.apply_model_settings(show_message=False)
        if client is None:
            messagebox.showwarning("模型设置不完整", "请先填写正确的模型设置。")
            return
        self.llm_status.set("正在测试模型连接……")

        def worker() -> None:
            try:
                decision = client.decide("请帮助我查看当前 agent 可以做什么。", has_project=False)
                text = f"连接成功：模型返回动作 {decision.action}"
                if decision.message:
                    text += f"；说明：{decision.message}"
            except LLMError as exc:
                self.after(
                    0,
                    lambda error=str(exc): self.llm_status.set(f"连接失败：{error}"),
                )
                return
            except Exception as exc:
                self.after(
                    0,
                    lambda error=str(exc): self.llm_status.set(f"连接失败：{error}"),
                )
                return
            self.after(0, lambda: self.llm_status.set(text))

        threading.Thread(target=worker, daemon=True).start()

    def login_codex_chatgpt(self) -> None:
        executable = find_codex_executable()
        if executable is None:
            messagebox.showerror("Codex CLI 未找到", "没有找到 Codex CLI，无法启动登录。")
            return
        self.llm_backend.set("codex_cli")
        try:
            launch_codex_device_login(executable=executable)
        except Exception as exc:
            messagebox.showerror("登录启动失败", str(exc))
            return
        self.llm_status.set(
            "已打开独立的 ChatGPT 设备登录窗口。请按窗口提示完成授权，"
            "完成后点击“检查登录”，再点击“测试连接”。"
        )
        messagebox.showinfo(
            "请完成登录",
            "请在新打开的窗口中完成设备授权。\n\n"
            f"登录只保存在：{codex_home_path()}\n"
            "不会覆盖你现有的 Codex 或 CC Switch 配置。",
        )

    def check_codex_login(self) -> None:
        self.llm_backend.set("codex_cli")
        self.llm_status.set("正在检查 RNA-seq Agent 的独立登录状态……")

        def worker() -> None:
            try:
                status = codex_login_status()
                text = f"Codex 登录状态：{status}；独立目录：{codex_home_path()}"
            except Exception as exc:
                text = f"检查登录失败：{exc}"
            self.after(0, lambda value=text: self.llm_status.set(value))

        threading.Thread(target=worker, daemon=True).start()

    def disable_model(self) -> None:
        for key in (
            "RNASEQ_AGENT_LLM_BASE_URL",
            "RNASEQ_AGENT_LLM_BACKEND",
            "RNASEQ_AGENT_LLM_MODEL",
            "RNASEQ_AGENT_LLM_API_KEY",
            "RNASEQ_AGENT_LLM_API_MODE",
            "RNASEQ_AGENT_LLM_TIMEOUT",
        ):
            os.environ.pop(key, None)
        self.llm_api_key.set("")
        payload = load_defaults() or {}
        payload.pop("llm", None)
        save_defaults(payload)
        self.llm_status.set("模型已禁用，程序将使用本地规则路由。")

    def save_config(self) -> Path | None:
        try:
            config_path = self.save_config_silent()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return None
        messagebox.showinfo("保存成功", f"配置已保存：\n{config_path}")
        return config_path

    def save_config_silent(self) -> Path:
        config = self.build_config()
        config_path = self.output_dir / config["project"]["id"] / "project.json"
        save_json(config_path, config)
        self.current_config_path = config_path
        self.status_text.set(f"配置已保存：{config_path}")
        return config_path

    def validate_form(self) -> None:
        try:
            config = self.build_config()
            result = validate_local_fastqs(config)
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        if result.ok:
            messagebox.showinfo("校验通过", "本地 FASTQ 和配置校验通过。")
            return
        lines = []
        lines.extend(result.errors)
        lines.extend(str(path) for path in result.missing_files[:12])
        if len(result.missing_files) > 12:
            lines.append(f"还有 {len(result.missing_files) - 12} 个缺失文件未显示。")
        messagebox.showwarning("校验未通过", "\n".join(lines) or "校验未通过。")

    def show_estimate(self) -> None:
        try:
            runtime = estimate_runtime(self.build_config())
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        messagebox.showinfo("预计耗时", runtime.summary)

    def start_run(self, wait: bool) -> None:
        if self.running:
            messagebox.showinfo("正在运行", "已有任务正在运行，请等待当前任务结束。")
            return
        if not self._prepare_server_credential(require_password=True):
            return
        try:
            config_path = self.save_config_silent()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.running = True
        mode = "运行并等待完成" if wait else "提交后返回"
        self.status_text.set(f"已启动：{mode}，配置 {config_path}")
        threading.Thread(target=self._run_worker, args=(config_path, wait), daemon=True).start()

    def start_upload(self) -> None:
        if self.running:
            messagebox.showinfo("正在处理", "已有任务正在处理，请等待当前任务结束。")
            return
        if not self._prepare_server_credential(require_password=True):
            return
        try:
            config = self.build_config()
            validation = validate_local_fastqs(config, check_pipeline=False)
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        if not validation.ok:
            messagebox.showerror("FASTQ 校验失败", "\n".join(validation_summary(validation)))
            return
        remote_data_dir = config["samples"]["remote_data_dir"]
        if not messagebox.askyesno(
            "确认上传 FASTQ",
            "Agent 将创建远程目录并上传当前 FASTQ：\n\n"
            f"{remote_data_dir}\n\n"
            "此操作不会提交 Slurm 任务。同名远程文件会被替换。是否继续？",
        ):
            return
        try:
            config_path = self.save_config_silent()
        except Exception as exc:
            messagebox.showerror("保存配置失败", str(exc))
            return
        self.running = True
        self.status_text.set(f"正在校验并上传 FASTQ 到 {remote_data_dir}……")
        threading.Thread(
            target=self._upload_worker,
            args=(config_path,),
            daemon=True,
        ).start()

    def _upload_worker(self, config_path: Path) -> None:
        try:
            outcome = upload_project_fastqs(config_path)
        except Exception as exc:
            self.after(0, self._upload_finished, False, str(exc), config_path)
            return
        self.after(0, self._upload_finished, True, outcome.message, config_path)

    def _upload_finished(
        self,
        ok: bool,
        message: str,
        config_path: Path,
    ) -> None:
        self.running = False
        self.current_config_path = config_path
        self.status_text.set(message)
        if ok:
            messagebox.showinfo("FASTQ 上传完成", message)
        else:
            messagebox.showerror("FASTQ 上传失败", message)

    def _run_worker(self, config_path: Path, wait: bool) -> None:
        try:
            outcome = run_project(config_path, wait=wait)
        except Exception as exc:
            self.after(0, self._run_finished, False, str(exc), config_path)
            return
        self.after(0, self._run_finished, True, outcome.message, config_path)

    def _run_finished(self, ok: bool, message: str, config_path: Path) -> None:
        self.running = False
        self.current_config_path = config_path
        self.status_text.set(message)
        if ok:
            messagebox.showinfo("任务状态", message)
        else:
            messagebox.showerror("任务失败", message)

    def refresh_project_status(self) -> None:
        if not self._prepare_server_credential(require_password=True):
            return
        config_path = self.current_config_path
        if config_path is None:
            try:
                config_path = self.save_config_silent()
            except Exception as exc:
                messagebox.showerror("配置错误", str(exc))
                return

        def worker() -> None:
            try:
                status = refresh_status(config_path)
                state = status.get("state", "unknown")
                message = status.get("message", "")
                text = f"{state}: {message}" if message else state
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("刷新状态失败", str(exc)))
                return
            self.after(0, lambda: self.status_text.set(text))

        threading.Thread(target=worker, daemon=True).start()

    def choose_ssh_key(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 SSH 私钥",
            filetypes=[("SSH private key", "*"), ("All files", "*.*")],
        )
        if path:
            self.ssh_key_path.set(path)
            self.ssh_auth_mode.set("key")

    def prompt_ssh_password(self) -> bool:
        host = self.server_host.get().strip()
        user = self.server_user.get().strip()
        if not host or not user:
            messagebox.showwarning("服务器信息不完整", "请先填写服务器地址和用户名。")
            return False
        password = simpledialog.askstring(
            "临时 SSH 密码",
            f"请输入 {user}@{host} 的服务器密码。\n"
            "密码只保存在当前程序内存中，关闭程序后消失。",
            show="*",
            parent=self,
        )
        if password is None:
            return False
        if not password:
            messagebox.showwarning("密码为空", "没有保存空密码。")
            return False
        self.ssh_auth_mode.set("password")
        set_ssh_credential(host, user, mode="password", password=password)
        self.ssh_status.set("已保存本次运行的临时密码（仅内存，不写入项目或日志）。")
        return True

    def clear_server_credential(self) -> None:
        host = self.server_host.get().strip()
        user = self.server_user.get().strip()
        clear_ssh_credential(host, user)
        self.ssh_status.set("已清除当前程序内存中的临时服务器凭证。")

    def _prepare_server_credential(self, *, require_password: bool) -> bool:
        host = self.server_host.get().strip()
        user = self.server_user.get().strip()
        mode = self.ssh_auth_mode.get().strip() or "key"
        if not host or not user:
            messagebox.showwarning("服务器信息不完整", "请填写服务器地址和用户名。")
            return False
        if mode == "password":
            current = get_ssh_credential(host, user)
            if not current.password:
                if not require_password:
                    return False
                return self.prompt_ssh_password()
            return True
        set_ssh_credential(
            host,
            user,
            mode=mode,
            key_path=self.ssh_key_path.get().strip(),
        )
        return True

    def test_ssh_connection(self) -> None:
        if not self._prepare_server_credential(require_password=True):
            return
        try:
            config = self.build_config()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.ssh_status.set("正在测试服务器连接……")

        def worker() -> None:
            try:
                result = test_server_connection(config)
                if "RNASEQ_AGENT_SSH_OK" not in result.stdout:
                    raise RuntimeError("服务器已响应，但没有返回预期测试标记。")
                environment = probe_server_environment(config)
                home = environment.get("home", "")
                scheduler = environment.get("scheduler", "")
                text = "服务器连接成功。当前凭证可用于创建目录、上传和提交任务。"
                if home:
                    text += f" 检测到主目录：{home}。"
                if scheduler:
                    text += f" 检测到调度器：{scheduler}。"
            except Exception as exc:
                text = f"服务器连接失败：{exc}"
                self.after(0, lambda value=text: self.ssh_status.set(value))
                return
            self.after(
                0,
                lambda value=text, detected_home=home, detected_scheduler=scheduler:
                self._apply_server_probe(
                    value,
                    detected_home,
                    detected_scheduler,
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _apply_server_probe(
        self,
        status_text: str,
        home: str,
        scheduler: str,
    ) -> None:
        if home and (
            not self.remote_base_dir.get().strip()
            or "username" in self.remote_base_dir.get()
        ):
            self.remote_base_dir.set(f"{home.rstrip('/')}/rnaseq_projects")
        if scheduler in {"slurm", "pbs", "local"}:
            self.scheduler.set(scheduler)
        self.ssh_status.set(status_text)


def run_gui(output_dir: Path) -> None:
    app = ConfigApp(output_dir)
    app.mainloop()
