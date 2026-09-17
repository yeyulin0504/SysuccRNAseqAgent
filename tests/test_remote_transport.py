from __future__ import annotations

import socket
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from rnaseq_agent.execution import (
    CommandOutputLimitError,
    CommandResult,
    CommandTimeoutError,
)
from rnaseq_agent.remote_transport import (
    MAX_REMOTE_CAPTURE_BYTES,
    REMOTE_COMMAND_TIMEOUT_SECONDS,
    ParamikoTransport,
    SystemSSHTransport,
    create_remote_transport,
)
from rnaseq_agent.ssh_auth import (
    SSHCredential,
    clear_ssh_credential,
    get_ssh_credential,
    normalize_auth_mode,
    set_ssh_credential,
)
from rnaseq_agent.ssh_identity import normalize_ssh_identity


SERVER = {
    "host": "hpc.example.edu",
    "user": "researcher",
    "port": 22,
}

INVALID_IDENTITIES = [
    {"host": "-oProxyCommand=calc", "user": "alice", "port": 22},
    {"host": "good.example", "user": "-Fbad", "port": 22},
    {"host": "bad host", "user": "alice", "port": 22},
    {"host": " good.example", "user": "alice", "port": 22},
    {"host": "alice@evil", "user": "alice", "port": 22},
    {"host": "good.example", "user": "alice;id", "port": 22},
    {"host": "good.example", "user": "alice ", "port": 22},
    {"host": "good.example", "user": "alice", "port": 0},
    {"host": "good.example", "user": "alice", "port": 65536},
    {"host": "good.example", "user": "alice", "port": True},
    {"host": "good.example", "user": "alice", "port": 22.0},
    {"host": "[]", "user": "alice", "port": 22},
    {"host": ":", "user": "alice", "port": 22},
    {"host": "1:::2", "user": "alice", "port": 22},
    {"host": "1.2.3.999", "user": "alice", "port": 22},
    {"host": "foo..bar", "user": "alice", "port": 22},
    {"host": "[example.com]", "user": "alice", "port": 22},
    {"host": ".leading", "user": "alice", "port": 22},
    {"host": "trailing.", "user": "alice", "port": 22},
    {"host": "-leading.example", "user": "alice", "port": 22},
    {"host": "label-.example", "user": "alice", "port": 22},
    {"host": f"{'a' * 64}.example", "user": "alice", "port": 22},
]

VALID_IDENTITIES = [
    {"host": "hpc.example.edu", "user": "alice", "port": 22},
    {"host": "192.0.2.10", "user": "alice_1", "port": "2222"},
    {"host": "[2001:db8::1]", "user": "alice.dev", "port": None},
    {"host": "2001:db8::1", "user": "alice-dev", "port": ""},
]

INVALID_SCOPED_IPV6_HOSTS = [
    "fe80::1%eth0",
    "fe80::1%bad zone",
    "fe80::1%bad\tzone",
    "fe80::1%bad\nzone",
    "fe80::1%bad;id",
    "fe80::1%x] -oProxyCommand=calc",
    "[fe80::1%eth0]",
    "[fe80::1%x] -oProxyCommand=calc]",
]


class SSHCredentialTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_ssh_credential(SERVER["host"], SERVER["user"])

    def test_password_is_only_kept_in_runtime_store(self) -> None:
        set_ssh_credential(
            SERVER["host"],
            SERVER["user"],
            mode="password",
            password="temporary-secret",
        )

        credential = get_ssh_credential(SERVER["host"], SERVER["user"])

        self.assertEqual(credential.password, "temporary-secret")
        self.assertNotIn("password", SERVER)

    def test_password_mode_requires_runtime_password(self) -> None:
        set_ssh_credential(
            SERVER["host"],
            SERVER["user"],
            mode="password",
        )
        with self.assertRaisesRegex(RuntimeError, "临时密码"):
            create_remote_transport({"server": SERVER})

    def test_password_mode_selects_paramiko_transport(self) -> None:
        set_ssh_credential(
            SERVER["host"],
            SERVER["user"],
            mode="password",
            password="temporary-secret",
        )
        transport = create_remote_transport({"server": SERVER})
        self.assertIsInstance(transport, ParamikoTransport)

    def test_null_port_defaults_to_ssh_port_22(self) -> None:
        set_ssh_credential(
            SERVER["host"],
            SERVER["user"],
            mode="password",
            password="temporary-secret",
        )
        transport = create_remote_transport({"server": {**SERVER, "port": None}})

        self.assertIsInstance(transport, ParamikoTransport)
        self.assertEqual(transport.port, 22)

    def test_pass_alias_from_web_form_selects_password_mode(self) -> None:
        """WEB 前端 settings.html 用 'pass' 作为按钮值，必须等价于 'password'。

        否则 set_ssh_credential 会静默降级为 key 模式并丢弃密码，
        用户明明填了密码却仍走密钥认证、报 Permission denied。
        """
        set_ssh_credential(
            SERVER["host"],
            SERVER["user"],
            mode="pass",
            password="temporary-secret",
        )

        credential = get_ssh_credential(SERVER["host"], SERVER["user"])

        self.assertEqual(credential.mode, "password")
        self.assertEqual(credential.password, "temporary-secret")
        self.assertIsInstance(create_remote_transport({"server": SERVER}), ParamikoTransport)

    def test_normalize_auth_mode_maps_aliases_and_unknown_values(self) -> None:
        self.assertEqual(normalize_auth_mode("pass"), "password")
        self.assertEqual(normalize_auth_mode("password"), "password")
        self.assertEqual(normalize_auth_mode("key"), "key")
        self.assertEqual(normalize_auth_mode("system"), "system")
        self.assertEqual(normalize_auth_mode(""), "key")
        self.assertEqual(normalize_auth_mode(None), "key")
        self.assertEqual(normalize_auth_mode("nonsense"), "key")


@pytest.mark.parametrize("server", INVALID_IDENTITIES)
def test_create_remote_transport_rejects_invalid_ssh_identity_before_credential_lookup(
    server: dict,
) -> None:
    with patch("rnaseq_agent.remote_transport.get_ssh_credential") as credential_lookup:
        with pytest.raises(ValueError):
            create_remote_transport({"server": server})

    credential_lookup.assert_not_called()


@pytest.mark.parametrize("server", INVALID_IDENTITIES)
@pytest.mark.parametrize("transport_type", [SystemSSHTransport, ParamikoTransport])
def test_transport_constructor_rejects_invalid_ssh_identity(
    server: dict,
    transport_type: type,
) -> None:
    with pytest.raises(ValueError):
        transport_type(server, SSHCredential(mode="system"))


@pytest.mark.parametrize("server", VALID_IDENTITIES)
def test_create_remote_transport_accepts_normalized_ssh_identity(server: dict) -> None:
    with patch(
        "rnaseq_agent.remote_transport.get_ssh_credential",
        return_value=SSHCredential(mode="system"),
    ):
        transport = create_remote_transport({"server": server})

    assert isinstance(transport, SystemSSHTransport)
    assert transport.port == (22 if server["port"] in (None, "") else int(server["port"]))


@pytest.mark.parametrize("host", ["[2001:db8::1]", "2001:0db8:0:0:0:0:0:1"])
def test_normalize_ssh_identity_returns_canonical_bare_ipv6(host: str) -> None:
    identity = normalize_ssh_identity({"host": host, "user": "alice", "port": 22})

    assert identity.host == "2001:db8::1"


@pytest.mark.parametrize("host", INVALID_SCOPED_IPV6_HOSTS)
def test_normalize_ssh_identity_rejects_scoped_ipv6(host: str) -> None:
    with pytest.raises(ValueError):
        normalize_ssh_identity({"host": host, "user": "alice", "port": 22})


