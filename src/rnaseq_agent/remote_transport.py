from __future__ import annotations

import ipaddress
import queue
import socket
import threading
import time
from pathlib import Path
from typing import Any, Protocol, Sequence

from .execution import (
    CommandOutputLimitError,
    CommandResult,
    CommandTimeoutError,
    run_command,
    run_command_bounded,
)
from .shell import remote_path
from .ssh_auth import SSHCredential, get_ssh_credential
from .ssh_identity import normalize_ssh_identity


REMOTE_COMMAND_TIMEOUT_SECONDS = 120
MAX_REMOTE_CAPTURE_BYTES = 4 * 1024 * 1024


def _remaining_remote_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CommandTimeoutError(
            f"Remote command exceeded the "
            f"{REMOTE_COMMAND_TIMEOUT_SECONDS:g}-second time limit."
        )
    return remaining


def _resolve_remote_addresses(
    host: str,
    port: int,
    deadline: float,
) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if literal.version == 4:
            address = (host, port)
            family = socket.AF_INET
        else:
            address = (host, port, 0, 0)
            family = socket.AF_INET6
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)]

    result_queue: queue.Queue[
        tuple[
            bool,
            list[tuple[int, int, int, str, tuple[Any, ...]]] | Exception,
        ]
    ] = queue.Queue(maxsize=1)
    abandoned = threading.Event()

    def resolve() -> None:
        try:
            result: list[tuple[int, int, int, str, tuple[Any, ...]]] | Exception = (
                socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
            )
            succeeded = True
        except Exception as exc:
            result = exc
            succeeded = False
        if not abandoned.is_set():
            try:
                result_queue.put_nowait((succeeded, result))
            except queue.Full:
                pass

    # CPython cannot cancel a blocked platform resolver. A daemon thread lets the
    # caller honor its deadline; it only resolves addresses and never sees a
    # credential or opens a socket if the abandoned lookup returns later.
    resolver = threading.Thread(
        target=resolve,
        name="rnaseq-agent-dns-resolver",
        daemon=True,
    )
    resolver.start()
    try:
        try:
            succeeded, result = result_queue.get(
                timeout=_remaining_remote_time(deadline)
            )
        except queue.Empty as exc:
            raise CommandTimeoutError(
                f"Remote command exceeded the "
                f"{REMOTE_COMMAND_TIMEOUT_SECONDS:g}-second time limit."
            ) from exc
        _remaining_remote_time(deadline)
    finally:
        abandoned.set()
    if not succeeded:
        assert isinstance(result, Exception)
        raise result
    assert isinstance(result, list)
    return result


def _connect_remote_socket(host: str, port: int, deadline: float) -> socket.socket:
    addresses = _resolve_remote_addresses(host, port, deadline)
    last_error: OSError | None = None
    for family, socktype, protocol, _, address in addresses:
        connection = socket.socket(family, socktype, protocol)
        try:
            connection.settimeout(_remaining_remote_time(deadline))
            connection.connect(address)
            _remaining_remote_time(deadline)
            return connection
        except BaseException as exc:
            connection.close()
            if isinstance(exc, OSError):
                last_error = exc
                continue
            raise
    if last_error is not None:
        raise last_error
    raise OSError(f"No network address was found for {host}:{port}.")


def _connect_paramiko_with_deadline(
    client: Any,
    connection: socket.socket,
    *,
    hostname: str,
    port: int,
    username: str,
    password: str | None,
    deadline: float,
) -> None:
    remaining = _remaining_remote_time(deadline)
    result_queue: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)
    abandoned = threading.Event()

    def connect() -> None:
        try:
            client.connect(
                hostname=hostname,
                port=port,
                username=username,
                password=password,
                timeout=remaining,
                auth_timeout=remaining,
                banner_timeout=remaining,
                allow_agent=False,
                look_for_keys=False,
                sock=connection,
            )
            result: BaseException | None = None
        except BaseException as exc:
            result = exc
        if abandoned.is_set():
            client.close()
            connection.close()
            return
        try:
            result_queue.put_nowait(result)
        except queue.Full:
            pass
        if abandoned.is_set():
            client.close()
            connection.close()

    # Closing Paramiko and its socket interrupts real handshake/auth waits. The
    # daemon only remains if a dependency ignores close; any late return closes
    # both resources again and cannot publish a usable client.
    try:
        worker = threading.Thread(
            target=connect,
            name="rnaseq-agent-paramiko-connect",
            daemon=True,
        )
        worker.start()
        try:
            result = result_queue.get(timeout=_remaining_remote_time(deadline))
        except queue.Empty as exc:
            raise CommandTimeoutError(
                f"Remote command exceeded the "
                f"{REMOTE_COMMAND_TIMEOUT_SECONDS:g}-second time limit."
            ) from exc
        if result is not None:
            raise result
        _remaining_remote_time(deadline)
        return
    except BaseException:
        abandoned.set()
        client.close()
        connection.close()
        raise


