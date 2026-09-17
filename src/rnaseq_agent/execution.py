from __future__ import annotations

import os
import signal
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


_CLEANUP_GRACE_SECONDS = 1.0


def _create_windows_kill_job(process: subprocess.Popen[bytes]) -> int | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        information = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        configured = kernel32.SetInformationJobObject(
            job,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            job, wintypes.HANDLE(int(process._handle))
        )
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _close_windows_job(job: int | None) -> None:
    if os.name != "nt" or job is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(wintypes.HANDLE(job))
    except (AttributeError, OSError, TypeError, ValueError):
        pass


def _terminate_process_tree(
    process: subprocess.Popen[bytes], windows_job: int | None
) -> None:
    if os.name == "nt":
        if windows_job is not None:
            _close_windows_job(windows_job)
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_CLEANUP_GRACE_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _wait_for_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=_CLEANUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=_CLEANUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass


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

    popen_options: dict[str, object] = {}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(
        list(command),
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **popen_options,
    )
    windows_job = _create_windows_kill_job(process)
    assert process.stdout is not None
    assert process.stderr is not None

    lock = threading.Lock()
    overflow = threading.Event()
    captures = {"stdout": bytearray(), "stderr": bytearray()}
    captured_bytes = 0
    reader_done = {"stdout": threading.Event(), "stderr": threading.Event()}

    def drain(name: str, pipe: BinaryIO) -> None:
        nonlocal captured_bytes
        try:
            while not overflow.is_set():
                chunk = (
                    pipe.read1(64 * 1024)
                    if hasattr(pipe, "read1")
                    else pipe.read(64 * 1024)
                )
                if not chunk:
                    return
                with lock:
                    if captured_bytes + len(chunk) > max_capture_bytes:
                        overflow.set()
                        return
                    captures[name].extend(chunk)
                    captured_bytes += len(chunk)
        except (OSError, ValueError):
            return
        finally:
            reader_done[name].set()

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
    tree_terminated = False
    try:
        while True:
            if overflow.is_set():
                break
            if process.poll() is not None and all(
                event.is_set() for event in reader_done.values()
            ):
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.01)

        if timed_out or overflow.is_set():
            _terminate_process_tree(process, windows_job)
            tree_terminated = True
        _wait_for_process(process)
    finally:
        if process.poll() is None and not tree_terminated:
            _terminate_process_tree(process, windows_job)
            tree_terminated = True
        _wait_for_process(process)
        if not tree_terminated:
            _close_windows_job(windows_job)
        for pipe in (process.stdout, process.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        cleanup_deadline = time.monotonic() + _CLEANUP_GRACE_SECONDS
        for reader in readers:
            reader.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))

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
