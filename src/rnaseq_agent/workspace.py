"""Workspace registry: multiple projects under one local workspace root.

UI design (2026-09-07): a localhost research workbench with a project home
page (create / open / archive), each project owning multiple conversations
and a full analysis trail::

    Project
    ├── Thread A ── Message...
    ├── Thread B ── Message...
    ├── Spec Revision 1
    │   └── Run Attempt 1 ── Artifact...
    └── Spec Revision 2
        └── Run Attempt 2 ── Artifact...

This module owns the *directory layout* and the *project registry* only.
Thread/message storage lives in :mod:`rnaseq_agent.threads`. Scientific
state (config, changesets, attempts, artifacts) stays in the existing
ProjectSession/run_agent files under each project directory.

Rules enforced here (framework section 6 / UI doc section 3):

- a project id belongs to at most one project;
- archiving a project never deletes its runs/artifacts (re-open restores);
- deleting is not offered in v1 (UI doc: only archive in first release).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .project_intake import derive_visible_state
from .storage import load_json, save_json

WORKSPACE_FILE = "workspace.json"
PROJECT_MARKER = "session.json"  # a project dir is one that hosts a session.json


class WorkspaceError(RuntimeError):
    pass


def default_workspace_dir() -> Path:
    return Path("runs/workspace")


class Workspace:
    """Registry of projects under one root directory."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root or default_workspace_dir()).resolve()
        self.registry_path = self.root / WORKSPACE_FILE

    # -- registry helpers ------------------------------------------------

    def _load_registry(self) -> dict[str, Any]:
        if not self.registry_path.is_file():
            return {"schema_version": 1, "projects": {}}
        return load_json(self.registry_path)

    def _save_registry(self, registry: dict[str, Any]) -> None:
        save_json(self.registry_path, registry)

    def _project_meta(self, project_id: str) -> dict[str, Any] | None:
        registry = self._load_registry()
        return registry.get("projects", {}).get(project_id)

    def project_dir(self, project_id: str) -> Path:
        """Directory that hosts the project's session/config/attempts."""
        project_id = _safe_project_id(project_id)
        return self.root / project_id

    # -- query -----------------------------------------------------------

    def list_projects(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        registry = self._load_registry()
        projects = registry.get("projects", {})
        rows: list[dict[str, Any]] = []
        for project_id, meta in projects.items():
            if not include_archived and meta.get("archived"):
                continue
            project_dir = self.project_dir(project_id)
            state = derive_visible_state(project_dir, registry_state=meta.get("state"))
            rows.append(
                {
                    "project_id": project_id,
                    "title": meta.get("title", project_id),
                    "owner": meta.get("owner", ""),
                    "created_at": meta.get("created_at", ""),
                    "updated_at": meta.get("updated_at", ""),
                    "archived": bool(meta.get("archived")),
                    "state": state,
                    "description": meta.get("description", ""),
                    "thread_count": meta.get("thread_count", 0),
                }
            )
        rows.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
        return rows

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        meta = self._project_meta(project_id)
        if meta is None:
            return None
        project_dir = self.project_dir(project_id)
        return {
            "project_id": project_id,
            "title": meta.get("title", project_id),
            "owner": meta.get("owner", ""),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "archived": bool(meta.get("archived")),
            "state": derive_visible_state(project_dir, registry_state=meta.get("state")),
            "description": meta.get("description", ""),
            "project_dir": str(project_dir),
        }

    # -- lifecycle -------------------------------------------------------

    def create_project(
        self,
        project_id: str,
        *,
        title: str = "",
        owner: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """Create a new project entry in the workspace registry."""
        project_id = _safe_project_id(project_id)
        if self._project_meta(project_id) is not None:
            raise WorkspaceError(f"项目 {project_id!r} 已存在。")
        now = _utc_now()
        registry = self._load_registry()
        registry.setdefault("projects", {})[project_id] = {
            "project_id": project_id,
            "title": title or project_id,
            "owner": owner,
            "description": description,
            "created_at": now,
            "updated_at": now,
            "archived": False,
            "state": "drafting",
            "thread_count": 0,
        }
        self._save_registry(registry)
        self.project_dir(project_id).mkdir(parents=True, exist_ok=True)
        return registry["projects"][project_id]

    def archive_project(self, project_id: str) -> dict[str, Any]:
        """Archive a project (analysis data is preserved on disk)."""
        registry = self._load_registry()
        meta = registry.get("projects", {}).get(project_id)
        if meta is None:
            raise WorkspaceError(f"项目 {project_id!r} 不存在。")
        meta["archived"] = True
        meta["updated_at"] = _utc_now()
        self._save_registry(registry)
        return meta

    def unarchive_project(self, project_id: str) -> dict[str, Any]:
        registry = self._load_registry()
        meta = registry.get("projects", {}).get(project_id)
        if meta is None:
            raise WorkspaceError(f"项目 {project_id!r} 不存在。")
        meta["archived"] = False
        meta["updated_at"] = _utc_now()
        self._save_registry(registry)
        return meta

    def touch(self, project_id: str, state: str = "") -> None:
        """Update activity/state bookkeeping (session writes real state too)."""
        registry = self._load_registry()
        meta = registry.get("projects", {}).get(project_id)
        if meta is None:
            return
        meta["updated_at"] = _utc_now()
        if state:
            meta["state"] = state
        self._save_registry(registry)

    def set_thread_count(self, project_id: str, count: int) -> None:
        registry = self._load_registry()
        meta = registry.get("projects", {}).get(project_id)
        if meta is None:
            return
        meta["thread_count"] = count
        meta["updated_at"] = _utc_now()
        self._save_registry(registry)


def _safe_project_id(project_id: str) -> str:
    from .safety import identifier_error

    project_id = str(project_id).strip()
    error = identifier_error(project_id, "project.id")
    if error:
        raise WorkspaceError(error)
    return project_id


def _infer_state(project_dir: Path) -> str:
    """Best-effort state from session.json when the registry has no entry."""
    try:
        payload = load_json(project_dir / PROJECT_MARKER)
    except (OSError, ValueError):
        return "drafting"
    return str(payload.get("state") or "drafting")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
