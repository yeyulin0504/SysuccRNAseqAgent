from __future__ import annotations

import hashlib
import os
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, StringVar, Text, Tk, filedialog, messagebox, simpledialog
from tkinter import ttk
from typing import Any

from .configuration import normalize_config
from .actions import (
    explain_project,
    generate_project_report,
    load_project_config,
    status_summary,
    summarize_project,
    validation_summary,
)
from .defaults import DEFAULT_PIPELINE, DEFAULT_REFERENCE
from .downstream import downstream_errors
from .estimate import estimate_runtime
from .reference_catalog import (
    ReferenceCandidate,
    candidates_for_species,
    catalog_reference_fields,
    has_unsupported_assembly,
    infer_reference_candidates,
    normalize_explicit_species,
)
from .text_attachment import AttachmentError, TextAttachment, load_text_attachment
from .llm import (
    CodexCLIClient,
    LLMDecision,
    LLMError,
    OnboardingDecision,
    OpenAICompatibleClient,
    codex_home_path,
    codex_login_status,
    find_codex_executable,
    launch_codex_device_login,
    load_llm_client_from_env,
)
from .llm_policy import (
    ConfigPatch,
    OnboardingPatch,
    apply_onboarding_proposals,
    apply_proposals,
)
from .llm_submission import inspect_llm_submission, submit_llm_contract
from .remote import project_remote_workdir
from .remote_transport import probe_server_environment, test_server_connection
from .run_agent import refresh_status, run_project, upload_project_fastqs
from .ssh_auth import (
    clear_ssh_credential,
    get_ssh_credential,
    set_ssh_credential,
)
from .storage import load_defaults, save_defaults, save_json
from .validation import ValidationResult, validate_local_fastqs


