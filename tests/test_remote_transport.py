from __future__ import annotations

import socket
import sys
import threading
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
    _connect_paramiko_with_deadline,
    _prepare_paramiko_client_with_deadline,
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
    def test_execute_bounded_preserves_browse_resolution_exit_codes(
        self,
        run_command_bounded: MagicMock,
    ) -> None:
        exit_codes = iter((44, 45))
        run_command_bounded.side_effect = lambda *args, **kwargs: (
            CommandResult([], next(exit_codes), "", "")
            if kwargs.get("check") is False
            else (_ for _ in ()).throw(RuntimeError("bounded command checked"))
        )
        transport = SystemSSHTransport(SERVER, SSHCredential(mode="system"))

        results = [
            transport.execute_bounded(
                f"resolved=$(realpath -e -- /path-{code}) || exit {code}",
                absolute_deadline=time.monotonic() + 5,
                max_capture_bytes=1024,
            )
            for code in (44, 45)
        ]

        self.assertEqual([result.returncode for result in results], [44, 45])
        self.assertIs(run_command_bounded.call_args.kwargs["check"], False)

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


def test_paramiko_host_key_loading_obeys_total_deadline(monkeypatch) -> None:
    class UnexpectedSocket:
        def settimeout(self, timeout: float) -> None:
            raise AssertionError("late TCP socket must not receive a timeout")

        def close(self) -> None:
            pass

    class DelayedHostKeyClient:
        def __init__(self) -> None:
            self.loader_exited = threading.Event()
            self.close_count = 0
            self.policy_called = False
            self.connect_called = False
            self.exec_called = False

        def load_system_host_keys(self) -> None:
            try:
                time.sleep(0.12)
            finally:
                self.loader_exited.set()

        def set_missing_host_key_policy(self, policy) -> None:
            self.policy_called = True

        def connect(self, **kwargs) -> None:
            self.connect_called = True

        def exec_command(self, *args, **kwargs):
            self.exec_called = True
            raise AssertionError("expired client must not execute a command")

        def close(self) -> None:
            self.close_count += 1

    client = DelayedHostKeyClient()
    socket_calls: list[tuple[object, ...]] = []
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: object(),
    )

    def record_socket(*args):
        socket_calls.append(args)
        return UnexpectedSocket()

    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(socket, "socket", record_socket)
    monkeypatch.setattr("rnaseq_agent.remote_transport.REMOTE_COMMAND_TIMEOUT_SECONDS", 0.05)
    transport = ParamikoTransport(
        {**SERVER, "host": "192.0.2.10"},
        SSHCredential(mode="password", password="secret"),
    )
    started = time.monotonic()

    with pytest.raises(CommandTimeoutError):
        transport.execute("printf ok")

    elapsed = time.monotonic() - started
    close_count_at_deadline = client.close_count
    assert elapsed < 0.10
    assert client.loader_exited.wait(1)
    assert close_count_at_deadline >= 1
    assert client.policy_called is False
    assert socket_calls == []
    assert client.connect_called is False
    assert client.exec_called is False