def _prepare_paramiko_client_with_deadline(paramiko: Any, deadline: float) -> Any:
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    abandoned = threading.Event()
    client_lock = threading.Lock()
    client_holder: list[Any] = []

    def close_client() -> None:
        with client_lock:
            client = client_holder[0] if client_holder else None
        if client is not None:
            client.close()

    def prepare() -> None:
        client = None
        try:
            client = paramiko.SSHClient()
            with client_lock:
                client_holder.append(client)
            if abandoned.is_set():
                client.close()
                return
            client.load_system_host_keys()
            if abandoned.is_set():
                client.close()
                return
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            if abandoned.is_set():
                client.close()
                return
            result = (True, client)
        except BaseException as exc:
            if client is not None:
                client.close()
            result = (False, exc)
        if abandoned.is_set():
            if client is not None:
                client.close()
            return
        try:
            result_queue.put_nowait(result)
        except queue.Full:
            pass
        if abandoned.is_set() and client is not None:
            client.close()

    try:
        worker = threading.Thread(
            target=prepare,
            name="rnaseq-agent-paramiko-prepare",
            daemon=True,
        )
        worker.start()
        try:
            succeeded, result = result_queue.get(
                timeout=_remaining_remote_time(deadline)
            )
        except queue.Empty as exc:
            raise CommandTimeoutError(
                f"Remote command exceeded the "
                f"{REMOTE_COMMAND_TIMEOUT_SECONDS:g}-second time limit."
            ) from exc
        if not succeeded:
            raise result
        _remaining_remote_time(deadline)
        return result
    except BaseException:
        abandoned.set()
        close_client()
        raise


class RemoteTransport(Protocol):
    def execute(self, remote_command: str) -> CommandResult: ...

    def upload(self, local_paths: Sequence[Path], remote_dir: str) -> CommandResult: ...

    def download(self, remote_file: str, local_path: Path) -> CommandResult: ...


class DeadlineAwareRemoteTransport(RemoteTransport, Protocol):
    def execute_bounded(
        self,
        remote_command: str,
        *,
        absolute_deadline: float,
        max_capture_bytes: int,
        check: bool = False,
    ) -> CommandResult: ...


def create_remote_transport(config: dict[str, Any]) -> RemoteTransport:
    server = config["server"]
    identity = normalize_ssh_identity(server)
    credential = get_ssh_credential(identity.host, identity.user)
    if credential.mode == "password":
        if not credential.password:
            raise RuntimeError(
                "当前选择了密码登录，但本次程序中没有临时密码。"
                "请在 GUI“服务器”页点击“输入临时密码”。"
            )
        return ParamikoTransport(server, credential)
    return SystemSSHTransport(server, credential)


def test_server_connection(config: dict[str, Any]) -> CommandResult:
    transport = create_remote_transport(config)
    return transport.execute("printf 'RNASEQ_AGENT_SSH_OK'")


def probe_server_environment(config: dict[str, Any]) -> dict[str, str]:
    transport = create_remote_transport(config)
    result = transport.execute(
        "printf 'HOME=%s\\n' \"$HOME\"; "
        "if command -v sbatch >/dev/null 2>&1; then echo 'SCHEDULER=slurm'; "
        "elif command -v qsub >/dev/null 2>&1; then echo 'SCHEDULER=pbs'; "
        "else echo 'SCHEDULER=local'; fi"
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"HOME", "SCHEDULER"}:
            values[key.lower()] = value.strip()
    return values


