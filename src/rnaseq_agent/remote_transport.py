from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, Sequence

from .execution import CommandResult, run_command
from .shell import remote_path
from .ssh_auth import SSHCredential, get_ssh_credential


REMOTE_COMMAND_TIMEOUT_SECONDS = 120
MAX_REMOTE_CAPTURE_BYTES = 4 * 1024 * 1024


class RemoteTransport(Protocol):
    def execute(self, remote_command: str) -> CommandResult: ...

    def upload(self, local_paths: Sequence[Path], remote_dir: str) -> CommandResult: ...

    def download(self, remote_file: str, local_path: Path) -> CommandResult: ...


def create_remote_transport(config: dict[str, Any]) -> RemoteTransport:
    server = config["server"]
    credential = get_ssh_credential(server["host"], server["user"])
    if credential.mode == "password":
        if not credential.password:
            raise RuntimeError(
                "当前选择了密码登录，但本次程序中没有临时密码。"
                "请在 GUI“服务器”页点击“输入临时密码”。"
            )
        return ParamikoTransport(server, credential)
    return SystemSSHTransport(server, credential)


def _server_port(server: dict[str, Any]) -> int:
    value = server.get("port") or 22
    return int(value)


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
        self.host = str(server["host"])
        self.user = str(server["user"])
        self.port = _server_port(server)
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
        return run_command(["ssh", *self._ssh_options(), self.target, remote_command])

    def upload(self, local_paths: Sequence[Path], remote_dir: str) -> CommandResult:
        sources = [str(path).replace("\\", "/") for path in local_paths]
        return run_command(
            [
                "scp",
                *self._scp_options(),
                *sources,
                f"{self.target}:{remote_path(remote_dir)}/",
            ]
        )

    def download(self, remote_file: str, local_path: Path) -> CommandResult:
        return run_command(
            [
                "scp",
                *self._scp_options(),
                f"{self.target}:{remote_file}",
                str(local_path).replace("\\", "/"),
            ]
        )


class ParamikoTransport:
    def __init__(self, server: dict[str, Any], credential: SSHCredential) -> None:
        self.host = str(server["host"])
        self.user = str(server["user"])
        self.password = credential.password
        self.port = _server_port(server)
        self.target = f"{self.user}@{self.host}"

    def _connect(self):
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError(
                "密码登录需要 Paramiko。请重新安装项目依赖后再试。"
            ) from exc
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
                password=self.password,
                timeout=15,
                auth_timeout=20,
                banner_timeout=20,
                allow_agent=False,
                look_for_keys=False,
            )
        except Exception as exc:
            client.close()
            if "known_hosts" in str(exc):
                raise RuntimeError(
                    "服务器主机指纹尚未被本机信任。请先在终端手动执行一次 "
                    f"`ssh {self.user}@{self.host}`，核对并接受主机指纹后再测试。"
                ) from exc
            raise
        return client

    def execute(self, remote_command: str) -> CommandResult:
        with self._connect() as client:
            _, stdout, stderr = client.exec_command(
                remote_command,
                timeout=REMOTE_COMMAND_TIMEOUT_SECONDS,
            )
            stdout.channel.settimeout(REMOTE_COMMAND_TIMEOUT_SECONDS)
            stdout_bytes = stdout.read(MAX_REMOTE_CAPTURE_BYTES + 1)
            stderr_bytes = stderr.read(MAX_REMOTE_CAPTURE_BYTES + 1)
            if (
                len(stdout_bytes) > MAX_REMOTE_CAPTURE_BYTES
                or len(stderr_bytes) > MAX_REMOTE_CAPTURE_BYTES
            ):
                stdout.channel.close()
                raise RuntimeError(
                    "Remote command output exceeded the safe capture limit."
                )
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
            returncode = stdout.channel.recv_exit_status()
        result = CommandResult(
            command=["ssh", self.target, remote_command],
            returncode=returncode,
            stdout=stdout_text,
            stderr=stderr_text,
        )
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