def test_paramiko_abandons_nonreturning_host_key_loader(monkeypatch) -> None:
    class UnexpectedSocket:
        def settimeout(self, timeout: float) -> None:
            raise AssertionError("late TCP socket must not receive a timeout")

        def close(self) -> None:
            pass

    class BlockingHostKeyClient:
        def __init__(self) -> None:
            self.loader_entered = threading.Event()
            self.release_loader = threading.Event()
            self.loader_exited = threading.Event()
            self.close_count = 0
            self.policy_called = False
            self.connect_called = False
            self.exec_called = False

        def load_system_host_keys(self) -> None:
            self.loader_entered.set()
            try:
                self.release_loader.wait()
            finally:
                self.loader_exited.set()

        def set_missing_host_key_policy(self, policy) -> None:
            self.policy_called = True

        def connect(self, **kwargs) -> None:
            self.connect_called = True

        def exec_command(self, *args, **kwargs):
            self.exec_called = True
            raise AssertionError("abandoned client must not execute a command")

        def close(self) -> None:
            self.close_count += 1

    client = BlockingHostKeyClient()
    socket_calls: list[tuple[object, ...]] = []
    result: list[BaseException | None] = []
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: object(),
    )

    def record_socket(*args):
        socket_calls.append(args)
        return UnexpectedSocket()

    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(socket, "socket", record_socket)
    monkeypatch.setattr("rnaseq_agent.remote_transport.REMOTE_COMMAND_TIMEOUT_SECONDS", 0.05)
    transport = ParamikoTransport(
        {**SERVER, "host": "192.0.2.10"},
        SSHCredential(mode="password", password="secret"),
    )

    def execute() -> None:
        try:
            transport.execute("printf ok")
        except BaseException as exc:
            result.append(exc)
        else:
            result.append(None)

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    try:
        assert client.loader_entered.wait(0.5)
        worker.join(0.10)
        finished_within_budget = not worker.is_alive()
        close_count_at_deadline = client.close_count
        socket_calls_at_deadline = list(socket_calls)
    finally:
        client.release_loader.set()
        assert client.loader_exited.wait(1)
        worker.join(1)

    assert finished_within_budget
    assert len(result) == 1
    assert isinstance(result[0], CommandTimeoutError)
    assert close_count_at_deadline >= 1
    assert client.close_count >= 1
    assert client.policy_called is False
    assert socket_calls_at_deadline == []
    assert socket_calls == []
    assert client.connect_called is False
    assert client.exec_called is False


def test_paramiko_closes_client_and_maps_host_key_loader_error(monkeypatch) -> None:
    class FailingHostKeyClient:
        def __init__(self) -> None:
            self.closed = False

        def load_system_host_keys(self) -> None:
            raise RuntimeError("cannot read known_hosts")

        def set_missing_host_key_policy(self, policy) -> None:
            raise AssertionError("policy setup must not follow loader failure")

        def close(self) -> None:
            self.closed = True

    client = FailingHostKeyClient()
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *args: pytest.fail("TCP setup must not follow loader failure"),
    )
    transport = ParamikoTransport(
        {**SERVER, "host": "192.0.2.10"},
        SSHCredential(mode="password", password="secret"),
    )

    with pytest.raises(RuntimeError, match="服务器主机指纹"):
        transport._connect(time.monotonic() + 1)

    assert client.closed is True


