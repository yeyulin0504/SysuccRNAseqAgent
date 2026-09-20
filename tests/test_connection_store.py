"""全局连接配置（所有项目共用、永久保存）。

用户需求：服务器连接只需配置一次，之后所有项目自动继承，重启程序后
仍然生效（包括密码）。本模块把连接信息落到一个用户级配置文件，
密码在 Windows 上用 DPAPI 加密，只有当前 Windows 用户能解密。
"""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import threading
import time
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
    ApprovedDataRoot,
    BrowsePolicyConflictError,
    browse_policy_revision,
    load_browse_policy,
    list_approved_data_roots,
    approve_data_root,
    revoke_data_root,
)
from rnaseq_agent.ssh_identity import SSHIdentity
from rnaseq_agent.storage import ConnectionStoreCorruptError
from rnaseq_agent import storage


def _save_connection_worker(store_dir: Path, barrier) -> None:
    os.environ["RNASEQ_AGENT_HOME"] = str(store_dir)
    barrier.wait(timeout=10)
    save_connection({"host": "h"}, store_dir=store_dir)


def _save_llm_worker(store_dir: Path, barrier) -> None:
    os.environ["RNASEQ_AGENT_HOME"] = str(store_dir)
    barrier.wait(timeout=10)
    save_llm({"model": "m"}, store_dir=store_dir)


def _transaction_contender_worker(path: str, ready, entered) -> None:
    from rnaseq_agent.storage import locked_json_transaction

    ready.set()

    def mutate(current):
        entered.set()
        return current

    locked_json_transaction(Path(path), mutate)


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    return tmp_path / "conn"


