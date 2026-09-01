from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .analysis_contract import ContractError, create_project_contract, verify_project_contract
from .actions import (
    generate_project_report,
    load_project_config,
    load_status_summary,
    refresh_project_status_action,
    run_project_action,
    status_summary,
    validate_project,
    validation_summary,
)
from .chat import run_chat
from .configuration import normalize_config
from .estimate import estimate_runtime
from .gui import run_gui
from .mvp_cli import main as mvp_main
from .preflight import PreflightError, run_preflight
from .remote import build_directory_scan_command, build_upload_commands
from .storage import load_json, save_json
from .ssh_auth import clear_ssh_credential, set_ssh_credential
from .wizard import run_wizard
from .workflow_profiles import apply_workflow_profile, list_workflow_profiles


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rnaseq-agent",
        description="Interactive RNA-seq analysis agent.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    wizard = subparsers.add_parser("wizard", help="Start the interactive project wizard.")
    wizard.add_argument(
        "--output-dir",
        default="runs",
        help="Directory where project configs will be written. Default: runs",
    )

    chat = subparsers.add_parser(
        "chat",
        help="Start the conversational project-session agent.",
    )
    chat.add_argument(
        "--output-dir",
        default="runs",
        help="Directory where project configs will be written. Default: runs",
    )

    gui = subparsers.add_parser("gui", help="Start the graphical project setup interface.")
    gui.add_argument(
        "--output-dir",
        default="runs",
        help="Directory where project configs will be written. Default: runs",
    )

    estimate = subparsers.add_parser("estimate", help="Estimate runtime from a saved project config.")
    estimate.add_argument("config", help="Path to project.json")

    scan = subparsers.add_parser(
        "scan-command",
        help="Print an optional SSH command for checking a remote FASTQ directory.",
    )
    scan.add_argument("config", help="Path to project.json")

    upload = subparsers.add_parser(
        "upload-command",
        help="Print commands that upload local FASTQ files to the server.",
    )
    upload.add_argument("config", help="Path to project.json")

    validate = subparsers.add_parser(
        "validate-local",
        help="Check whether local FASTQ files referenced by the project config exist.",
    )
    validate.add_argument("config", help="Path to project.json")

    preflight = subparsers.add_parser(
        "preflight",
        help="Run one privacy-preserving, read-only server environment check.",
    )
    preflight.add_argument("config", help="Path to project.json")
    preflight.add_argument(
        "--output",
        help="JSON output path. Default: <project_dir>/preflight.json",
    )
    preflight.add_argument(
        "--prompt-password",
        action="store_true",
        help=(
            "Prompt locally for a temporary SSH password. The password is kept "
            "in process memory only and is never accepted as a command argument."
        ),
    )

    run = subparsers.add_parser(
        "run",
        help="Validate, upload, submit, poll, download, and optionally notify for a project.",
    )
    run.add_argument("config", help="Path to project.json")
    run.add_argument(
        "--no-wait",
        action="store_true",
        help="Submit the remote job and return without polling for completion.",
    )

    status = subparsers.add_parser(
        "status",
        help="Refresh project status from the server and print the current state.",
    )
    status.add_argument("config", help="Path to project.json")
    status.add_argument(
        "--cached",
        action="store_true",
        help="Only print the status already stored in project.json without contacting the server.",
    )

    report = subparsers.add_parser(
        "report",
        help="Generate a Markdown summary report for a project run.",
    )
    report.add_argument("config", help="Path to project.json")
    report.add_argument(
        "--output",
        help="Path to write the Markdown report. Default: <project_dir>/report.md",
    )

    subparsers.add_parser(
        "profiles",
        help="List versioned RNA-seq workflow skill profiles.",
    )

    apply_profile = subparsers.add_parser(
        "apply-profile",
        help="Apply a versioned workflow skill profile to project.json.",
    )
    apply_profile.add_argument("config", help="Path to project.json")
    apply_profile.add_argument("profile", help="Workflow profile ID")

    contract = subparsers.add_parser(
        "contract",
        help="Create or verify an immutable analysis contract.",
    )
    contract_subparsers = contract.add_subparsers(dest="contract_command", required=True)
    contract_create = contract_subparsers.add_parser(
        "create",
        help="Validate inputs, freeze the analysis, and activate contract mode.",
    )
    contract_create.add_argument("config", help="Path to project.json")
    contract_create.add_argument(
        "--output",
        help="Contract path. Default: <project_dir>/analysis_contract.json",
    )
    contract_verify = contract_subparsers.add_parser(
        "verify",
        help="Recompute input and script fingerprints and verify the approved contract.",
    )
    contract_verify.add_argument("config", help="Path to project.json")
    contract_verify.add_argument("--contract", help="Optional contract path override")

    mvp = subparsers.add_parser(
        "mvp",
        help="Capability-aware, auditable MVP project session.",
    )
    mvp.add_argument("mvp_args", nargs=argparse.REMAINDER)

    web = subparsers.add_parser(
        "web",
        help="Start the localhost web workbench (framework phase-1 UI).",
    )
    web.add_argument(
        "--project-dir",
        default="runs/mvp_web",
        help="Project directory. Default: runs/mvp_web",
    )
    web.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Loopback port. Default: 8000",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "wizard":
        run_wizard(Path(args.output_dir))
        return 0

    if args.command == "chat":
        run_chat(Path(args.output_dir))
        return 0

    if args.command == "gui":
        run_gui(Path(args.output_dir))
        return 0

    if args.command == "estimate":
        config = load_project_config(Path(args.config))
        runtime = estimate_runtime(config)
        print(runtime.summary)
        return 0

    if args.command == "scan-command":
        config = load_project_config(Path(args.config))
        server = config["server"]
        remote_dir = config["samples"]["remote_data_dir"]
        command = build_directory_scan_command(server["host"], server["user"], remote_dir)
        print(command.description)
        print(f"{command.command[0]} {command.command[1]} \"{command.command[2]}\"")
        return 0

    if args.command == "upload-command":
        config = load_project_config(Path(args.config))
        for command in build_upload_commands(config):
            print(command.description)
            print(_format_command(command.command))
        return 0

    if args.command == "validate-local":
        _, result = validate_project(Path(args.config))
        print(f"Checked FASTQ files: {result.checked_files}")
        for line in validation_summary(result):
            print(line)
        return 0 if result.ok else 1

    if args.command == "preflight":
        credential_identity: tuple[str, str] | None = None
        temporary_password = ""
        try:
            if args.prompt_password:
                config = normalize_config(load_json(Path(args.config).resolve()))
                server = config.get("server", {})
                host = str(server.get("host", "")).strip()
                user = str(server.get("user", "")).strip()
                if not host or not user:
                    raise PreflightError(
                        "Password prompt requires configured server.host and server.user."
                    )
                temporary_password = getpass.getpass(
                    f"Temporary SSH password for {user}@{host}: "
                )
                if not temporary_password:
                    raise PreflightError("No SSH password was entered.")
                set_ssh_credential(
                    host,
                    user,
                    mode="password",
                    password=temporary_password,
                )
                credential_identity = (host, user)
            output, report = run_preflight(
                Path(args.config),
                output_path=Path(args.output) if args.output else None,
            )
        except (PreflightError, OSError, ValueError, EOFError, KeyboardInterrupt) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            if credential_identity is not None:
                clear_ssh_credential(*credential_identity)
            temporary_password = ""
        summary = report["summary"]
        print(f"Preflight result: {report['overall']}")
        print(f"Errors: {summary['errors']}; warnings: {summary['warnings']}")
        print(f"Sanitized report written to: {output}")
        return 0 if report["overall"] in {"pass", "warning"} else 1

    if args.command == "run":
        try:
            outcome = run_project_action(Path(args.config), wait=not args.no_wait)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(outcome.message)
        print(f"Project directory: {outcome.project_dir}")
        return 0 if outcome.state in {"submitted", "completed"} else 1

    if args.command == "status":
        if args.cached:
            lines = load_status_summary(Path(args.config))
        else:
            status = refresh_project_status_action(Path(args.config))
            lines = status_summary(status)
        for line in lines:
            print(line)
        return 0

    if args.command == "report":
        output = generate_project_report(
            Path(args.config),
            output_path=Path(args.output) if args.output else None,
        )
        print(f"Report written to: {output}")
        return 0

    if args.command == "profiles":
        for profile_id, profile in list_workflow_profiles():
            print(f"{profile_id}: {profile['description']}")
        return 0

    if args.command == "apply-profile":
        config_path = Path(args.config)
        try:
            config = normalize_config(load_json(config_path))
            updated = apply_workflow_profile(config, args.profile)
            save_json(config_path, normalize_config(updated))
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Applied workflow profile {args.profile} to {config_path}")
        return 0

    if args.command == "contract":
        config_path = Path(args.config)
        if args.contract_command == "create":
            try:
                path, contract_payload = create_project_contract(
                    config_path,
                    output_path=Path(args.output) if args.output else None,
                )
            except (ContractError, OSError, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(f"Contract written to: {path}")
            print(f"Contract ID: {contract_payload['contract_id']}")
            return 0

        verification = verify_project_contract(
            config_path,
            contract_path=Path(args.contract) if args.contract else None,
        )
        print(f"Contract ID: {verification.contract_id or 'unknown'}")
        print(f"Current ID: {verification.current_contract_id or 'unknown'}")
        if verification.ok:
            print("Analysis contract verification passed.")
            return 0
        for error in verification.errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    if args.command == "mvp":
        return mvp_main(args.mvp_args)

    if args.command == "web":
        try:
            from .webapp import run_server
        except ImportError as exc:
            print(
                f"缺少 web 依赖（{exc}）。请安装：pip install langgraph fastapi uvicorn jinja2",
                file=sys.stderr,
            )
            return 1
        run_server(project_dir=Path(args.project_dir), port=args.port)
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


def _format_command(command: list[str]) -> str:
    return " ".join(_quote_arg(arg) for arg in command)


def _quote_arg(arg: str) -> str:
    if not arg or any(char.isspace() for char in arg):
        return '"' + arg.replace('"', '\\"') + '"'
    return arg