def test_paramiko_prepare_start_interruption_abandons_worker(monkeypatch) -> None:
    class BlockingHostKeyClient:
        def __init__(self) -> None:
            self.loader_entered = threading.Event()
            self.release_loader = threading.Event()
            self.close_count = 0
            self.policy_called = False

        def load_system_host_keys(self) -> None:
            self.loader_entered.set()
            self.release_loader.wait()

        def set_missing_host_key_policy(self, policy) -> None:
            self.policy_called = True

        def close(self) -> None:
            self.close_count += 1

    client = BlockingHostKeyClient()
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: object(),
    )
    started_workers: list[threading.Thread] = []
    published_results: list[object] = []
    original_start = threading.Thread.start
    original_put_nowait = __import__("queue").Queue.put_nowait

    def interrupt_after_start(worker) -> None:
        started_workers.append(worker)
        original_start(worker)
        assert client.loader_entered.wait(0.5)
        raise KeyboardInterrupt()

    def record_publication(result_queue, result) -> None:
        published_results.append(result)
        original_put_nowait(result_queue, result)

    monkeypatch.setattr(
        "rnaseq_agent.remote_transport.threading.Thread.start",
        interrupt_after_start,
    )
    monkeypatch.setattr(
        "rnaseq_agent.remote_transport.queue.Queue.put_nowait",
        record_publication,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            _prepare_paramiko_client_with_deadline(
                fake_paramiko,
                time.monotonic() + 1,
            )
        close_count_at_interruption = client.close_count
    finally:
        client.release_loader.set()
        for worker in started_workers:
            worker.join(1)

    assert all(not worker.is_alive() for worker in started_workers)
    assert close_count_at_interruption >= 1
    assert client.close_count >= 1
    assert client.policy_called is False
    assert published_results == []


def test_paramiko_dns_resolution_obeys_total_deadline(monkeypatch) -> None:
    import paramiko

    transport = ParamikoTransport(
        SERVER,
        SSHCredential(mode="password", password="resolver-secret"),
    )
    monkeypatch.setattr("rnaseq_agent.remote_transport.REMOTE_COMMAND_TIMEOUT_SECONDS", 0.05)
    resolver_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    resolver_exited = threading.Event()
    connect_calls: list[dict[str, object]] = []
    original_connect = paramiko.SSHClient.connect

    def delayed_getaddrinfo(*args, **kwargs):
        resolver_calls.append((args, kwargs))
        try:
            time.sleep(0.12)
            raise socket.timeout("delayed resolver")
        finally:
            resolver_exited.set()

    def recording_connect(client, *args, **kwargs):
        connect_calls.append(kwargs)
        return original_connect(client, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", delayed_getaddrinfo)
    monkeypatch.setattr(paramiko.SSHClient, "connect", recording_connect)
    started = time.monotonic()

    with pytest.raises(CommandTimeoutError):
        transport.execute("printf ok")

    elapsed = time.monotonic() - started
    assert elapsed < 0.10
    assert resolver_exited.wait(1)
    assert resolver_calls
    assert "resolver-secret" not in repr(resolver_calls)
    assert connect_calls == []


def test_paramiko_abandons_nonreturning_dns_without_late_connect(monkeypatch) -> None:
    import paramiko

    transport = ParamikoTransport(
        SERVER,
        SSHCredential(mode="password", password="resolver-secret"),
    )
    monkeypatch.setattr("rnaseq_agent.remote_transport.REMOTE_COMMAND_TIMEOUT_SECONDS", 0.05)
    resolver_entered = threading.Event()
    release_resolver = threading.Event()
    resolver_exited = threading.Event()
    connect_calls: list[dict[str, object]] = []
    result: list[BaseException | None] = []
    original_connect = paramiko.SSHClient.connect

    def blocked_getaddrinfo(*args, **kwargs):
        resolver_entered.set()
        try:
            release_resolver.wait()
            raise socket.timeout("released resolver")
        finally:
            resolver_exited.set()

    def recording_connect(client, *args, **kwargs):
        connect_calls.append(kwargs)
        return original_connect(client, *args, **kwargs)

    def execute() -> None:
        try:
            transport.execute("printf ok")
        except BaseException as exc:
            result.append(exc)
        else:
            result.append(None)

    monkeypatch.setattr(socket, "getaddrinfo", blocked_getaddrinfo)
    monkeypatch.setattr(paramiko.SSHClient, "connect", recording_connect)
    worker = threading.Thread(target=execute, daemon=True)
    started = time.monotonic()
    worker.start()
    assert resolver_entered.wait(0.5)
    worker.join(0.10)
    finished_within_budget = not worker.is_alive()
    elapsed = time.monotonic() - started
    calls_at_deadline = list(connect_calls)

    release_resolver.set()
    assert resolver_exited.wait(1)
    worker.join(1)

    assert finished_within_budget
    assert elapsed < 0.10
    assert len(result) == 1
    assert isinstance(result[0], CommandTimeoutError)
    assert calls_at_deadline == []
    assert connect_calls == []


def test_paramiko_closes_socket_when_tcp_connect_returns_after_deadline(monkeypatch) -> None:
    class DelayedSocket:
        def __init__(self, *args) -> None:
            self.closed = False

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def connect(self, address) -> None:
            time.sleep(0.05)

        def close(self) -> None:
            self.closed = True

    class FakeSSHClient:
        def __init__(self) -> None:
            self.connect_called = False
            self.closed = False

        def load_system_host_keys(self) -> None:
            pass

        def set_missing_host_key_policy(self, policy) -> None:
            pass

        def connect(self, **kwargs) -> None:
            self.connect_called = True

        def close(self) -> None:
            self.closed = True

    client = FakeSSHClient()
    connection = DelayedSocket()
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(socket, "socket", lambda *args: connection)
    monkeypatch.setattr("rnaseq_agent.remote_transport.REMOTE_COMMAND_TIMEOUT_SECONDS", 0.03)
    transport = ParamikoTransport(
        {**SERVER, "host": "192.0.2.10"},
        SSHCredential(mode="password", password="secret"),
    )

    with pytest.raises(CommandTimeoutError):
        transport.execute("printf ok")

    assert connection.closed is True
    assert client.connect_called is False
    assert client.closed is True


def test_paramiko_handshake_obeys_total_deadline(monkeypatch) -> None:
    class FakeSocket:
        def __init__(self, *args) -> None:
            self.closed = False

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def connect(self, address) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    class DelayedSSHClient:
        def __init__(self) -> None:
            self.connect_exited = threading.Event()
            self.late_closed = threading.Event()
            self.close_count = 0
            self.exec_called = False

        def load_system_host_keys(self) -> None:
            pass

        def set_missing_host_key_policy(self, policy) -> None:
            pass

        def connect(self, **kwargs) -> None:
            try:
                time.sleep(0.12)
            finally:
                self.connect_exited.set()

        def exec_command(self, *args, **kwargs):
            self.exec_called = True
            raise AssertionError("expired client must not execute a command")

        def close(self) -> None:
            self.close_count += 1
            if self.connect_exited.is_set():
                self.late_closed.set()

    client = DelayedSSHClient()
    connection = FakeSocket()
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(socket, "socket", lambda *args: connection)
    monkeypatch.setattr("rnaseq_agent.remote_transport.REMOTE_COMMAND_TIMEOUT_SECONDS", 0.05)
    transport = ParamikoTransport(
        {**SERVER, "host": "192.0.2.10"},
        SSHCredential(mode="password", password="secret"),
    )
    started = time.monotonic()

    with pytest.raises(CommandTimeoutError):
        transport.execute("printf ok")

    elapsed = time.monotonic() - started
    assert elapsed < 0.10
    assert client.connect_exited.wait(1)
    assert client.late_closed.wait(1)
    assert client.close_count >= 1
    assert connection.closed is True
    assert client.exec_called is False


def test_paramiko_abandons_nonreturning_handshake_without_late_success(monkeypatch) -> None:
    class FakeSocket:
        def __init__(self, *args) -> None:
            self.closed = False

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def connect(self, address) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    class BlockingSSHClient:
        def __init__(self) -> None:
            self.connect_entered = threading.Event()
            self.release_connect = threading.Event()
            self.connect_exited = threading.Event()
            self.late_closed = threading.Event()
            self.close_count = 0
            self.exec_called = False

        def load_system_host_keys(self) -> None:
            pass

        def set_missing_host_key_policy(self, policy) -> None:
            pass

        def connect(self, **kwargs) -> None:
            self.connect_entered.set()
            try:
                self.release_connect.wait()
            finally:
                self.connect_exited.set()

        def exec_command(self, *args, **kwargs):
            self.exec_called = True
            raise AssertionError("abandoned client must not execute a command")

        def close(self) -> None:
            self.close_count += 1
            if self.connect_exited.is_set():
                self.late_closed.set()

    client = BlockingSSHClient()
    connection = FakeSocket()
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(socket, "socket", lambda *args: connection)
    monkeypatch.setattr("rnaseq_agent.remote_transport.REMOTE_COMMAND_TIMEOUT_SECONDS", 0.05)
    transport = ParamikoTransport(
        {**SERVER, "host": "192.0.2.10"},
        SSHCredential(mode="password", password="secret"),
    )
    result: list[BaseException | None] = []

    def execute() -> None:
        try:
            transport.execute("printf ok")
        except BaseException as exc:
            result.append(exc)
        else:
            result.append(None)

    worker = threading.Thread(target=execute, daemon=True)
    started = time.monotonic()
    worker.start()
    assert client.connect_entered.wait(0.5)
    worker.join(0.10)
    finished_within_budget = not worker.is_alive()
    elapsed = time.monotonic() - started

    client.release_connect.set()
    assert client.connect_exited.wait(1)
    assert client.late_closed.wait(1)
    worker.join(1)

    assert finished_within_budget
    assert elapsed < 0.10
    assert len(result) == 1
    assert isinstance(result[0], CommandTimeoutError)
    assert client.close_count >= 1
    assert connection.closed is True
    assert client.exec_called is False


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_paramiko_handshake_wait_interruption_closes_owned_resources(
    monkeypatch,
    interruption_type: type[BaseException],
) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class BlockingClient:
        def __init__(self) -> None:
            self.connect_entered = threading.Event()
            self.release_connect = threading.Event()
            self.connect_exited = threading.Event()
            self.close_count = 0

        def connect(self, **kwargs) -> None:
            self.connect_entered.set()
            try:
                self.release_connect.wait()
            finally:
                self.connect_exited.set()

        def close(self) -> None:
            self.close_count += 1

    client = BlockingClient()
    connection = FakeSocket()

    def interrupt_wait(result_queue, timeout=None):
        assert client.connect_entered.wait(0.5)
        raise interruption_type()

    monkeypatch.setattr("rnaseq_agent.remote_transport.queue.Queue.get", interrupt_wait)
    try:
        with pytest.raises(interruption_type):
            _connect_paramiko_with_deadline(
                client,
                connection,
                hostname="192.0.2.10",
                port=22,
                username="researcher",
                password="secret",
                deadline=time.monotonic() + 1,
            )
        close_count_at_interruption = client.close_count
        socket_closed_at_interruption = connection.closed
    finally:
        client.release_connect.set()
        assert client.connect_exited.wait(1)

    assert close_count_at_interruption >= 1
    assert socket_closed_at_interruption is True


def test_paramiko_handshake_publication_interruption_abandons_result(monkeypatch) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class SuccessfulClient:
        def __init__(self) -> None:
            self.close_count = 0
            self.exec_called = False

        def connect(self, **kwargs) -> None:
            pass

        def exec_command(self, *args, **kwargs):
            self.exec_called = True
            raise AssertionError("interrupted client must not execute a command")

        def close(self) -> None:
            self.close_count += 1

    client = SuccessfulClient()
    connection = FakeSocket()
    publication_entered = threading.Event()
    release_publication = threading.Event()
    publication_exited = threading.Event()
    original_put_nowait = __import__("queue").Queue.put_nowait

    def controlled_put(result_queue, result) -> None:
        publication_entered.set()
        try:
            release_publication.wait()
            original_put_nowait(result_queue, result)
        finally:
            publication_exited.set()

    def interrupt_at_publication(result_queue, timeout=None):
        assert publication_entered.wait(0.5)
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        "rnaseq_agent.remote_transport.queue.Queue.put_nowait",
        controlled_put,
    )
    monkeypatch.setattr(
        "rnaseq_agent.remote_transport.queue.Queue.get",
        interrupt_at_publication,
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            _connect_paramiko_with_deadline(
                client,
                connection,
                hostname="192.0.2.10",
                port=22,
                username="researcher",
                password="secret",
                deadline=time.monotonic() + 1,
            )
        close_count_at_interruption = client.close_count
        socket_closed_at_interruption = connection.closed
    finally:
        release_publication.set()
        assert publication_exited.wait(1)

    assert close_count_at_interruption >= 1
    assert socket_closed_at_interruption is True
    assert client.close_count >= 1
    assert connection.closed is True
    assert client.exec_called is False


def test_paramiko_handshake_start_interruption_abandons_worker(monkeypatch) -> None:
    class FakeSocket:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    class BlockingClient:
        def __init__(self) -> None:
            self.connect_entered = threading.Event()
            self.release_connect = threading.Event()
            self.close_count = 0
            self.exec_called = False

        def connect(self, **kwargs) -> None:
            self.connect_entered.set()
            self.release_connect.wait()

        def exec_command(self, *args, **kwargs):
            self.exec_called = True
            raise AssertionError("interrupted client must not execute a command")

        def close(self) -> None:
            self.close_count += 1

    client = BlockingClient()
    connection = FakeSocket()
    started_workers: list[threading.Thread] = []
    published_results: list[object] = []
    original_start = threading.Thread.start
    original_put_nowait = __import__("queue").Queue.put_nowait

    def interrupt_after_start(worker) -> None:
        started_workers.append(worker)
        original_start(worker)
        assert client.connect_entered.wait(0.5)
        raise SystemExit()

    def record_publication(result_queue, result) -> None:
        published_results.append(result)
        original_put_nowait(result_queue, result)

    monkeypatch.setattr(
        "rnaseq_agent.remote_transport.threading.Thread.start",
        interrupt_after_start,
    )
    monkeypatch.setattr(
        "rnaseq_agent.remote_transport.queue.Queue.put_nowait",
        record_publication,
    )
    try:
        with pytest.raises(SystemExit):
            _connect_paramiko_with_deadline(
                client,
                connection,
                hostname="192.0.2.10",
                port=22,
                username="researcher",
                password="secret",
                deadline=time.monotonic() + 1,
            )
        client_close_count_at_interruption = client.close_count
        socket_close_count_at_interruption = connection.close_count
    finally:
        client.release_connect.set()
        for worker in started_workers:
            worker.join(1)

    assert all(not worker.is_alive() for worker in started_workers)
    assert client_close_count_at_interruption >= 1
    assert socket_close_count_at_interruption >= 1
    assert client.close_count >= 1
    assert connection.close_count >= 1
    assert published_results == []
    assert client.exec_called is False


def test_paramiko_maps_connection_socket_timeout_to_typed_error(monkeypatch) -> None:
    class FakeSocket:
        def __init__(self, *args) -> None:
            self.closed = False

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def connect(self, address) -> None:
            pass

        def close(self) -> None:
            self.closed = True

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
    connection = FakeSocket()
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 22))
        ],
    )
    monkeypatch.setattr(socket, "socket", lambda *args: connection)
    transport = ParamikoTransport(SERVER, SSHCredential(mode="password", password="secret"))

    with pytest.raises(CommandTimeoutError):
        transport.execute("printf ok")

    assert client.closed is True
    assert connection.closed is True


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
    sockets = []

    class FakeSocket:
        def __init__(self, family, socktype, protocol=0) -> None:
            self.family = family
            self.socktype = socktype
            self.protocol = protocol
            self.timeouts: list[float] = []
            self.closed = False
            sockets.append(self)

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def connect(self, address) -> None:
            if len(sockets) == 1:
                time.sleep(0.02)
                raise ConnectionRefusedError(10061, "first address refused")

        def close(self) -> None:
            self.closed = True

    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 22, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 22)),
        ],
    )
    monkeypatch.setattr(socket, "socket", FakeSocket)
    transport = ParamikoTransport(SERVER, SSHCredential(mode="password", password="secret"))
    timeout_seconds = 0.25

    connected = transport._connect(time.monotonic() + timeout_seconds)

    assert connected is client
    assert client.connect_kwargs is not None
    assert client.connect_kwargs["hostname"] == SERVER["host"]
    assert client.connect_kwargs["sock"] is sockets[1]
    assert 0 < client.connect_kwargs["timeout"] <= timeout_seconds
    assert client.connect_kwargs["auth_timeout"] == client.connect_kwargs["timeout"]
    assert client.connect_kwargs["banner_timeout"] == client.connect_kwargs["timeout"]
    assert sockets[0].closed is True
    assert sockets[1].closed is False
    assert sockets[1].timeouts[0] < sockets[0].timeouts[0]


