"""Conversational project-session state machine with change-set audit.

This module implements the MVP of the interactive, auditable workflow loop
described in the design document: discuss -> confirm -> execute -> rollback
-> continue. It deliberately reuses the existing execution machinery
(pipeline rendering, analysis contract, run_agent) and adds two things the
design requires and the codebase was missing:

- a lightweight capability-aware session state machine;
- a ChangeSet audit trail: every user modification is recorded to a
  ``changesets.jsonl`` file, so the full history is traceable and reverting
  a step produces a new recorded change rather than silently overwriting.

Session states:

    idle -> drafting -> planned -> confirmed -> executing -> completed|failed
              ^           |
              +-- edit ---+   (recorded as a ChangeSet; plan is rebuilt)
              +-- rollback-+   (restore previous recorded config; plan rebuilt)
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .actions import (
    generate_project_report,
    load_project_config,
    prepare_project_config,
    validate_project,
)
from .adapter import adapter_inspect, adapter_materialize, adapter_plan
from .analysis_contract import create_project_contract, verify_project_contract
from .capability import (
    ExecutionPlan,
    GateResult,
    NOT_EVALUABLE,
    gate_a_check,
    list_capabilities,
    resolve_capability,
)
from .output_validator import mark_stale_downstream, register_artifacts
from .run_agent import run_project
from .storage import append_jsonl, load_json, save_json

SESSION_FILE = "session.json"
CHANGESET_FILE = "changesets.jsonl"
# 框架 15.2 冻结的 bulk RNA 黄金路线能力槽。
DEFAULT_CAPABILITY_ID = "workflow.bulk_rna.grch38_pe_expression_fusion"

# States in the MVP session machine (framework section 6.3).
IDLE = "idle"
DRAFTING = "drafting"
PLANNED = "planned"
CONFIRMED = "confirmed"
EXECUTING = "executing"
COMPLETED = "completed"
FAILED = "failed"
WAITING_USER = "WAITING_USER"  # 框架：等待人工确认（QC 检查点）
WAITING_HPC = "WAITING_HPC"  # 框架：作业已提交，等待外部状态
STALE = "STALE"  # 框架：上游变化导致旧产物失效


class SessionError(RuntimeError):
    pass


class ProjectSession:
    """Stateful, auditable project session bound to one project directory."""

    def __init__(
        self,
        project_dir: Path,
        *,
        capability_id: str = DEFAULT_CAPABILITY_ID,
    ) -> None:
        self.project_dir = project_dir.resolve()
        self.capability_id = capability_id
        # The session owns its directory: project.json lives next to
        # session.json instead of being nested under <dir>/<project_id>/.
        self.config_path = self.project_dir / "project.json"
        self.config: dict[str, Any] | None = None
        self.execution_plan: ExecutionPlan | None = None
        self.state = IDLE
        self.capability = resolve_capability(capability_id)

    def _project_id_for_dir(self) -> str:
        return self.project_dir.name

    # -- file layout ----------------------------------------------------

    @property
    def session_path(self) -> Path:
        return self.project_dir / SESSION_FILE

    @property
    def changeset_path(self) -> Path:
        return self.project_dir / CHANGESET_FILE

    def _log(self, event: str, payload: dict[str, Any]) -> None:
        append_jsonl(
            self.changeset_path,
            {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "state": self.state,
                "event": event,
                **payload,
            },
        )

    # -- persistence ----------------------------------------------------

    def _save_session(self) -> None:
        save_json(
            self.session_path,
            {
                "schema_version": 1,
                "state": self.state,
                "capability_id": self.capability_id,
                "config_path": str(self.config_path),
                "plan": _plan_to_dict(self.execution_plan) if self.execution_plan else None,
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )

    def load_session(self) -> bool:
        if not self.session_path.is_file():
            return False
        payload = load_json(self.session_path)
        self.state = str(payload.get("state", IDLE))
        self.capability_id = str(payload.get("capability_id", DEFAULT_CAPABILITY_ID))
        self.capability = resolve_capability(self.capability_id)
        config_path = Path(str(payload.get("config_path", "")))
        if config_path.is_file():
            self.config_path = config_path.resolve()
            self.config = load_project_config(self.config_path)
        plan = payload.get("plan")
        if isinstance(plan, dict) and plan.get("config") and self.config is None:
            self.config = plan["config"]
        self.execution_plan = _plan_from_dict(plan) if isinstance(plan, dict) else None
        return True

    # -- session flow ---------------------------------------------------

    def new_project(self, config: dict[str, Any]) -> GateResult:
        """Create a new project config and move to the drafting state."""
        self._require_state(IDLE)
        normalized = prepare_project_config(config)
        save_json(self.config_path, normalized)
        self.config = load_project_config(self.config_path)
        self.state = DRAFTING
        self.execution_plan = None
        self._log(
            "project_created",
            {
                "config": str(self.config_path),
                "new_config": deepcopy(self.config),
            },
        )
        self._save_session()
        return self.gate_check()

    def gate_check(self) -> GateResult:
        """Run the metadata-level Gate-A check against the current draft."""
        self._require_project()
        assert self.config is not None
        config, validation = validate_project(self.config_path)
        gate = gate_a_check(
            self.capability,
            config,
            validation_errors=validation.errors,
            missing_files=[str(path) for path in validation.missing_files],
        )
        if gate.verdict == NOT_EVALUABLE:
            self.state = DRAFTING
        return gate

    def plan(self) -> ExecutionPlan:
        """Adapter.plan: read-only pre-check then build a deterministic plan.

        Framework call chain: Gate-A -> Adapter.inspect/plan. The adapter
        inspects the draft (read-only) and produces the ExecutionPlan that
        the user will confirm before freezing.
        """
        self._require_state(DRAFTING, PLANNED, CONFIRMED)
        assert self.config is not None
        config, validation = validate_project(self.config_path)
        inspection = adapter_inspect(
            self.capability,
            config,
            validation_errors=validation.errors,
            missing_files=[str(path) for path in validation.missing_files],
        )
        self.execution_plan = adapter_plan(self.capability, self.config, inspection=inspection)
        self.state = PLANNED
        self._save_session()
        return self.execution_plan

    def edit(
        self,
        patch: dict[str, Any],
        *,
        note: str = "",
    ) -> GateResult:
        """Apply a user-approved parameter change and record a ChangeSet.

        Editing is only allowed while the plan is not frozen. Applying the
        patch writes a new config snapshot, appends a ChangeSet to the audit
        trail, drops the stale plan, and re-runs the Gate-A check.
        """
        self._require_state(DRAFTING, PLANNED)
        assert self.config is not None
        previous = deepcopy(self.config)
        updated = deepcopy(self.config)
        _apply_patch(updated, patch)
        if updated == previous:
            raise SessionError("修改没有产生任何变化，已忽略。")

        save_json(self.config_path, updated)
        self.config = load_project_config(self.config_path)
        self.state = DRAFTING
        self.execution_plan = None
        # Framework 6.2: ChangeSet invalidates affected downstream artifacts.
        try:
            stale = mark_stale_downstream(self.project_dir, "project_config")
        except OSError:
            stale = []
        self._log(
            "changeset_applied",
            {
                "note": note,
                "patch": patch,
                "previous_config_sha256": _config_sha256(previous),
                "new_config_sha256": _config_sha256(self.config),
                "previous_config": deepcopy(previous),
                "new_config": deepcopy(self.config),
                "stale_artifacts": stale,
            },
        )
        self._save_session()
        return self.gate_check()

    def rollback(self, target_index: int | None = None) -> bool:
        """Roll back to a previously recorded config snapshot.

        ``target_index`` is the 1-based index of the ChangeSet whose config we
        want to restore; by default the last change is rolled back. Rolling
        back produces a new ChangeSet instead of silently overwriting state.
        """
        self._require_state(DRAFTING, PLANNED, CONFIRMED)
        entries = _load_changesets(self.changeset_path)
        if not entries:
            raise SessionError("没有可回滚的历史记录。")

        restored: dict[str, Any] | None = None
        restored_event = ""
        if target_index is not None:
            for entry in entries:
                if entry.get("index") == target_index and entry.get("new_config"):
                    # Restoring to the state right AFTER that change's snapshot.
                    restored = entry["new_config"]
                    restored_event = entry.get("event", "changeset_applied")
                    break
            if restored is None:
                raise SessionError(f"没有找到第 {target_index} 次变更记录。")
        else:
            # Stack-based rollback: restore the state BEFORE the most recent
            # change that produced the config we currently hold. Matching on
            # the sha256 of the live config makes repeated rollback() calls
            # walk backwards one ChangeSet at a time instead of always
            # replaying the same snapshot.
            if self.state == CONFIRMED:
                # Undoing a freeze means returning to the editable draft that
                # existed right before the contract was frozen. The config
                # itself is unchanged; we just release the lock on it.
                for entry in reversed(entries):
                    if entry.get("event") == "contract_frozen" and entry.get("previous_config"):
                        restored = entry["previous_config"]
                        restored_event = "contract_frozen"
                        break
            else:
                current_sha = _config_sha256(self.config)
                for entry in reversed(entries):
                    if (
                        entry.get("event") == "changeset_applied"
                        and entry.get("new_config")
                        and entry.get("previous_config")
                        and _config_sha256(entry["new_config"]) == current_sha
                    ):
                        restored = entry["previous_config"]
                        restored_event = entry.get("event", "changeset_applied")
                        break
            if restored is None:
                # Fall back: if the current config wasn't produced by a
                # changeset (e.g. we are CONFIRMED right after a freeze),
                # restore the snapshot taken at the most recent event.
                for entry in reversed(entries):
                    if entry.get("previous_config"):
                        restored = entry["previous_config"]
                        restored_event = entry.get("event", "changeset_applied")
                        break
            if restored is None:
                raise SessionError("最近一次变更没有可恢复的配置快照。")

        save_json(self.config_path, restored)
        self.config = load_project_config(self.config_path)
        self.state = DRAFTING
        self.execution_plan = None
        self._log(
            "rollback",
            {
                "restored_event": restored_event,
                "target_index": target_index,
                "restored_config_sha256": _config_sha256(self.config),
            },
        )
        self._save_session()
        return True

    def confirm(self) -> dict[str, Any]:
        """Freeze the plan into an immutable Analysis Contract.

        After freezing, the Adapter materializes the actual run inputs
        (rendered scripts) into the project directory, ready for the
        Execution Gateway.
        """
        self._require_state(PLANNED)
        assert self.execution_plan is not None
        assert self.config is not None
        if self.execution_plan.summary.startswith("Adapter 预检未通过"):
            raise SessionError("Adapter 预检未通过，无法冻结契约。请先修正配置。")
        config_path, contract = create_project_contract(self.config_path)
        # Framework 5.2: Analysis Contract -> Adapter.materialize.
        self.config = load_project_config(self.config_path)
        materialized = adapter_materialize(config_path, self.config)
        self.state = CONFIRMED
        self._log(
            "contract_frozen",
            {
                "contract_id": contract["contract_id"],
                "contract_path": str(config_path),
                "materialized_scripts": {k: str(v) for k, v in materialized.items()},
                "previous_config": deepcopy(self.config),
            },
        )
        self._save_session()
        return contract

    def execute(self, *, wait: bool = True) -> dict[str, Any]:
        """Run the frozen project through the execution gateway."""
        self._require_state(CONFIRMED, EXECUTING)
        self.state = EXECUTING
        self._save_session()
        outcome = run_project(self.config_path, wait=wait)
        self.state = outcome.state
        self._log(
            "execution_finished",
            {"state": outcome.state, "message": outcome.message},
        )
        self._save_session()
        return {"state": outcome.state, "message": outcome.message}

    def refresh_status(self) -> dict[str, Any]:
        from .run_agent import refresh_status

        self._require_project()
        status = refresh_status(self.config_path)
        state = str(status.get("state", ""))
        if state in {"remote_completed", "completed"}:
            self.state = COMPLETED
        elif state in {"failed", "run_failed", "validation_failed", "policy_failed", "download_failed", "result_validation_failed"}:
            self.state = FAILED
        self._save_session()
        return status

    def report(self) -> Path:
        self._require_project()
        return generate_project_report(self.config_path)

    # -- helpers --------------------------------------------------------

    def _require_project(self) -> None:
        if self.config is None or not self.config_path.is_file():
            raise SessionError("当前会话还没有项目配置，请先 new。")

    def _require_state(self, *allowed: str) -> None:
        if self.state not in allowed:
            allowed_text = "/".join(allowed)
            raise SessionError(f"当前状态 {self.state} 不允许该操作；允许的状态：{allowed_text}。")

    def summary_lines(self) -> list[str]:
        lines = [
            f"能力：{self.capability.capability_id} @ {self.capability.version}",
            f"状态：{self.state}",
            f"项目配置：{self.config_path}",
        ]
        if self.config:
            project = self.config.get("project", {})
            lines.append(f"项目：{project.get('id', 'unknown')}（{project.get('title', '')}）")
            items = self.config.get("samples", {}).get("items", [])
            lines.append(f"样本数：{len(items)}")
            server = self.config.get("server", {})
            lines.append(
                f"调度器：{server.get('scheduler', 'unknown')}，"
                f"远程目录：{server.get('remote_workdir', '')}"
            )
            # 框架 15.3：条件开放阶段可见性。
            if self.config.get("pipeline", {}).get("diffexp", {}).get("enabled"):
                from .differential import DEG_DESIGN_FORMULA, diffexp_design_of

                design = diffexp_design_of(self.config)
                lines.append(
                    f"差异表达（条件开放）：DESeq2 {DEG_DESIGN_FORMULA}，"
                    f"对比 {design['contrast']}（reference={design['reference_condition']}）"
                )
            elif self.config.get("diffexp"):
                lines.append("差异表达：未启用（DESeq2 条件开放，可对会话说“打开差异表达”）。")
        if self.execution_plan:
            lines.append("已生成执行计划：")
            lines.extend(f"  - {step}" for step in self.execution_plan.steps)
        return lines


def run_project_session(project_dir: Path) -> ProjectSession:
    session = ProjectSession(project_dir)
    session.load_session()
    return session


# -- config helpers ------------------------------------------------------


def _apply_patch(config: dict[str, Any], patch: dict[str, Any]) -> None:
    """Apply a shallow top-level patch with a few nested conveniences."""
    for key, value in patch.items():
        if key == "samples.items" and isinstance(value, list):
            config.setdefault("samples", {})["items"] = deepcopy(value)
        elif key == "pipeline" and isinstance(value, dict):
            config.setdefault("pipeline", {}).update(deepcopy(value))
        elif key == "server" and isinstance(value, dict):
            config.setdefault("server", {}).update(deepcopy(value))
        elif key == "reference" and isinstance(value, dict):
            config.setdefault("reference", {}).update(deepcopy(value))
        else:
            config[key] = deepcopy(value)


def _config_sha256(config: dict[str, Any]) -> str:
    from .analysis_contract import canonical_sha256

    return canonical_sha256(config)


def _load_changesets(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            import json

            entries.append(json.loads(line))
        except ValueError:
            continue
    for index, entry in enumerate(entries, start=1):
        entry.setdefault("index", index)
    return entries


def _plan_to_dict(plan: ExecutionPlan) -> dict[str, Any]:
    return {
        "capability_id": plan.capability_id,
        "config": plan.config,
        "steps": plan.steps,
        "summary": plan.summary,
    }


def _plan_from_dict(payload: dict[str, Any]) -> ExecutionPlan | None:
    try:
        return ExecutionPlan(
            capability_id=str(payload.get("capability_id", "")),
            config=payload.get("config", {}),
            steps=list(payload.get("steps", [])),
            summary=str(payload.get("summary", "")),
        )
    except (TypeError, ValueError):
        return None
