from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .actions import (
    explain_project,
    generate_project_report,
    load_project_config,
    prepare_project_config,
    project_config_path,
    refresh_project_status_action,
    run_project_action,
    save_project_config,
    status_summary,
    summarize_project,
    validate_project,
    validation_summary,
)
from .defaults import DEFAULT_PIPELINE, DEFAULT_REFERENCE
from .llm import (
    CodexCLIClient,
    LLMDecision,
    LLMError,
    OpenAICompatibleClient,
    load_llm_client_from_env,
)
from .llm_policy import AgentStep, ConfigPatch, apply_proposals
from .llm_submission import inspect_llm_submission, submit_llm_contract
from .storage import load_defaults, save_defaults


class ChatSession:
    def __init__(
        self,
        output_dir: Path,
        llm: OpenAICompatibleClient | CodexCLIClient | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.current_config_path: Path | None = None
        self.pending_proposals: tuple[ConfigPatch, ...] = ()
        self.pending_config: dict[str, Any] | None = None
        self.llm = llm if llm is not None else load_llm_client_from_env()

    def run(self) -> Path:
        self.say("你好，我是 SYSU RNA-seq Agent。")
        self.say("我现在支持项目会话：先新建或打开项目，再继续校验、运行、查状态、生成报告和查看结果解释。")
        self.say("可用命令：new, open <path>, validate, run, run-nowait, status, report, explain, summary, where, proposal, apply proposal, save proposal, discard proposal, help, quit")
        if self.llm is None:
            self.say("模型状态：未配置，当前使用本地规则路由。")
        else:
            self.say(f"模型状态：已连接 {self.llm.resolved_api_mode}，模型为 {self.llm.model}。")
        self.say("")

        while True:
            try:
                command = input("chat> ").strip()
            except EOFError:
                self.say("")
                self.say("输入已结束，退出当前会话。")
                return self.current_config_path or self.output_dir
            if not command:
                continue
            if self.dispatch_command(command):
                continue
            if self.dispatch_llm_command(command):
                continue
            self.say("未识别的命令。你也可以直接说“打开项目”“生成报告”“看看状态”“解释结果”。")

    def dispatch_command(self, command: str) -> bool:
        lower = command.lower().strip()
        if lower in {"proposal", "review proposal", "查看建议", "查看配置建议"}:
            self.show_pending_proposals()
            return True
        if lower in {"discard proposal", "discard", "丢弃建议", "取消建议"}:
            self.pending_proposals = ()
            self.say("已丢弃待审阅的配置建议。")
            return True
        if lower in {"apply proposal", "apply", "应用建议", "应用配置建议"}:
            self.apply_pending_proposals()
            return True
        if lower in {"save proposal", "保存建议", "保存配置建议"}:
            self.save_pending_config()
            return True
        if lower in {"quit", "exit", "退出"}:
            self.say("已退出。")
            raise SystemExit(0)
        if lower in {"help", "?", "帮助", "help me"}:
            self.show_help()
            return True
        if lower in {"new", "create", "setup", "新建", "新建项目", "创建项目", "开始配置", "开始新建"}:
            self.create_project()
            return True
        if lower.startswith("open "):
            self.open_project(command[5:].strip())
            return True
        if lower.startswith("打开项目"):
            self.open_project(command.replace("打开项目", "", 1).strip())
            return True
        if lower.startswith("打开 "):
            self.open_project(command[3:].strip())
            return True
        if lower in {"validate", "校验", "检查配置", "检查项目", "验证配置", "验证项目"}:
            self.validate_current_project()
            return True
        if lower in {"run", "运行", "开始运行", "执行项目", "跑起来"}:
            self.run_current_project(wait=True)
            return True
        if lower in {"run-nowait", "提交运行", "先提交", "后台运行", "不要等待"}:
            self.run_current_project(wait=False)
            return True
        if lower in {"status", "状态", "查看状态", "看看状态", "刷新状态", "当前状态"}:
            self.show_current_status()
            return True
        if lower in {"report", "报告", "生成报告", "导出报告", "写报告"}:
            self.generate_current_report()
            return True
        if lower in {"explain", "解释", "解释结果", "怎么看结果", "结果说明", "如何解读"}:
            self.explain_current_results()
            return True
        if lower in {"summary", "摘要", "项目摘要", "总结一下"}:
            self.show_current_summary()
            return True
        if lower in {"where", "路径", "项目路径", "当前项目在哪", "当前文件在哪"}:
            self.show_current_location()
            return True
        if lower in {"model", "模型", "模型状态", "llm"}:
            self.show_model_status()
            return True
        return False

    def dispatch_llm_command(self, command: str) -> bool:
        if self.llm is None:
            return False
        config: dict[str, Any] | None = None
        if self.current_config_path is not None:
            try:
                config = load_project_config(self.current_config_path)
            except Exception:
                config = None
        try:
            decision = self.llm.decide(
                command,
                has_project=self.current_config_path is not None,
                config=config,
            )
        except LLMError as exc:
            self.say(f"模型理解失败，已停止本次动作：{exc}")
            return True
        return self.execute_llm_decision(decision)

    def execute_llm_decision(self, decision: LLMDecision) -> bool:
        if decision.action == "answer":
            self.say(decision.message or "我可以解释当前 RNA-seq 工作流与结果，但不会代替你执行操作。")
            return True
        if decision.action == "config_patch_proposal":
            self.pending_proposals = decision.proposals
            self.say(decision.message or "这里是一份待审阅的配置建议。")
            self.show_pending_proposals()
            self.say("输入 apply proposal 可仅在内存中应用；输入 discard proposal 可丢弃。保存仍需使用明确的保存操作。")
            return True
        if decision.action == "agent_plan":
            self.execute_agent_plan(decision)
            return True
        if decision.action == "contract_submission_request":
            self.submit_llm_contract_request(decision.message)
            return True
        self.say("模型响应不符合受限策略，未执行任何操作。")
        return True

    def execute_agent_plan(self, decision: LLMDecision) -> None:
        if not decision.steps:
            self.say("模型响应不符合受限策略，未执行任何操作。")
            return
        if self.require_current_project() is None:
            return
        self.say(decision.message or "将按本地受限步骤执行；远程操作仍需你确认。")
        for step in decision.steps:
            if not self.execute_agent_step(step):
                return

    def execute_agent_step(self, step: AgentStep) -> bool:
        if step.kind == "validate":
            self.validate_current_project()
            return True
        if step.kind == "summary":
            self.show_current_summary()
            return True
        if step.kind == "report":
            self.generate_current_report()
            return True
        if step.kind == "status":
            return self.show_current_status()
        if step.kind == "run":
            return self.run_current_project(wait=step.wait)
        if step.kind == "contract_submission":
            return self.submit_llm_contract_request("")
        self.say("模型响应不符合受限策略，未执行任何操作。")
        return False

    def submit_llm_contract_request(self, message: str) -> bool:
        config_path = self.require_current_project()
        if config_path is None:
            return False
        try:
            preview = inspect_llm_submission(config_path)
        except Exception as exc:
            self.say(f"当前保存项目不能通过模型通道提交：{exc}")
            return False
        self.say(message or "模型请求提交当前绑定的分析合同。")
        self.say(f"已在本地验证当前合同：{preview.contract_id}")
        if not self.ask_yes_no(
            f"是否确认提交已验证合同 {preview.contract_id}？",
            default=False,
            help_text=(
                "此操作会连接服务器、上传 FASTQ、创建或更新远程文件，并提交调度任务。"
                "模型不能重试同一合同。"
            ),
        ):
            self.say("已取消合同提交。")
            return False
        try:
            outcome = submit_llm_contract(config_path)
        except Exception as exc:
            self.say(f"合同提交失败：{exc}")
            return False
        self.say(outcome.message)
        self.say(f"项目目录：{outcome.project_dir}")
        return True

    def show_pending_proposals(self) -> None:
        if not self.pending_proposals:
            self.say("当前没有待审阅的配置建议。")
            return
        for patch in self.pending_proposals:
            reason = f"（{patch.reason}）" if patch.reason else ""
            self.say(f"- {patch.path}: {patch.value!r} {reason}")

    def apply_pending_proposals(self) -> None:
        if not self.pending_proposals:
            self.say("当前没有待审阅的配置建议。")
            return
        if self.current_config_path is None:
            self.say("请先通过直接命令新建或打开项目；模型建议不会新建项目。")
            return
        if not self.ask_yes_no("是否仅在当前会话内应用这些配置建议？", default=False):
            self.say("已取消应用建议。")
            return
        try:
            config = load_project_config(self.current_config_path)
            self.pending_config = apply_proposals(config, self.pending_proposals)
        except Exception as exc:
            self.say(f"无法在内存中应用配置建议：{exc}")
            return
        self.pending_proposals = ()
        self.say("配置建议已应用到当前会话内存，尚未写入 project.json。")

    def save_pending_config(self) -> None:
        if self.pending_config is None:
            self.say("当前没有已应用且待保存的配置建议。")
            return
        config_path = self.require_current_project()
        if config_path is None:
            return
        if not self.ask_yes_no(
            "是否将当前会话内的配置建议写入 project.json？",
            default=False,
            help_text="保存会覆盖当前项目配置，但不会连接服务器或提交任务。",
        ):
            self.say("已取消保存建议。")
            return
        try:
            saved_path, _, _ = save_project_config(self.output_dir, self.pending_config)
        except Exception as exc:
            self.say(f"保存配置建议失败：{exc}")
            return
        self.current_config_path = saved_path
        self.pending_config = None
        self.say(f"配置建议已保存：{saved_path}")

    def create_project(self) -> Path:
        self.say("")
        self.say("开始新建项目配置。")
        reference = self.collect_reference()
        project = self.collect_project()
        server = self.collect_server(project["id"])
        sequencing = self.collect_sequencing()
        samples = self.collect_samples(project["id"], server, sequencing)
        pipeline = self.collect_pipeline()
        polling = self.collect_polling()
        notification = self.collect_notification()

        config = {
            "schema_version": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "project": project,
            "server": server,
            "reference": reference,
            "sequencing": sequencing,
            "samples": samples,
            "pipeline": pipeline,
            "polling": polling,
            "notification": notification,
            "status": {
                "state": "configured",
                "message": "Project config created by chat mode.",
            },
        }

        preview = prepare_project_config(config)
        preview_path = project_config_path(self.output_dir, project["id"])
        self.show_summary_preview(preview, preview_path)
        if not self.ask_yes_no("以上配置是否确认保存？", default=True, help_text="选择 n 会退出且不写入 project.json。"):
            raise SystemExit("已取消保存。")

        config_path, _, runtime_summary = save_project_config(self.output_dir, config)
        self.current_config_path = config_path
        self.say("")
        self.say(f"配置已保存：{config_path}")
        self.say(runtime_summary)
        self.say("当前会话已绑定该项目。现在可直接输入 validate / run / status / report / explain。")
        self._show_validation_result(config_path)
        return config_path

    def open_project(self, raw_path: str) -> None:
        if not raw_path:
            self.say("请在 open 或“打开项目”后面提供 project.json 路径。")
            return
        config_path = Path(raw_path.strip().strip('"')).expanduser()
        if not config_path.exists():
            self.say(f"文件不存在：{config_path}")
            return
        try:
            config = load_project_config(config_path)
        except Exception as exc:
            self.say(f"打开项目失败：{exc}")
            return
        self.current_config_path = config_path
        self.say(f"已打开项目：{config_path}")
        self.say(f"当前项目：{config.get('project', {}).get('id', 'unknown')}")
        for line in status_summary(config.get("status", {})):
            self.say(line)

    def validate_current_project(self) -> None:
        config_path = self.require_current_project()
        if config_path is None:
            return
        self._show_validation_result(config_path)

    def run_current_project(self, *, wait: bool) -> bool:
        config_path = self.require_current_project()
        if config_path is None:
            return False
        mode = "上传 FASTQ 并提交远程分析任务" if wait else "上传 FASTQ 并提交远程分析任务后返回"
        if not self.ask_yes_no(
            f"是否确认{mode}？",
            default=False,
            help_text="此操作会连接服务器、创建或更新远程文件，并可能占用调度器计算资源。",
        ):
            self.say("已取消运行。")
            return False
        try:
            outcome = run_project_action(config_path, wait=wait)
        except Exception as exc:
            self.say(f"运行失败：{exc}")
            return False
        self.say(outcome.message)
        self.say(f"项目目录：{outcome.project_dir}")
        return True

    def show_current_status(self) -> bool:
        config_path = self.require_current_project()
        if config_path is None:
            return False
        if not self.ask_yes_no(
            "是否连接服务器刷新当前项目状态？",
            default=False,
            help_text="此操作会通过 SSH 查询远程调度器和项目状态。",
        ):
            self.say("已取消刷新状态。")
            return False
        try:
            status = refresh_project_status_action(config_path)
        except Exception as exc:
            self.say(f"刷新状态失败：{exc}")
            return False
        for line in status_summary(status):
            self.say(line)
        return True

    def generate_current_report(self) -> None:
        config_path = self.require_current_project()
        if config_path is None:
            return
        try:
            output = generate_project_report(config_path)
        except Exception as exc:
            self.say(f"生成报告失败：{exc}")
            return
        self.say(f"报告已生成：{output}")

    def explain_current_results(self) -> None:
        config_path = self.require_current_project()
        if config_path is None:
            return
        try:
            config = load_project_config(config_path)
        except Exception as exc:
            self.say(f"读取项目失败：{exc}")
            return

        for line in explain_project(config):
            self.say(line)

    def show_current_summary(self) -> None:
        config_path = self.require_current_project()
        if config_path is None:
            return
        try:
            config = load_project_config(config_path)
        except Exception as exc:
            self.say(f"读取项目失败：{exc}")
            return
        for line in summarize_project(config):
            self.say(line)

    def show_current_location(self) -> None:
        config_path = self.require_current_project()
        if config_path is None:
            return
        self.say(f"当前项目文件：{config_path}")

    def require_current_project(self) -> Path | None:
        if self.current_config_path is None:
            self.say("当前会话还没有绑定项目，请先输入 new 或 open <path>。")
            return None
        return self.current_config_path

    def show_help(self) -> None:
        self.say("可用命令：")
        self.say("- new / 新建项目：新建项目配置")
        self.say("- open <path> / 打开项目 <path>：打开已有 project.json")
        self.say("- validate / 校验 / 检查配置：校验当前项目的本地 FASTQ 和配置")
        self.say("- run / 运行：运行当前项目，并等待结束")
        self.say("- run-nowait / 后台运行：提交当前项目后立即返回")
        self.say("- status / 看看状态：刷新并显示当前项目状态")
        self.say("- report / 生成报告：为当前项目生成 Markdown 报告")
        self.say("- explain / 解释结果：显示模板化结果解读建议")
        self.say("- summary / 项目摘要：显示当前项目摘要")
        self.say("- where / 项目路径：显示当前绑定的 project.json 路径")
        self.say("- model / 模型状态：显示当前模型连接状态")
        self.say("- help / 帮助：显示帮助")
        self.say("- apply proposal / 应用建议：仅在会话内应用待审阅的配置建议")
        self.say("- save proposal / 保存建议：将已应用的建议写入当前项目")
        self.say("- discard proposal / 丢弃建议：丢弃待审阅的配置建议")
        self.say("- quit / 退出：退出")

    def show_model_status(self) -> None:
        if self.llm is None:
            self.say("当前未配置模型，agent 使用本地规则路由。")
            self.say("设置 RNASEQ_AGENT_LLM_BASE_URL 和 RNASEQ_AGENT_LLM_MODEL 后可启用模型。")
            return
        self.say(f"模型：{self.llm.model}")
        self.say(f"Base URL：{self.llm.base_url}")
        self.say(f"接口类型：{self.llm.resolved_api_mode}")
        self.say("执行策略：模型只能解释 RNA-seq 内容或提出待审阅的配置建议，不能执行操作。")

    def _show_validation_result(self, config_path: Path) -> None:
        try:
            _, validation = validate_project(config_path)
        except Exception as exc:
            self.say(f"校验失败：{exc}")
            return
        for line in validation_summary(validation):
            self.say(line)

    def collect_reference(self) -> dict[str, Any]:
        self.section(1, "固定参考配置")
        saved = load_defaults()
        if saved:
            reference = saved["reference"]
            self.say(f"已找到默认参考配置：{reference['name']}")
            if self.ask_yes_no("是否继续使用这份默认参考配置？", default=True):
                return reference

        self.say("默认使用 GENCODE Human Release 47 / GRCh38.p14 / ALL。")
        self.say(f"GTF: {DEFAULT_REFERENCE['gtf_url']}")
        self.say(f"FASTA: {DEFAULT_REFERENCE['genome_fasta_url']}")
        if self.ask_yes_no("是否使用默认参考配置？", default=True):
            save_defaults({"reference": DEFAULT_REFERENCE})
            return DEFAULT_REFERENCE.copy()

        reference = DEFAULT_REFERENCE.copy()
        reference["name"] = self.ask("参考配置名", reference["name"], "例如 GENCODE_R47_GRCh38p14_ALL")
        reference["remote_gtf_path"] = self.ask(
            "服务器上的 GTF 文件路径",
            reference["remote_gtf_path"],
            "例如 /data/ref/gencode/human/release_47_all/gencode.v47...annotation.gtf",
        )
        reference["remote_genome_fasta_path"] = self.ask(
            "服务器上的 genome FASTA 路径",
            reference["remote_genome_fasta_path"],
            "例如 /data/ref/gencode/human/release_47_all/GRCh38.p14.genome.fa",
        )
        reference["star_index_dir"] = self.ask("STAR index 目录", reference["star_index_dir"])
        reference["rsem_index_prefix"] = self.ask("RSEM index prefix", reference["rsem_index_prefix"])
        reference["arriba_blacklist_path"] = self.ask("Arriba blacklist 路径，可留空", reference["arriba_blacklist_path"])
        reference["arriba_known_fusions_path"] = self.ask(
            "Arriba known fusions 路径，可留空",
            reference["arriba_known_fusions_path"],
        )
        save_defaults({"reference": reference})
        return reference

    def collect_project(self) -> dict[str, str]:
        self.section(2, "项目基本信息")
        project_id = self.ask("项目 ID", f"rnaseq_{datetime.now():%Y%m%d_%H%M%S}", "建议只用英文、数字和下划线。")
        title = self.ask("项目名称", project_id, "可写中文，用于自己识别项目。")
        owner = self.ask("负责人/用户", "local_user")
        return {"id": project_id, "title": title, "owner": owner}

    def collect_server(self, project_id: str) -> dict[str, Any]:
        self.section(3, "服务器信息")
        profile = self.ask("服务器配置名", "sysu_hpc")
        host = self.ask("服务器地址 host", "your.server.edu", "例如 hpc.example.edu。")
        user = self.ask("服务器用户名", "username")
        remote_base_dir = self.ask(
            "服务器项目根目录",
            f"/data/users/{user}/rnaseq_projects",
            f"本项目会放到 <根目录>/{project_id}。",
        )
        scheduler = self.ask_choice("服务器调度器", ["slurm", "pbs", "local"], "slurm")
        threads = self.ask_int("每个任务线程数", 16)
        memory_gb = self.ask_int("内存 GB", 64)
        init_commands = self.ask_list(
            "服务器初始化命令",
            "",
            "如 module load STAR fastp subread rsem arriba；多个命令用 ; 分隔；没有就回车。",
        )
        return {
            "profile": profile,
            "host": host,
            "user": user,
            "remote_base_dir": remote_base_dir,
            "scheduler": scheduler,
            "threads": threads,
            "memory_gb": memory_gb,
            "shell": "bash",
            "init_commands": init_commands,
        }

    def collect_sequencing(self) -> dict[str, Any]:
        self.section(4, "测序信息")
        layout = self.ask_choice("测序类型", ["paired", "single"], "paired")
        reads = self.ask_int("每个样本 reads 数量估计，单位 M", 40)
        strandedness = self.ask_choice("链特异性", ["auto", "unstranded", "forward", "reverse"], "auto")
        return {
            "layout": layout,
            "reads_per_sample_million": reads,
            "strandedness": strandedness,
        }

    def collect_samples(self, project_id: str, server: dict[str, Any], sequencing: dict[str, Any]) -> dict[str, Any]:
        self.section(5, "本地 FASTQ 和样本信息")
        local_data_dir = self.ask("本地 FASTQ 目录", "D:/data/rnaseq/raw_fastq")
        default_remote = f"{server['remote_base_dir'].rstrip('/')}/{project_id}/raw"
        remote_data_dir = self.ask("服务器接收 FASTQ 目录", default_remote)

        paired = sequencing["layout"] == "paired"
        self.say("接下来逐个录入样本。样本录入完成后，在样本 ID 处直接回车结束。")
        self.say("示例：Ctrl_1 / control / Ctrl_1_R1.fastq.gz / Ctrl_1_R2.fastq.gz")
        items: list[dict[str, str]] = []
        index = 1
        while True:
            sample_id = self.ask(f"样本 {index} ID", "" if items else f"sample_{index}")
            if not sample_id:
                if items:
                    break
                self.say("至少需要 1 个样本。")
                continue
            condition = self.ask(f"{sample_id} 分组/条件", "control" if index == 1 else "treatment")
            fastq_1 = self.ask(f"{sample_id} FASTQ R1", f"{sample_id}_R1.fastq.gz")
            sample = {
                "sample_id": sample_id,
                "condition": condition,
                "fastq_1": fastq_1,
            }
            if paired:
                sample["fastq_2"] = self.ask(f"{sample_id} FASTQ R2", f"{sample_id}_R2.fastq.gz")
            items.append(sample)
            index += 1

        return {
            "source": "local_upload",
            "local_data_dir": local_data_dir,
            "remote_data_dir": remote_data_dir,
            "items": items,
        }

    def collect_pipeline(self) -> dict[str, Any]:
        self.section(6, "分析流程设置")
        pipeline = {}
        for step, cfg in DEFAULT_PIPELINE.items():
            enabled = self.ask_yes_no(f"是否启用 {step} ({cfg['version']})？", default=cfg["enabled"])
            pipeline[step] = {
                "enabled": enabled,
                "version": cfg["version"],
            }
        return pipeline

    def collect_polling(self) -> dict[str, Any]:
        self.section(7, "轮询设置")
        interval_seconds = self.ask_int("轮询间隔秒数", 300)
        timeout_hours = self.ask_int("最长等待小时数", 168)
        return {
            "interval_seconds": interval_seconds,
            "timeout_hours": timeout_hours,
        }

    def collect_notification(self) -> dict[str, Any]:
        self.section(8, "结束提醒")
        enabled = self.ask_yes_no("是否启用邮件通知？", default=True)
        if not enabled:
            return {"email_enabled": False}

        recipient = self.ask("接收邮箱", "user@example.com")
        smtp_host = self.ask("SMTP 服务器", "smtp.example.com")
        smtp_port = self.ask_int("SMTP 端口", 587)
        smtp_user = self.ask("SMTP 用户名", recipient)
        password_env = self.ask("SMTP 密码环境变量名", "RNASEQ_AGENT_SMTP_PASSWORD")
        return {
            "email_enabled": True,
            "recipient": recipient,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_user": smtp_user,
            "password_env": password_env,
            "notify_on": ["completed", "failed"],
        }

    def show_summary_preview(self, config: dict[str, Any], config_path: Path) -> None:
        self.section(9, "配置摘要")
        self.say(f"将保存到：{config_path}")
        for line in summarize_project(config):
            self.say(line)

    def section(self, index: int, title: str) -> None:
        self.say("")
        self.say(f"[{index}/9] {title}")

    def say(self, message: str) -> None:
        print(message)

    def ask(self, prompt: str, default: str, help_text: str = "") -> str:
        while True:
            suffix = f" [{default}]" if default else ""
            value = input(f"{prompt}{suffix}: ").strip()
            if value.lower() == "quit":
                raise SystemExit("已退出。")
            if value == "?":
                self.say(help_text or "直接输入答案；如果有默认值，回车会使用默认值。")
                continue
            return value or default

    def ask_int(self, prompt: str, default: int, help_text: str = "") -> int:
        while True:
            value = self.ask(prompt, str(default), help_text)
            try:
                return int(value)
            except ValueError:
                self.say("这里需要填写整数。")

    def ask_yes_no(self, prompt: str, default: bool, help_text: str = "") -> bool:
        default_text = "Y/n" if default else "y/N"
        while True:
            value = input(f"{prompt} [{default_text}]: ").strip().lower()
            if value == "quit":
                raise SystemExit("已退出。")
            if value == "?":
                self.say(help_text or "请输入 y 或 n；直接回车使用默认值。")
                continue
            if not value:
                return default
            if value in {"y", "yes", "是"}:
                return True
            if value in {"n", "no", "否"}:
                return False
            self.say("请输入 y 或 n。")

    def ask_choice(self, prompt: str, choices: list[str], default: str) -> str:
        choices_text = "/".join(choices)
        while True:
            value = self.ask(f"{prompt} ({choices_text})", default, f"可选值：{choices_text}")
            if value in choices:
                return value
            self.say(f"请输入以下选项之一：{choices_text}")

    def ask_list(self, prompt: str, default: str, help_text: str = "") -> list[str]:
        text = self.ask(prompt, default, help_text)
        if not text:
            return []
        return [item.strip() for item in text.split(";") if item.strip()]


def run_chat(output_dir: Path) -> Path:
    return ChatSession(output_dir).run()