class SystemSSHTransport:
    def __init__(self, server: dict[str, Any], credential: SSHCredential) -> None:
        identity = normalize_ssh_identity(server)
        self.host = identity.host
        self.user = identity.user
        self.port = identity.port
        self.target = f"{self.user}@{self.host}"
        self.credential = credential

    def _common_options(self) -> list[str]:
        options = ["-o", "ConnectTimeout=15", "-o", "BatchMode=yes"]
        if self.credential.mode == "key":
            if self.credential.key_path:
                options.extend(["-i", self.credential.key_path])
        return options

    def _ssh_options(self) -> list[str]:
        options = self._common_options()
        if self.port != 22:
            options.extend(["-p", str(self.port)])
        return options

    def _scp_options(self) -> list[str]:
        options = self._common_options()
        if self.port != 22:
            options.extend(["-P", str(self.port)])
        return options

    def execute(self, remote_command: str) -> CommandResult:
        return run_command_bounded(
            [
                "ssh",
                *self._ssh_options(),
                "-l",
                self.user,
                "--",
                self.host,
                remote_command,
            ],
            timeout_seconds=REMOTE_COMMAND_TIMEOUT_SECONDS,
            max_capture_bytes=MAX_REMOTE_CAPTURE_BYTES,
        )

    def execute_bounded(
        self,
        remote_command: str,
        *,
        absolute_deadline: float,
        max_capture_bytes: int,
        check: bool = False,
    ) -> CommandResult:
        remaining = _remaining_remote_time(absolute_deadline)
        return run_command_bounded(
            [
                "ssh",
                *self._ssh_options(),
                "-l",
                self.user,
                "--",
                self.host,
                remote_command,
            ],
            timeout_seconds=remaining,
            max_capture_bytes=max_capture_bytes,
            check=check,
        )

    def upload(self, local_paths: Sequence[Path], remote_dir: str) -> CommandResult:
        sources = [str(path).replace("\\", "/") for path in local_paths]
        return run_command(
            [
                "scp",
                *self._scp_options(),
                "--",
                *sources,
                f"{self.user}@{self._scp_host()}:{remote_path(remote_dir)}/",
            ]
        )

    def download(self, remote_file: str, local_path: Path) -> CommandResult:
        return run_command(
            [
                "scp",
                *self._scp_options(),
                "--",
                f"{self.user}@{self._scp_host()}:{remote_file}",
                str(local_path).replace("\\", "/"),
            ]
        )

    def _scp_host(self) -> str:
        if ":" in self.host and not (self.host.startswith("[") and self.host.endswith("]")):
            return f"[{self.host}]"
        return self.host


