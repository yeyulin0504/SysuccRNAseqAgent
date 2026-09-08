"""LangGraph control-plane graph for the bulk RNA golden route.

Framework section 5.1 / 5.6: LangGraph is the control plane. Nodes mark the
boundaries where state must be saved or a scientific decision is made, not
every single shell command.

This module implements a ``BulkRNAGraph`` driving the same ``ProjectSession``
state machine through LangGraph, with an interrupt (framework section 6.1
checkpoint #4) after fastp QC so the user can confirm before STAR runs.

M1 (2026-09-07): the graph now opens the *real* QC evidence and the *real*
attempt before deciding (gap #1/#2/#4):

- ``node_wait_qc`` inspects the attempt's QC artifacts first; a pause is only
  offered when a QC evidence set exists. Resuming records the decision against
  the same attempt so every dialogue sees one artifact/checkpoint.
- ``node_validate_output`` validates the manifest that the real attempt
  produced instead of an empty contract (``{}`` no longer passes by default).
- ``node_post_run_gate`` feeds the real QC summary into the gate.

The graph can be compiled with a disk checkpointer (SQLite per project), so a
``thread_id == project_id`` graph run survives restarts and interrupts.

The LLM never reaches this layer with shell strings: it only requests a
``capability_id`` and the graph drives the audited session.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
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
from .output_validator import ValidationFailure, validate_output_contract, post_run_gate
from .session import (
    CONFIRMED,
    DRAFTING,
    PLANNED,
    ProjectSession,
    SessionError,
)
from .storage import load_json

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
    # M1: real attempt binding (one logical run across dialogues).
    run_id: str
    attempt_dir: str
    qc_evidence: dict[str, Any]  # {sample_id, files, summary, ok}
    qc_decision: dict[str, Any]  # {approved, user, decided_at, thread_id}


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


def node_diffexp_gate(state: BulkRNAState) -> BulkRNAState:
    """条件开放（框架 15.3）：diffexp 启用时执行 DEG 设计门禁。

    门禁只接受冻结的非配对两组设计（每组达到生物学重复阈值、batch 与
    condition 不混杂）。不通过时返回 NOT_EVALUABLE 并附可解释原因；
    未启用 diffexp 时该节点直接放行（PASS）。
    """
    from .differential import deg_gate, diffexp_is_requested

    session = _session(state)
    if session.config is None:
        return {"status": FAIL, "message": "会话尚无项目配置。"}
    if not diffexp_is_requested(session.config):
        return {"status": PASS, "message": "diffexp 未启用，跳过差异表达门禁。"}
    gate = deg_gate(session.config)
    if not gate.ok:
        return {"status": NOT_EVALUABLE, "message": "DEG 设计门禁未通过：" + "; ".join(gate.reasons)}
    return {"status": PASS, "message": "DEG 设计门禁通过：满足冻结的非配对两组模板。"}


def _qc_evidence_for(project_dir: Path) -> dict[str, Any]:
    """Real QC evidence from the most recent completed QC stage attempt.

    Returns ``{"ok": False}`` when no attempt has produced QC artifacts yet
    (gap #1: resume must never pass without a real QC output). Evidence fields
    mirror the fastp/QC JSONs plus the stage result_manifest summary so every
    dialogue binds to the same artifact set.
    """
    project_dir = Path(project_dir)
    status_path = project_dir / "project.json"
    if not status_path.is_file():
        return {"ok": False, "message": "项目尚无配置。", "files": []}
    payload = load_json(status_path)
    status = payload.get("status", {})
    run_id = str(status.get("run_id") or status.get("attempt_run_id") or "").strip()
    attempt_dir = project_dir / "attempts" / run_id if run_id else project_dir / "attempts"
    if not attempt_dir.is_dir():
        return {"ok": False, "message": "尚未创建执行 attempt。", "files": []}

    manifest_path = attempt_dir / "result_manifest.json"
    qc_ok = False
    summary: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    if manifest_path.is_file():
        manifest = load_json(manifest_path)
        summary = dict(manifest.get("summary", {}))
        qc_ok = bool(summary.get("ok"))
        files = list(summary.get("scientific_files", []))

    # Fall back to inspecting fastp QC JSONs directly under the attempt.
    fastp_dir = attempt_dir / "downloads" / "extracted" / "fastp"
    sample_ids: list[str] = []
    if fastp_dir.is_dir():
        qc_ok = True
        for json_path in sorted(fastp_dir.glob("*.json")):
            sample_ids.append(json_path.stem)
            files.append({"name": json_path.name, "category": "scientific"})
    elif qc_ok:
        sample_ids = sorted({str(item.get("name", "")).split(".", 1)[0] for item in files})
    if not files and not sample_ids:
        return {"ok": False, "message": "QC 产物缺失：attempt 中未找到 fastp 检查结果。", "files": []}

    return {
        "ok": qc_ok,
        "run_id": run_id,
        "attempt_dir": str(attempt_dir),
        "files": files,
        "sample_ids": sample_ids,
        "summary": summary,
        "message": f"找到 QC 证据 {len(files) or len(sample_ids)} 项（attempt {run_id}）。",
    }


def node_wait_qc(state: BulkRNAState) -> BulkRNAState:
    """Checkpoint #4 (framework 6.1): pause for QC confirmation before STAR.

    M1 gap #1 fix: the node *verifies* that real QC artifacts exist before
    offering the interrupt. No QC evidence -> ``FAIL`` (never auto-pass).
    With evidence and no stored decision -> LangGraph interrupt. On resume the
    user decision is recorded into the state and bound to the same attempt.
    """
    session = _session(state)
    project_dir = session.project_dir
    evidence = _qc_evidence_for(project_dir)

    if not evidence.get("ok"):
        return {
            "status": FAIL,
            "message": evidence.get("message", "QC 产物缺失，无法进入比对阶段。"),
            "qc_evidence": evidence,
        }

    decision = interrupt(
        {
            "checkpoint": "fastp_qc",
            "state": WAITING_USER,
            "evidence": {
                "run_id": evidence.get("run_id", ""),
                "attempt_dir": evidence.get("attempt_dir", ""),
                "files": [item.get("name") for item in evidence.get("files", [])],
                "summary": evidence.get("summary", {}),
            },
            "downstream": "validate_output -> post_run_gate -> report",
        }
    )
    approved = bool(decision) and decision.get("approved") is not False
    decided = {
        "approved": approved,
        "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if isinstance(decision, dict):
        for key in ("user", "thread_id", "note"):
            if decision.get(key) is not None:
                decided[key] = decision[key]
    # Persist against the project so every dialogue reads the same binding.
    try:
        session.record_qc_decision(
            approved=approved,
            user=str(decided.get("user", "")),
            thread_id=str(decided.get("thread_id", "")),
            note=str(decided.get("note", "")),
        )
    except Exception:  # noqa: BLE001 - decision persistence must not crash node
        pass
    return {
        "status": PASS if approved else WAITING_USER,
        "message": "fastp QC 已确认，继续下游阶段。" if approved else "fastp QC 需重新检查。",
        "qc_evidence": evidence,
        "qc_decision": decided,
        "run_id": evidence.get("run_id", ""),
        "attempt_dir": evidence.get("attempt_dir", ""),
    }


def node_confirm_contract(state: BulkRNAState) -> BulkRNAState:
    """User confirmed: freeze the Analysis Contract, materialize scripts."""
    session = _session(state)
    try:
        contract = session.confirm()
    except SessionError as exc:
        return {"status": FAIL, "message": str(exc)}
    return {"status": CONFIRMED, "contract_id": contract["contract_id"]}


def node_execute_qc(state: BulkRNAState) -> BulkRNAState:
    """Stage 1 (fastp QC) execution gateway (说明书 §5.1 / M1 gap #2).

    Submits the frozen contract's QC stage and waits for its *real* terminal
    state (``session.execute_stage('qc', wait=True)``). A QC attempt that is
    still queued/running returns ``WAITING_HPC`` (edge loops back into the QC
    checkpoint so polling-driven resumes converge on the same attempt). On a
    terminal QC the node records the attempt binding for the checkpoint.

    Counts 直入：项目没有 FASTQ 主流程，改执行 ``counts`` 阶段
    （DESeq2/CMScaller 读取上传的 counts_matrix.tsv）。
    """
    session = _session(state)
    stage = "counts" if _is_counts_entry_state(state) else "qc"
    # Reuse an existing stage submission when one is already mid-flight.
    status_payload = load_json(session.project_dir / "project.json").get("status", {})
    current_state = str(status_payload.get("state") or "")
    run_id = str(status_payload.get("run_id") or "")
    attempt_dir = str(status_payload.get("attempt_dir") or "")
    if current_state in {"submitted", "running", "queued", "preparing"}:
        return {
            "status": WAITING_HPC,
            "message": f"{stage} 作业已提交（{status_payload.get('job_id', '')}），等待 HPC 完成。",
            "run_id": run_id,
            "attempt_dir": attempt_dir,
        }
    if current_state in {"stage_completed", "remote_completed", "results_verified"}:
        return {
            "status": WAITING_HPC,
            "message": f"{stage} 阶段已到达终态（{current_state}），等待结果校验。",
            "run_id": run_id,
            "attempt_dir": attempt_dir,
        }
    try:
        outcome = session.execute_stage(stage, wait=True)
    except SessionError as exc:
        return {"status": FAIL, "message": str(exc)}
    status_payload = load_json(session.project_dir / "project.json").get("status", {})
    run_id = str(status_payload.get("run_id") or "")
    attempt_dir = str(status_payload.get("attempt_dir") or "")
    outcome_state = outcome.get("state", "")
    if outcome_state == "stage_completed":
        return {
            "status": WAITING_HPC,
            "message": outcome.get("message", ""),
            "run_id": run_id,
            "attempt_dir": attempt_dir,
        }
    if outcome_state in {"submitted", "running", "queued", "preparing"}:
        return {
            "status": WAITING_HPC,
            "message": outcome.get("message", ""),
            "run_id": run_id,
            "attempt_dir": attempt_dir,
        }
    return {
        "status": FAIL,
        "message": outcome.get("message", f"{stage} 阶段未成功完成。"),
        "run_id": run_id,
        "attempt_dir": attempt_dir,
    }


def _attempt_validation_report(
    project_dir: Path,
    required_artifacts: dict[str, str],
) -> dict[str, Any]:
    """Validate the artifacts the *real* attempt downloaded (gap #4 fix).

    Reads ``attempt_dir/result_manifest.json`` written by run_agent and checks
    the required capability artifacts against the extracted result tree. An
    attempt that never produced a manifest fails the contract instead of
    passing an empty ``{}``.
    """
    payload = load_json(project_dir / "project.json")
    run_id = str(payload.get("status", {}).get("run_id") or "").strip()
    attempt_dir = project_dir / "attempts" / run_id if run_id else None
    if attempt_dir is None or not attempt_dir.is_dir():
        raise ValidationFailure("执行 attempt 不存在，无法校验输出产物。")
    manifest_path = attempt_dir / "result_manifest.json"
    if not manifest_path.is_file():
        raise ValidationFailure("attempt 未生成 result_manifest.json，输出契约校验失败。")
    manifest = load_json(manifest_path)
    # ``create_result_manifest`` stores the authoritative validation under
    # body.validation.  Accept the legacy summary shape for old attempts.
    summary = manifest.get("body", {}).get("validation")
    if not isinstance(summary, dict):
        summary = manifest.get("summary", {})
    if not summary.get("ok"):
        raise ValidationFailure(
            FAIL_OUTPUT_CONTRACT
            + ": attempt 结果不完整。"
            + "; ".join(list(summary.get("errors", []))[:5])
        )
    return validate_output_contract(
        {},
        attempts_dir=attempt_dir,
        required_artifacts=required_artifacts,
    )


def _required_artifacts_for_run(config: dict[str, Any], *, counts_entry: bool) -> dict[str, str]:
    """Return only artifacts enabled in the frozen runtime configuration."""
    pipeline = config.get("pipeline", {})
    required: dict[str, str] = {}
    if counts_entry:
        if pipeline.get("diffexp", {}).get("enabled"):
            required.update(
                {
                    "diffexp/deseq2_results.tsv": "DESeq2 complete result table",
                    "diffexp/deseq2_summary.json": "DESeq2 run summary",
                }
            )
        if pipeline.get("cms", {}).get("enabled"):
            required.update(
                {
                    "cms/cms_result.csv": "CMS classification result",
                    "cms/cms_summary.json": "CMS run summary",
                }
            )
        return required
    # The legacy FASTQ graph still validates its registered workflow contract.
    return {}


def node_validate_output(state: BulkRNAState) -> BulkRNAState:
    """Output Validator: schema/hash checks on the real attempt artifacts."""
    session = _session(state)
    project_dir = session.project_dir
    if _is_counts_entry_state(state):
        required = _required_artifacts_for_run(session.config or {}, counts_entry=True)
    else:
        required = getattr(session.capability, "output_artifacts", {}) or {}
    try:
        report = _attempt_validation_report(project_dir, required)
        return {"status": PASS, "validation_report": report}
    except ValidationFailure as exc:
        return {"status": FAIL_OUTPUT_CONTRACT, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": FAIL_OUTPUT_CONTRACT, "message": str(exc)}


def node_post_run_gate(state: BulkRNAState) -> BulkRNAState:
    """Post-run Gate: QC/OOD/confidence refusal -> ABSTAIN or PASS.

    M1: reads the *real* QC summary (alignment/duplication hints) produced by
    the attempt, instead of an empty report that always passes.
    """
    qc_summary: dict[str, Any] = {}
    evidence = state.get("qc_evidence")
    if isinstance(evidence, dict):
        qc_summary = dict(evidence.get("summary", {}) or {})
    else:
        # Fall back to reading the attempt manifest directly.
        session = _session(state)
        project_dir = session.project_dir
        payload = load_json(project_dir / "project.json")
        run_id = str(payload.get("status", {}).get("run_id") or "").strip()
        attempt_dir = project_dir / "attempts" / run_id if run_id else None
        if attempt_dir is not None and (attempt_dir / "result_manifest.json").is_file():
            manifest = load_json(attempt_dir / "result_manifest.json")
            qc_summary = dict(manifest.get("summary", {}) or {})

    result = post_run_gate({}, qc_report=qc_summary)
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
        return "diffexp_gate"
    return "end_fail"


def _edge_after_diffexp_gate(state: BulkRNAState) -> str:
    """Diffexp gate passed (or skipped) -> freeze the contract, then run."""
    if state.get("status") == PASS:
        return "confirm_contract"
    return "end_fail"


def _edge_after_contract(state: BulkRNAState) -> str:
    """Contract frozen -> execute the QC stage first (说明书 §5.1).

    Counts 直入（cms.run_mode == "counts" / study.input.counts_matrix）没有
    FASTQ 主流程；直接执行 ``counts`` 阶段（DESeq2/CMScaller 读上传矩阵）。
    """
    if state.get("status") == CONFIRMED:
        return "execute_qc"
    return "end_fail"


def _is_counts_entry_state(state: BulkRNAState) -> bool:
    """Whether the bound project is a counts 直入 project (no FASTQ stages)."""
    try:
        session = _session(state)
        config = session.config
    except Exception:  # noqa: BLE001 - no config yet -> default FASTQ path
        return False
    if config is None:
        return False
    from .run_agent import _counts_entry

    return _counts_entry(config)


def _edge_after_execute_qc(state: BulkRNAState) -> str:
    """QC stage reached a terminal state -> QC checkpoint / halt.

    Counts 直入项目没有 FASTQ QC 检查点；counts 阶段完成后直接校验产物。
    """
    if _is_counts_entry_state(state):
        if state.get("status") in {"PASS", "WAITING_HPC"}:
            return "validate_output"
        return "end_fail"
    if state.get("status") in {PASS, "WAITING_HPC"}:
        return "wait_qc"
    return "end_fail"


def _edge_after_qc(state: BulkRNAState) -> str:
    """QC decision recorded -> run the validated outputs through gates."""
    if state.get("status") == PASS:
        return "validate_output"
    return "end_fail"


def _edge_after_validate(state: BulkRNAState) -> str:
    if state.get("status") == PASS:
        return "post_run_gate"
    return "end_fail"


def _edge_after_post_run(state: BulkRNAState) -> str:
    if state.get("status") == PASS:
        return "report"
    return "end_fail"


def build_bulk_rna_graph(checkpointer: Any | None = None):
    """Build the LangGraph control-plane graph.

    The graph is deliberately a straight line with a QC interrupt; it drives
    the audited ProjectSession rather than issuing shell commands itself.

    ``checkpointer`` (optional) enables durable runs: pass a LangGraph
    checkpointer (e.g. :func:`sqlite_checkpointer_for`) and invoke with
    ``{"configurable": {"thread_id": project_id}}`` so a graph run survives
    restarts and resumes from interrupts (gap #3).
    """
    if StateGraph is None:  # pragma: no cover - langgraph not installed
        raise ImportError("langgraph is required to build the control-plane graph")

    builder = StateGraph(BulkRNAState)
    builder.add_node("gate_a", node_gate_a)
    builder.add_node("adapter_plan", node_adapter_plan)
    builder.add_node("diffexp_gate", node_diffexp_gate)
    builder.add_node("confirm_contract", node_confirm_contract)
    builder.add_node("execute_qc", node_execute_qc)
    builder.add_node("wait_qc", node_wait_qc)
    builder.add_node("validate_output", node_validate_output)
    builder.add_node("post_run_gate", node_post_run_gate)
    builder.add_node("report", node_report)
    builder.add_node("end_fail", node_end_fail)

    builder.add_edge(START, "gate_a")
    builder.add_conditional_edges("gate_a", _edge_after_gate_a)
    builder.add_conditional_edges("adapter_plan", _edge_after_plan)
    builder.add_conditional_edges("diffexp_gate", _edge_after_diffexp_gate)
    builder.add_conditional_edges("confirm_contract", _edge_after_contract)
    builder.add_conditional_edges("execute_qc", _edge_after_execute_qc)
    builder.add_conditional_edges("wait_qc", _edge_after_qc)
    builder.add_conditional_edges("validate_output", _edge_after_validate)
    builder.add_conditional_edges("post_run_gate", _edge_after_post_run)
    builder.add_edge("report", END)
    builder.add_edge("end_fail", END)
    return builder.compile(checkpointer=checkpointer)


CHECKPOINT_FILE = "langgraph.sqlite3"


def sqlite_checkpointer_for(project_dir: Path):
    """Open a per-project SQLite checkpointer (context manager).

    Usage::

        with sqlite_checkpointer_for(project_dir) as checkpointer:
            graph = build_bulk_rna_graph(checkpointer)
            result = graph.invoke(state, config={"configurable": {"thread_id": project_id}})

    The file lives next to the session so a restart of the server can resume
    the exact same thread (UI spec section 6: four states survive restarts).
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    db_path = project_dir / CHECKPOINT_FILE
    return SqliteSaver.from_conn_string(str(db_path))
