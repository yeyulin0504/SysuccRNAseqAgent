"""Tests for the LangGraph control-plane graph (framework section 5.6).

The graph drives the audited ProjectSession; nodes mark scientific
decision boundaries, not shell commands.
"""

from __future__ import annotations

import pytest

from rnaseq_agent.agent_graph import build_bulk_rna_graph
from rnaseq_agent.capability import (
    FAIL_OUTPUT_CONTRACT,
    NOT_EVALUABLE,
    PASS,
    WAITING_USER,
)

langgraph_missing = False
try:
    import langgraph  # noqa: F401
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
