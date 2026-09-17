from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Sequence


class CommandTimeoutError(RuntimeError):
    """A bounded child process exceeded its wall-clock limit."""


class CommandOutputLimitError(RuntimeError):
    """A bounded child process exceeded its combined capture limit."""


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


def run_command(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> CommandResult:
    completed = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        encoding="utf-8",
    )
    result = CommandResult(
        command=list(command),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    if check and result.returncode != 0:
        message = (
            f"Command failed with exit code {result.returncode}: {' '.join(result.command)}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
        raise RuntimeError(message)
    return result


def run_command_bounded(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    max_capture_bytes: int,
    cwd: Path | None = None,
    check: bool = True,
) -> CommandResult:
    """Run a command while bounding wall time and combined captured output."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_capture_bytes <= 0:
        raise ValueError("max_capture_bytes must be positive")

    process = subprocess.Popen(
        list(command),
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    lock = threading.Lock()
    overflow = threading.Event()
    captures = {"stdout": bytearray(), "stderr": bytearray()}
    captured_bytes = 0

    def drain(name: str, pipe: BinaryIO) -> None:
        nonlocal captured_bytes
        while not overflow.is_set():
            chunk = pipe.read(64 * 1024)
            if not chunk:
                return
            with lock:
                if captured_bytes + len(chunk) > max_capture_bytes:
                    overflow.set()
                    return
                captures[name].extend(chunk)
                captured_bytes += len(chunk)

    readers = [
        threading.Thread(
            target=drain,
            args=("stdout", process.stdout),
            daemon=True,
            name="command-stdout",
        ),
        threading.Thread(
            target=drain,
            args=("stderr", process.stderr),
            daemon=True,
            name="command-stderr",
        ),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    try:
        while process.poll() is None:
            if overflow.is_set():
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.01)

        if timed_out or overflow.is_set():
            process.kill()
        process.wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        for reader in readers:
            reader.join()
        process.stdout.close()
        process.stderr.close()

    if timed_out:
        raise CommandTimeoutError(
            f"Command exceeded the {timeout_seconds:g}-second time limit."
        )
    if overflow.is_set():
        raise CommandOutputLimitError(
            f"Command output exceeded the {max_capture_bytes}-byte capture limit."
        )

    result = CommandResult(
        command=list(command),
        returncode=int(process.returncode or 0),
        stdout=bytes(captures["stdout"]).decode("utf-8", errors="replace"),
        stderr=bytes(captures["stderr"]).decode("utf-8", errors="replace"),
    )
    if check and result.returncode != 0:
        message = (
            f"Command failed with exit code {result.returncode}: {' '.join(result.command)}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
        raise RuntimeError(message)
    return result