class TestConnectionFilePath:
    def test_default_path_lives_under_user_home(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("RNASEQ_AGENT_HOME", str(tmp_path / "home"))
        path = connection_file_path()
        assert path.name == CONNECTION_FILE_NAME
        assert (tmp_path / "home") in path.parents


def _root(**overrides):
    values = dict(root_id="root_a", host="h.example", user="alice", port=22,
                  requested_path="/srv/reads", canonical_path="/srv/reads",
                  created_at="2026-09-17T00:00:00Z", revoked_at=None)
    values.update(overrides)
    return ApprovedDataRoot(**values)


def test_legacy_store_without_roots_is_available_empty_policy(tmp_path):
    store = tmp_path / "store"
    save_connection({"host": "h.example", "user": "alice", "port": 22}, store_dir=store)
    policy = load_browse_policy(store_dir=store)
    assert policy.available is True
    assert policy.identity == SSHIdentity("h.example", "alice", 22)
    assert policy.roots == ()


def test_missing_or_invalid_identity_is_unavailable_without_fake_identity(tmp_path):
    store = tmp_path / "store"
    save_connection({"host": "h.example"}, store_dir=store)
    policy = load_browse_policy(store_dir=store)
    assert policy.available is False
    assert policy.identity is None
    assert policy.roots == ()


def test_policy_revision_ignores_llm_and_unrelated_connection_fields(tmp_path):
    store = tmp_path / "store"
    save_connection({"host": "h.example", "user": "alice", "port": 22, "threads": 2}, store_dir=store)
    before = load_browse_policy(store_dir=store).revision
    save_llm({"model": "new"}, store_dir=store)
    save_connection({"threads": 64}, store_dir=store)
    assert load_browse_policy(store_dir=store).revision == before


def test_policy_revision_changes_for_active_root_or_identity(tmp_path):
    store = tmp_path / "store"
    save_connection({"host": "h.example", "user": "alice", "port": 22}, store_dir=store)
    before = load_browse_policy(store_dir=store).revision
    approve_data_root(_root(), expected_revision=before, store_dir=store)
    after_root = load_browse_policy(store_dir=store).revision
    assert after_root != before
    save_connection({"user": "bob"}, store_dir=store)
    assert load_browse_policy(store_dir=store).revision != after_root


def test_duplicate_active_canonical_root_is_idempotent(tmp_path):
    store = tmp_path / "store"
    save_connection({"host": "h.example", "user": "alice", "port": 22}, store_dir=store)
    revision = load_browse_policy(store_dir=store).revision
    first = approve_data_root(_root(), expected_revision=revision, store_dir=store)
    second = approve_data_root(_root(root_id="root_b", requested_path="/srv/alias"), expected_revision=first.revision, store_dir=store)
    assert second.revision == first.revision
    assert second.roots == first.roots


def test_more_than_32_active_roots_fails_closed_without_transport(tmp_path):
    store = tmp_path / "store"
    payload = {"host": "h.example", "user": "alice", "port": 22,
               "approved_data_roots": [_root(root_id=f"root_{i}", requested_path=f"/srv/{i}", canonical_path=f"/srv/{i}").__dict__ for i in range(33)]}
    from rnaseq_agent.private_files import ensure_private_directory
    ensure_private_directory(store)
    (store / "connection.json").write_text(json.dumps(payload), encoding="utf-8")
    policy = load_browse_policy(store_dir=store)
    assert policy.available is False and policy.identity is None and policy.roots == ()


def test_approve_rechecks_root_identity_against_locked_live_payload(tmp_path):
    store = tmp_path / "store"
    save_connection({"host": "h.example", "user": "alice", "port": 22}, store_dir=store)
    revision = load_browse_policy(store_dir=store).revision
    with pytest.raises(BrowsePolicyConflictError):
        approve_data_root(_root(host="other.example"), expected_revision=revision, store_dir=store)
    assert list_approved_data_roots(store_dir=store) == ()


def test_stale_approve_and_revoke_use_typed_revision_conflict(tmp_path):
    store = tmp_path / "store"
    save_connection({"host": "h.example", "user": "alice", "port": 22}, store_dir=store)
    revision = load_browse_policy(store_dir=store).revision
    approved = approve_data_root(_root(), expected_revision=revision, store_dir=store)
    with pytest.raises(BrowsePolicyConflictError) as exc_info:
        revoke_data_root("root_a", expected_revision=revision, store_dir=store)
    assert exc_info.value.error_code == "REMOTE_ROOT_REVISION_CONFLICT"
    revoked = revoke_data_root("root_a", expected_revision=approved.revision, store_dir=store)
    assert revoked.roots == ()
    assert revoked.available is True


def test_invalid_root_collection_fails_closed_and_is_not_repaired(tmp_path):
    store = tmp_path / "store"
    save_connection({"host": "h.example", "user": "alice", "port": 22}, store_dir=store)
    path = store / "connection.json"
    payload = json.loads(path.read_text("utf-8"))
    payload["approved_data_roots"] = [{"root_id": "broken"}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    policy = load_browse_policy(store_dir=store)
    assert policy.available is False and policy.identity is None and policy.roots == ()
    with pytest.raises(ConnectionStoreCorruptError):
        approve_data_root(_root(), expected_revision=policy.revision, store_dir=store)


def test_concurrent_connection_and_llm_saves_preserve_both_blocks(tmp_path, monkeypatch):
    store = tmp_path / "store"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    first = context.Process(target=_save_connection_worker, args=(store, barrier))
    second = context.Process(target=_save_llm_worker, args=(store, barrier))
    first.start(); second.start(); barrier.wait(timeout=5)
    first.join(10); second.join(10)
    assert first.exitcode == second.exitcode == 0
    saved = json.loads((store / "connection.json").read_text("utf-8"))
    assert saved["host"] == "h"
    assert saved["llm"]["model"] == "m"


def test_reader_never_observes_partial_json_during_writes(tmp_path):
    store = tmp_path / "store"
    path = store / "connection.json"
    save_connection({"host": "before"}, store_dir=store)
    errors: list[Exception] = []
    stop = threading.Event()

    def writer() -> None:
        try:
            for i in range(100):
                save_connection({"host": f"h-{i}"}, store_dir=store)
        finally:
            stop.set()

    def reader() -> None:
        while not stop.is_set():
            try:
                payload = json.loads(path.read_text("utf-8"))
                assert isinstance(payload, dict)
            except PermissionError:
                time.sleep(0.001)
                continue
            except Exception as exc:
                errors.append(exc)
                stop.set()

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert not errors


def test_corrupt_existing_document_is_rejected_unchanged(tmp_path):
    from rnaseq_agent.private_files import ensure_private_directory

    store = tmp_path / "store"
    path = store / "connection.json"
    raw = b"{not valid json"
    ensure_private_directory(store)
    path.write_bytes(raw)
    with pytest.raises(ConnectionStoreCorruptError):
        save_connection({"host": "h"}, store_dir=store)
    assert path.read_bytes() == raw


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration test")
def test_windows_transaction_rejects_existing_broadened_connection_file(tmp_path):
    import ctypes

    store = tmp_path / "store"
    save_connection({"host": "before"}, store_dir=store)
    path = store / CONNECTION_FILE_NAME
    before = path.read_bytes()
    adv = ctypes.windll.advapi32
    kernel = ctypes.windll.kernel32
    broad = ctypes.c_void_p()
    assert adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        "D:(A;;GA;;;WD)", 1, ctypes.byref(broad), None
    )
    try:
        broad_dacl = ctypes.c_void_p()
        present = ctypes.c_bool()
        defaulted = ctypes.c_bool()
        assert adv.GetSecurityDescriptorDacl(
            broad, ctypes.byref(present), ctypes.byref(broad_dacl), ctypes.byref(defaulted)
        )
        adv.SetNamedSecurityInfoW.restype = ctypes.c_ulong
        assert adv.SetNamedSecurityInfoW(str(path), 1, 4, None, None, broad_dacl, None) == 0
    finally:
        kernel.LocalFree(broad)

    with pytest.raises((OSError, PermissionError)):
        save_connection({"host": "after"}, store_dir=store)
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["dump", "fsync", "replace"])
def test_atomic_transaction_failure_preserves_final_bytes_and_removes_temp(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    store = tmp_path / "store"
    save_connection({"host": "before"}, store_dir=store)
    path = store / CONNECTION_FILE_NAME
    before = path.read_bytes()
    observed_temp_modes: list[int] = []

    if failure == "dump":
        def fail_dump(*args, **kwargs):
            raise OSError("injected dump failure")
        monkeypatch.setattr(storage.json, "dump", fail_dump)
    elif failure == "fsync":
        def fail_fsync(*args, **kwargs):
            raise OSError("injected fsync failure")
        monkeypatch.setattr(storage.os, "fsync", fail_fsync)
    else:
        def fail_replace(*args, **kwargs):
            if os.name != "nt":
                observed_temp_modes.append(stat.S_IMODE(Path(args[0]).stat().st_mode))
            raise OSError("injected replace failure")
        monkeypatch.setattr(storage.os, "replace", fail_replace)

    with pytest.raises(OSError):
        save_connection({"host": "after"}, store_dir=store)

    assert path.read_bytes() == before
    assert not list(store.glob(f".{path.name}.*"))
    if os.name != "nt" and failure == "replace":
        assert observed_temp_modes == [0o600]


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL integration test")
def test_windows_connection_file_has_protected_current_user_dacl(tmp_path):
    from rnaseq_agent.private_files import _read_windows_acl, _windows_sid
    store = tmp_path / "store"
    save_connection({"host": "h"}, store_dir=store)
    path = store / CONNECTION_FILE_NAME
    adv = __import__("ctypes").windll.advapi32
    kernel = __import__("ctypes").windll.kernel32
    ctypes = __import__("ctypes")
    sd = ctypes.c_void_p(); dacl = ctypes.c_void_p(); owner = ctypes.c_void_p()
    assert adv.GetNamedSecurityInfoW(str(path), 1, 4, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd)) == 0
    try:
        control = ctypes.c_ushort(); revision = ctypes.c_ubyte()
        assert adv.GetSecurityDescriptorControl(sd, ctypes.byref(control), ctypes.byref(revision))
        assert control.value & 0x1000
        protected, trustees = _read_windows_acl(path)
        assert protected
        assert set(trustees) in ({_windows_sid(), "SY"}, {_windows_sid(), "S-1-5-18"})
    finally:
        kernel.LocalFree(sd)


