from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .defaults import DEFAULT_PIPELINE, DEFAULT_REFERENCE
from .estimate import estimate_runtime
from .remote import project_remote_workdir
from .storage import load_defaults, save_defaults, save_json


TOTAL_STEPS = 8


def run_wizard(output_dir: Path) -> Path:
    print("SYSU RNA-seq Agent 交互式向导")
    print("目标：采集本地数据、上传到服务器、提交分析、轮询状态、下载结果并生成解读。")
    print()

    defaults = _ensure_defaults()

    project = _step(1, "项目基本信息", _collect_project)
    server = _step(2, "服务器信息", _collect_server)
    sequencing = _step(3, "测序信息", _collect_sequencing)
    samples = _step(4, "本地 FASTQ 数据", _collect_samples)
    pipeline = _step(5, "分析流程步骤", _collect_pipeline)
    polling = _step(6, "轮询设置", _collect_polling)
    notification = _step(7, "结束提醒", _collect_notification)

    remote_workdir = project_remote_workdir(server["remote_base_dir"], project["id"])
    if samples["remote_data_dir"] == "AUTO":
        samples["remote_data_dir"] = f"{remote_workdir}/raw"

    config = {
        "schema_version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "project": project,
        "server": {**server, "remote_workdir": remote_workdir},
        "reference": defaults["reference"],
        "sequencing": sequencing,
        "samples": samples,
        "pipeline": pipeline,
        "polling": polling,
        "notification": notification,
        "status": {
            "state": "configured",
            "message": "Project config created. Data upload and remote execution are not submitted yet.",
        },
    }

    runtime = estimate_runtime(config)
    config["runtime_estimate"] = {"hours": runtime.hours, "summary": runtime.summary}

    project_id = project["id"]
    config_path = output_dir / project_id / "project.json"
    _step(8, "保存配置", lambda: _save_project(config_path, config, runtime.summary))

    print()
    print(f"配置已生成：{config_path}")
    print(runtime.summary)
    print("下一步：执行 run 命令，agent 会自动校验、上传、提交、轮询和下载结果。")
    return config_path


def _ensure_defaults() -> dict[str, Any]:
    saved = load_defaults()
    if saved:
        print(f"[固定配置] 已加载一次性参考配置：{saved['reference']['name']}")
        return saved

    print("[固定配置] 第一次使用，需要确认默认参考配置。")
    print("参考示例：GENCODE Human Release 47 / GRCh38.p14 / ALL")
    print(f"GTF 下载地址: {DEFAULT_REFERENCE['gtf_url']}")
    print(f"FASTA 下载地址: {DEFAULT_REFERENCE['genome_fasta_url']}")
    print(f"服务器 GTF 路径示例: {DEFAULT_REFERENCE['remote_gtf_path']}")
    print(f"服务器 FASTA 路径示例: {DEFAULT_REFERENCE['remote_genome_fasta_path']}")
    use_default = _ask_yes_no("是否使用以上默认参考配置？", default=True)
    reference = DEFAULT_REFERENCE if use_default else _collect_reference()

    payload = {"reference": reference}
    save_defaults(payload)
    print(f"[固定配置] 已保存到用户默认配置：{reference['name']}")
    print()
    return payload


def _step(index: int, title: str, func: Callable[[], Any]) -> Any:
    print(f"[{index}/{TOTAL_STEPS}] {title}")
    result = func()
    print(f"进度：{index}/{TOTAL_STEPS} 完成\n")
    return result


def _collect_project() -> dict[str, str]:
    print("示例：project_id = rnaseq_lung_cancer_001")
    project_id = _ask("项目 ID", default=f"rnaseq_{datetime.now():%Y%m%d_%H%M%S}")
    title = _ask("项目名称", default=project_id)
    owner = _ask("负责人/用户", default="local_user")
    return {"id": project_id, "title": title, "owner": owner}


