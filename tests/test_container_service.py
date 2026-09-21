from __future__ import annotations

import pytest

from rnaseq_agent.container_service import (
    ContainerSettingsError,
    build_pull_command,
    build_test_command,
    validate_image_settings,
)


def _settings() -> dict:
    return {
        "enabled": True,
        "engine": "apptainer",
        "image_uri": "docker://ghcr.io/sysucc/rnaseq-downstream:2026.09",
        "image_path": "/hwdata/home/yeyulin/containers/rnaseq-downstream-2026.09.sif",
        "bind_paths": ["/hwdata/home/yeyulin/projects"],
    }


def test_validates_downstream_image_settings() -> None:
    assert validate_image_settings(_settings())["engine"] == "apptainer"


@pytest.mark.parametrize(
    "patch",
    [
        {"image_uri": "https://example.invalid/image"},
        {"image_path": "relative/image.sif"},
        {"image_path": "/tmp/image.img"},
        {"engine": "docker", "image_uri": ""},
    ],
)
def test_rejects_unsafe_image_settings(patch: dict) -> None:
    settings = {**_settings(), **patch}
    with pytest.raises(ContainerSettingsError):
        validate_image_settings(settings)


def test_builds_fixed_apptainer_pull_command() -> None:
    command = build_pull_command(_settings())
    assert "apptainer pull --force" in command
    assert "/hwdata/home/yeyulin/containers/rnaseq-downstream-2026.09.sif" in command
    assert "docker://ghcr.io/sysucc/rnaseq-downstream:2026.09" in command


def test_test_command_uses_cleanenv_binds_and_r_package_probe() -> None:
    command = build_test_command(_settings())
    assert "apptainer --version" in command
    assert "exec --cleanenv" in command
    assert "--bind /hwdata/home/yeyulin/projects" in command
    assert "loadNamespace" in command and "DESeq2" in command
    assert "CMScaller" in command


def test_docker_engine_uses_image_uri_without_sif_path() -> None:
    settings = {
        "enabled": True,
        "engine": "docker",
        "image_uri": "ghcr.io/sysucc/rnaseq-downstream:2026.09",
        "image_path": "",
        "bind_paths": ["/data/project"],
    }
    assert validate_image_settings(settings)["engine"] == "docker"
    assert build_pull_command(settings) == "docker pull ghcr.io/sysucc/rnaseq-downstream:2026.09"
    command = build_test_command(settings)
    assert "docker run --rm" in command
    assert "-v /data/project:/data/project" in command
