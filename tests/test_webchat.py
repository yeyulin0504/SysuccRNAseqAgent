"""Tests for the web chat rule router (local intent -> audited action).

Design doc 5.2: the assistant only emits structured action requests;
deterministic session code validates and executes them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rnaseq_agent.webchat import (
    ChatIntent,
    execute_intent,
    route_intent,
)


def _project_dir(tmp_path: Path) -> Path:
    return tmp_path / "proj"


def _make_session(tmp_path: Path):
    from rnaseq_agent.session import ProjectSession

    project_dir = _project_dir(tmp_path)
    project_dir.mkdir(parents=True, exist_ok=True)
    data_dir = tmp_path / "fastq"
    data_dir.mkdir(parents=True, exist_ok=True)
    import gzip

    for name in ("a_R1", "a_R2", "b_R1", "b_R2"):
        with gzip.open(data_dir / f"{name}.fastq.gz", "wt", encoding="ascii") as handle:
            handle.write("@r\nACGT\n+\nIIII\n")

    session = ProjectSession(project_dir)
    config = {
        "schema_version": 1,
        "project": {"id": "proj", "title": "Demo", "owner": "t"},
        "study": {"cancer_type": "pan_cancer", "design": "independent_two_group"},
        "server": {
            "profile": "mvp_local",
            "host": "localhost",
            "user": "t",
            "remote_base_dir": f"{project_dir}/remote",
            "remote_workdir": f"{project_dir}/remote/w",
            "scheduler": "local",
            "threads": 4,
            "memory_gb": 16,
            "shell": "bash",
            "init_commands": [],
        },
        "reference": {
            "name": "GENCODE_R47_GRCh38p14_ALL",
            "remote_gtf_path": f"{tmp_path}/ref.gtf",
            "remote_genome_fasta_path": f"{tmp_path}/ref.fa",
            "star_index_dir": f"{tmp_path}/star_idx",
            "rsem_index_prefix": f"{tmp_path}/rsem",
        },
        "sequencing": {"layout": "paired", "reads_per_sample_million": 20, "strandedness": "auto"},
        "samples": {
            "source": "local_upload",
            "local_data_dir": str(data_dir),
            "remote_data_dir": "remote",
            "items": [
                {"sample_id": "a", "condition": "control", "fastq_1": "a_R1.fastq.gz", "fastq_2": "a_R2.fastq.gz"},
                {"sample_id": "b", "condition": "treatment", "fastq_1": "b_R1.fastq.gz", "fastq_2": "b_R2.fastq.gz"},
            ],
        },
        "pipeline": {
            "fastp": {"enabled": True, "version": "0.24.1"},
            "star": {"enabled": True, "version": "2.7.11b"},
            "arriba": {"enabled": True, "version": "2.5.0"},
            "featurecounts": {"enabled": True, "version": "Subread 2.1.1"},
            "rsem": {"enabled": True, "version": "1.2.28"},
            "diffexp": {"enabled": False, "version": "DESeq2"},
        },
        "polling": {"interval_seconds": 300, "timeout_hours": 24},
        "notification": {"email_enabled": False},
    }
    session.new_project(config)
    return session


def _make_deg_session(tmp_path: Path):
    """6-sample (3v3) session with a valid independent two-group design."""
    from rnaseq_agent.session import ProjectSession

    project_dir = _project_dir(tmp_path)
    project_dir.mkdir(parents=True, exist_ok=True)
    data_dir = tmp_path / "fastq"
    data_dir.mkdir(parents=True, exist_ok=True)
    import gzip

    for name in ("c1_R1", "c1_R2", "c2_R1", "c2_R2", "c3_R1", "c3_R2",
                 "t1_R1", "t1_R2", "t2_R1", "t2_R2", "t3_R1", "t3_R2"):
        with gzip.open(data_dir / f"{name}.fastq.gz", "wt", encoding="ascii") as handle:
            handle.write("@r\nACGT\n+\nIIII\n")

    session = ProjectSession(project_dir)
    items = []
    for sample_id, condition in (
        ("c1", "control"), ("c2", "control"), ("c3", "control"),
        ("t1", "treatment"), ("t2", "treatment"), ("t3", "treatment"),
    ):
        items.append(
            {
                "sample_id": sample_id,
                "condition": condition,
                "fastq_1": f"{sample_id}_R1.fastq.gz",
                "fastq_2": f"{sample_id}_R2.fastq.gz",
            }
        )
    config = {
        "schema_version": 1,
        "project": {"id": "proj", "title": "Demo DEG", "owner": "t"},
        "study": {"cancer_type": "pan_cancer", "design": "independent_two_group"},
        "server": {
            "profile": "mvp_local",
            "host": "localhost",
            "user": "t",
            "remote_base_dir": f"{project_dir}/remote",            "remote_workdir": f"{project_dir}/remote/w",
            "scheduler": "local",
            "threads": 4,
            "memory_gb": 16,
            "shell": "bash",
            "init_commands": [],
        },
        "reference": {
            "name": "GENCODE_R47_GRCh38p14_ALL",
            "remote_gtf_path": f"{tmp_path}/ref.gtf",
            "remote_genome_fasta_path": f"{tmp_path}/ref.fa",
            "star_index_dir": f"{tmp_path}/star_idx",
            "rsem_index_prefix": f"{tmp_path}/rsem",
        },
        "sequencing": {"layout": "paired", "reads_per_sample_million": 20, "strandedness": "auto"},
        "samples": {
            "source": "local_upload",
            "local_data_dir": str(data_dir),
            "remote_data_dir": "remote",
            "items": items,
        },
        "pipeline": {
            "fastp": {"enabled": True, "version": "0.24.1"},
            "star": {"enabled": True, "version": "2.7.11b"},
            "arriba": {"enabled": False, "version": "2.5.0"},
            "featurecounts": {"enabled": True, "version": "Subread 2.1.1"},
            "rsem": {"enabled": False, "version": "1.2.28"},
            "diffexp": {"enabled": False, "version": "DESeq2"},
        },
        # M1.6：reference_condition 显式声明（control 为参考组）。
        "diffexp": {"reference_condition": "control"},
        "polling": {"interval_seconds": 300, "timeout_hours": 24},
        "notification": {"email_enabled": False},
    }
    session.new_project(config)
    return session


class TestRouteIntent:
    def test_plan_keyword(self) -> None:
        intent = route_intent("帮我生成执行计划")
        assert intent is not None and intent.action == "plan"

    def test_confirm_keyword(self) -> None:
        intent = route_intent("确认冻结")
        assert intent is not None and intent.action == "confirm"

    def test_threads_edit(self) -> None:
        intent = route_intent("把线程改成 16")
        assert intent is not None and intent.action == "edit"
        assert intent.params == {"server": {"threads": 16}}

    def test_disable_step(self) -> None:
        intent = route_intent("关闭 arriba")
        assert intent is not None and intent.action == "edit"
        assert intent.params == {"pipeline": {"arriba": {"enabled": False}}}

    def test_rollback_keyword(self) -> None:
        intent = route_intent("回滚")
        assert intent is not None and intent.action == "rollback"

    def test_enable_diffexp(self) -> None:
        intent = route_intent("帮我做差异表达")
        assert intent is not None and intent.action == "edit"
        assert intent.params == {"pipeline": {"diffexp": {"enabled": True}}}

    def test_disable_diffexp(self) -> None:
        intent = route_intent("关闭差异表达")
        assert intent is not None and intent.action == "edit"
        assert intent.params == {"pipeline": {"diffexp": {"enabled": False}}}

    def test_diffexp_with_reference(self) -> None:
        intent = route_intent("以 control 为对照做差异表达")
        assert intent is not None and intent.action == "edit"
        assert intent.params["pipeline"]["diffexp"]["enabled"] is True
        assert intent.params["diffexp"]["reference_condition"] == "control"

    def test_diffexp_design_query(self) -> None:
        intent = route_intent("差异表达设计是不是合格")
        assert intent is not None and intent.action == "deg_status"

    def test_diffexp_intent_matches_only_when_requested(self) -> None:
        # “差异表达”几个字单独出现不应误判成步骤开关之外的行为。
        intent = route_intent("这个差异表达的结果怎么解读")
        assert intent is None or intent.action in {"deg_status"}

    def test_unrecognized_returns_none(self) -> None:
        assert route_intent("你今天心情怎么样") is None

    def test_browse_samples_with_explicit_remote_path(self) -> None:
        intent = route_intent("浏览 /hwdata/home/yeyulin/rna 找我的 RNA-seq 样本")
        assert intent is not None and intent.action == "browse_samples"
        assert intent.params == {"path": "/hwdata/home/yeyulin/rna"}

    def test_browse_samples_can_use_configured_default_path(self) -> None:
        intent = route_intent("你可以浏览我的服务器目录找样本吗")
        assert intent is not None and intent.action == "browse_samples"
        assert intent.params == {}


class TestKnowledgeQuestionsAreNotHijacked:
    """知识提问必须放行给大模型，不能被工具意图抢走。

    用户诉求（2026-09-15）：「我需要 llm 的思考执行结果」。此前「解释一下
    差异表达的原理」「测序深度对差异表达有什么影响」这类纯提问会命中
    ``_match_diffexp``，被当成步骤开关 / 状态查询，导致配好大模型也拿不到
    模型答复，只能看到一句生硬的规则文案。
    """

    @pytest.mark.parametrize(
        "question",
        [
            "解释一下差异表达的原理",
            "什么是差异表达",
            "差异表达和差异分析的区别是什么",
            "为什么做差异分析需要生物学重复",
            "不太理解测序深度对差异表达的影响",
            "请用两三句话解释一下什么是测序深度，以及它对差异表达结果有什么影响。",
            "用两三句话解释一下 RNA-seq 里 TPM 和 FPKM 的区别，以及做差异分析时更推荐哪一个。",
            "帮我讲讲 DESeq2 和 edgeR 有什么区别",
        ],
    )
    def test_knowledge_question_routes_to_none(self, question: str) -> None:
        assert route_intent(question) is None, f"提问被误判成工具动作：{question}"

    @pytest.mark.parametrize(
        "command",
        [
            "帮我做差异表达",
            "启用差异表达",
            "以 control 为对照做差异表达",
            "关闭差异表达",
            "差异表达设计是不是合格",
            "这个差异表达的结果怎么解读",
        ],
    )
    def test_real_commands_still_route(self, command: str) -> None:
        # 命令语义不能被「放行提问」的守卫误伤。
        assert route_intent(command) is not None, f"命令被误放行：{command}"


class TestExecuteIntent:
    def test_plan_updates_session(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        intent = ChatIntent("plan", message="生成计划")
        result = execute_intent(session, intent)
        assert "steps" in result
        assert session.state == "planned"

    def test_edit_records_changeset(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        intent = ChatIntent(
            "edit",
            params={"server": {"threads": 32}},
            note="对话改线程",
        )
        result = execute_intent(session, intent)
        assert result["state"] == "drafting"
        assert session.config["server"]["threads"] == 32
        assert session.changeset_path.is_file()

    def test_confirm_freezes_when_local_files_exist(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        session.plan()
        intent = ChatIntent("confirm")
        result = execute_intent(session, intent)
        # Local FASTQ fixtures exist, so the contract freezes.
        assert result["state"] == "confirmed"
        assert "contract_id" in result

    def test_enable_diffexp_via_edit(self, tmp_path: Path) -> None:
        session = _make_deg_session(tmp_path)
        intent = ChatIntent(
            "edit",
            params={"pipeline": {"diffexp": {"enabled": True}}},
            note="对话启用 diffexp",
        )
        result = execute_intent(session, intent)
        assert result["state"] == "drafting"
        assert session.config["pipeline"]["diffexp"]["enabled"] is True

    def test_deg_status_reports_design(self, tmp_path: Path) -> None:
        session = _make_deg_session(tmp_path)
        intent = ChatIntent("deg_status")
        result = execute_intent(session, intent)
        # 尚未启用。
        assert "尚未启用" in result["reply"]
        # 启用后重新查询应报告对比。
        session.edit({"pipeline": {"diffexp": {"enabled": True}}}, note="enable")
        result = execute_intent(session, intent)
        assert "treatment_vs_control" in result["reply"]

    def test_deg_plan_and_confirm_flow(self, tmp_path: Path) -> None:
        session = _make_deg_session(tmp_path)
        session.edit({"pipeline": {"diffexp": {"enabled": True}}}, note="enable diffexp")
        session.plan()
        plan = session.execution_plan
        assert plan is not None
        assert any("差异表达" in step and "treatment_vs_control" in step for step in plan.steps)
        contract = session.confirm()
        assert contract["contract_id"].startswith("sha256:")
        # diffexp 相关文件已物化。
        scripts = session.project_dir / "generated_scripts"
        assert (scripts / "diffexp_deseq2.R").is_file()
        assert (scripts / "colData.tsv").is_file()

    def test_deg_plan_refused_when_design_insufficient(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)  # 只有 2 个样本 (1v1)
        session.edit({"pipeline": {"diffexp": {"enabled": True}}}, note="enable diffexp")
        session.plan()
        assert session.state == "planned"
        plan = session.execution_plan
        assert plan is not None
        assert "Adapter 预检未通过" in plan.summary
        # 确认必须被拒绝。
        with pytest.raises(Exception):
            session.confirm()