def _collect_server() -> dict[str, Any]:
    print("示例：host = hpc.example.edu, remote_base_dir = /data/users/you/rnaseq_projects")
    print("如果服务器需要 module load 或 conda activate，可以填写初始化命令。")
    profile = _ask("服务器配置名", default="sysu_hpc")
    host = _ask("服务器地址 host", default="your.server.edu")
    user = _ask("服务器用户名", default="username")
    remote_base_dir = _ask("服务器项目根目录", default=f"/data/users/{user}/rnaseq_projects")
    scheduler = _ask_choice("调度器", ["slurm", "pbs", "local"], default="slurm")
    threads = _ask_int("每个任务线程数", default=16)
    memory_gb = _ask_int("内存 GB", default=64)
    shell = _ask("服务器 shell", default="bash")
    init_commands = _ask_list(
        "服务器初始化命令，多个命令用 ; 分隔，留空表示无需初始化",
        default="",
    )
    return {
        "profile": profile,
        "host": host,
        "user": user,
        "remote_base_dir": remote_base_dir,
        "scheduler": scheduler,
        "threads": threads,
        "memory_gb": memory_gb,
        "shell": shell,
        "init_commands": init_commands,
    }


def _collect_sequencing() -> dict[str, Any]:
    print("示例：paired-end，约 40M reads/样本，strandedness 先用 auto。")
    layout = _ask_choice("测序类型", ["paired", "single"], default="paired")
    reads = _ask_int("每个样本 reads 数量估计，单位 M", default=40)
    strandedness = _ask_choice("链特异性", ["auto", "unstranded", "forward", "reverse"], default="auto")
    return {
        "layout": layout,
        "reads_per_sample_million": reads,
        "strandedness": strandedness,
    }


def _collect_samples() -> dict[str, Any]:
    print("主流程：从本地 FASTQ 开始，agent 先上传到服务器，再提交分析。")
    print("示例：本地目录 D:/data/rnaseq/raw_fastq，文件 Ctrl_1_R1.fastq.gz / Ctrl_1_R2.fastq.gz")
    source = "local_upload"
    local_data_dir = _ask("本地 FASTQ 目录", default="D:/data/rnaseq/raw_fastq")
    remote_data_dir = _ask("服务器接收 FASTQ 目录，回车自动使用 <项目目录>/raw", default="AUTO")
    count = _ask_int("样本数量", default=2)

    items = []
    for i in range(1, count + 1):
        print(f"样本 {i} 示例：sample_id=Ctrl_1, condition=control, R1=Ctrl_1_R1.fastq.gz")
        sample_id = _ask(f"样本 {i} ID", default=f"sample_{i}")
        condition = _ask(f"样本 {i} 分组/条件", default="control" if i == 1 else "treatment")
        fastq_1 = _ask(f"样本 {i} FASTQ R1", default=f"{sample_id}_R1.fastq.gz")
        fastq_2 = _ask(f"样本 {i} FASTQ R2", default=f"{sample_id}_R2.fastq.gz")
        items.append(
            {
                "sample_id": sample_id,
                "condition": condition,
                "fastq_1": fastq_1,
                "fastq_2": fastq_2,
            }
        )

    return {
        "source": source,
        "local_data_dir": local_data_dir,
        "remote_data_dir": remote_data_dir,
        "items": items,
    }


def _collect_pipeline() -> dict[str, Any]:
    pipeline = {}
    for step, cfg in DEFAULT_PIPELINE.items():
        enabled = _ask_yes_no(f"是否启用 {step} ({cfg['version']})？", default=cfg["enabled"])
        pipeline[step] = {"enabled": enabled, "version": cfg["version"]}
    return pipeline


def _collect_polling() -> dict[str, Any]:
    print("示例：每 300 秒轮询一次，最多等待 168 小时。")
    interval_seconds = _ask_int("轮询间隔秒数", default=300)
    timeout_hours = _ask_int("最长等待小时数", default=168)
    return {
        "interval_seconds": interval_seconds,
        "timeout_hours": timeout_hours,
    }


