"""全局连接配置（所有项目共用、永久保存）。

用户需求：服务器连接只需配置一次，之后所有项目自动继承，重启程序后
仍然生效（包括密码）。本模块把连接信息落到一个用户级配置文件，
密码在 Windows 上用 DPAPI 加密，只有当前 Windows 用户能解密。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from rnaseq_agent.connection_store import (
    CONNECTION_FILE_NAME,
    apply_connection_to_config,
    clear_password,
    connection_file_path,
    load_connection,
    save_connection,
)


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    return tmp_path / "conn"


class TestConnectionFilePath:
    def test_default_path_lives_under_user_home(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("RNASEQ_AGENT_HOME", str(tmp_path / "home"))
        path = connection_file_path()
        assert path.name == CONNECTION_FILE_NAME
        assert (tmp_path / "home") in path.parents


class TestSaveAndLoad:
    def test_round_trips_non_secret_fields(self, store_dir: Path) -> None:
        save_connection(
            {
                "host": "10.30.24.1",
                "user": "yeyulin",
                "port": 22,
                "scheduler": "slurm",
                "remote_base_dir": "/hwdata/home/yeyulin/",
                "remote_workdir": "/hwdata/home/yeyulin/demo",
                "auth_mode": "password",
            },
            store_dir=store_dir,
        )

        loaded = load_connection(store_dir=store_dir)

        assert loaded["host"] == "10.30.24.1"
        assert loaded["user"] == "yeyulin"
        assert loaded["port"] == 22
        assert loaded["scheduler"] == "slurm"
        assert loaded["auth_mode"] == "password"

    def test_password_is_never_stored_in_plaintext(self, store_dir: Path) -> None:
        save_connection(
            {"host": "h", "user": "u", "auth_mode": "password", "password": "TopSecret123"},
            store_dir=store_dir,
        )

        raw = (store_dir / CONNECTION_FILE_NAME).read_text(encoding="utf-8")
        assert "TopSecret123" not in raw

    def test_password_round_trips_through_encryption(self, store_dir: Path) -> None:
        save_connection(
            {"host": "h", "user": "u", "auth_mode": "password", "password": "TopSecret123"},
            store_dir=store_dir,
        )

        loaded = load_connection(store_dir=store_dir)

        assert loaded["password"] == "TopSecret123"

    def test_key_mode_drops_password(self, store_dir: Path) -> None:
        save_connection(
            {"host": "h", "user": "u", "auth_mode": "key", "password": "should-not-persist"},
            store_dir=store_dir,
        )

        loaded = load_connection(store_dir=store_dir)

        assert "password" not in loaded or not loaded["password"]

    def test_load_missing_file_returns_empty(self, store_dir: Path) -> None:
        assert load_connection(store_dir=store_dir) == {}

    def test_load_corrupt_file_returns_empty(self, store_dir: Path) -> None:
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / CONNECTION_FILE_NAME).write_text("{not json", encoding="utf-8")

        assert load_connection(store_dir=store_dir) == {}

    def test_saved_file_is_private_on_posix(self, store_dir: Path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX 权限位在 Windows 上不适用")
        save_connection({"host": "h", "user": "u"}, store_dir=store_dir)

        mode = (store_dir / CONNECTION_FILE_NAME).stat().st_mode
        assert not (mode & stat.S_IROTH)
        assert not (mode & stat.S_IWOTH)

    def test_save_merges_with_existing_fields(self, store_dir: Path) -> None:
        save_connection({"host": "h", "user": "u", "scheduler": "slurm"}, store_dir=store_dir)
        save_connection({"threads": 16}, store_dir=store_dir)

        loaded = load_connection(store_dir=store_dir)

        assert loaded["host"] == "h"
        assert loaded["scheduler"] == "slurm"
        assert loaded["threads"] == 16


class TestClearPassword:
    def test_clear_password_keeps_other_fields(self, store_dir: Path) -> None:
        save_connection(
            {"host": "h", "user": "u", "auth_mode": "password", "password": "pw", "scheduler": "slurm"},
            store_dir=store_dir,
        )

        clear_password(store_dir=store_dir)
        loaded = load_connection(store_dir=store_dir)

        assert not loaded.get("password")
        assert loaded["host"] == "h"
        assert loaded["scheduler"] == "slurm"


class TestApplyToConfig:
    def test_applies_connection_but_keeps_project_specific_paths(self) -> None:
        config = {
            "project": {"id": "demo01"},
            "server": {
                "host": "localhost",
                "user": "local_user",
                "remote_base_dir": "/proj/base",
                "remote_workdir": "/proj/work",
                "scheduler": "local",
            },
        }
        connection = {
            "host": "10.30.24.1",
            "user": "yeyulin",
            "port": 22,
            "scheduler": "slurm",
            "remote_base_dir": "/hwdata/home/yeyulin/",
            "remote_workdir": "/hwdata/home/yeyulin/demo",
            "auth_mode": "password",
        }

        merged = apply_connection_to_config(config, connection)

        assert merged["server"]["host"] == "10.30.24.1"
        assert merged["server"]["user"] == "yeyulin"
        assert merged["server"]["scheduler"] == "slurm"
        assert merged["server"]["remote_base_dir"] == "/hwdata/home/yeyulin/"
        assert merged["project"]["id"] == "demo01"

    def test_does_not_apply_secret_into_config(self) -> None:
        config = {"server": {"host": "localhost"}, "project": {"id": "p"}}
        merged = apply_connection_to_config(config, {"host": "h", "password": "secret"})

        assert "secret" not in json.dumps(merged)

    def test_empty_connection_leaves_config_untouched(self) -> None:
        config = {"server": {"host": "localhost", "user": "local_user"}, "project": {"id": "p"}}
        merged = apply_connection_to_config(config, {})

        assert merged["server"]["host"] == "localhost"

    def test_does_not_mutate_input_config(self) -> None:
        config = {"server": {"host": "localhost"}, "project": {"id": "p"}}
        apply_connection_to_config(config, {"host": "10.0.0.1"})

        assert config["server"]["host"] == "localhost"