class ConfigApp(Tk):
    ONBOARDING_FIELDS = (
        ("project.id", "请先给这个分析项目取一个简短的英文项目 ID，例如 rnaseq_lung_001。"),
        ("project.title", "这个项目的显示名称是什么？可以用中文。"),
        ("server.host", "请填写集群服务器地址（host，不含 ssh://）。"),
        ("server.user", "请填写你的集群用户名。"),
        ("server.remote_base_dir", "请填写服务器上存放项目的根目录。"),
        ("sequencing.layout", "你的测序数据是双端 paired 还是单端 single？"),
        ("samples.local_data_dir", "请填写本机存放 FASTQ 文件的文件夹路径。不会读取或上传文件。"),
    )

    def __init__(self, output_dir: Path) -> None:
        super().__init__()
        self.output_dir = output_dir
        self.title("SYSU RNA-seq Agent")
        self.geometry("1040x740")
        self.minsize(920, 640)
        self.running = False
        self.validation_in_progress = False
        self.current_config_path: Path | None = None
        self.loaded_config: dict[str, Any] | None = None
        self.pending_proposals: tuple[ConfigPatch, ...] = ()
        self.pending_onboarding_proposals: tuple[OnboardingPatch, ...] = ()
        self.onboarding_active = False
        self.onboarding_field_index = 0
        self.onboarding_turn = 0
        self.notebook: ttk.Notebook | None = None
        self.text_attachment: TextAttachment | None = None
        self.pending_reference_candidates: tuple[ReferenceCandidate, ...] = ()
        self.awaiting_species_answer = False
        self.reference_index_state = "unconfigured"
        self.reference_candidate_text: Text | None = None
        self.attachment_status = StringVar(value="尚未添加本地文本元数据。")

        saved = load_defaults() or {}
        self.reference_defaults = saved.get("reference", {})
        saved_llm = saved.get("llm", {})

        self.project_id = StringVar()
        self.project_title = StringVar()
        self.owner = StringVar()

        self.server_profile = StringVar()
        self.server_host = StringVar()
        self.server_user = StringVar()
        self.remote_base_dir = StringVar()
        self.scheduler = StringVar(value="slurm")
        self.threads = StringVar(value="16")
        self.memory_gb = StringVar(value="64")
        self.ssh_auth_mode = StringVar(value="key")
        self.ssh_key_path = StringVar(value="")
        self.ssh_status = StringVar(value="尚未测试服务器连接。")

        self.layout = StringVar(value="paired")
        self.reads_per_sample_million = StringVar(value="40")
        self.strandedness = StringVar(value="auto")
        self.local_data_dir = StringVar()
        self.remote_data_dir = StringVar(value="AUTO")

        reference_keys = (
            *DEFAULT_REFERENCE,
            "provider",
            "catalog_id",
            "catalog_manifest_sha256",
            "index_state",
        )
        self.ref_vars = {
            key: StringVar(value=str(self.reference_defaults.get(key, "")))
            for key in reference_keys
        }
        self.pipeline_vars = {
            step: BooleanVar(value=bool(config["enabled"])) for step, config in DEFAULT_PIPELINE.items()
        }
        self.downstream_enabled = BooleanVar(value=False)
        self.downstream_use_batch = BooleanVar(value=False)
        self.downstream_min_count = StringVar(value="10")
        self.downstream_min_samples = StringVar(value="2")
        self.downstream_padj = StringVar(value="0.05")
        self.downstream_abs_log2fc = StringVar(value="1.0")
        self.downstream_go_ora = BooleanVar(value=True)
        self.downstream_kegg_ora = BooleanVar(value=True)
        self.downstream_gsea = BooleanVar(value=True)
        self.downstream_organism = StringVar()
        self.downstream_gmt_enabled = BooleanVar(value=False)
        self.downstream_gmt_path = StringVar()
        self.downstream_gmt_sha256 = StringVar()
        self.downstream_runtime_image = StringVar()
        self.downstream_runtime_sha256 = StringVar()
        self.downstream_metadata_text: Text
        self.downstream_contrasts_text: Text
        self.downstream_summary: Text

        self.poll_interval = StringVar(value="300")
        self.poll_timeout = StringVar(value="168")

        self.email_enabled = BooleanVar(value=False)
        self.recipient = StringVar()
        self.smtp_host = StringVar()
        self.smtp_port = StringVar(value="587")
        self.smtp_user = StringVar()
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
        self._configure_theme()
        root = ttk.Frame(self, style="App.TFrame", padding=12)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root, style="Header.TFrame", padding=(16, 10))
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(
            header,
            text="SYSU RNA-seq Agent",
            style="Header.TLabel",
        ).pack(side="left")
        ttk.Label(
            header,
            text="本地表单与确认优先；远程操作必须逐次确认",
            style="Muted.TLabel",
        ).pack(side="right")

        workspace = ttk.Frame(root, style="App.TFrame")
        workspace.pack(fill="both", expand=True)
        sidebar = ttk.Frame(workspace, style="Sidebar.TFrame", padding=10)
        sidebar.pack(side="left", fill="y", padx=(0, 10))
        content = ttk.Frame(workspace, style="App.TFrame")
        content.pack(side="left", fill="both", expand=True)
        self.pages: dict[str, ttk.Frame] = {}
        page_specs = (
            ("model", "模型设置", self._model_tab),
            ("agent", "SYSU_Agent", self._chat_tab),
            ("project", "项目", self._project_tab),
            ("server", "服务器", self._server_tab),
            ("reference", "参考", self._reference_tab),
            ("samples", "样本", self._samples_tab),
            ("pipeline", "流程", self._pipeline_tab),
            ("downstream", "下游分析", self._downstream_tab),
            ("notification", "通知", self._notification_tab),
        )
        for page_name, label, builder in page_specs:
            ttk.Button(
                sidebar,
                text=label,
                style="Nav.TButton",
                command=lambda name=page_name: self._show_page(name),
            ).pack(fill="x", pady=2)
            self.pages[page_name] = builder(content)
        self._show_page("model")

        footer = ttk.Frame(root, style="Header.TFrame", padding=(10, 8))
        footer.pack(fill="x", pady=(10, 0))
        status_row = ttk.Frame(footer, style="Header.TFrame")
        status_row.pack(fill="x", pady=(0, 6))
        ttk.Label(
            status_row,
            textvariable=self.status_text,
            style="Muted.TLabel",
            wraplength=880,
            justify="left",
        ).pack(fill="x")

        action_row = ttk.Frame(footer, style="Header.TFrame")
        action_row.pack(fill="x")
        actions = (
            ("保存配置", "TButton", self.save_config),
            ("校验配置", "TButton", self.validate_form),
            ("估算耗时", "TButton", self.show_estimate),
            ("仅上传 FASTQ", "Remote.TButton", self.start_upload),
            ("保存并运行", "Remote.TButton", lambda: self.start_run(wait=True)),
            ("仅提交", "Remote.TButton", lambda: self.start_run(wait=False)),
            ("打开项目", "TButton", self.open_project_config),
            ("刷新状态", "TButton", self.refresh_project_status),
        )
        for index, (label, style, command) in enumerate(actions):
            row, column = divmod(index, 4)
            ttk.Button(action_row, text=label, style=style, command=command).grid(
                row=row,
                column=column,
                padx=(0, 6) if column < 3 else 0,
                pady=(0, 4) if row == 0 else 0,
                sticky="ew",
            )
        for column in range(4):
            action_row.columnconfigure(column, weight=1)

    def _configure_theme(self) -> None:
        self.configure(background="#111827")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#111827")
        style.configure("Header.TFrame", background="#1f2937")
        style.configure("Sidebar.TFrame", background="#172033")
        style.configure("TLabel", background="#111827", foreground="#e5e7eb")
        style.configure("Header.TLabel", background="#1f2937", foreground="#f9fafb", font=("TkDefaultFont", 14, "bold"))
        style.configure("Muted.TLabel", background="#1f2937", foreground="#aab5c5")
        style.configure("TButton", background="#334155", foreground="#f8fafc", padding=(10, 6))
        style.map("TButton", background=[("active", "#475569")])
        style.configure("Nav.TButton", background="#172033", anchor="w", padding=(12, 8))
        style.map("Nav.TButton", background=[("active", "#334155")])
        style.configure("Remote.TButton", background="#9f1239", foreground="#fff1f2")
        style.map("Remote.TButton", background=[("active", "#be123c")])
        style.configure("TEntry", fieldbackground="#0f172a", foreground="#f8fafc")
        style.configure("TCombobox", fieldbackground="#0f172a", foreground="#f8fafc")

    def _show_page(self, page_name: str) -> None:
        for page in self.pages.values():
            page.pack_forget()
        self.pages[page_name].pack(fill="both", expand=True)

    def _project_tab(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        self._entry(frame, "项目 ID", self.project_id, 0, "必填，如 rnaseq_lung_cancer_001")
        self._entry(frame, "项目名称", self.project_title, 1, "选填，如 小鼠肺组织 RNA-seq")
        self._entry(frame, "负责人/用户", self.owner, 2, "选填，如 your_name")
        return frame

    def _server_tab(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        self._entry(frame, "服务器配置名", self.server_profile, 0, "选填，如 sysu_hpc")
        self._entry(frame, "服务器地址 host", self.server_host, 1, "必填，如 hpc.example.edu")
        self._entry(frame, "服务器用户名", self.server_user, 2, "必填，如 your_username")
        self._entry(frame, "服务器项目根目录", self.remote_base_dir, 3, "必填，如 /data/users/your_username/rnaseq_projects")
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

    def _reference_tab(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        ttk.Label(
            frame,
            text=(
                "本地文本元数据只用于本机物种推断，不会发送给模型、保存到项目、"
                "上传或触发远程操作。"
            ),
            wraplength=820,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        attachment_row = ttk.Frame(frame)
        attachment_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Button(
            attachment_row,
            text="添加本地文本元数据",
            command=self.choose_text_attachment,
        ).pack(side="left")
        ttk.Button(
            attachment_row,
            text="移除元数据",
            command=self.remove_text_attachment,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(
            attachment_row,
            textvariable=self.attachment_status,
            wraplength=620,
        ).pack(side="left", padx=(12, 0))

        ttk.Label(frame, text="待确认参考候选").grid(
            row=2, column=0, sticky="nw", pady=(0, 5)
        )
        self.reference_candidate_text = Text(frame, height=5, width=76, state="disabled")
        self.reference_candidate_text.grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=(0, 5)
        )
        ttk.Button(
            frame,
            text="应用待确认参考",
            command=self.apply_pending_reference_catalog,
        ).grid(row=3, column=1, sticky="w", pady=(0, 5))
        ttk.Button(
            frame,
            text="配置参考索引",
            command=self.configure_reference_index,
        ).grid(row=3, column=2, sticky="w", pady=(0, 5))

        fields = [
            ("参考配置名", "name"),
            ("服务器 GTF 路径", "remote_gtf_path"),
            ("服务器 genome FASTA 路径", "remote_genome_fasta_path"),
            ("STAR index 目录", "star_index_dir"),
            ("RSEM index prefix", "rsem_index_prefix"),
            ("Arriba blacklist 路径", "arriba_blacklist_path"),
            ("Arriba known fusions 路径", "arriba_known_fusions_path"),
        ]
        for row, (label, key) in enumerate(fields, start=4):
            self._entry(frame, label, self.ref_vars[key], row, "选填，如由已批准参考目录填入")
        ttk.Button(frame, text="保存为默认参考配置", command=self.save_reference_defaults).grid(
            row=len(fields) + 4, column=1, sticky="e", pady=(10, 0)
        )
        frame.columnconfigure(1, weight=1)
        return frame

    def _samples_tab(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        self._combo(frame, "测序类型", self.layout, ["paired", "single"], 0)
        self._entry(frame, "reads/样本，单位 M", self.reads_per_sample_million, 1)
        self._combo(frame, "链特异性", self.strandedness, ["auto", "unstranded", "forward", "reverse"], 2)
        self._entry(frame, "本地 FASTQ 目录", self.local_data_dir, 3, "必填，如 D:/data/rnaseq/raw_fastq")
        ttk.Button(frame, text="选择目录", command=self.choose_fastq_dir).grid(row=3, column=2, padx=(6, 0))
        self._entry(frame, "服务器接收 FASTQ 目录", self.remote_data_dir, 4, "填 AUTO 则使用 <项目目录>/raw")

        ttk.Label(frame, text="样本表：sample_id, condition, fastq_1, fastq_2").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(12, 4)
        )
        self.sample_text = Text(frame, height=12, width=90)
        self.sample_text.grid(row=6, column=0, columnspan=3, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(6, weight=1)
        return frame

    def _pipeline_tab(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        for row, (step, config) in enumerate(DEFAULT_PIPELINE.items()):
            ttk.Checkbutton(frame, text=f"{step} ({config['version']})", variable=self.pipeline_vars[step]).grid(
                row=row, column=0, sticky="w", pady=4
            )
        self._entry(frame, "轮询间隔秒数", self.poll_interval, len(DEFAULT_PIPELINE) + 1)
        self._entry(frame, "最长等待小时数", self.poll_timeout, len(DEFAULT_PIPELINE) + 2)
        return frame

    def _downstream_tab(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        ttk.Checkbutton(
            frame,
            text="启用固定 bulk RNA-seq 下游分析（DESeq2、QC、富集）",
            variable=self.downstream_enabled,
            command=self._refresh_downstream_summary,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(
            frame,
            text=(
                "固定输入为 featurecounts/gene_counts.txt。统计设计、样本分组、GMT、镜像路径和运行时信息不会发送给普通对话模型；"
                "下游 R 仅在已确认的 Slurm/PBS 任务中运行。"
            ),
            wraplength=860,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Label(frame, text="样本元数据：sample_id,condition,batch（batch 选填）").grid(
            row=2, column=0, columnspan=3, sticky="w"
        )
        self.downstream_metadata_text = Text(frame, height=7, width=92)
        self.downstream_metadata_text.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(3, 8))
        ttk.Checkbutton(
            frame,
            text="在设计中加入 batch（~ batch + condition）",
            variable=self.downstream_use_batch,
            command=self._refresh_downstream_summary,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Label(frame, text="对比：id,numerator,denominator（固定为 numerator 相对 denominator）").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        self.downstream_contrasts_text = Text(frame, height=5, width=92)
        self.downstream_contrasts_text.grid(row=6, column=0, columnspan=3, sticky="nsew", pady=(3, 8))
        self._entry(frame, "最小计数", self.downstream_min_count, 7, "默认 10")
        self._entry(frame, "最少样本数", self.downstream_min_samples, 8, "默认 2")
        self._entry(frame, "Padj 阈值", self.downstream_padj, 9, "默认 0.05")
        self._entry(frame, "|log2FC| 阈值", self.downstream_abs_log2fc, 10, "默认 1.0")
        ttk.Checkbutton(frame, text="GO ORA", variable=self.downstream_go_ora).grid(row=11, column=1, sticky="w", pady=3)
        ttk.Checkbutton(frame, text="KEGG/离线路径 ORA", variable=self.downstream_kegg_ora).grid(row=11, column=2, sticky="w", pady=3)
        ttk.Checkbutton(frame, text="GSEA", variable=self.downstream_gsea).grid(row=12, column=1, sticky="w", pady=3)
        self._combo(frame, "富集物种", self.downstream_organism, ["", "human", "mouse"], 13)
        ttk.Checkbutton(frame, text="使用本地 .gmt 文本基因集", variable=self.downstream_gmt_enabled).grid(row=14, column=1, sticky="w", pady=3)
        self._entry(frame, "GMT 文件", self.downstream_gmt_path, 15, "选填，如 D:/sets/hallmark.gmt")
        ttk.Button(frame, text="选择 GMT", command=self.choose_downstream_gmt).grid(row=15, column=2, sticky="w", padx=(8, 0))
        self._entry(frame, "GMT SHA-256", self.downstream_gmt_sha256, 16, "选择文件后自动计算")
        self._entry(frame, "下游 Apptainer 镜像", self.downstream_runtime_image, 17, "必填，如 /shared/containers/rnaseq-bioconductor.sif")
        self._entry(frame, "镜像 SHA-256", self.downstream_runtime_sha256, 18, "必填，64 位十六进制摘要")
        ttk.Button(frame, text="审阅并应用下游配置", command=self.apply_downstream_configuration).grid(
            row=19, column=1, sticky="w", pady=(10, 5)
        )
        ttk.Label(frame, text="审阅摘要（点击“审阅并应用”前不会保存或连接服务器）").grid(
            row=20, column=0, columnspan=3, sticky="w", pady=(8, 2)
        )
        self.downstream_summary = Text(frame, height=8, width=92, state="disabled", wrap="word")
        self.downstream_summary.grid(row=21, column=0, columnspan=3, sticky="nsew")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(3, weight=1)
        frame.rowconfigure(6, weight=1)
        frame.rowconfigure(21, weight=1)
        self._refresh_downstream_summary()
        return frame

    def _notification_tab(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        ttk.Checkbutton(frame, text="启用邮件通知", variable=self.email_enabled).grid(row=0, column=1, sticky="w", pady=5)
        self._entry(frame, "接收邮箱", self.recipient, 1)
        self._entry(frame, "SMTP 服务器", self.smtp_host, 2)
        self._entry(frame, "SMTP 端口", self.smtp_port, 3)
        self._entry(frame, "SMTP 用户名", self.smtp_user, 4)
        self._entry(frame, "SMTP 密码环境变量名", self.smtp_password_env, 5)
        ttk.Label(frame, text="密码/授权码不写入配置文件，请放在环境变量中。").grid(row=6, column=1, sticky="w")
        return frame

    def _model_tab(self, parent: ttk.Frame) -> ttk.Frame:
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
            text="进入 SYSU_Agent 引导",
            command=self.start_onboarding,
        ).pack(side="left", padx=(8, 0))
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

    def _chat_tab(self, parent: ttk.Frame) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=16)
        ttk.Label(
            frame,
            text="在引导阶段，Agent 每次只收集一个字段；建议须点击“应用引导填写”后才会写入表单。",
            wraplength=850,
        ).pack(anchor="w", pady=(0, 8))

        self.chat_text = Text(
            frame,
            height=24,
            width=100,
            state="disabled",
            wrap="word",
            background="#0f172a",
            foreground="#e5e7eb",
            insertbackground="#f8fafc",
            relief="flat",
            padx=12,
            pady=12,
        )
        self.chat_text.pack(fill="both", expand=True)

        input_row = ttk.Frame(frame)
        input_row.pack(fill="x", pady=(8, 0))
        entry = ttk.Entry(input_row, textvariable=self.chat_input)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _event: self.send_chat_message())
        ttk.Button(input_row, text="发送", command=self.send_chat_message).pack(side="left", padx=(8, 0))
        ttk.Button(input_row, text="应用引导填写", command=self.apply_pending_onboarding_proposals).pack(side="left", padx=(8, 0))
        ttk.Button(input_row, text="丢弃引导填写", command=self.discard_pending_onboarding_proposals).pack(side="left", padx=(8, 0))
        ttk.Button(input_row, text="应用其他建议", command=self.apply_pending_proposals).pack(side="left", padx=(8, 0))
        ttk.Button(input_row, text="丢弃建议", command=self.discard_pending_proposals).pack(side="left", padx=(8, 0))
        ttk.Button(input_row, text="清空", command=self.clear_chat).pack(side="left", padx=(8, 0))

        self._append_chat(
            "SYSU_Agent",
            "您好！我是您的 SYSU_Agent。请先在“1. 模型设置”配置并应用模型，然后点击“进入 SYSU_Agent 引导”。我只会提出待确认的表单填写建议，不会自动保存、上传或连接集群。",
        )
        return frame

    def start_onboarding(self) -> None:
        client = self.apply_model_settings(show_message=False)
        if client is None:
            self._append_chat("SYSU_Agent", "模型尚未配置完成。你也可以点击后继续使用本地引导，但不会调用模型提取内容。")
        self.onboarding_active = True
        self.onboarding_field_index = 0
        self.onboarding_turn += 1
        self.pending_onboarding_proposals = ()
        self._show_page("agent")
        self._append_chat("SYSU_Agent", "您好！我是您的 SYSU_Agent。")
        self._ask_onboarding_question()

    def _ask_onboarding_question(self) -> None:
        if self.onboarding_field_index >= len(self.ONBOARDING_FIELDS):
            self.onboarding_active = False
            self._append_chat("SYSU_Agent", "基础信息已收集完成。请检查各标签页的表单；需要保存、上传或提交时，请使用底部按钮并逐次确认。")
            return
        _, question = self.ONBOARDING_FIELDS[self.onboarding_field_index]
        self._append_chat("SYSU_Agent", question)

    def _current_onboarding_field(self) -> str:
        return self.ONBOARDING_FIELDS[self.onboarding_field_index][0]

    def _handle_local_onboarding_skip(self, message: str) -> bool:
        if message.strip().lower() not in {
            "跳过",
            "跳过这一项",
            "跳过这项",
            "我自己填",
            "跳过这一项我自己填",
            "稍后填写",
        }:
            return False
        self.pending_onboarding_proposals = ()
        self.onboarding_field_index += 1
        self.onboarding_turn += 1
        self._append_chat("SYSU_Agent", "已跳过当前字段，当前字段未修改；你可以稍后在表单中自行填写。")
        self._ask_onboarding_question()
        return True

    def _submit_onboarding_message(self, message: str) -> None:
        field = self._current_onboarding_field()
        client = load_llm_client_from_env()
        if not isinstance(client, OpenAICompatibleClient):
            self._append_chat("SYSU_Agent", "当前未使用兼容 API 模型。请根据问题直接填写，或在模型设置完成后重试。")
            return
        self.onboarding_turn += 1
        turn = self.onboarding_turn
        self._append_chat("SYSU_Agent", "正在整理为待确认的表单填写建议……")
        threading.Thread(
            target=self._onboarding_llm_worker,
            args=(client, message, field, turn),
            daemon=True,
        ).start()

    def _onboarding_llm_worker(
        self,
        client: OpenAICompatibleClient,
        message: str,
        field: str,
        turn: int,
    ) -> None:
        try:
            decision = client.decide_onboarding(message, current_field=field)
        except Exception as exc:
            self.after(0, lambda error=str(exc): self._append_chat("SYSU_Agent", f"无法提取填写建议：{error}"))
            return
        self.after(0, lambda result=decision, expected=turn: self._receive_onboarding_decision(result, expected))

    def _receive_onboarding_decision(self, decision: OnboardingDecision, turn: int) -> None:
        if not self.onboarding_active or turn != self.onboarding_turn:
            return
        if not decision.proposals:
            self._append_chat("SYSU_Agent", decision.message or "我还不能确定该字段，请按问题补充说明。")
            return
        self.pending_onboarding_proposals = decision.proposals
        self._append_chat("SYSU_Agent", decision.message or "请确认以下填写建议。")
        self._append_chat("SYSU_Agent", self._onboarding_proposal_preview())
        self._append_chat("SYSU_Agent", "点击“应用引导填写”后才会修改当前表单；不会保存或连接服务器。")

    def _onboarding_proposal_preview(self) -> str:
        return "\n".join(["待确认的引导填写：", *[
            f"- {patch.field} → {patch.value!r}" for patch in self.pending_onboarding_proposals
        ]])

    def apply_pending_onboarding_proposals(self) -> None:
        if not self.pending_onboarding_proposals:
            self._append_chat("SYSU_Agent", "当前没有待确认的引导填写。")
            return
        if not messagebox.askyesno("应用引导填写", "只修改当前表单，不会保存、读取文件或连接服务器。是否应用？"):
            return
        try:
            candidate = apply_onboarding_proposals(self.build_config(), self.pending_onboarding_proposals)
        except Exception as exc:
            self._append_chat("SYSU_Agent", f"无法应用引导填写：{exc}")
            return
        self._apply_loaded_config(candidate)
        self.pending_onboarding_proposals = ()
        self.onboarding_field_index += 1
        self.onboarding_turn += 1
        self._append_chat("SYSU_Agent", "已写入当前表单，尚未保存。")
        self._ask_onboarding_question()

    def discard_pending_onboarding_proposals(self) -> None:
        self.pending_onboarding_proposals = ()
        self._append_chat("SYSU_Agent", "已丢弃该建议，请重新说明。")

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

    def open_project_config(self) -> None:
        selected = filedialog.askopenfilename(
            title="打开已保存项目",
            filetypes=[("Project JSON", "project.json"), ("JSON files", "*.json")],
        )
        if not selected:
            return
        try:
            config = load_project_config(Path(selected))
        except Exception as exc:
            messagebox.showerror("打开项目失败", str(exc))
            return
        self._apply_loaded_config(config)
        self.current_config_path = Path(selected)
        self.loaded_config = deepcopy(config)
        self.pending_proposals = ()
        self.status_text.set(f"已打开项目：{self.current_config_path}")
        messagebox.showinfo("打开成功", f"已载入项目：\n{self.current_config_path}")

    def _set_text(self, widget: Text, value: str) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", value)

    def _apply_loaded_config(self, config: dict[str, Any]) -> None:
        previous_host = self.server_host.get().strip()
        previous_user = self.server_user.get().strip()
        if previous_host and previous_user:
            clear_ssh_credential(previous_host, previous_user)

        project = config["project"]
        server = config["server"]
        sequencing = config.get("sequencing", {})
        samples = config.get("samples", {})
        notification = config.get("notification", {})
        self.project_id.set(str(project.get("id", "")))
        self.project_title.set(str(project.get("title", project.get("id", ""))))
        self.owner.set(str(project.get("owner", "local_user")))
        self.server_profile.set(str(server.get("profile", "")))
        self.server_host.set(str(server.get("host", "")))
        self.server_user.set(str(server.get("user", "")))
        self.remote_base_dir.set(str(server.get("remote_base_dir", "")))
        self.scheduler.set(str(server.get("scheduler", "slurm")))
        self.threads.set(str(server.get("threads", 16)))
        self.memory_gb.set(str(server.get("memory_gb", 64)))
        self.ssh_auth_mode.set(str(server.get("auth_mode", "key")))
        self.ssh_key_path.set(str(server.get("key_path", "")))
        self._set_text(self.init_text, "\n".join(server.get("init_commands", [])))
        self.layout.set(str(sequencing.get("layout", "paired")))
        self.reads_per_sample_million.set(str(sequencing.get("reads_per_sample_million", 40)))
        self.strandedness.set(str(sequencing.get("strandedness", "auto")))
        self.local_data_dir.set(str(samples.get("local_data_dir", "")))
        self.remote_data_dir.set(str(samples.get("remote_data_dir", "AUTO")))
        rows = [
            ",".join(str(item.get(key, "")) for key in ("sample_id", "condition", "fastq_1", "fastq_2"))
            for item in samples.get("items", [])
        ]
        self._set_text(self.sample_text, "\n".join(rows))
        for key, variable in self.ref_vars.items():
            variable.set(str(config.get("reference", {}).get(key, "")))
        for step, variable in self.pipeline_vars.items():
            variable.set(bool(config.get("pipeline", {}).get(step, {}).get("enabled", False)))
        if "downstream_enabled" in self.__dict__:
            self._apply_loaded_downstream_config(config.get("downstream", {}))
        polling = config.get("polling", {})
        self.poll_interval.set(str(polling.get("interval_seconds", 300)))
        self.poll_timeout.set(str(polling.get("timeout_hours", 168)))
        self.email_enabled.set(bool(notification.get("email_enabled", False)))
        self.recipient.set(str(notification.get("recipient", "")))
        self.smtp_host.set(str(notification.get("smtp_host", "")))
        self.smtp_port.set(str(notification.get("smtp_port", 587)))
        self.smtp_user.set(str(notification.get("smtp_user", "")))
        self.smtp_password_env.set(str(notification.get("password_env", "")))
        if self.ssh_auth_mode.get() == "password":
            self.ssh_status.set("已载入服务器设置；SSH 密码不会保存，请在本次会话中重新输入。")
        else:
            self.ssh_status.set("已载入服务器设置；尚未测试服务器连接。")

    def _apply_loaded_downstream_config(self, downstream: dict[str, Any]) -> None:
        enrichment = downstream.get("enrichment", {})
        gmt = enrichment.get("gmt", {})
        design = downstream.get("design", {})
        filtering = downstream.get("filtering", {})
        de = downstream.get("differential_expression", {})
        runtime = downstream.get("runtime", {})
        self.downstream_enabled.set(bool(downstream.get("enabled", False)))
        self.downstream_use_batch.set(bool(design.get("batch_column")))
        self.downstream_min_count.set(str(filtering.get("min_count", 10)))
        self.downstream_min_samples.set(str(filtering.get("min_samples", 2)))
        self.downstream_padj.set(str(de.get("padj_threshold", 0.05)))
        self.downstream_abs_log2fc.set(str(de.get("abs_log2_fold_change", 1.0)))
        self.downstream_go_ora.set(bool(enrichment.get("go_ora", True)))
        self.downstream_kegg_ora.set(bool(enrichment.get("kegg_ora", True)))
        self.downstream_gsea.set(bool(enrichment.get("gsea", True)))
        self.downstream_organism.set(str(enrichment.get("organism", "")))
        self.downstream_gmt_enabled.set(bool(gmt.get("enabled", False)))
        self.downstream_gmt_path.set(str(gmt.get("source_path", "")))
        self.downstream_gmt_sha256.set(str(gmt.get("sha256", "")))
        self.downstream_runtime_image.set(str(runtime.get("image_path", "")))
        self.downstream_runtime_sha256.set(str(runtime.get("image_sha256", "")))
        metadata_rows = [
            ",".join(str(item.get(key, "")) for key in ("sample_id", "condition", "batch"))
            for item in downstream.get("metadata", {}).get("samples", [])
            if isinstance(item, dict)
        ]
        contrast_rows = [
            ",".join(str(item.get(key, "")) for key in ("id", "numerator", "denominator"))
            for item in downstream.get("contrasts", [])
            if isinstance(item, dict)
        ]
        self._set_text(self.downstream_metadata_text, "\n".join(metadata_rows))
        self._set_text(self.downstream_contrasts_text, "\n".join(contrast_rows))
        self._refresh_downstream_summary()

    def _merge_config(self, base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        merged = deepcopy(base)
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._merge_config(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _config_save_path(self, config: dict[str, Any]) -> Path:
        if self.current_config_path is None:
            return self.output_dir / config["project"]["id"] / "project.json"
        loaded_id = (self.loaded_config or {}).get("project", {}).get("id")
        if loaded_id and config["project"]["id"] != loaded_id:
            raise ValueError("当前表单的项目 ID 已变更。为避免覆盖已打开的 project.json，请重新打开或新建项目。")
        return self.current_config_path

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
            "downstream": self._downstream_config(),
            "polling": {"interval_seconds": int(self.poll_interval.get()), "timeout_hours": int(self.poll_timeout.get())},
            "notification": self._notification_config(),
            "status": {"state": "configured", "message": "Project config created by GUI mode."},
        }
        if self.loaded_config is not None:
            config.pop("created_at", None)
            config.pop("status", None)
        config = self._merge_config(self.loaded_config or {}, config)
        config = normalize_config(config)
        runtime = estimate_runtime(config)
        config["runtime_estimate"] = {"hours": runtime.hours, "summary": runtime.summary}
        return config

    def collect_reference(self) -> dict[str, Any]:
        return {
            key: variable.get().strip()
            for key, variable in self.ref_vars.items()
        }

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

    def _parse_downstream_rows(self, widget: Text, expected_columns: int, label: str) -> list[list[str]]:
        rows: list[list[str]] = []
        for line_number, raw_line in enumerate(widget.get("1.0", "end").splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            values = [value.strip() for value in line.split(",")]
            if len(values) != expected_columns:
                raise ValueError(f"{label}第 {line_number} 行必须包含 {expected_columns} 列，以英文逗号分隔。")
            rows.append(values)
        return rows

    def _downstream_config(self) -> dict[str, Any]:
        metadata = [
            {"sample_id": sample_id, "condition": condition, "batch": batch}
            for sample_id, condition, batch in self._parse_downstream_rows(
                self.downstream_metadata_text, 3, "下游样本元数据"
            )
        ]
        contrasts = [
            {
                "id": contrast_id,
                "factor": "condition",
                "numerator": numerator,
                "denominator": denominator,
            }
            for contrast_id, numerator, denominator in self._parse_downstream_rows(
                self.downstream_contrasts_text, 3, "下游对比"
            )
        ]
        gmt_path = self.downstream_gmt_path.get().strip()
        return {
            "enabled": bool(self.downstream_enabled.get()),
            "profile_id": "bulk_rnaseq_deseq2_v1",
            "input": {"kind": "featurecounts_raw_counts", "path": "featurecounts/gene_counts.txt"},
            "metadata": {"samples": metadata},
            "design": {
                "condition_column": "condition",
                "batch_column": "batch" if self.downstream_use_batch.get() else "",
                "formula": "~ batch + condition" if self.downstream_use_batch.get() else "~ condition",
            },
            "contrasts": contrasts,
            "filtering": {
                "min_count": int(self.downstream_min_count.get()),
                "min_samples": int(self.downstream_min_samples.get()),
            },
            "differential_expression": {
                "padj_threshold": float(self.downstream_padj.get()),
                "abs_log2_fold_change": float(self.downstream_abs_log2fc.get()),
            },
            "enrichment": {
                "enabled": bool(self.downstream_go_ora.get() or self.downstream_kegg_ora.get() or self.downstream_gsea.get()),
                "go_ora": bool(self.downstream_go_ora.get()),
                "kegg_ora": bool(self.downstream_kegg_ora.get()),
                "gsea": bool(self.downstream_gsea.get()),
                "id_type": "ENSEMBL",
                "organism": self.downstream_organism.get().strip(),
                "gmt": {
                    "enabled": bool(self.downstream_gmt_enabled.get()),
                    "source_filename": Path(gmt_path).name if gmt_path else "",
                    "source_path": gmt_path,
                    "sha256": self.downstream_gmt_sha256.get().strip(),
                },
            },
            "runtime": {
                "environment_kind": "apptainer",
                "image_path": self.downstream_runtime_image.get().strip(),
                "image_sha256": self.downstream_runtime_sha256.get().strip(),
                "rscript_path": "Rscript",
            },
        }

    def _downstream_review_text(self) -> str:
        try:
            downstream = self._downstream_config()
        except (TypeError, ValueError) as exc:
            return f"请先修正下游表单：{exc}"
        if not downstream["enabled"]:
            return "下游分析未启用：本次只运行已勾选的上游流程。"
        metadata = downstream["metadata"]["samples"]
        contrasts = downstream["contrasts"]
        design = downstream["design"]["formula"]
        filtering = downstream["filtering"]
        de = downstream["differential_expression"]
        enrichment = downstream["enrichment"]
        runtime = downstream["runtime"]
        lines = [
            "固定输入：featurecounts/gene_counts.txt 原始计数",
            f"设计：{design}；样本：" + (", ".join(f"{row['sample_id']}={row['condition']}" for row in metadata) or "未填写"),
            "对比：" + (", ".join(f"{row['id']}: {row['numerator']} / {row['denominator']}" for row in contrasts) or "未填写"),
            f"过滤：count ≥ {filtering['min_count']}，至少 {filtering['min_samples']} 个样本；DE：padj ≤ {de['padj_threshold']}，|log2FC| ≥ {de['abs_log2_fold_change']}",
            f"富集：GO={enrichment['go_ora']}，KEGG/离线路径={enrichment['kegg_ora']}，GSEA={enrichment['gsea']}；物种={enrichment['organism'] or '未填写'}；ID=ENSEMBL",
            f"GMT：{enrichment['gmt']['source_filename'] or '未使用'}；SHA-256={enrichment['gmt']['sha256'] or '未填写'}",
            f"不可变运行时：{runtime['image_path'] or '未填写'}；SHA-256={runtime['image_sha256'] or '未填写'}",
            "执行方式：作为同一个已确认的调度任务中的 featureCounts 后续步骤运行；不会在登录节点直接运行。",
        ]
        return "\n".join(lines)

    def _refresh_downstream_summary(self) -> None:
        if "downstream_summary" not in self.__dict__:
            return
        self.downstream_summary.configure(state="normal")
        self._set_text(self.downstream_summary, self._downstream_review_text())
        self.downstream_summary.configure(state="disabled")

    def choose_downstream_gmt(self) -> None:
        selected = filedialog.askopenfilename(title="选择本地 GMT 文本基因集", filetypes=[("GMT files", "*.gmt")])
        if not selected:
            return
        path = Path(selected)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            messagebox.showerror("无法读取 GMT", str(exc))
            return
        self.downstream_gmt_path.set(str(path))
        self.downstream_gmt_sha256.set(digest)
        self.downstream_gmt_enabled.set(True)
        self._refresh_downstream_summary()

    def apply_downstream_configuration(self) -> None:
        review = self._downstream_review_text()
        if not messagebox.askyesno(
            "应用下游配置",
            review + "\n\n确认后仅应用到当前表单；不会保存、上传、预检、连接服务器或提交任务。是否继续？",
        ):
            return
        try:
            config = self.build_config()
        except Exception as exc:
            messagebox.showerror("下游配置错误", str(exc))
            return
        errors = downstream_errors(config)
        if errors:
            messagebox.showwarning("下游配置未通过", "\n".join(errors))
            return
        self.status_text.set("下游配置已审阅并应用到当前表单；请保存或在提交前再次确认。")
        self._refresh_downstream_summary()

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

    def choose_text_attachment(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择本地文本元数据",
            filetypes=[
                ("文本元数据", "*.txt *.csv *.tsv *.json *.yaml *.yml *.md"),
                ("所有文件", "*.*"),
            ],
        )
        if not selected:
            return
        try:
            attachment = load_text_attachment(Path(selected))
        except AttachmentError as exc:
            messagebox.showerror("无法添加元数据", str(exc))
            return
        self.text_attachment = attachment
        self.attachment_status.set(
            f"已在本地加载 {attachment.name}（{attachment.size_bytes} bytes）；未保存或发送。"
        )
        self._stage_reference_candidates(
            infer_reference_candidates(attachment.text),
            unsupported_assembly=has_unsupported_assembly(attachment.text),
        )

    def remove_text_attachment(self) -> None:
        self.text_attachment = None
        self.pending_reference_candidates = ()
        self.awaiting_species_answer = False
        self.attachment_status.set("尚未添加本地文本元数据。")
        self._render_reference_candidates()

    def _stage_reference_candidates(
        self,
        candidates: tuple[ReferenceCandidate, ...],
        *,
        unsupported_assembly: bool = False,
    ) -> None:
        self.pending_reference_candidates = candidates
        self._render_reference_candidates()
        if candidates:
            self.awaiting_species_answer = False
            self._append_chat(
                "SYSU_Agent",
                "已在本地从元数据推断出参考候选。请在“参考”页核对后，点击“应用待确认参考”。",
            )
            return
        self.awaiting_species_answer = not unsupported_assembly
        if unsupported_assembly:
            self._append_chat(
                "SYSU_Agent",
                "本地元数据识别到 mm10/GRCm38，但当前批准目录没有匹配参考；不会改用 GRCm39。请提供匹配的已批准参考后再继续。",
            )
            return
        self._append_chat(
            "SYSU_Agent",
            "本地元数据无法确定唯一物种。请直接回复“人类”或“小鼠”；这不会发送给模型。",
        )

    def _render_reference_candidates(self) -> None:
        if self.reference_candidate_text is None:
            return
        lines = ["当前没有待确认参考候选。"]
        if self.pending_reference_candidates:
            lines = ["以下候选仅待确认，尚未写入表单："]
            for candidate in self.pending_reference_candidates:
                entry = candidate.entry
                evidence = "、".join(candidate.evidence)
                lines.append(
                    f"- {entry.catalog_id}: {entry.species}, {entry.assembly} "
                    f"（{candidate.confidence}；证据：{evidence}）"
                )
        self.reference_candidate_text.configure(state="normal")
        self.reference_candidate_text.delete("1.0", "end")
        self.reference_candidate_text.insert("1.0", "\n".join(lines))
        self.reference_candidate_text.configure(state="disabled")

    def _handle_local_species_answer(self, message: str) -> bool:
        if not self.awaiting_species_answer:
            return False
        species = normalize_explicit_species(message)
        if species is None:
            self._append_chat(
                "SYSU_Agent",
                "请仅回复“人类”或“小鼠”。该回答只在本地用于选择参考候选。",
            )
            return True
        self._stage_reference_candidates(candidates_for_species(species))
        return True

    def apply_pending_reference_catalog(self) -> None:
        if not self.pending_reference_candidates:
            self._append_chat("SYSU_Agent", "当前没有待确认参考候选。")
            return
        if len(self.pending_reference_candidates) != 1:
            self._append_chat(
                "SYSU_Agent",
                "当前有多个参考候选；请先在后续目录版本中明确选择一个组装版本。",
            )
            return
        candidate = self.pending_reference_candidates[0]
        entry = candidate.entry
        if not messagebox.askyesno(
            "应用参考目录",
            f"将把已批准目录 {entry.catalog_id} 的字段填入当前参考表单。\n\n"
            "这不会保存、连接服务器、下载参考、上传 FASTQ 或提交任务。是否应用？",
        ):
            return
        for key, value in catalog_reference_fields(entry.catalog_id).items():
            if key in self.ref_vars:
                self.ref_vars[key].set(value)
        self.reference_index_state = "unconfigured"
        if "index_state" in self.ref_vars:
            self.ref_vars["index_state"].set(self.reference_index_state)
        self.pending_reference_candidates = ()
        self.awaiting_species_answer = False
        self.reference_index_state = "unconfigured"
        self._render_reference_candidates()
        self._append_chat("SYSU_Agent", "已填入当前参考表单，尚未保存。")

    def configure_reference_index(self) -> None:
        if not self.ref_vars["catalog_id"].get().strip():
            self._append_chat("SYSU_Agent", "请先确认参考身份，再配置与其匹配的索引。")
            return
        has_existing = messagebox.askyesno(
            "配置参考索引",
            "你是否已有与当前参考身份匹配、可用的指定 STAR/RSEM 索引？\n\n"
            "选择“是”只允许你手动填写路径，随后仍需可选的只读预检；不会连接服务器。",
        )
        if has_existing:
            self.reference_index_state = "existing_pending_preflight"
            self.ref_vars["index_state"].set(self.reference_index_state)
            self._append_chat(
                "SYSU_Agent",
                "请手动填写已有的 STAR/RSEM index 路径及所需 GTF/FASTA 路径。"
                "当前仅标记为待只读预检验证，尚未保存、连接或运行。",
            )
            return
        build_plan = messagebox.askyesno(
            "尚未指定索引",
            "没有已有索引。是否只生成“未来自建索引计划”？\n\n"
            "选择“是”不会生成命令、下载文件、构建索引或提交任务；选择“否”可继续审阅公开候选搜索查询。",
        )
        if build_plan:
            self.reference_index_state = "build_plan_pending"
            self.ref_vars["index_state"].set(self.reference_index_state)
            self._append_chat(
                "SYSU_Agent",
                "已记录待确认的自建索引计划：需要匹配的参考 FASTA/GTF、STAR/RSEM 工具与计算资源。"
                "这只是计划，不会生成命令、下载、构建、连接服务器或提交任务。",
            )
            return
        self._confirm_public_reference_search()

    def _confirm_public_reference_search(self) -> None:
        from .llm_policy import ProposalError, build_reference_search_query

        enabled_tools = tuple(
            step for step, variable in self.pipeline_vars.items() if variable.get() and step in {"star", "rsem"}
        )
        try:
            query = build_reference_search_query(
                species=self.ref_vars["species"].get().strip(),
                assembly=self.ref_vars["assembly"].get().strip(),
                annotation_release=self.ref_vars["release"].get().strip(),
                layout=self.layout.get().strip(),
                enabled_tools=enabled_tools,
            )
        except ProposalError as exc:
            self._append_chat("SYSU_Agent", str(exc))
            return
        displayed = (
            "将仅发送以下公开信息以寻找官方参考/索引候选：\n"
            f"物种：{query['species']}\n组装：{query['assembly']}\n注释：{query['annotation_release']}\n"
            f"测序：{query['layout']} RNA-seq\n工具：{', '.join(query['enabled_tools'])}\n\n"
            "不会发送本地 metadata、附件、路径、项目/样本/服务器信息或凭据。"
        )
        if not messagebox.askyesno("确认公开候选查询", displayed + "\n\n确认后仅记录待搜索候选，不会下载、填入索引路径、连接或运行。是否继续？"):
            self._append_chat("SYSU_Agent", "已取消公开候选查询；未发送任何信息。")
            return
        self.reference_index_state = "search_candidate_pending"
        self.ref_vars["index_state"].set(self.reference_index_state)
        self._append_chat(
            "SYSU_Agent",
            "已确认公开候选查询范围。当前版本不会把候选自动变成运行路径；"
            "请在获取并核对官方候选后，手动提供已有部署索引或建立独立的构建计划。",
        )

    def send_chat_message(self) -> None:
        message = self.chat_input.get().strip()
        if not message:
            return
        self.chat_input.set("")
        self._append_chat("你", message)
        if self._handle_local_species_answer(message):
            return
        if self.onboarding_active:
            if self._handle_local_onboarding_skip(message):
                return
            if self.pending_onboarding_proposals:
                self._append_chat("SYSU_Agent", "请先应用或丢弃当前待确认填写建议。")
                return
            self._submit_onboarding_message(message)
            return
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
            self._start_local_validation(show_dialog=False)
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
            self._append_chat("Agent", "请使用底部“保存并运行”按钮，并在确认窗口中确认远程副作用。")
            return True
        if lower in {"后台运行", "仅提交", "run-nowait"}:
            self._append_chat("Agent", "请使用底部“仅提交”按钮，并在确认窗口中确认远程副作用。")
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
                config=self._llm_safe_config(),
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
        if decision.action == "answer":
            self._append_chat(
                "Agent",
                decision.message or "我可以解释 RNA-seq 工作流，但不会替你执行操作。",
            )
            return
        if decision.action == "config_patch_proposal":
            self.pending_proposals = decision.proposals
            self._append_chat("Agent", decision.message or "这里是一份待审阅的配置建议。")
            self._append_chat("Agent", self._proposal_preview())
            self._append_chat("Agent", "请点击“应用建议”仅修改当前表单，或点击“丢弃建议”。保存配置仍需单独点击“保存配置”。")
            return
        if decision.action == "contract_submission_request":
            self._request_llm_contract_submission(decision.message)
            return
        self._append_chat("Agent", "模型响应不符合受限策略，未执行任何操作。")

    def _request_llm_contract_submission(self, message: str) -> None:
        config_path = self.current_config_path
        if config_path is None:
            self._append_chat("Agent", "模型合同提交只能使用当前已保存并绑定的项目；不会自动保存表单。")
            return
        try:
            preview = inspect_llm_submission(config_path)
        except Exception as exc:
            self._append_chat("Agent", f"当前保存项目不能通过模型通道提交：{exc}")
            return
        if self.running:
            self._append_chat("Agent", "已有任务正在运行，请等待当前任务结束。")
            return
        confirmed = messagebox.askyesno(
            "确认合同提交",
            f"模型请求提交已验证合同：\n{preview.contract_id}\n\n"
            "这会连接服务器、上传 FASTQ、创建或更新远程文件，并提交调度任务。"
            "同一合同不能通过模型通道自动重试。是否继续？",
        )
        if not confirmed:
            self._append_chat("Agent", "已取消合同提交。")
            return
        if not self._prepare_server_credential(require_password=True):
            return
        self.running = True
        self.status_text.set(f"正在提交已验证合同 {preview.contract_id}……")
        threading.Thread(
            target=self._llm_contract_submission_worker,
            args=(config_path,),
            daemon=True,
        ).start()

    def _llm_contract_submission_worker(self, config_path: Path) -> None:
        try:
            outcome = submit_llm_contract(config_path)
        except Exception as exc:
            self.after(0, self._run_finished, False, str(exc), config_path)
            return
        self.after(0, self._run_finished, True, outcome.message, config_path)

    def _llm_safe_config(self) -> dict[str, Any] | None:
        try:
            return self.build_config()
        except Exception:
            return None

    def _proposal_preview(self) -> str:
        if not self.pending_proposals:
            return "当前没有待审阅的配置建议。"
        lines = ["待审阅的配置建议："]
        for patch in self.pending_proposals:
            reason = f"；原因：{patch.reason}" if patch.reason else ""
            lines.append(f"- {patch.path} → {patch.value!r}{reason}")
        return "\n".join(lines)

    def apply_pending_proposals(self) -> None:
        if not self.pending_proposals:
            self._append_chat("Agent", "当前没有待审阅的配置建议。")
            return
        if not messagebox.askyesno("应用配置建议", "仅修改当前表单，不会保存配置或连接服务器。是否应用？"):
            return
        try:
            candidate = apply_proposals(self.build_config(), self.pending_proposals)
        except Exception as exc:
            self._append_chat("Agent", f"无法应用配置建议：{exc}")
            return
        sequencing = candidate["sequencing"]
        self.layout.set(str(sequencing["layout"]))
        self.strandedness.set(str(sequencing["strandedness"]))
        self.reads_per_sample_million.set(str(sequencing["reads_per_sample_million"]))
        polling = candidate["polling"]
        self.poll_interval.set(str(polling["interval_seconds"]))
        self.poll_timeout.set(str(polling["timeout_hours"]))
        for step, variable in self.pipeline_vars.items():
            variable.set(bool(candidate["pipeline"][step]["enabled"]))
        self.pending_proposals = ()
        self._append_chat("Agent", "配置建议已应用到当前表单，尚未保存到 project.json。")

    def discard_pending_proposals(self) -> None:
        self.pending_proposals = ()
        self._append_chat("Agent", "已丢弃待审阅的配置建议。")

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
        config_path = self._config_save_path(config)
        save_json(config_path, config)
        self.current_config_path = config_path
        self.loaded_config = deepcopy(config)
        self.status_text.set(f"配置已保存：{config_path}")
        return config_path

    def validate_form(self) -> None:
        self._start_local_validation(show_dialog=True)

    def _start_local_validation(self, *, show_dialog: bool) -> None:
        if self.validation_in_progress:
            message = "本地 FASTQ 校验正在进行中，请等待完成。"
            if show_dialog:
                messagebox.showinfo("正在校验", message)
            else:
                self._append_chat("Agent", message)
            return
        try:
            config = self.build_config()
        except Exception as exc:
            if show_dialog:
                messagebox.showerror("配置错误", str(exc))
            else:
                self._append_chat("Agent", f"校验失败：{exc}")
            return
        self.validation_in_progress = True
        self.status_text.set("正在本地校验 FASTQ；较大的 .fastq.gz 文件可能需要数分钟。")
        threading.Thread(
            target=self._local_validation_worker,
            args=(config, show_dialog),
            daemon=True,
        ).start()

    def _local_validation_worker(self, config: dict[str, Any], show_dialog: bool) -> None:
        try:
            result = validate_local_fastqs(config)
        except Exception as exc:
            self.after(0, self._local_validation_finished, None, str(exc), show_dialog)
            return
        self.after(0, self._local_validation_finished, result, None, show_dialog)

    def _local_validation_finished(
        self,
        result: ValidationResult | None,
        error: str | None,
        show_dialog: bool,
    ) -> None:
        self.validation_in_progress = False
        if error is not None:
            self.status_text.set("本地 FASTQ 校验失败。")
            if show_dialog:
                messagebox.showerror("校验失败", error)
            else:
                self._append_chat("Agent", f"校验失败：{error}")
            return
        assert result is not None
        self.status_text.set("本地 FASTQ 校验通过。" if result.ok else "本地 FASTQ 校验未通过，请查看结果。")
        if not show_dialog:
            self._append_chat("Agent", "\n".join(validation_summary(result)))
            return
        self._show_validation_result(result)

    def _show_validation_result(self, result: Any) -> None:
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
        mode = "保存配置、上传 FASTQ 并提交分析任务" if wait else "保存配置、上传 FASTQ 并提交分析任务后返回"
        downstream_notice = ""
        if self.downstream_enabled.get():
            downstream_notice = (
                "\n\n本次还会在同一个调度任务的 featureCounts 后运行下游分析：\n"
                + self._downstream_review_text()
                + "\n下游阶段不会在登录节点直接运行，也不会另行提交嵌套任务。"
            )
        if not messagebox.askyesno(
            "确认运行",
            f"Agent 将{mode}。这会连接服务器、创建或更新远程文件，并可能在调度器中占用计算资源。"
            f"{downstream_notice}\n\n是否继续？",
        ):
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
        if not messagebox.askyesno(
            "确认刷新状态",
            "Agent 将连接服务器读取当前任务状态；如当前配置尚未保存，会先保存配置。是否继续？",
        ):
            return
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
