from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .configuration import normalize_config
from .preflight import PreflightError, build_read_only_probe, run_preflight
from .ssh_auth import clear_ssh_credential, set_ssh_credential
from .storage import load_json


def _bundle_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="readonly_hpc_preflight",
        description="Portable password-authenticated, read-only HPC preflight.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Preflight-only JSON. Default: preflight_project.json beside the executable.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Sanitized JSON output. Default: server_preflight.json beside the executable.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Validate the bundled runtime and fixed probe without connecting to SSH.",
    )
    return parser


def _resolve_path(value: Path | None, default_name: str) -> Path:
    if value is None:
        return _bundle_directory() / default_name
    if value.is_absolute():
        return value.resolve()
    return (_bundle_directory() / value).resolve()


def _load_identity(config_path: Path) -> tuple[dict, str, str]:
    config = normalize_config(load_json(config_path))
    server = config.get("server", {})
    host = str(server.get("host", "")).strip()
    user = str(server.get("user", "")).strip()
    if not host or not user:
        raise PreflightError(
            "preflight_project.json is missing the server address or username."
        )
    return config, host, user


def _run_self_test(config_path: Path) -> int:
    try:
        import bcrypt
        import cryptography
        import nacl
        import paramiko

        config, _, _ = _load_identity(config_path)
        command = build_read_only_probe(config)
    except (ImportError, OSError, ValueError, PreflightError):
        print("SELF-TEST FAILED: bundled runtime or preflight config is incomplete.")
        return 2

    forbidden = re.compile(
        r"(^|[;&|]\s*)(mkdir|touch|rm|mv|cp|chmod|chown|sbatch|qsub)\b"
    )
    init_commands = config.get("server", {}).get("init_commands", [])
    if forbidden.search(command) or any(str(item) in command for item in init_commands):
        print("SELF-TEST FAILED: the generated probe did not pass the read-only policy check.")
        return 2
    print(
        "SELF-TEST PASS: runtime, password SSH library, config, and fixed probe are ready."
    )
    print(f"Paramiko {paramiko.__version__}; cryptography {cryptography.__version__}")
    return 0


def _publish_fresh_report(candidate: Path, destination: Path) -> Path:
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not payload.get("generated_at"):
        raise PreflightError("The fresh sanitized report is incomplete.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = destination.with_name(f"{destination.name}.previous-{stamp}")
        destination.replace(backup)
        print(f"Previous report preserved as: {backup.name}")
    candidate.replace(destination)
    return destination


def _write_debug_log(destination: Path, message: str) -> Path:
    debug_path = destination.with_name("preflight_debug.txt")
    payload = [
        "Hospital HPC read-only preflight debug",
        f"generated_at={datetime.now(timezone.utc).isoformat()}",
        f"message={message}",
        "password_saved=false",
        "fastq_or_sample_data_accessed=false",
    ]
    debug_path.write_text("\n".join(payload) + "\n", encoding="utf-8")
    return debug_path


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config_path = _resolve_path(args.config, "preflight_project.json")
    output_path = _resolve_path(args.output, "server_preflight.json")

    if args.self_test:
        return _run_self_test(config_path)

    attempt_token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = output_path.with_name(
        f".{output_path.name}.pending-{attempt_token}-{os.getpid()}"
    )
    identity: tuple[str, str] | None = None
    temporary_password = ""
    try:
        _, host, user = _load_identity(config_path)
        temporary_password = getpass.getpass(
            "Hospital HPC SSH password (input is hidden): "
        )
        if not temporary_password:
            raise PreflightError("No SSH password was entered; no connection was attempted.")
        set_ssh_credential(
            host,
            user,
            mode="password",
            password=temporary_password,
        )
        identity = (host, user)
        _, report = run_preflight(
            config_path,
            output_path=candidate,
        )
        published = _publish_fresh_report(candidate, output_path)
    except (PreflightError, OSError, ValueError, json.JSONDecodeError) as exc:
        debug_path = _write_debug_log(output_path, str(exc))
        print(f"Preflight did not complete: {exc}", file=sys.stderr)
        print(f"Debug details were saved to: {debug_path.name}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("Preflight cancelled; no password or report was saved.", file=sys.stderr)
        return 2
    finally:
        if identity is not None:
            clear_ssh_credential(*identity)
        temporary_password = ""
        if candidate.exists():
            candidate.unlink(missing_ok=True)

    summary = report["summary"]
    print(f"Fresh sanitized report: {published}")
    print(
        f"Result: {report['overall']}; errors: {summary['errors']}; "
        f"warnings: {summary['warnings']}"
    )
    return 0 if report["overall"] in {"pass", "warning"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