def test_bracketed_ipv6_password_credential_selects_paramiko_transport() -> None:
    bracketed_host = "[2001:db8::1]"
    canonical_host = "2001:db8::1"
    clear_ssh_credential(canonical_host, "alice")
    try:
        set_ssh_credential(
            bracketed_host,
            "alice",
            mode="password",
            password="temporary-secret",
        )

        transport = create_remote_transport(
            {"server": {"host": bracketed_host, "user": "alice", "port": 22}}
        )

        assert isinstance(transport, ParamikoTransport)
        assert transport.password == "temporary-secret"
        clear_ssh_credential(canonical_host, "alice")
        assert get_ssh_credential(bracketed_host, "alice") == SSHCredential()
    finally:
        clear_ssh_credential(bracketed_host, "alice")
        clear_ssh_credential(canonical_host, "alice")


class SystemSSHTransportTests(unittest.TestCase):
    @patch("rnaseq_agent.remote_transport.run_command_bounded")
    def test_key_mode_uses_batch_mode_and_private_key(
        self,
        run_command_bounded: MagicMock,
    ) -> None:
        run_command_bounded.return_value = CommandResult(
            command=[],
            returncode=0,
            stdout="ok",
            stderr="",
        )
        transport = SystemSSHTransport(
            SERVER,
            SSHCredential(mode="key", key_path="C:/keys/id_ed25519"),
        )

        transport.execute("printf ok")

        command = run_command_bounded.call_args.args[0]
        self.assertIn("BatchMode=yes", command)
        self.assertIn("C:/keys/id_ed25519", command)
        self.assertNotIn("temporary-secret", command)
        self.assertEqual(
            run_command_bounded.call_args.kwargs,
            {
                "timeout_seconds": REMOTE_COMMAND_TIMEOUT_SECONDS,
                "max_capture_bytes": MAX_REMOTE_CAPTURE_BYTES,
            },
        )

    @patch("rnaseq_agent.remote_transport.run_command")
    def test_upload_command_does_not_contain_password(
        self,
        run_command: MagicMock,
    ) -> None:
        run_command.return_value = CommandResult(
            command=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        transport = SystemSSHTransport(SERVER, SSHCredential(mode="system"))

        transport.upload([Path("sample.fastq.gz")], "/remote/raw")

        command = run_command.call_args.args[0]
        self.assertNotIn("password", " ".join(command).lower())

    @patch("rnaseq_agent.remote_transport.run_command_bounded")
    def test_nonstandard_port_uses_ssh_specific_flag(
        self,
        run_command_bounded: MagicMock,
    ) -> None:
        run_command_bounded.return_value = CommandResult([], 0, "", "")
        server = {**SERVER, "port": 2222}
        transport = SystemSSHTransport(server, SSHCredential(mode="system"))

        transport.execute("printf ok")
        ssh_command = run_command_bounded.call_args.args[0]

        self.assertIn("-p", ssh_command)
        self.assertIn("2222", ssh_command)

    @patch("rnaseq_agent.remote_transport.run_command")
    def test_nonstandard_port_keeps_scp_specific_flag(
        self,
        run_command: MagicMock,
    ) -> None:
        run_command.return_value = CommandResult([], 0, "", "")
        transport = SystemSSHTransport(
            {**SERVER, "port": 2222}, SSHCredential(mode="system")
        )

        transport.upload([Path("sample.fastq.gz")], "/remote/raw")

        command = run_command.call_args.args[0]
        self.assertIn("-P", command)
        self.assertIn("2222", command)

    @patch("rnaseq_agent.remote_transport.run_command_bounded")
    def test_execute_separates_validated_login_and_host_from_options(
        self,
        run_command_bounded: MagicMock,
    ) -> None:
        run_command_bounded.return_value = CommandResult([], 0, "ok", "")
        transport = SystemSSHTransport(SERVER, SSHCredential(mode="system"))

        transport.execute("printf ok")

        command = run_command_bounded.call_args.args[0]
        separator = command.index("--")
        self.assertEqual(command[separator - 2:separator], ["-l", "researcher"])
        self.assertEqual(command[separator + 1], "hpc.example.edu")
        self.assertFalse(command[separator + 1].startswith("-"))

    @patch("rnaseq_agent.remote_transport.run_command_bounded")
    def test_execute_passes_bare_ipv6_host_to_ssh(
        self,
        run_command_bounded: MagicMock,
    ) -> None:
        run_command_bounded.return_value = CommandResult([], 0, "ok", "")
        transport = SystemSSHTransport(
            {**SERVER, "host": "[2001:db8::1]"}, SSHCredential(mode="system")
        )

        transport.execute("printf ok")

        command = run_command_bounded.call_args.args[0]
        separator = command.index("--")
        self.assertEqual(command[separator + 1], "2001:db8::1")

    @patch("rnaseq_agent.remote_transport.run_command")
    def test_scp_brackets_normalized_ipv6_host(self, run_command: MagicMock) -> None:
        run_command.return_value = CommandResult([], 0, "", "")
        transport = SystemSSHTransport(
            {**SERVER, "host": "[2001:db8::1]"}, SSHCredential(mode="system")
        )

        transport.upload([Path("sample.fastq.gz")], "/remote/raw")

        command = run_command.call_args.args[0]
        self.assertIn("researcher@[2001:db8::1]:/remote/raw/", command)


class _FakeChannel:
    def __init__(
        self,
        *,
        stdout_chunks: list[bytes] | None = None,
        stderr_chunks: list[bytes] | None = None,
        returncode: int = 0,
        release_stdout_after_stderr: bool = False,
        trickle: bool = False,
    ) -> None:
        self.stdout_chunks = list(stdout_chunks or [])
        self.stderr_chunks = list(stderr_chunks or [])
        self.returncode = returncode
        self.release_stdout_after_stderr = release_stdout_after_stderr
        self.stderr_consumed = False
        self.trickle = trickle
        self.closed = False

    def settimeout(self, timeout: int) -> None:
        self.timeout = timeout

    def recv_ready(self) -> bool:
        if self.trickle:
            return True
        if self.release_stdout_after_stderr and not self.stderr_consumed:
            return False
        return bool(self.stdout_chunks)

    def recv(self, size: int) -> bytes:
        if self.trickle:
            return b"x"
        return self.stdout_chunks.pop(0)

    def recv_stderr_ready(self) -> bool:
        return bool(self.stderr_chunks)

    def recv_stderr(self, size: int) -> bytes:
        self.stderr_consumed = True
        return self.stderr_chunks.pop(0)

    def exit_status_ready(self) -> bool:
        return (
            not self.trickle
            and not self.stdout_chunks
            and not self.stderr_chunks
        )

    def recv_exit_status(self) -> int:
        return self.returncode

    def close(self) -> None:
        self.closed = True


class _FakeStream:
    def __init__(self, channel: _FakeChannel) -> None:
        self.channel = channel

    def read(self, size: int) -> bytes:
        raise AssertionError("Paramiko streams must be drained through the shared channel")


class _FakeClient:
    def __init__(self, channel: _FakeChannel) -> None:
        self.channel = channel
        self.stdout = _FakeStream(self.channel)
        self.stderr = _FakeStream(self.channel)
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
        return None

    def exec_command(self, command: str, timeout: int):
        return None, self.stdout, self.stderr

    def close(self) -> None:
        self.closed = True


def test_paramiko_uses_combined_output_limit(monkeypatch) -> None:
    transport = ParamikoTransport(SERVER, SSHCredential(mode="password", password="secret"))
    monkeypatch.setattr("rnaseq_agent.remote_transport.MAX_REMOTE_CAPTURE_BYTES", 10)
    client = _FakeClient(
        _FakeChannel(stdout_chunks=[b"o" * 6], stderr_chunks=[b"e" * 6])
    )
    monkeypatch.setattr(transport, "_connect", lambda *args, **kwargs: client)

    with pytest.raises(CommandOutputLimitError):
        transport.execute("printf ok")

    assert client.channel.closed is True
    assert client.closed is True


def test_paramiko_drains_stderr_before_stdout_backpressure_can_block(monkeypatch) -> None:
    transport = ParamikoTransport(SERVER, SSHCredential(mode="password", password="secret"))
    channel = _FakeChannel(
        stdout_chunks=[b"stdout"],
        stderr_chunks=[b"stderr"],
        release_stdout_after_stderr=True,
    )
    client = _FakeClient(channel)
    monkeypatch.setattr(transport, "_connect", lambda *args, **kwargs: client)

    result = transport.execute("printf ok")

    assert result.stdout == "stdout"
    assert result.stderr == "stderr"
    assert client.closed is True


def test_paramiko_enforces_total_deadline_during_trickle_output(monkeypatch) -> None:
    transport = ParamikoTransport(SERVER, SSHCredential(mode="password", password="secret"))
    client = _FakeClient(_FakeChannel(trickle=True))
    monkeypatch.setattr(transport, "_connect", lambda *args, **kwargs: client)
    monkeypatch.setattr("rnaseq_agent.remote_transport.REMOTE_COMMAND_TIMEOUT_SECONDS", 0.05)
    started = __import__("time").monotonic()

    with pytest.raises(CommandTimeoutError):
        transport.execute("printf ok")

    assert __import__("time").monotonic() - started < 1
    assert client.channel.closed is True
    assert client.closed is True


def test_paramiko_maps_channel_timeout_to_typed_error(monkeypatch) -> None:
    class TimeoutChannel(_FakeChannel):
        def recv(self, size: int) -> bytes:
            raise socket.timeout("test timeout")

    transport = ParamikoTransport(SERVER, SSHCredential(mode="password", password="secret"))
    client = _FakeClient(TimeoutChannel(stdout_chunks=[b"unread"]))
    monkeypatch.setattr(transport, "_connect", lambda *args, **kwargs: client)

    with pytest.raises(CommandTimeoutError):
        transport.execute("printf ok")

    assert client.channel.closed is True
    assert client.closed is True


def test_paramiko_connection_time_counts_toward_total_deadline(monkeypatch) -> None:
    transport = ParamikoTransport(SERVER, SSHCredential(mode="password", password="secret"))
    client = _FakeClient(_FakeChannel())
    monkeypatch.setattr("rnaseq_agent.remote_transport.REMOTE_COMMAND_TIMEOUT_SECONDS", 0.03)

    def delayed_connect(*args, **kwargs):
        time.sleep(0.05)
        return client

    monkeypatch.setattr(transport, "_connect", delayed_connect)

    with pytest.raises(CommandTimeoutError):
        transport.execute("printf ok")

    assert client.closed is True


def test_paramiko_maps_connection_socket_timeout_to_typed_error(monkeypatch) -> None:
    class TimeoutSSHClient:
        def __init__(self) -> None:
            self.closed = False

        def load_system_host_keys(self) -> None:
            pass

        def set_missing_host_key_policy(self, policy) -> None:
            pass

        def connect(self, **kwargs) -> None:
            raise socket.timeout("connect timed out")

        def close(self) -> None:
            self.closed = True

    client = TimeoutSSHClient()
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    transport = ParamikoTransport(SERVER, SSHCredential(mode="password", password="secret"))

    with pytest.raises(CommandTimeoutError):
        transport.execute("printf ok")

    assert client.closed is True


def test_paramiko_connect_receives_remaining_total_budget(monkeypatch) -> None:
    class FakeSSHClient:
        def __init__(self) -> None:
            self.connect_kwargs = None
            self.closed = False

        def load_system_host_keys(self) -> None:
            pass

        def set_missing_host_key_policy(self, policy) -> None:
            pass

        def connect(self, **kwargs) -> None:
            self.connect_kwargs = kwargs

        def close(self) -> None:
            self.closed = True

    client = FakeSSHClient()
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    transport = ParamikoTransport(SERVER, SSHCredential(mode="password", password="secret"))
    timeout_seconds = 0.25

    connected = transport._connect(time.monotonic() + timeout_seconds)

    assert connected is client
    assert client.connect_kwargs is not None
    assert 0 < client.connect_kwargs["timeout"] <= timeout_seconds
    assert client.connect_kwargs["auth_timeout"] == client.connect_kwargs["timeout"]
    assert client.connect_kwargs["banner_timeout"] == client.connect_kwargs["timeout"]


if __name__ == "__main__":
    unittest.main()
