"""Interactive MVP CLI: a capability-aware, auditable project session.

This is the user-facing MVP entry point. It drives the same state machine as
``session.ProjectSession`` but keeps the conversation prompt-driven:

    mvp new                     -> create a new project draft
    mvp open <path>             -> resume a session
    mvp edit <key> <json>       -> apply a user-approved change (recorded)
    mvp plan                    -> build the execution plan
    mvp confirm                 -> freeze the plan into an Analysis Contract
    mvp rollback [n]            -> revert to a previous ChangeSet snapshot
    mvp run [--nowait]          -> execute through the gateway
    mvp status                  -> refresh server status
    mvp report                  -> generate the Markdown report
    mvp history                 -> show the ChangeSet audit trail
    mvp caps                    -> list available capabilities

The design contract is respected: the CLI never asks the LLM to produce shell
commands. Tool execution happens only through the registered capability and
the existing Execution Gateway (``run_agent.run_project``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .actions import summarize_project
from .capability import GateResult, PASS, list_capabilities
from .session import (
    ProjectSession,
    SessionError,
    _load_changesets,
)


def build_mvp_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rnaseq-agent mvp",
        description="Capability-aware, auditable MVP project session.",
    )
    subparsers = parser.add_subparsers(dest="mvp_command", required=True)

    def _add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--project-dir",
            default="runs/mvp_demo",
            help="Project directory holding session.json. Default: runs/mvp_demo",
        )

    subparsers.add_parser("new", help="Create a new project draft (prompts minimal questions).")
    subparsers.add_parser("open", help="Resume a session in a project directory.")
    subparsers.add_parser("caps", help="List registered capabilities.")

    plan = subparsers.add_parser("plan", help="Build the execution plan for the current draft.")
    _add_common(plan)
    confirm = subparsers.add_parser("confirm", help="Freeze the plan into an immutable Analysis Contract.")
    _add_common(confirm)
    run = subparsers.add_parser("run", help="Execute the frozen project (add --nowait to skip polling).")
    _add_common(run)
    run.add_argument("--nowait", action="store_true", help="Submit and return without polling.")
    status = subparsers.add_parser("status", help="Refresh and show server status.")
    _add_common(status)
    report = subparsers.add_parser("report", help="Generate the Markdown summary report.")
    _add_common(report)
    history = subparsers.add_parser("history", help="Show the ChangeSet audit trail.")
    _add_common(history)
    rollback = subparsers.add_parser("rollback", help="Roll back to a previous ChangeSet snapshot.")
    _add_common(rollback)
    edit = subparsers.add_parser("edit", help="Edit the current project config (interactive).")
    _add_common(edit)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_mvp_parser()
    args = parser.parse_args(argv)

    command = args.mvp_command
    try:
        if command == "caps":
            return _cmd_caps()
        if command == "new":
            return _cmd_new()
        if command == "open":
            return _cmd_open()
        if command in {"plan", "confirm", "run", "status", "report", "history", "rollback", "edit"}:
            return _cmd_session(command, args)
    except (SessionError, ValueError, OSError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


# -- sub-commands ---------------------------------------------------------


def _cmd_caps() -> int:
    for capability in list_capabilities():
        print(f"{capability.capability_id}  ({capability.version})")
        print(f"  标题：{capability.title}")
        print(f"  说明：{capability.description}")
        print(f"  输入契约：layout={sorted(capability.input_contract['layout'])}, "
              f"min_samples={capability.input_contract['min_samples']}, "
              f"designs={sorted(capability.input_contract['designs'])}")
        print(f"  产物：{', '.join(sorted(capability.output_artifacts))}")
        print()
    return 0


def _cmd_new() -> int:
    session = ProjectSession(_default_project_dir())
    if session.session_path.is_file():
        print(f"该目录已有会话：{session.session_path}")
        print("使用 `mvp open <目录>` 恢复，或删除会话文件后重新 new。")
        return 1
    print("开始创建新项目草稿（MVP 精简版，只问关键问题）。")
    config = _prompt_config(session)
    gate = session.new_project(config)
    print()
    print("项目草稿已创建。")
    print_gate(gate)
    if gate.verdict == PASS:
        print("下一步：输入 `mvp plan` 生成执行计划。")
    else:
        print("当前草稿未通过 Gate-A 检查，可继续 `mvp edit` 修改。")
    return 0 if gate.verdict == PASS else 1


def _cmd_open() -> int:
    project_dir = Path(input("项目目录（session.json 所在目录，回车使用 runs/mvp_demo）：").strip() or "runs/mvp_demo")
    session = ProjectSession(project_dir)
    if not session.session_path.is_file():
        print(f"没有找到会话文件：{session.session_path}")
        print("请先使用 `mvp new` 创建，或检查目录路径。")
        return 1
    session.load_session()
    print(f"已恢复会话：{project_dir}")
    for line in session.summary_lines():
        print(f"  {line}")
    return 0


def _cmd_session(command: str, args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir) if getattr(args, "project_dir", None) else Path("runs/mvp_demo")
    session = ProjectSession(project_dir)
    if not session.session_path.is_file():
        print(f"没有找到会话文件：{session.session_path}")
        print("请先使用 `mvp new` 创建项目。")
        return 1
    session.load_session()

    if command == "plan":
        plan = session.plan()
        print("执行计划：")
        for step in plan.steps:
            print(f"  - {step}")
        print()
        print("确认无误后输入 `mvp confirm` 冻结为 Analysis Contract。")
        return 0

    if command == "confirm":
        contract = session.confirm()
        print("Analysis Contract 已冻结：")
        print(f"  contract_id：{contract['contract_id']}")
        print(f"  创建时间：{contract['created_at']}")
        print("下一步：`mvp run` 开始执行。")
        return 0

    if command == "run":
        wait = not getattr(args, "nowait", False)
        print("确认执行后开始运行。" if wait else "提交执行（不等待完成）。")
        outcome = session.execute(wait=wait)
        print(f"状态：{outcome['state']}")
        print(outcome["message"])
        return 0

    if command == "status":
        status = session.refresh_status()
        print(f"状态：{status.get('state', 'unknown')}")
        message = status.get("message")
        if message:
            print(f"说明：{message}")
        return 0

    if command == "report":
        path = session.report()
        print(f"报告已生成：{path}")
        return 0

    if command == "history":
        entries = _load_changesets(session.changeset_path)
        if not entries:
            print("暂无变更记录。")
            return 0
        for entry in entries:
            print(
                f"#{entry.get('index')}  {entry.get('timestamp', '')}  "
                f"{entry.get('event', '')}  "
                f"({entry.get('state', '')})"
            )
            note = entry.get("note")
            if note:
                print(f"      说明：{note}")
            patch = entry.get("patch")
            if patch:
                print(f"      变更：{_compact_json(patch)}")
        return 0

    if command == "rollback":
        target = None
        raw = input("要回滚到第几次变更（直接回车回滚最后一次）：").strip()
        if raw:
            try:
                target = int(raw)
            except ValueError:
                print("请输入变更序号或直接回车。")
                return 1
        session.rollback(target)
        print(f"已回滚。当前会话状态：{session.state}")
        gate = session.gate_check()
        print_gate(gate)
        print("请重新执行 `mvp plan` 生成新计划。")
        return 0

    if command == "edit":
        return _cmd_edit(session)

    return 0


def _cmd_edit(session: ProjectSession) -> int:
    if session.config is None:
        print("当前会话没有项目配置。")
        return 1
    print("当前配置摘要：")
    for line in summarize_project(session.config):
        print(f"  {line}")
    print()
    print("支持修改的键（示例）：")
    print("  samples.items  -> JSON 数组，替换样本列表")
    print("  pipeline.star.enabled -> true/false")
    print("  server.threads / server.memory_gb -> 数值")
    print("  sequencing.layout -> paired/single")
    raw = input("修改键（例如 pipeline.star.enabled）：").strip()
    if not raw:
        print("未修改。")
        return 0
    value_raw = input(f"{raw} = ").strip()
    if not value_raw:
        print("未修改。")
        return 0
    note = input("修改说明（可选，写入审计日志）：").strip()
    patch = _parse_patch(raw, value_raw)
    gate = session.edit(patch, note=note)
    print("修改已应用，ChangeSet 已记录。")
    print_gate(gate)
    print("请重新执行 `mvp plan`。")
    return 0 if gate.verdict == PASS else 1


# -- helpers ---------------------------------------------------------------


def _default_project_dir() -> Path:
    return Path("runs/mvp_demo")


def print_gate(gate: GateResult) -> None:
    if gate.verdict == PASS:
        print("Gate-A 检查通过：可进入计划阶段。")
    else:
        print("Gate-A 检查未通过（NOT_EVALUABLE）：")
        for reason in gate.reasons:
            print(f"  - {reason}")


def _parse_patch(raw: str, value_raw: str) -> dict[str, Any]:
    """Convert an edit like ``pipeline.star.enabled=true`` into a patch dict."""
    import json

    parts = raw.split(".")
    value: Any
    lowered = value_raw.strip().lower()
    if lowered in {"true", "false"}:
        value = lowered == "true"
    else:
        try:
            value = json.loads(value_raw)
        except ValueError:
            value = value_raw

    if parts[0] == "pipeline" and len(parts) >= 2:
        step = parts[1]
        if len(parts) == 2:
            raise SessionError("pipeline 需要指定具体键，例如 pipeline.star.enabled。")
        key = ".".join(parts[2:])
        return {"pipeline": {step: {key: value}}}

    if parts[0] == "server" and len(parts) >= 2:
        return {"server": {".".join(parts[1:]): value}}

    if parts[0] == "sequencing" and len(parts) >= 2:
        return {"sequencing": {".".join(parts[1:]): value}}

    if parts[0] == "samples" and len(parts) >= 2:
        if parts[1] == "items":
            return {"samples.items": value}

    return {raw: value}


def _compact_json(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _prompt_config(session: ProjectSession) -> dict[str, Any]:
    from datetime import datetime

    from .chat import ChatSession  # reuse question helpers only if present

    project_id = input(f"项目 ID [{datetime.now():%Y%m%d_%H%M%S}]：").strip() or f"{datetime.now():%Y%m%d_%H%M%S}"
    title = input(f"项目名称 [{project_id}]：").strip() or project_id
    owner = input("负责人/用户 [local_user]：").strip() or "local_user"

    print()
    print(f"当前能力：{session.capability.capability_id}（{session.capability.title}）")
    capability_choice = input("使用该能力？[Y/n]：").strip().lower()
    if capability_choice in {"n", "no", "否"}:
        caps = list_capabilities()
        for index, capability in enumerate(caps, start=1):
            print(f"  {index}. {capability.capability_id}")
        choice = input("选择能力编号：").strip()
        try:
            capability = caps[int(choice) - 1]
            session.capability_id = capability.capability_id
            session.capability = capability
        except (ValueError, IndexError):
            print("无效选择，使用默认能力。")

    print()
    layout = input("测序类型 (paired/single) [paired]：").strip().lower() or "paired"
    if layout not in {"paired", "single"}:
        layout = "paired"

    print()
    print("请输入样本信息。每行：sample_id,condition,R1[,R2]。空行结束。")
    print("示例：ctrl_1,control,ctrl_1_R1.fastq.gz,ctrl_1_R2.fastq.gz")
    items = []
    while True:
        line = input(f"样本 {len(items) + 1}（空行结束）：").strip()
        if not line:
            if items:
                break
            print("至少需要一个样本。")
            continue
        fields = [field.strip() for field in line.split(",")]
        if len(fields) < 3:
            print("格式错误：需要至少 sample_id,condition,R1。")
            continue
        sample = {
            "sample_id": fields[0],
            "condition": fields[1],
            "fastq_1": fields[2],
        }
        if layout == "paired":
            if len(fields) < 4 or not fields[3]:
                print("双端测序需要 R2。")
                continue
            sample["fastq_2"] = fields[3]
        items.append(sample)

    print()
    local_data_dir = input("本地 FASTQ 目录 [D:/data/rnaseq/raw_fastq]：").strip() or "D:/data/rnaseq/raw_fastq"
    host = input("服务器 host [your.server.edu]：").strip() or "your.server.edu"
    user = input("服务器用户名 [username]：").strip() or "username"
    remote_base_dir = input("服务器项目根目录 [/data/users/username/rnaseq_projects]：").strip() or f"/data/users/{user}/rnaseq_projects"
    scheduler = input("调度器 (slurm/pbs/local) [local]：").strip().lower() or "local"
    if scheduler not in {"slurm", "pbs", "local"}:
        scheduler = "local"

    print()
    print("参考基因组设置（可先用占位路径，之后用 mvp edit 修改）：")
    star_index = input("STAR 索引目录 [/ref/star]：").strip() or "/ref/star"
    gtf_path = input("GTF 注释文件 [/ref/gencode.v47.gtf]：").strip() or "/ref/gencode.v47.gtf"
    genome_fasta = input("基因组 fasta [/ref/GRCh38.fa]：").strip() or "/ref/GRCh38.fa"
    rsem_prefix = input("RSEM 索引前缀 [/ref/rsem]（可留空）：").strip() or ""
    reference = {
        "name": "GENCODE_R47_GRCh38p14_ALL",
        "remote_gtf_path": gtf_path,
        "remote_genome_fasta_path": genome_fasta,
        "star_index_dir": star_index,
        "rsem_index_prefix": rsem_prefix or "/ref/rsem",
    }

    pipeline = {}
    from .defaults import DEFAULT_PIPELINE

    for step, cfg in DEFAULT_PIPELINE.items():
        default_enabled = bool(cfg["enabled"])
        if not default_enabled:
            continue
        enabled = input(f"启用 {step} ({cfg['version']})？[Y/n]：").strip().lower()
        pipeline[step] = {
            "enabled": not (enabled in {"n", "no", "否"}),
            "version": cfg["version"],
        }

    return {
        "schema_version": 1,
        "project": {"id": project_id, "title": title, "owner": owner},
        "server": {
            "profile": "mvp_local",
            "host": host,
            "user": user,
            "remote_base_dir": remote_base_dir,
            "scheduler": scheduler,
            "threads": 8,
            "memory_gb": 32,
            "shell": "bash",
            "init_commands": [],
        },
        "sequencing": {"layout": layout, "reads_per_sample_million": 40, "strandedness": "auto"},
        "samples": {
            "source": "local_upload",
            "local_data_dir": local_data_dir,
            "remote_data_dir": "AUTO",
            "items": items,
        },
        "reference": reference,
        "pipeline": pipeline,
        "polling": {"interval_seconds": 300, "timeout_hours": 168},
        "notification": {"email_enabled": False},
    }


if __name__ == "__main__":
    raise SystemExit(main())