def test_transaction_callback_waits_for_shared_lock(tmp_path):
    from rnaseq_agent.storage import locked_json_transaction, project_state_lock

    store = tmp_path / "store"
    save_connection({"host": "h"}, store_dir=store)
    entered = threading.Event()
    ready = threading.Event()
    release = threading.Event()
    path = store / "connection.json"

    def contender() -> None:
        ready.set()
        locked_json_transaction(path, lambda current: (entered.set() or current))

    with project_state_lock(path):
        thread = threading.Thread(target=contender)
        thread.start()
        assert ready.wait(2)
        assert not entered.wait(0.2)
    thread.join(2)
    assert entered.is_set()


def test_transaction_callback_waits_for_shared_lock_across_processes(tmp_path):
    from rnaseq_agent.storage import project_state_lock

    store = tmp_path / "store"
    path = store / "connection.json"
    save_connection({"host": "h"}, store_dir=store)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    entered = context.Event()
    process = context.Process(
        target=_transaction_contender_worker,
        args=(str(path), ready, entered),
    )

    with project_state_lock(path):
        process.start()
        assert ready.wait(5)
        assert not entered.wait(0.5)
    process.join(10)
    assert process.exitcode == 0
    assert entered.is_set()


def test_new_connection_store_directory_is_private_and_existing_broad_directory_fails_closed(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX mode assertions are not applicable on Windows")
    new_store = tmp_path / "new-store"
    save_connection({"host": "h"}, store_dir=new_store)
    assert stat.S_IMODE(new_store.stat().st_mode) == 0o700
    assert stat.S_IMODE((new_store / CONNECTION_FILE_NAME).stat().st_mode) == 0o600

    broad_store = tmp_path / "broad-store"
    broad_store.mkdir(mode=0o755)
    os.chmod(broad_store, 0o755)
    with pytest.raises((OSError, PermissionError)):
        save_connection({"host": "h"}, store_dir=broad_store)
    assert not (broad_store / CONNECTION_FILE_NAME).exists()


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
