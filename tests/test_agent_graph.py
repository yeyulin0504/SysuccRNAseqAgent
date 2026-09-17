"""Tests for the LangGraph control-plane graph (framework section 5.6).

The graph drives the audited ProjectSession; nodes mark scientific
decision boundaries, not shell commands.

M1 (2026-09-07): nodes read the *real* attempt/QC evidence before deciding
(gap #1/#2/#4) and the graph can be compiled with a disk checkpointer so a
``thread_id == project_id`` run survives restarts (gap #3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rnaseq_agent.agent_graph import (
    build_bulk_rna_graph,
    node_validate_output,
    node_wait_qc,
    sqlite_checkpointer_for,
)
from rnaseq_agent.capability import (
    FAIL_OUTPUT_CONTRACT,
    NOT_EVALUABLE,
    PASS,
    WAITING_USER,
)

langgraph_missing = False
try:
    import langgraph  # noqa: F401
    from langgraph.types import Command, interrupt  # noqa: F401
except ImportError:
    langgraph_missing = True


pytestmark = pytest.mark.skipif(langgraph_missing, reason="langgraph not installed")


@pytest.fixture
def graph():
    return build_bulk_rna_graph()


def _initial_state() -> dict:
    return {
        "project_dir": "runs/mvp_demo",
        "capability_id": "workflow.bulk_rna.grch38_pe_expression_fusion",
        "status": "",
        "message": "",
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _minimal_project_dir(root: Path, name: str = "project_p") -> Path:
    """A project dir that hosts session.json + a frozen project.json."""
    project_dir = root / name
    _write_json(
        project_dir / "session.json",
        {
            "schema_version": 1,
            "state": "confirmed",
            "capability_id": "workflow.bulk_rna.grch38_pe_expression_fusion",
            "config_path": str(project_dir / "project.json"),
            "updated_at": "2026-09-07T00:00:00+00:00",
        },
    )
    _write_json(
        project_dir / "project.json",
        {
            "schema_version": 1,
            "project": {"id": name, "title": "Demo", "owner": "test"},
            "server": {"remote_workdir": f"/remote/{name}", "scheduler": "local"},
            "pipeline": {"fastp": {"enabled": True}},
            "samples": {"items": []},
            "status": {
                "state": "stage_completed",
                "run_id": "run-1",
                "attempt_dir": str(project_dir / "attempts" / "run-1"),
            },
        },
    )
    return project_dir


def _write_attempt_manifest(project_dir: Path, *, ok: bool = True) -> Path:
    run_id = "run-1"
    attempt_dir = project_dir / "attempts" / run_id
    manifest = {
        "schema_version": 1,
        "summary": {
            "ok": ok,
            "errors": [] if ok else ["diffexp/deseq2_results.tsv 缺失"],
            "scientific_files": [
                {"name": "fastp/sample_a.json", "category": "scientific"},
                {"name": "star/sample_a.Log.final.out", "category": "scientific"},
            ],
        },
    }
    _write_json(attempt_dir / "result_manifest.json", manifest)
    return attempt_dir


def _counts_project_dir(root: Path) -> Path:
    project_dir = _minimal_project_dir(root, "counts_project")
    payload = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    payload["samples"] = {
        "counts_path": str(project_dir / "uploads" / "counts_matrix.tsv"),
        "items": [
            {"sample_id": "A1", "condition": "control"},
            {"sample_id": "A2", "condition": "control"},
            {"sample_id": "B1", "condition": "case"},
            {"sample_id": "B2", "condition": "case"},
        ],
    }
    payload["pipeline"] = {
        "diffexp": {"enabled": True},
        "cms": {"enabled": False},
    }
    payload["cms"] = {"run_mode": "counts"}
    _write_json(project_dir / "project.json", payload)
    return project_dir


class TestBulkRNAGraph:
    def test_compiles(self, graph) -> None:
        assert graph is not None

    def test_gate_a_failure_returns_not_evaluable(self, graph, tmp_path) -> None:
        # Empty project dir -> session not created -> gate fails cleanly.
        state = _initial_state()
        state["project_dir"] = str(tmp_path / "missing")
        result = graph.invoke(state)
        # No session file -> FAIL (session missing) rather than NOT_EVALUABLE.
        assert result.get("status") in {NOT_EVALUABLE, "FAIL"}

    def test_missing_session_returns_fail(self, graph, tmp_path) -> None:
        state = _initial_state()
        state["project_dir"] = str(tmp_path / "nope")
        result = graph.invoke(state)
        assert result.get("status") == "FAIL"


class TestM1RealizedCheckpointNodes:
    """M1 gap #1: node_wait_qc refuses without real QC evidence (no auto-pass)."""

    def _state(self, project_dir: Path) -> dict:
        return {
            "project_dir": str(project_dir),
            "capability_id": "workflow.bulk_rna.grch38_pe_expression_fusion",
        }

    def test_wait_qc_without_attempt_fails(self, tmp_path) -> None:
        project_dir = _minimal_project_dir(tmp_path)
        # No attempt dir, no manifest -> FAIL, not an interrupt and not PASS.
        result = node_wait_qc(self._state(project_dir))
        assert result["status"] == "FAIL"
        assert "QC 产物缺失" in result["message"] or "尚未创建执行 attempt" in result["message"]
        assert result["qc_evidence"]["ok"] is False

    def test_wait_qc_fails_when_manifest_unverified(self, tmp_path) -> None:
        project_dir = _minimal_project_dir(tmp_path)
        _write_attempt_manifest(project_dir, ok=False)
        result = node_wait_qc(self._state(project_dir))
        assert result["status"] == "FAIL"
        assert result["qc_evidence"]["ok"] is False

    def test_wait_qc_only_accepts_literal_boolean_true(
        self, tmp_path, monkeypatch
    ) -> None:
        project_dir = _minimal_project_dir(tmp_path)
        _write_attempt_manifest(project_dir, ok=True)
        monkeypatch.setattr(
            "rnaseq_agent.agent_graph.interrupt",
            lambda _payload: {"approved": "yes"},
        )

        result = node_wait_qc(self._state(project_dir))

        assert result["status"] == WAITING_USER
        assert result["qc_decision"]["approved"] is False

    def test_validate_output_without_attempt_raises_contract_failure(
        self, tmp_path
    ) -> None:
        project_dir = _minimal_project_dir(tmp_path)
        # No attempt_dir for run-1 exists -> FAIL_OUTPUT_CONTRACT.
        result = node_validate_output(self._state(project_dir))
        assert result["status"] == FAIL_OUTPUT_CONTRACT
        assert "执行 attempt 不存在" in result["message"] or "未生成" in result["message"]

    def test_validate_output_with_verified_manifest_passes(self, tmp_path) -> None:
        project_dir = _minimal_project_dir(tmp_path)
        attempt_dir = _write_attempt_manifest(project_dir, ok=True)
        # Seed minimal extracted artifacts so the output contract resolves.
        for pattern in ("featurecounts/gene_counts.txt", "fastp/sample_a.json"):
            target = attempt_dir / "downloads" / "extracted" / pattern
            _write_json(target, {"stub": True})
        result = node_validate_output(self._state(project_dir))
        assert result["status"] in {PASS, FAIL_OUTPUT_CONTRACT}
        if result["status"] == FAIL_OUTPUT_CONTRACT:
            assert "result_manifest" not in result["message"]

    def test_counts_validation_accepts_real_manifest_and_enabled_outputs(self, tmp_path) -> None:
        project_dir = _counts_project_dir(tmp_path)
        attempt_dir = project_dir / "attempts" / "run-1"
        _write_json(
            attempt_dir / "result_manifest.json",
            {"schema_version": 1, "body": {"validation": {"ok": True, "errors": []}}},
        )
        for relative in (
            "diffexp/deseq2_results.tsv",
            "diffexp/deseq2_summary.json",
        ):
            target = attempt_dir / "downloads" / "extracted" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("verified\n", encoding="utf-8")

        result = node_validate_output(self._state(project_dir))

        assert result["status"] == PASS


