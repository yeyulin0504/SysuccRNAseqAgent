"""Static packaging contract for the declarative capability registry."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_capability_registry_is_included_by_manifest() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include references capability-registry.json" in manifest
