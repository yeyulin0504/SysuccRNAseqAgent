"""Tests for the auditable project session state machine."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from rnaseq_agent.session import (
    COMPLETED,
    CONFIRMED,
    DRAFTING,
    EXECUTING,
    IDLE,
    PLANNED,
    ProjectSession,
    SessionError,
    _load_changesets,
)


def _write_fastq(path: Path, sequence: str = "ACGT") -> None:
    with gzip.open(path, "wt", encoding="ascii") as handle:
        handle.write(f"@read1\n{sequence}\n+\n{'I' * len(sequence)}\n")


def _write_config(project_dir: Path, config: dict) -> Path:
    config_path = project_dir / "project.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


def _minimal_config(project_dir: Path) -> dict:
    return {
        "schema_version": 1,
        "project": {"id": project_dir.name, "title": "Demo", "owner": "test"},
        "server": {
            "profile": "mvp_local",
            "host": "localhost",
            "user": "test",
            "remote_base_dir": "/data/users/test/rnaseq_projects",
            "remote_workdir": f"/data/users/test/rnaseq_projects/{project_dir.name}",
            "scheduler": "local",
            "threads": 4,
            "memory_gb": 16,
            "shell": "bash",
            "init_commands": [],
        },
        "reference": {
            "name": "GENCODE_R47_GRCh38p14_ALL",
            "remote_gtf_path": "/ref/gencode.v47.gtf",
            "remote_genome_fasta_path": "/ref/GRCh38.fa",
            "star_index_dir": "/ref/star",
            "rsem_index_prefix": "/ref/rsem",
        },
        "sequencing": {"layout": "paired", "reads_per_sample_million": 20, "strandedness": "auto"},
        "samples": {
            "source": "local_upload",
            "local_data_dir": "tests/fixtures/fastq",
            "remote_data_dir": "remote",
            "items": [
                {
                    "sample_id": "sample_a",
                    "condition": "control",
                    "fastq_1": "a_R1.fastq.gz",
                    "fastq_2": "a_R2.fastq.gz",
                },
                {
                    "sample_id": "sample_b",
                    "condition": "treatment",
                    "fastq_1": "b_R1.fastq.gz",
                    "fastq_2": "b_R2.fastq.gz",
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
        "polling": {"interval_seconds": 300, "timeout_hours": 168},
        "notification": {"email_enabled": False},
    }


def _valid_config(tmp_path: Path) -> dict:
    """A config whose FASTQ files actually exist so contracts can be frozen."""
    data_dir = tmp_path / "fastq"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("a_R1", "a_R2", "b_R1", "b_R2"):
        _write_fastq(data_dir / f"{name}.fastq.gz")
    config = _minimal_config(tmp_path / "mvp_test_project")
    config["samples"]["local_data_dir"] = str(data_dir)
    return config


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    return tmp_path / "mvp_test_project"


@pytest.fixture
def session(project_dir: Path) -> ProjectSession:
    return ProjectSession(project_dir)


class TestSessionStateMachine:
    def test_initial_state_is_idle(self, session: ProjectSession) -> None:
        assert session.state == IDLE
        assert session.config is None

    def test_new_project_moves_to_drafting(self, session: ProjectSession, project_dir: Path) -> None:
        gate = session.new_project(_minimal_config(project_dir))
        assert session.state == DRAFTING
        assert session.config is not None
        assert session.config_path.is_file()
        assert session.session_path.is_file()
        # Fastq files do not exist in the fixture dir -> Gate-A should fail.
        assert not gate.ok

    def test_new_project_from_wrong_state_raises(
        self, session: ProjectSession, project_dir: Path
    ) -> None:
        session.new_project(_minimal_config(project_dir))
        with pytest.raises(SessionError):
            session.new_project(_minimal_config(project_dir))

    def test_plan_requires_draft(self, session: ProjectSession) -> None:
        with pytest.raises(SessionError):
            session.plan()

    def test_plan_builds_execution_plan(self, session: ProjectSession, project_dir: Path) -> None:
        session.new_project(_minimal_config(project_dir))
        plan = session.plan()
        assert session.state == PLANNED
        assert plan.capability_id == "bulk_rnaseq_expression_v1"
        assert plan.steps

    def test_edit_records_changeset_and_rebuilds_plan(
        self, session: ProjectSession, project_dir: Path
    ) -> None:
        session.new_project(_minimal_config(project_dir))
        gate = session.edit({"pipeline": {"star": {"enabled": False}}}, note="disable star for testing")
        assert session.state == DRAFTING
        assert session.execution_plan is None
        entries = _load_changesets(session.changeset_path)
        assert len(entries) == 2  # project_created + changeset_applied
        assert entries[1]["event"] == "changeset_applied"
        assert entries[1]["note"] == "disable star for testing"

    def test_edit_with_no_change_raises(self, session: ProjectSession, project_dir: Path) -> None:
        session.new_project(_minimal_config(project_dir))
        with pytest.raises(SessionError):
            session.edit({}, note="no-op")

    def test_rollback_restores_previous_config(
        self, session: ProjectSession, project_dir: Path
    ) -> None:
        session.new_project(_minimal_config(project_dir))
        config_before = json.loads(session.config_path.read_text(encoding="utf-8"))
        session.edit({"pipeline": {"star": {"enabled": False}}}, note="disable star")
        config_after = json.loads(session.config_path.read_text(encoding="utf-8"))
        assert config_after["pipeline"]["star"]["enabled"] is False

        session.rollback()
        restored = json.loads(session.config_path.read_text(encoding="utf-8"))
        assert restored["pipeline"]["star"]["enabled"] is True
        assert restored["project"] == config_before["project"]
        entries = _load_changesets(session.changeset_path)
        assert any(entry["event"] == "rollback" for entry in entries)

    def test_rollback_to_specific_index(
        self, session: ProjectSession, project_dir: Path
    ) -> None:
        session.new_project(_minimal_config(project_dir))
        session.edit({"pipeline": {"star": {"enabled": False}}}, note="change 1")
        session.edit({"server": {"threads": 8}}, note="change 2")
        entries = _load_changesets(session.changeset_path)
        change_1 = next(e for e in entries if e["event"] == "changeset_applied" and e["note"] == "change 1")
        index_1 = change_1["index"]
        session.rollback(index_1)
        restored = json.loads(session.config_path.read_text(encoding="utf-8"))
        # Rolled back to after change 1: star disabled, threads still 4.
        assert restored["pipeline"]["star"]["enabled"] is False
        assert restored["server"]["threads"] == 4

    def test_repeated_rollback_walks_backward_stack(
        self, session: ProjectSession, project_dir: Path
    ) -> None:
        """连续 rollback 应按栈顺序逐次回退，而不是重复回到同一个快照。"""
        session.new_project(_minimal_config(project_dir))
        session.edit({"server": {"threads": 16}}, note="change 1: threads 4->16")
        session.edit({"server": {"threads": 24}}, note="change 2: threads 16->24")
        assert session.config["server"]["threads"] == 24

        session.rollback()
        assert session.config["server"]["threads"] == 16  # 撤掉 change 2
        session.rollback()
        assert session.config["server"]["threads"] == 4  # 再撤掉 change 1

    def test_rollback_after_confirm_restores_frozen_snapshot(
        self, session: ProjectSession, project_dir: Path, tmp_path: Path
    ) -> None:
        """契约冻结后仍可回滚到冻结前的配置，供用户修改参数后重新冻结。"""
        config = _valid_config(tmp_path)
        session.new_project(config)
        session.edit({"server": {"threads": 16}}, note="threads 4->16")
        session.plan()
        session.confirm()
        assert session.state == CONFIRMED

        session.rollback()
        assert session.state == DRAFTING
        assert session.config["server"]["threads"] == 16  # 回到冻结前的快照
        entries = _load_changesets(session.changeset_path)
        assert any(entry["event"] == "rollback" for entry in entries)

    def test_confirm_freezes_contract(
        self, session: ProjectSession, project_dir: Path, tmp_path: Path
    ) -> None:
        config = _valid_config(tmp_path)
        session.new_project(config)
        session.plan()
        contract = session.confirm()
        assert session.state == CONFIRMED
        assert contract["contract_id"].startswith("sha256:")
        contract_path = project_dir / "analysis_contract.json"
        assert contract_path.is_file()

    def test_confirm_requires_planned_state(self, session: ProjectSession, project_dir: Path) -> None:
        session.new_project(_minimal_config(project_dir))
        with pytest.raises(SessionError):
            session.confirm()

    def test_session_persistence_round_trip(
        self, session: ProjectSession, project_dir: Path
    ) -> None:
        session.new_project(_minimal_config(project_dir))
        session.plan()
        # A fresh session in the same dir should resume the planned state.
        resumed = ProjectSession(project_dir)
        assert resumed.load_session()
        assert resumed.state == PLANNED
        assert resumed.config is not None
        assert resumed.execution_plan is not None
        assert resumed.execution_plan.capability_id == "bulk_rnaseq_expression_v1"

    def test_summary_lines_include_capability(self, session: ProjectSession, project_dir: Path) -> None:
        session.new_project(_minimal_config(project_dir))
        lines = session.summary_lines()
        assert any("bulk_rnaseq_expression_v1" in line for line in lines)
        assert any("样本数" in line for line in lines)


class TestChangesetAudit:
    def test_changeset_file_created(self, session: ProjectSession, project_dir: Path) -> None:
        session.new_project(_minimal_config(project_dir))
        assert session.changeset_path.is_file()
        entries = _load_changesets(session.changeset_path)
        assert entries[0]["event"] == "project_created"

    def test_changesets_have_indexes(self, session: ProjectSession, project_dir: Path) -> None:
        session.new_project(_minimal_config(project_dir))
        session.edit({"pipeline": {"fastp": {"enabled": False}}}, note="skip fastp")
        entries = _load_changesets(session.changeset_path)
        assert [entry["index"] for entry in entries] == [1, 2]
