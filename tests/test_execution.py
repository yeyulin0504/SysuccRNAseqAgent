from __future__ import annotations

import sys
import time

import pytest

from rnaseq_agent.execution import (
    CommandOutputLimitError,
    CommandTimeoutError,
    run_command_bounded,
)


def test_bounded_command_kills_timeout_child() -> None:
    started = time.monotonic()

    with pytest.raises(CommandTimeoutError) as caught:
        run_command_bounded(
            [
                sys.executable,
                "-c",
                "import time; print('ready', flush=True); time.sleep(30)",
            ],
            timeout_seconds=0.2,
            max_capture_bytes=4096,
        )

    assert time.monotonic() - started < 5
    assert "ready" not in str(caught.value)


def test_bounded_command_kills_output_overflow() -> None:
    started = time.monotonic()

    with pytest.raises(CommandOutputLimitError):
        run_command_bounded(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 8192); sys.stdout.flush()",
            ],
            timeout_seconds=5,
            max_capture_bytes=1024,
        )

    assert time.monotonic() - started < 5


def test_bounded_command_applies_one_combined_output_limit() -> None:
    with pytest.raises(CommandOutputLimitError):
        run_command_bounded(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stdout.buffer.write(b'o' * 700); sys.stdout.flush(); "
                    "sys.stderr.buffer.write(b'e' * 700); sys.stderr.flush()"
                ),
            ],
            timeout_seconds=5,
            max_capture_bytes=1024,
        )


def test_bounded_command_decodes_utf8_and_replaces_invalid_bytes() -> None:
    result = run_command_bounded(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "sys.stdout.buffer.write('你好'.encode()); "
                "sys.stderr.buffer.write(b'\\xff')"
            ),
        ],
        timeout_seconds=5,
        max_capture_bytes=4096,
    )

    assert result.stdout == "你好"
    assert result.stderr == "\ufffd"


def test_bounded_command_errors_do_not_echo_command_secrets() -> None:
    secret = "test-only-secret-sentinel"

    with pytest.raises(CommandTimeoutError) as caught:
        run_command_bounded(
            [sys.executable, "-c", "import time; time.sleep(30)", secret],
            timeout_seconds=0.1,
            max_capture_bytes=1024,
        )

    assert secret not in str(caught.value)


def _grandchild_command(*, output_bytes: int = 0) -> str:
    output = (
        f"sys.stdout.buffer.write(b'x' * {output_bytes}); sys.stdout.flush(); "
        if output_bytes
        else ""
    )
    return f"import sys, time; {output}time.sleep(4)"


def test_bounded_command_kills_grandchild_process_tree_on_timeout() -> None:
    grandchild = _grandchild_command()
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "time.sleep(30)"
    )
    started = time.monotonic()

    with pytest.raises(CommandTimeoutError):
        run_command_bounded(
            [sys.executable, "-c", parent],
            timeout_seconds=0.15,
            max_capture_bytes=4096,
        )

    assert time.monotonic() - started < 2


def test_bounded_command_times_out_when_exited_parent_leaves_inherited_pipes_open() -> None:
    grandchild = _grandchild_command()
    parent = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])"
    )
    started = time.monotonic()

    with pytest.raises(CommandTimeoutError):
        run_command_bounded(
            [sys.executable, "-c", parent],
            timeout_seconds=0.15,
            max_capture_bytes=4096,
        )

    assert time.monotonic() - started < 2


def test_bounded_command_kills_grandchild_process_tree_on_output_overflow() -> None:
    grandchild = _grandchild_command(output_bytes=8192)
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "time.sleep(30)"
    )
    started = time.monotonic()

    with pytest.raises(CommandOutputLimitError):
        run_command_bounded(
            [sys.executable, "-c", parent],
            timeout_seconds=5,
            max_capture_bytes=1024,
        )

    assert time.monotonic() - started < 2
