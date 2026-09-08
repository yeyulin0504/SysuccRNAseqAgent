from pathlib import Path
import tomllib
import unittest


class TestPyprojectMetadata(unittest.TestCase):
    def test_authors_are_project_metadata_not_optional_dependencies(self) -> None:
        root = Path(__file__).resolve().parents[1]
        payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(payload["project"]["authors"][0]["name"], "Qi Zhao")
        self.assertNotIn("authors", payload["project"].get("optional-dependencies", {}))

    def test_control_plane_installs_sqlite_checkpointer(self) -> None:
        root = Path(__file__).resolve().parents[1]
        payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = payload["project"]["optional-dependencies"]["control-plane"]
        self.assertTrue(
            any(item.startswith("langgraph-checkpoint-sqlite") for item in dependencies)
        )


if __name__ == "__main__":
    unittest.main()
