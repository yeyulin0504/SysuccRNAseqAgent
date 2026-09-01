"""LangGraph control-plane graph for the bulk RNA golden route.

Framework section 5.1 / 5.6: LangGraph is the control plane. Nodes mark the
boundaries where state must be saved or a scientific decision is made, not
every single shell command.

This module implements a ``BulkRNAGraph`` driving the same ``ProjectSession``
state machine through LangGraph, with an interrupt (framework section 6.1
checkpoint #4) after fastp QC so the user can confirm before STAR runs.

The LLM never reaches this layer with shell strings: it only requests a
``capability_id`` and the graph drives the audited session.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, TypedDict

try:  # langgraph is an optional runtime dependency for the control plane.
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt
except ImportError:  # pragma: no cover - exercised when langgraph is absent
    StateGraph = None  # type: ignore[assignment]
    START = "START"  # type: ignore[assignment]
    END = "END"  # type: ignore[assignment]

    def interrupt(value: Any) -> Any:  # type: ignore[misc]
        return value

from .capability import (
    FAIL,
    FAIL_OUTPUT_CONTRACT,
    NOT_EVALUABLE,
    PASS,
    STALE,
    WAITING_HPC,
    WAITING_USER,
)
from .output_validator import validate_output_contract, post_run_gate
from .session import (
    CONFIRMED,
    DRAFTING,
    PLANNED,
    ProjectSession,
    SessionError,
)

# Graph state: everything the control plane persists between nodes.
class BulkRNAState(TypedDict, total=False):
    project_dir: str
    capability_id: str
    status: str  # framework 6.3 status vocabulary
    message: str
    plan_summary: str
    contract_id: str
    reason_codes: list[str]
    artifacts: dict[str, Any]
    validation_report: dict[str, Any]
    output_artifacts: dict[str, str]


def _session(state: BulkRNAState) -> ProjectSession:
    project_dir = Path(state.get("project_dir", "runs/mvp_demo"))
    session = ProjectSession(project_dir, capability_id=state.get("capability_id", ""))
    session.load_session()
    return session


# -- nodes ------------------------------------------------------------------


def node_gate_a(state: BulkRNAState) -> BulkRNAState:
    """Gate-A: metadata-level suitability check (framework 5.2)."""
    session = _session(state)
    try:
        gate = session.gate_check()
    except SessionError as exc:
        return {"status": FAIL, "message": str(exc)}
    if not gate.ok:
        return {
            "status": NOT_EVALUABLE,
            "message": "Gate-A 未通过：" + "; ".join(gate.reasons),
        }
    return {"status": PASS, "message": "Gate-A 通过"}


def node_adapter_plan(state: BulkRNAState) -> BulkRNAState:
    """Adapter.inspect/plan: build the plan the user will confirm."""
    session = _session(state)
    try:
        plan = session.plan()
    except SessionError as exc:
        return {"status": FAIL, "message": str(exc)}
    if plan.summary.startswith("Adapter 预检未通过"):
        return {"status": NOT_EVALUABLE, "message": plan.summary}
    return {
        "status": PLANNED,
        "message": plan.summary,
        "plan_summary": plan.summary,
    }


def node_wait_qc(state: BulkRNAState) -> BulkRNAState:
    """Checkpoint #4 (framework 6.1): pause for QC confirmation before STAR.

    Uses a LangGraph interrupt: the graph halts here and control returns to
    the caller. On resume, the user decision (approve / redo) is fed back
    through the same node, which then records the choice in the session's
    audit trail.
    """
    decision = interrupt({"checkpoint": "fastp_qc", "state": WAITING_USER})
    approved = bool(decision) and decision.get("approved") is not False
    return {
        "status": PASS if approved else WAITING_USER,
        "message": "fastp QC 已确认，继续 STAR 比对。" if approved else "fastp QC 需重新检查。",
    }


def node_confirm_contract(state: BulkRNAState) -> BulkRNAState:
    """User confirmed: freeze the Analysis Contract, materialize scripts."""
    session = _session(state)
    try:
        contract = session.confirm()
    except SessionError as exc:
        return {"status": FAIL, "message": str(exc)}
    return {"status": CONFIRMED, "contract_id": contract["contract_id"]}


def node_execute(state: BulkRNAState) -> BulkRNAState:
    """Execution Gateway: submit the frozen project to the scheduler."""
    session = _session(state)
    try:
        outcome = session.execute(wait=False)
    except SessionError as exc:
        return {"status": FAIL, "message": str(exc)}
    return {"status": WAITING_HPC, "message": outcome.get("message", "")}


def node_validate_output(state: BulkRNAState) -> BulkRNAState:
    """Output Validator: schema/hash checks on produced artifacts."""
    session = _session(state)
    project_dir = session.project_dir
    required = getattr(session.capability, "output_artifacts", {}) or {}
    try:
        report = validate_output_contract(
            {},
            attempts_dir=project_dir,
            required_artifacts=required,
        )
        return {"status": PASS, "validation_report": report}
    except Exception as exc:  # noqa: BLE001 - ValidationFailure
        return {"status": FAIL_OUTPUT_CONTRACT, "message": str(exc)}


def node_post_run_gate(state: BulkRNAState) -> BulkRNAState:
    """Post-run Gate: QC/OOD/confidence refusal -> ABSTAIN or PASS."""
    result = post_run_gate({})
    if result["status"] == "PASS":
        return {"status": PASS, "message": result["message"]}
    return {"status": result["status"], "message": result["message"], "reason_codes": result["reason_codes"]}


def node_report(state: BulkRNAState) -> BulkRNAState:
    """Final report generation (framework report structure)."""
    session = _session(state)
    try:
        report_path = session.report()
    except SessionError as exc:
        return {"status": FAIL, "message": str(exc)}
    return {"status": "completed", "message": f"报告已生成：{report_path}"}


def node_end_fail(state: BulkRNAState) -> BulkRNAState:
    """Terminal sink for a failed / refused path (framework section 13)."""
    return {
        "status": state.get("status", FAIL),
        "message": state.get("message", "流程终止。"),
        "reason_codes": state.get("reason_codes", []),
    }


# -- conditional edges ------------------------------------------------------


def _edge_after_gate_a(state: BulkRNAState) -> str:
    if state.get("status") == PASS:
        return "adapter_plan"
    return "end_fail"


def _edge_after_plan(state: BulkRNAState) -> str:
    if state.get("status") == PLANNED:
        return "wait_qc"
    return "end_fail"


def _edge_after_qc(state: BulkRNAState) -> str:
    if state.get("status") == PASS:
        return "confirm_contract"
    return "end_fail"


def _edge_after_validate(state: BulkRNAState) -> str:
    if state.get("status") == PASS:
        return "post_run_gate"
    return "end_fail"


def _edge_after_post_run(state: BulkRNAState) -> str:
    if state.get("status") == PASS:
        return "report"
    return "end_fail"


def build_bulk_rna_graph():
    """Build the LangGraph control-plane graph.

    The graph is deliberately a straight line with a QC interrupt; it drives
    the audited ProjectSession rather than issuing shell commands itself.
    """
    if StateGraph is None:  # pragma: no cover - langgraph not installed
        raise ImportError("langgraph is required to build the control-plane graph")

    builder = StateGraph(BulkRNAState)
    builder.add_node("gate_a", node_gate_a)
    builder.add_node("adapter_plan", node_adapter_plan)
    builder.add_node("wait_qc", node_wait_qc)
    builder.add_node("confirm_contract", node_confirm_contract)
    builder.add_node("execute", node_execute)
    builder.add_node("validate_output", node_validate_output)
    builder.add_node("post_run_gate", node_post_run_gate)
    builder.add_node("report", node_report)
    builder.add_node("end_fail", node_end_fail)

    builder.add_edge(START, "gate_a")
    builder.add_conditional_edges("gate_a", _edge_after_gate_a)
    builder.add_conditional_edges("adapter_plan", _edge_after_plan)
    builder.add_conditional_edges("wait_qc", _edge_after_qc)
    builder.add_edge("confirm_contract", "execute")
    builder.add_edge("execute", "validate_output")
    builder.add_conditional_edges("validate_output", _edge_after_validate)
    builder.add_conditional_edges("post_run_gate", _edge_after_post_run)
    builder.add_edge("report", END)
    builder.add_edge("end_fail", END)
    return builder.compile()
