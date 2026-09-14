from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rnaseq_agent.execution import CommandResult
from rnaseq_agent.remote_transport import (
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


class SystemSSHTransportTests(unittest.TestCase):
    @patch("rnaseq_agent.remote_transport.run_command")
    def test_key_mode_uses_batch_mode_and_private_key(
        self,
        run_command: MagicMock,
    ) -> None:
        run_command.return_value = CommandResult(
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

        command = run_command.call_args.args[0]
        self.assertIn("BatchMode=yes", command)
        self.assertIn("C:/keys/id_ed25519", command)
        self.assertNotIn("temporary-secret", command)

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

    @patch("rnaseq_agent.remote_transport.run_command")
    def test_nonstandard_port_uses_ssh_and_scp_specific_flags(
        self,
        run_command: MagicMock,
    ) -> None:
        run_command.return_value = CommandResult([], 0, "", "")
        server = {**SERVER, "port": 2222}
        transport = SystemSSHTransport(server, SSHCredential(mode="system"))

        transport.execute("printf ok")
        ssh_command = run_command.call_args.args[0]
        transport.upload([Path("sample.fastq.gz")], "/remote/raw")
        scp_command = run_command.call_args.args[0]

        self.assertIn("-p", ssh_command)
        self.assertIn("2222", ssh_command)
        self.assertIn("-P", scp_command)
        self.assertIn("2222", scp_command)


if __name__ == "__main__":
    unittest.main()
