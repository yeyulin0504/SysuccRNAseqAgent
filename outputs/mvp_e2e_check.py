"""端到端验证：MVP 全流程（new → plan → edit → plan → confirm → rollback → history）。

仅做本地会话级验证，不真正提交作业（execute 只演示到 local 调度器校验）。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rnaseq_agent.capability import list_capabilities
from rnaseq_agent.session import (
    DRAFTING,
    PLANNED,
    CONFIRMED,
    ProjectSession,
    SessionError,
    _load_changesets,
    _config_sha256,
)

DEMO_DATA = Path(__file__).resolve().parent / "mvp_demo_data"
REF_DIR = Path(__file__).resolve().parent  # 任意存在的目录，仅用于本地校验


def make_config(project_dir: Path) -> dict:
    return {
        "schema_version": 1,
        "project": {"id": project_dir.name, "title": "MVP 端到端演示", "owner": "e2e"},
        "server": {
            "profile": "mvp_local",
            "host": "localhost",
            "user": "demo",
            "remote_base_dir": f"{project_dir}/remote",
            "remote_workdir": f"{project_dir}/remote/work",
            "scheduler": "local",
            "threads": 8,
            "memory_gb": 32,
            "shell": "bash",
            "init_commands": [],
        },
        "reference": {
            "name": "GENCODE_R47_GRCh38p14_ALL",
            "remote_gtf_path": str(REF_DIR / "placeholder.gtf"),
            "remote_genome_fasta_path": str(REF_DIR / "placeholder.fa"),
            "star_index_dir": str(REF_DIR / "star_idx"),
            "rsem_index_prefix": str(REF_DIR / "rsem"),
        },
        "sequencing": {
            "layout": "paired",
            "reads_per_sample_million": 20,
            "strandedness": "auto",
        },
        "samples": {
            "source": "local_upload",
            "local_data_dir": str(DEMO_DATA),
            "remote_data_dir": "remote",
            "items": [
                {
                    "sample_id": "ctrl_1",
                    "condition": "control",
                    "fastq_1": "ctrl_1_R1.fastq.gz",
                    "fastq_2": "ctrl_1_R2.fastq.gz",
                },
                {
                    "sample_id": "trt_1",
                    "condition": "treatment",
                    "fastq_1": "trt_1_R1.fastq.gz",
                    "fastq_2": "trt_1_R2.fastq.gz",
                },
            ],
        },
        "pipeline": {
            "fastp": {"enabled": True, "version": "0.24.1"},
            "star": {"enabled": True, "version": "2.7.11b"},
            "arriba": {"enabled": False, "version": "2.5.0"},
            "featurecounts": {"enabled": True, "version": "Subread 2.1.1"},
            "rsem": {"enabled": False, "version": "1.2.28"},
        },
        "polling": {"interval_seconds": 300, "timeout_hours": 24},
        "notification": {"email_enabled": False},
    }


def banner(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="mvp_e2e_", dir=str(Path(__file__).resolve().parent)))
    project_dir = workdir / "e2e_demo"
    print(f"工作目录：{workdir}")

    # -- 0. 能力注册表 ------------------------------------------------
    banner("0. 能力注册表")
    for cap in list_capabilities():
        print(f"  - {cap.capability_id} v{cap.version}：{cap.title}")
        for k, v in cap.input_contract.items():
            print(f"      input_contract.{k} = {v}")

    # -- 1. new --------------------------------------------------------
    banner("1. new（创建项目，Gate-A 检查）")
    session = ProjectSession(project_dir, capability_id="bulk_rnaseq_expression_v1")
    gate = session.new_project(make_config(project_dir))
    print(f"  状态：{session.state}")
    print(f"  Gate-A verdict：{gate.verdict}")
    for r in gate.formatted():
        print(f"    - {r}")
    assert session.state == DRAFTING, session.state
    assert gate.ok, gate.reasons

    # -- 2. plan -------------------------------------------------------
    banner("2. plan（确定性执行计划）")
    plan = session.plan()
    print(f"  状态：{session.state}")
    print(f"  步骤链（{len(plan.steps)}）：")
    for s in plan.steps:
        print(f"    - {s}")
    assert session.state == PLANNED

    # -- 3. edit -------------------------------------------------------
    banner("3. edit（修改线程数 + 关闭 featureCounts → 自动回 drafting）")
    gate = session.edit(
        {"server": {"threads": 16}, "pipeline": {"featurecounts": {"enabled": False}}},
        note="用户确认：提高线程数，本演示先不跑 featureCounts",
    )
    print(f"  状态：{session.state}")
    print(f"  Gate-A verdict：{gate.verdict}")
    assert session.state == DRAFTING

    # -- 4. plan 2 -----------------------------------------------------
    banner("4. plan（重新生成计划）")
    plan2 = session.plan()
    print(f"  状态：{session.state}")
    for s in plan2.steps:
        print(f"    - {s}")
    assert session.state == PLANNED

    # -- 5. confirm ----------------------------------------------------
    banner("5. confirm（冻结 Analysis Contract）")
    contract = session.confirm()
    print(f"  状态：{session.state}")
    print(f"  contract_id：{contract['contract_id']}")
    print(f"  contract 文件：{contract.get('contract_path') or '（内嵌于 project.json）'}")
    assert session.state == CONFIRMED

    # -- 6. rollback 语义演示（先 open 一个草稿态会话做编辑） ----------
    banner("6. rollback（打开新草稿会话 → 再次编辑 → 回滚）")
    # 已 confirm 的项目不能再 edit，这里演示另一个草稿项目
    draft_dir = workdir / "draft_demo"
    draft = ProjectSession(draft_dir, capability_id="bulk_rnaseq_expression_v1")
    gate = draft.new_project(make_config(draft_dir))
    assert gate.ok, gate.reasons
    draft.edit({"server": {"threads": 16}}, note="第一次修改：线程 8→16")
    draft.edit({"server": {"threads": 24}}, note="第二次修改：线程 16→24")
    print(f"  修改前 threads：8")
    print(f"  当前 threads：{draft.config['server']['threads']}")
    ok = draft.rollback()
    print(f"  rollback() 结果：{ok}")
    print(f"  回滚后 threads：{draft.config['server']['threads']}（应回到 16）")
    assert draft.config["server"]["threads"] == 16
    ok2 = draft.rollback()
    print(f"  再次 rollback() 结果：{ok2}")
    print(f"  回滚后 threads：{draft.config['server']['threads']}（应回到 8）")
    assert draft.config["server"]["threads"] == 8

    # -- 7. history（ChangeSet 审计） ----------------------------------
    banner("7. history（ChangeSet 审计记录）")
    entries = _load_changesets(draft.changeset_path)
    for entry in entries:
        print(
            f"  #{entry.get('index')} {entry.get('event')} "
            f"@ {entry.get('timestamp')} state={entry.get('state')}"
        )
        if entry.get("event") == "changeset_applied":
            print(
                f"      sha256 {entry.get('previous_config_sha256', '')[:12]}… "
                f"→ {entry.get('new_config_sha256', '')[:12]}…"
            )
            print(f"      note：{entry.get('note', '')}")
    assert all(e.get("index") for e in entries)

    # -- 8. summary ----------------------------------------------------
    banner("8. session.summary_lines")
    for line in draft.summary_lines():
        print(f"  {line}")

    print("\n[OK] 端到端验证全部通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"\n[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
