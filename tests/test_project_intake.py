from pathlib import Path

from rnaseq_agent.project_intake import (
    append_history,
    derive_visible_state,
    history_items,
    load_intake,
    save_intake,
)


def test_empty_project_defaults_to_setup(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    assert load_intake(project_dir)["state"] == "setup"
    assert derive_visible_state(project_dir, registry_state="drafting") == "setup"


def test_input_ready_comes_from_intake_when_no_session_exists(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    save_intake(project_dir, {"route": "bulk_rna", "input_type": "counts_matrix", "state": "input_ready"})

    assert derive_visible_state(project_dir) == "input_ready"


def test_session_state_overrides_intake_state(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    save_intake(project_dir, {"route": "bulk_rna", "input_type": "counts_matrix", "state": "input_ready"})
    (project_dir / "session.json").write_text('{"state":"planned"}', encoding="utf-8")

    assert derive_visible_state(project_dir) == "planned"


def test_history_items_are_project_local_and_ordered(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    first = append_history(project_dir, {"type": "input_matrix", "name": "counts.tsv", "state": "ready"})
    second = append_history(project_dir, {"type": "analysis_plan", "name": "Plan", "state": "planned"})

    rows = history_items(project_dir)
    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    assert rows[0]["type"] == "input_matrix"
    assert rows[1]["state"] == "planned"
