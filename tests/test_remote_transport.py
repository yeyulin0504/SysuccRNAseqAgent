from __future__ import annotations

import unittest
from pathlib import Path
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
]

VALID_IDENTITIES = [
    {"host": "hpc.example.edu", "user": "alice", "port": 22},
    {"host": "192.0.2.10", "user": "alice_1", "port": "2222"},
    {"host": "[2001:db8::1]", "user": "alice.dev", "port": None},
    {"host": "2001:db8::1", "user": "alice-dev", "port": ""},
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


class _FakeChannel:
    def __init__(self, *, returncode: int = 0) -> None:
        self.returncode = returncode
        self.closed = False

    def settimeout(self, timeout: int) -> None:
        self.timeout = timeout

    def recv_exit_status(self) -> int:
        return self.returncode

    def close(self) -> None:
        self.closed = True


class _FakeStream:
    def __init__(self, payload: bytes, channel: _FakeChannel) -> None:
        self.payload = payload
        self.channel = channel

    def read(self, size: int) -> bytes:
        return self.payload[:size]


class _FakeClient:
    def __init__(self, stdout: bytes, stderr: bytes) -> None:
        self.channel = _FakeChannel()
        self.stdout = _FakeStream(stdout, self.channel)
        self.stderr = _FakeStream(stderr, self.channel)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def exec_command(self, command: str, timeout: int):
        return None, self.stdout, self.stderr


def test_paramiko_uses_combined_output_limit(monkeypatch) -> None:
    transport = ParamikoTransport(SERVER, SSHCredential(mode="password", password="secret"))
    half = MAX_REMOTE_CAPTURE_BYTES // 2 + 1
    client = _FakeClient(b"o" * half, b"e" * half)
    monkeypatch.setattr(transport, "_connect", lambda: client)

    with pytest.raises(CommandOutputLimitError):
        transport.execute("printf ok")

    assert client.channel.closed is True


def test_paramiko_maps_channel_timeout_to_typed_error(monkeypatch) -> None:
    import socket

    transport = ParamikoTransport(SERVER, SSHCredential(mode="password", password="secret"))
    client = _FakeClient(b"", b"")

    def timeout_read(size: int) -> bytes:
        raise socket.timeout("test timeout")

    client.stdout.read = timeout_read
    monkeypatch.setattr(transport, "_connect", lambda: client)

    with pytest.raises(CommandTimeoutError):
        transport.execute("printf ok")


if __name__ == "__main__":
    unittest.main()