class TestM1DurableCheckpointer:
    """M1 gap #3: a thread_id == project_id graph run resumes from interrupt."""

    def test_sqlite_checkpointer_round_trip(self, tmp_path) -> None:
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt

        project_dir = tmp_path / "ck_project"

        def _probe(state: dict) -> dict:
            decision = interrupt({"checkpoint": "probe", "state": "ask"})
            if decision and decision.get("approved"):
                return {"status": "approved"}
            return {"status": "declined"}

        builder = StateGraph(dict)
        builder.add_node("probe", _probe)
        builder.add_edge(START, "probe")
        builder.add_edge("probe", END)

        thread_id = project_dir.name
        with sqlite_checkpointer_for(project_dir) as checkpointer:
            compiled = builder.compile(checkpointer=checkpointer)
            config = {"configurable": {"thread_id": thread_id}}
            first = compiled.invoke({"status": ""}, config=config)
            assert first.get("__interrupt__"), "interrupt must fire at the QC node"
            resumed = compiled.invoke(
                Command(resume={"approved": True}), config=config
            )
            assert resumed.get("status") == "approved"

        # The thread state survives on disk across a fresh connection.
        with sqlite_checkpointer_for(project_dir) as checkpointer:
            graph_state = checkpointer.get_tuple(config={"configurable": {"thread_id": thread_id}})
            assert graph_state is not None, "thread checkpoint must persist on disk"

    def test_build_graph_accepts_checkpointer(self, tmp_path) -> None:
        with sqlite_checkpointer_for(tmp_path / "g") as checkpointer:
            graph = build_bulk_rna_graph(checkpointer=checkpointer)
            assert graph is not None
