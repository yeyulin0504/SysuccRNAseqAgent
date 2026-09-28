"""Static packaging contract for the declarative capability registry."""

from pathlib import Path
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def test_capability_registry_is_included_by_manifest() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include references capability-registry.json" in manifest
    assert "recursive-include src/rnaseq_agent/resources *.json" in manifest


def test_pyproject_declares_package_data_for_registry() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "package-data" in pyproject
    assert "capability-registry.json" in pyproject


def test_built_wheel_contains_package_registry(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "No module named build" in result.stderr:
        import pytest

        pytest.skip("build module is unavailable")
    assert result.returncode == 0, result.stderr
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        assert "rnaseq_agent/resources/capability-registry.json" in archive.namelist()