def _collect_notification() -> dict[str, Any]:
    print("分析完成后可邮件提醒。密码/授权码建议放到环境变量，不写入配置文件。")
    enabled = _ask_yes_no("是否启用邮件通知？", default=True)
    if not enabled:
        return {"email_enabled": False}

    recipient = _ask("接收邮箱", default="user@example.com")
    smtp_host = _ask("SMTP 服务器", default="smtp.example.com")
    smtp_port = _ask_int("SMTP 端口", default=587)
    smtp_user = _ask("SMTP 用户名", default=recipient)
    password_env = _ask("SMTP 密码环境变量名", default="RNASEQ_AGENT_SMTP_PASSWORD")
    return {
        "email_enabled": True,
        "recipient": recipient,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_user": smtp_user,
        "password_env": password_env,
        "notify_on": ["completed", "failed"],
    }


def _collect_reference() -> dict[str, str]:
    name = _ask("参考配置名", default=DEFAULT_REFERENCE["name"])
    gtf_url = _ask("GTF 下载 URL", default=DEFAULT_REFERENCE["gtf_url"])
    fasta_url = _ask("FASTA 下载 URL", default=DEFAULT_REFERENCE["genome_fasta_url"])
    remote_ref_dir = _ask("服务器参考目录", default=DEFAULT_REFERENCE["remote_ref_dir"])
    remote_gtf_path = _ask("服务器 GTF 文件路径", default=DEFAULT_REFERENCE["remote_gtf_path"])
    remote_genome_fasta_path = _ask("服务器 genome FASTA 路径", default=DEFAULT_REFERENCE["remote_genome_fasta_path"])
    star_index_dir = _ask("STAR index 目录", default=DEFAULT_REFERENCE["star_index_dir"])
    rsem_index_prefix = _ask("RSEM index prefix", default=DEFAULT_REFERENCE["rsem_index_prefix"])
    arriba_blacklist_path = _ask("Arriba blacklist 路径，可留空", default=DEFAULT_REFERENCE["arriba_blacklist_path"])
    arriba_known_fusions_path = _ask(
        "Arriba known fusions 路径，可留空",
        default=DEFAULT_REFERENCE["arriba_known_fusions_path"],
    )
    return {
        **DEFAULT_REFERENCE,
        "name": name,
        "gtf_url": gtf_url,
        "genome_fasta_url": fasta_url,
        "remote_ref_dir": remote_ref_dir,
        "remote_gtf_path": remote_gtf_path,
        "remote_genome_fasta_path": remote_genome_fasta_path,
        "star_index_dir": star_index_dir,
        "rsem_index_prefix": rsem_index_prefix,
        "arriba_blacklist_path": arriba_blacklist_path,
        "arriba_known_fusions_path": arriba_known_fusions_path,
    }


def _save_project(path: Path, config: dict[str, Any], estimate_summary: str) -> dict[str, str]:
    save_json(path, config)
    print(estimate_summary)
    return {"path": str(path)}


def _ask(prompt: str, default: str) -> str:
    value = input(f"{prompt} [{default}]: ").strip()
    return value or default


def _ask_int(prompt: str, default: int) -> int:
    while True:
        value = input(f"{prompt} [{default}]: ").strip()
        if not value:
            return default
        try:
            return int(value)
        except ValueError:
            print("请输入整数。")


def _ask_list(prompt: str, default: str) -> list[str]:
    value = input(f"{prompt} [{default}]: ").strip()
    text = value or default
    if not text:
        return []
    return [item.strip() for item in text.split(";") if item.strip()]


def _ask_yes_no(prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{hint}]: ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes", "是"}:
            return True
        if value in {"n", "no", "否"}:
            return False
        print("请输入 y 或 n。")


def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    choices_text = "/".join(choices)
    while True:
        value = input(f"{prompt} ({choices_text}) [{default}]: ").strip()
        if not value:
            return default
        if value in choices:
            return value
        print(f"请输入以下选项之一：{choices_text}")
