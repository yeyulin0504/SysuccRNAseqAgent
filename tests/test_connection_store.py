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

from rnaseq_agent.agent_tools import (
    TOOL_MODE_APPROVED_EXECUTE,
    TOOL_MODE_DISABLED,
    TOOL_MODE_READ_ONLY,
)
from rnaseq_agent.connection_store import (
    CONNECTION_FILE_NAME,
    apply_connection_to_config,
    apply_llm_to_config,
    clear_password,
    connection_file_path,
    llm_model_name,
    load_connection,
    load_llm,
    save_connection,
    save_llm,
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


class TestLlmStore:
    """大模型接入同样是「配一次、所有项目共用、永久保存」。

    用户的诉求是：在设置页填过大模型（provider / api_base / model / key）
    之后，任何项目里的对话都应走 LLM，而不是回退成规则式答复。因此 LLM
    配置与服务器连接一样存进用户级配置文件，api_key 同样 DPAPI 加密。
    """

    def test_round_trips_llm_fields(self, store_dir: Path) -> None:
        save_llm(
            {
                "enabled": True,
                "provider": "paratera",
                "api_base": "https://llmapi.paratera.com/v1",
                "model": "DeepSeek-V4-Flash",
            },
            store_dir=store_dir,
        )

        loaded = load_llm(store_dir=store_dir)

        assert loaded["enabled"] is True
        assert loaded["provider"] == "paratera"
        assert loaded["api_base"] == "https://llmapi.paratera.com/v1"
        assert loaded["model"] == "DeepSeek-V4-Flash"

    def test_api_key_is_never_stored_in_plaintext(self, store_dir: Path) -> None:
        save_llm({"api_key": "sk-TopSecret123"}, store_dir=store_dir)

        raw = (store_dir / CONNECTION_FILE_NAME).read_text(encoding="utf-8")
        assert "sk-TopSecret123" not in raw

    def test_api_key_round_trips_through_encryption(self, store_dir: Path) -> None:
        save_llm({"enabled": True, "api_key": "sk-TopSecret123"}, store_dir=store_dir)

        assert load_llm(store_dir=store_dir)["api_key"] == "sk-TopSecret123"

    def test_blank_api_key_keeps_the_stored_one(self, store_dir: Path) -> None:
        save_llm({"api_key": "sk-original"}, store_dir=store_dir)
        save_llm({"model": "m2"}, store_dir=store_dir)

        loaded = load_llm(store_dir=store_dir)

        assert loaded["api_key"] == "sk-original"
        assert loaded["model"] == "m2"

    def test_explicit_clear_drops_api_key(self, store_dir: Path) -> None:
        save_llm({"api_key": "sk-original"}, store_dir=store_dir)
        save_llm({"api_key": ""}, store_dir=store_dir)

        assert not load_llm(store_dir=store_dir).get("api_key")

    def test_llm_and_connection_coexist_in_one_file(self, store_dir: Path) -> None:
        save_connection({"host": "h", "user": "u"}, store_dir=store_dir)
        save_llm({"enabled": True, "model": "m"}, store_dir=store_dir)

        assert load_connection(store_dir=store_dir)["host"] == "h"
        assert load_llm(store_dir=store_dir)["model"] == "m"

    def test_load_llm_missing_file_returns_empty(self, store_dir: Path) -> None:
        assert load_llm(store_dir=store_dir) == {}

    def test_tool_mode_round_trips_with_the_user_level_llm_settings(self, store_dir: Path) -> None:
        save_llm({"enabled": True, "tool_mode": TOOL_MODE_READ_ONLY}, store_dir=store_dir)

        assert load_llm(store_dir=store_dir)["tool_mode"] == TOOL_MODE_READ_ONLY

    def test_existing_settings_default_to_full_approved_execution(self, store_dir: Path) -> None:
        save_llm({"enabled": True, "model": "m"}, store_dir=store_dir)

        assert load_llm(store_dir=store_dir)["tool_mode"] == TOOL_MODE_APPROVED_EXECUTE

    @pytest.mark.parametrize(
        "raw",
        [
            "{not json",
            "[]",
            json.dumps({"llm": []}),
        ],
    )
    def test_corrupt_document_or_llm_block_loads_tool_mode_fail_closed(
        self, store_dir: Path, raw: str
    ) -> None:
        store_dir.mkdir(parents=True)
        (store_dir / CONNECTION_FILE_NAME).write_text(raw, encoding="utf-8")

        assert load_llm(store_dir=store_dir)["tool_mode"] == TOOL_MODE_DISABLED

    def test_persisted_explicit_null_tool_mode_loads_fail_closed(
        self, store_dir: Path
    ) -> None:
        store_dir.mkdir(parents=True)
        (store_dir / CONNECTION_FILE_NAME).write_text(
            json.dumps({"llm": {"enabled": True, "tool_mode": None}}),
            encoding="utf-8",
        )

        assert load_llm(store_dir=store_dir)["tool_mode"] == TOOL_MODE_DISABLED

    def test_save_rejects_explicit_null_tool_mode(self, store_dir: Path) -> None:
        with pytest.raises(ValueError, match="tool_mode"):
            save_llm({"tool_mode": None}, store_dir=store_dir)

        assert load_llm(store_dir=store_dir) == {}

    def test_invalid_tool_mode_is_rejected_instead_of_silently_broadening_access(
        self, store_dir: Path
    ) -> None:
        with pytest.raises(ValueError, match="tool_mode"):
            save_llm({"tool_mode": "unlimited"}, store_dir=store_dir)

        assert load_llm(store_dir=store_dir) == {}

    @pytest.mark.parametrize("value", [False, 0, [], {}])
    def test_non_string_tool_modes_cannot_fall_back_to_full_access(
        self, store_dir: Path, value: object
    ) -> None:
        with pytest.raises(ValueError, match="tool_mode"):
            save_llm({"tool_mode": value}, store_dir=store_dir)

        assert load_llm(store_dir=store_dir) == {}

    @pytest.mark.parametrize("value", ["unlimited", False, 0, [], {}])
    def test_corrupt_persisted_tool_mode_loads_fail_closed(
        self, store_dir: Path, value: object
    ) -> None:
        store_dir.mkdir(parents=True)
        (store_dir / CONNECTION_FILE_NAME).write_text(
            json.dumps({"llm": {"enabled": True, "tool_mode": value}}),
            encoding="utf-8",
        )

        assert load_llm(store_dir=store_dir)["tool_mode"] == TOOL_MODE_DISABLED

    def test_disabled_tool_mode_coexists_with_the_api_key(self, store_dir: Path) -> None:
        save_llm(
            {"api_key": "sk-secret", "tool_mode": TOOL_MODE_DISABLED},
            store_dir=store_dir,
        )

        loaded = load_llm(store_dir=store_dir)
        assert loaded["api_key"] == "sk-secret"
        assert loaded["tool_mode"] == TOOL_MODE_DISABLED


class TestApplyLlmToConfig:
    def test_fills_empty_llm_block(self) -> None:
        config = {"project": {"id": "demo01"}, "llm": {"enabled": False, "api_key": ""}}

        merged = apply_llm_to_config(
            config,
            {"enabled": True, "api_base": "https://llm.example/v1", "model": "m", "api_key": "k"},
        )

        assert merged["llm"]["enabled"] is True
        assert merged["llm"]["api_base"] == "https://llm.example/v1"
        assert merged["llm"]["api_key"] == "k"
        assert merged["project"]["id"] == "demo01"

    def test_project_specific_llm_wins_over_shared(self) -> None:
        config = {"llm": {"enabled": False, "model": "project-model"}}

        merged = apply_llm_to_config(config, {"enabled": True, "model": "shared-model"})

        assert merged["llm"]["model"] == "project-model"
        assert merged["llm"]["enabled"] is True  # 共享的 enabled 补齐

    def test_shared_tool_mode_overrides_a_stale_project_value(self) -> None:
        merged = apply_llm_to_config(
            {"llm": {"tool_mode": TOOL_MODE_APPROVED_EXECUTE}},
            {"tool_mode": TOOL_MODE_DISABLED},
        )

        assert merged["llm"]["tool_mode"] == TOOL_MODE_DISABLED

    def test_empty_shared_llm_leaves_config_untouched(self) -> None:
        config = {"llm": {"model": "project-model"}}

        merged = apply_llm_to_config(config, {})

        assert merged["llm"]["model"] == "project-model"

    def test_does_not_mutate_input_config(self) -> None:
        config = {"llm": {"model": ""}}
        apply_llm_to_config(config, {"model": "shared"})

        assert config["llm"]["model"] == ""

    def test_secret_is_not_leaked_into_config_by_connection_merge(self) -> None:
        config = {"llm": {"model": "m"}}
        merged = apply_llm_to_config(config, {"api_key": "sk-secret"})

        # 需要 api_key 才能调用 LLM，因此它必须进入运行时 config；
        # 但 _editable_config 负责在下发前端前抹掉它（见 webapp 测试）。
        assert merged["llm"]["api_key"] == "sk-secret"


class TestLlmModelName:
    """模型名统一从 ``config["llm"]["model"]`` 读取。

    思考过程与状态行此前直接对顶层取 ``model``，永远取空，界面只好显示
    「未指定模型」。收口到这里后调用方不必再记得层级。
    """

    def test_reads_nested_model(self) -> None:
        assert llm_model_name({"llm": {"model": "DeepSeek-V4-Flash"}}) == "DeepSeek-V4-Flash"

    def test_strips_whitespace(self) -> None:
        assert llm_model_name({"llm": {"model": "  m  "}}) == "m"

    def test_top_level_model_is_not_used(self) -> None:
        # 顶层 model 不是配置契约的一部分，误读会掩盖真实缺配。
        assert llm_model_name({"model": "top-level"}) == ""

    def test_missing_or_odd_shapes_return_empty(self) -> None:
        assert llm_model_name(None) == ""
        assert llm_model_name({}) == ""
        assert llm_model_name({"llm": None}) == ""
        assert llm_model_name({"llm": {"model": None}}) == ""
        assert llm_model_name({"llm": {"model": "   "}}) == ""
        assert llm_model_name({"llm": "not-a-dict"}) == ""