class ParamikoTransport:
    def __init__(self, server: dict[str, Any], credential: SSHCredential) -> None:
        identity = normalize_ssh_identity(server)
        self.host = identity.host
        self.user = identity.user
        self.password = credential.password
        self.port = identity.port
        self.target = f"{self.user}@{self.host}"

    def _connect(self, deadline: float | None = None):
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError(
                "密码登录需要 Paramiko。请重新安装项目依赖后再试。"
            ) from exc
        if deadline is None:
            deadline = time.monotonic() + REMOTE_COMMAND_TIMEOUT_SECONDS
        client = None
        connection = None
        try:
            client = _prepare_paramiko_client_with_deadline(paramiko, deadline)
            connection = _connect_remote_socket(self.host, self.port, deadline)
            _connect_paramiko_with_deadline(
                client,
                connection,
                hostname=self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                deadline=deadline,
            )
            _remaining_remote_time(deadline)
        except BaseException as exc:
            if client is not None:
                client.close()
            if connection is not None:
                connection.close()
            if isinstance(exc, Exception) and "known_hosts" in str(exc):
                raise RuntimeError(
                    "服务器主机指纹尚未被本机信任。请先在终端手动执行一次 "
                    f"`ssh {self.user}@{self.host}`，核对并接受主机指纹后再测试。"
                ) from exc
            raise
        return client

    def execute(self, remote_command: str) -> CommandResult:
        deadline = time.monotonic() + REMOTE_COMMAND_TIMEOUT_SECONDS
        client = None
        channel = None
        try:
            client = self._connect(deadline)
            _, stdout, _ = client.exec_command(
                remote_command,
                timeout=_remaining_remote_time(deadline),
            )
            channel = stdout.channel
            channel.settimeout(_remaining_remote_time(deadline))
            stdout_capture = bytearray()
            stderr_capture = bytearray()
            captured_bytes = 0

            def capture(target: bytearray, chunk: bytes) -> None:
                nonlocal captured_bytes
                if captured_bytes + len(chunk) > MAX_REMOTE_CAPTURE_BYTES:
                    raise CommandOutputLimitError(
                        f"Remote command output exceeded the "
                        f"{MAX_REMOTE_CAPTURE_BYTES}-byte capture limit."
                    )
                target.extend(chunk)
                captured_bytes += len(chunk)

            while True:
                _remaining_remote_time(deadline)

                progressed = False
                if channel.recv_ready():
                    _remaining_remote_time(deadline)
                    capture(stdout_capture, channel.recv(64 * 1024))
                    progressed = True
                if channel.recv_stderr_ready():
                    _remaining_remote_time(deadline)
                    capture(stderr_capture, channel.recv_stderr(64 * 1024))
                    progressed = True

                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    returncode = channel.recv_exit_status()
                    break
                if not progressed:
                    time.sleep(min(0.01, _remaining_remote_time(deadline)))

            stdout_text = bytes(stdout_capture).decode("utf-8", errors="replace")
            stderr_text = bytes(stderr_capture).decode("utf-8", errors="replace")
        except CommandOutputLimitError:
            if channel is not None:
                channel.close()
            raise
        except CommandTimeoutError:
            if channel is not None:
                channel.close()
            raise
        except (socket.timeout, TimeoutError) as exc:
            if channel is not None:
                channel.close()
            raise CommandTimeoutError(
                f"Remote command exceeded the {REMOTE_COMMAND_TIMEOUT_SECONDS}-second time limit."
            ) from exc
        finally:
            if client is not None:
                client.close()
        result = CommandResult(
            command=["ssh", self.target, remote_command],
            returncode=returncode,
            stdout=stdout_text,
            stderr=stderr_text,
            captured_bytes=captured_bytes,
        )
        _raise_for_result(result)
        return result

    def execute_bounded(
        self,
        remote_command: str,
        *,
        absolute_deadline: float,
        max_capture_bytes: int,
        check: bool = False,
    ) -> CommandResult:
        # The Paramiko implementation already enforces the absolute deadline;
        # this bounded entry point additionally applies the caller's byte cap.
        if max_capture_bytes <= 0:
            raise CommandOutputLimitError("remote output limit exhausted")
        deadline = absolute_deadline
        client = None
        channel = None
        try:
            client = self._connect(deadline)
            _, stdout, _ = client.exec_command(
                remote_command,
                timeout=_remaining_remote_time(deadline),
            )
            channel = stdout.channel
            channel.settimeout(_remaining_remote_time(deadline))
            stdout_capture = bytearray()
            stderr_capture = bytearray()
            captured_bytes = 0

            def capture(target: bytearray, chunk: bytes) -> None:
                nonlocal captured_bytes
                if captured_bytes + len(chunk) > max_capture_bytes:
                    raise CommandOutputLimitError(
                        f"Remote command output exceeded the {max_capture_bytes}-byte capture limit."
                    )
                target.extend(chunk)
                captured_bytes += len(chunk)

            while True:
                _remaining_remote_time(deadline)
                progressed = False
                if channel.recv_ready():
                    capture(stdout_capture, channel.recv(64 * 1024))
                    progressed = True
                if channel.recv_stderr_ready():
                    capture(stderr_capture, channel.recv_stderr(64 * 1024))
                    progressed = True
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    returncode = channel.recv_exit_status()
                    break
                if not progressed:
                    time.sleep(min(0.01, _remaining_remote_time(deadline)))
            result = CommandResult(
                command=["ssh", self.target, remote_command],
                returncode=returncode,
                stdout=bytes(stdout_capture).decode("utf-8", errors="replace"),
                stderr=bytes(stderr_capture).decode("utf-8", errors="replace"),
                captured_bytes=captured_bytes,
            )
        except (socket.timeout, TimeoutError) as exc:
            raise CommandTimeoutError("Remote command exceeded its deadline.") from exc
        finally:
            if channel is not None and not getattr(channel, "closed", False):
                channel.close()
            if client is not None:
                client.close()
        if check:
            _raise_for_result(result)
        return result

    def upload(self, local_paths: Sequence[Path], remote_dir: str) -> CommandResult:
        uploaded: list[str] = []
        with self._connect() as client:
            sftp = client.open_sftp()
            try:
                for local_path in local_paths:
                    destination = f"{remote_dir.rstrip('/')}/{local_path.name}"
                    sftp.put(str(local_path), destination)
                    uploaded.append(local_path.name)
            finally:
                sftp.close()
        return CommandResult(
            command=[
                "sftp-upload",
                *(path.name for path in local_paths),
                f"{self.target}:{remote_dir}/",
            ],
            returncode=0,
            stdout="\n".join(uploaded),
            stderr="",
        )

    def download(self, remote_file: str, local_path: Path) -> CommandResult:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as client:
            sftp = client.open_sftp()
            try:
                sftp.get(remote_file, str(local_path))
            finally:
                sftp.close()
        return CommandResult(
            command=["sftp-download", f"{self.target}:{remote_file}", str(local_path)],
            returncode=0,
            stdout=str(local_path),
            stderr="",
        )


def _raise_for_result(result: CommandResult) -> None:
    if result.returncode == 0:
        return
    raise RuntimeError(
        f"Remote command failed with exit code {result.returncode}: "
        f"{' '.join(result.command)}\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