@pytest.mark.parametrize(
    ("host", "family", "address"),
    [
        ("192.0.2.10", socket.AF_INET, ("192.0.2.10", 22)),
        ("2001:db8::1", socket.AF_INET6, ("2001:db8::1", 22, 0, 0)),
    ],
)
def test_paramiko_literal_ip_skips_dns_and_preserves_host_key_name(
    monkeypatch,
    host: str,
    family: int,
    address: tuple,
) -> None:
    class FakeSocket:
        def __init__(self, actual_family, socktype, protocol=0) -> None:
            self.family = actual_family
            self.connected_address = None
            self.closed = False

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def connect(self, actual_address) -> None:
            self.connected_address = actual_address

        def close(self) -> None:
            self.closed = True

    class FakeSSHClient:
        def load_system_host_keys(self) -> None:
            pass

        def set_missing_host_key_policy(self, policy) -> None:
            pass

        def connect(self, **kwargs) -> None:
            self.connect_kwargs = kwargs

        def close(self) -> None:
            pass

    client = FakeSSHClient()
    fake_paramiko = SimpleNamespace(
        SSHClient=lambda: client,
        RejectPolicy=lambda: object(),
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: pytest.fail("literal IP must not invoke DNS"),
    )
    monkeypatch.setattr(socket, "socket", FakeSocket)
    transport = ParamikoTransport(
        {**SERVER, "host": host},
        SSHCredential(mode="password", password="secret"),
    )

    connected = transport._connect(time.monotonic() + 0.25)

    assert connected is client
    assert client.connect_kwargs["hostname"] == host
    assert client.connect_kwargs["sock"].family == family
    assert client.connect_kwargs["sock"].connected_address == address


if __name__ == "__main__":
    unittest.main()
