from __future__ import annotations

from dataclasses import dataclass

import pytest

from rnaseq_agent.connection_store import ApprovedDataRoot, BrowsePolicy
from rnaseq_agent.remote_browse import (
    BrowseExecutionBudget,
    BrowseDirectoryGroup,
    BrowseSampleRow,
    BrowseScanPayload,
    BrowseContext,
    BrowseResult,
    REMOTE_PATH_INVALID,
    browse_remote_fastqs,
    contains_posix_path,
    provider_browse_summary,
    build_scan_command,
    scan_result_from_canonical_files,
    scan_remote_fastqs,
    REMOTE_PATH_ESCAPE,
    REMOTE_POLICY_CHANGED,
    REMOTE_SCAN_INVALID_OUTPUT,
    ScanPathEscape,
    ScanPython3Required,
    REMOTE_SCAN_PYTHON3_REQUIRED,
)
from rnaseq_agent.ssh_identity import SSHIdentity


CONTEXT = BrowseContext(project_id="project-1", thread_id=None, source="workbench")
IDENTITY = SSHIdentity(host="example.test", user="alice", port=22)


def _root(path: str = "/data/root", *, root_id: str = "root-1") -> ApprovedDataRoot:
    return ApprovedDataRoot(
        root_id=root_id,
        host=IDENTITY.host,
        user=IDENTITY.user,
        port=IDENTITY.port,
        requested_path=path,
        canonical_path=path,
        created_at="2026-01-01T00:00:00Z",
        revoked_at=None,
    )


def _policy(*roots: ApprovedDataRoot) -> BrowsePolicy:
    return BrowsePolicy(
        identity=IDENTITY,
        roots=tuple(roots),
        revision="sha256:revision",
        available=True,
    )


class Spy:
    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("spy should not be called")


class ScriptedTransport:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def execute_bounded(self, command, *, absolute_deadline, max_capture_bytes):
        self.calls.append(command)
        for path, result in self.mapping.items():
            if path in command:
                return result
        raise AssertionError(command)


def _command_result(stdout: str, returncode: int = 0):
    from rnaseq_agent.execution import CommandResult

    return CommandResult(["scripted"], returncode, stdout, "", len(stdout.encode()))


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "relative/path",
        "./data",
        "/data/../secret",
        "/data/./reads",
        "/data//reads",
        "/data/reads/",
        "/data\x00/reads",
        "/data\nreads",
        "//server/share",
        "C:/reads",
        " /data/reads",
        "/data/reads ",
    ],
)
def test_invalid_lexical_path_short_circuits(value):
    policy_spy = Spy()
    transport_spy = Spy()
    scanner_spy = Spy()
    result = browse_remote_fastqs(
        value,
        CONTEXT,
        policy_spy,
        transport_spy,
        scanner_spy,
    )
    assert isinstance(result, BrowseResult)
    assert result.error_code == REMOTE_PATH_INVALID
    assert policy_spy.calls == 0
    assert transport_spy.calls == 0
    assert scanner_spy.calls == 0


def test_component_containment_uses_path_components():
    assert contains_posix_path("/data/root", "/data/root/sample") is True
    assert contains_posix_path("/data/root", "/data/root2/sample") is False


def test_no_active_root_fails_before_transport():
    transport = Spy()
    scanner = Spy()
    empty = BrowsePolicy(IDENTITY, (), "sha256:empty", True)
    result = browse_remote_fastqs("/data/reads", CONTEXT, lambda: empty, transport, scanner)
    assert result.error_code == "REMOTE_ROOT_NOT_APPROVED"
    assert transport.calls == 0
    assert scanner.calls == 0


def test_exact_root_authorizes_injected_scanner():
    root = _root()
    policy = _policy(root)
    transport = ScriptedTransport({
        "/data/root": _command_result("/data/root\n"),
    })
    payload = BrowseScanPayload(
        groups=(BrowseDirectoryGroup("g1", "/data/root", (BrowseSampleRow("s", "a_R1.fastq", None),), ()),),
        sample_count=1,
        unmatched_count=0,
        truncated=False,
    )
    calls = []
    def scanner(canonical_target, canonical_root, runner, transport_obj):
        calls.append((canonical_target, canonical_root))
        return payload
    result = browse_remote_fastqs("/data/root", CONTEXT, lambda: policy, lambda _: transport, scanner)
    assert result.ok is True
    assert result.authorization["root_id"] == "root-1"
    assert calls == [("/data/root", "/data/root")]


def test_stale_approved_root_fails_closed_before_target_scan():
    root = _root()
    policy = _policy(root)

    class Transport:
        def execute_bounded(self, command, *, absolute_deadline, max_capture_bytes):
            return _command_result("/data/moved\n")

    scanner = Spy()
    result = browse_remote_fastqs(
        "/data/root", CONTEXT, lambda: policy, lambda _: Transport(), scanner
    )
    assert result.error_code == "REMOTE_ROOT_STALE"
    assert scanner.calls == 0


def test_symlinked_target_escape_is_rejected_without_partial_scan():
    root = _root()
    policy = _policy(root)

    class Transport:
        def execute_bounded(self, command, *, absolute_deadline, max_capture_bytes):
            if "realpath -e" in command:
                if "-- /data/root)" in command:
                    return _command_result("/data/root\n")
                return _command_result("/data/outside\n")
            raise AssertionError(command)

    scanner = Spy()
    result = browse_remote_fastqs(
        "/data/root/run", CONTEXT, lambda: policy, lambda _: Transport(), scanner
    )
    assert result.error_code == "REMOTE_PATH_ESCAPE"
    assert scanner.calls == 0


def test_nested_approved_root_uses_longest_canonical_match():
    outer = _root("/data", root_id="outer")
    inner = _root("/data/root", root_id="inner")
    policy = _policy(outer, inner)

    class Transport:
        def execute_bounded(self, command, *, absolute_deadline, max_capture_bytes):
            if "-- /data/root/run)" in command:
                return _command_result("/data/root/run\n")
            if "-- /data/root)" in command:
                return _command_result("/data/root\n")
            if "-- /data)" in command:
                return _command_result("/data\n")
            raise AssertionError(command)

    payload = BrowseScanPayload(
        groups=(BrowseDirectoryGroup("g1", "/data/root/run", (BrowseSampleRow("s", "a_R1.fastq", None),), ()),),
        sample_count=1,
        unmatched_count=0,
        truncated=False,
    )
    seen = []
    result = browse_remote_fastqs(
        "/data/root/run", CONTEXT, lambda: policy, lambda _: Transport(),
        lambda target, canonical_root, *_: (seen.append((target, canonical_root)) or payload),
    )
    assert result.ok is True
    assert result.authorization["root_id"] == "inner"
    assert seen == [("/data/root/run", "/data/root")]


def test_policy_recheck_prevents_scan():
    root = _root()
    policy = _policy(root)
    changed = BrowsePolicy(IDENTITY, (), "sha256:changed", True)
    policies = iter([policy, changed])
    transport = ScriptedTransport({"/data/root": _command_result("/data/root\n")})
    scanner = Spy()
    result = browse_remote_fastqs("/data/root", CONTEXT, lambda: next(policies), lambda _: transport, scanner)
    assert result.error_code == "REMOTE_POLICY_CHANGED"
    assert scanner.calls == 0


def test_policy_change_during_scan_rejects_result_after_scanner_returns():
    root = _root()
    policy = _policy(root)
    changed = BrowsePolicy(IDENTITY, (), "sha256:changed", True)
    current = {"value": policy}
    transport = ScriptedTransport({"/data/root": _command_result("/data/root\n")})
    payload = BrowseScanPayload(
        groups=(BrowseDirectoryGroup("g1", "/data/root", (BrowseSampleRow("s", "a_R1.fastq", None),), ()),),
        sample_count=1,
        unmatched_count=0,
        truncated=False,
    )

    def scanner(*args):
        current["value"] = changed
        return payload

    result = browse_remote_fastqs(
        "/data/root", CONTEXT, lambda: current["value"], lambda _: transport, scanner
    )

    assert result.ok is False
    assert result.error_code == REMOTE_POLICY_CHANGED
    assert result.groups == ()
    assert result.sample_count == 0


def test_provider_summary_is_deidentified():
    result = BrowseResult(
        ok=True,
        blocked=False,
        error_code=None,
        message="/data/root/secret.fastq",
        groups=(BrowseDirectoryGroup("root-id", "/data/root", (BrowseSampleRow("unique-sample-basename", "/data/root/secret.fastq", None),), ()),),
        sample_count=1,
        unmatched_count=0,
        truncated=False,
        authorization={"root_id": "root-id", "canonical_target": "/data/root"},
        audit=None,
    )
    summary = provider_browse_summary(result, "source-ref")
    text = repr(summary)
    for secret in ("secret.fastq", "/data/root", "root-id", "unique-sample-basename", "example.test", "alice"):
        assert secret not in text


def test_execution_budget_reuses_deadline_and_subtracts_raw_bytes():
    from rnaseq_agent.execution import CommandResult

    class Transport:
        def __init__(self):
            self.calls = []

        def execute_bounded(self, command, *, absolute_deadline, max_capture_bytes):
            self.calls.append((command, absolute_deadline, max_capture_bytes))
            return CommandResult([], 0, "\ufffd", "", 3)

    runner = BrowseExecutionBudget.start(timeout_seconds=5, max_output_bytes=10)
    transport = Transport()
    runner.execute(transport, "one")
    runner.execute(transport, "two")
    assert transport.calls[0][1] == transport.calls[1][1]
    assert transport.calls[0][2] == 10
    assert transport.calls[1][2] == 7
    assert runner.remaining_output_bytes == 4


def test_scan_argv_keeps_hostile_path_out_of_helper_source():
    path = "/data/team/a'; touch /tmp/pwned; echo '"
    command = build_scan_command("/data/team", path)
    assert path not in command.helper_source
    assert command.argv[-1] == path
    assert command.max_depth == 2
    assert command.max_candidates == 5_000
    assert "find" not in command.remote_command


def test_scan_helper_executes_with_framed_positional_arguments(tmp_path):
    import json
    import subprocess
    import sys

    target = tmp_path / "target"
    target.mkdir()
    (target / "S1_R1.fastq.gz").write_bytes(b"reads")
    command = build_scan_command(str(tmp_path), str(target))
    completed = subprocess.run(
        [sys.executable, *command.argv[1:]],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["files"] == [str(target / "S1_R1.fastq.gz")]
    assert document["truncated"] is False


def test_browse_rejects_non_basename_sample_and_unmatched_rows():
    root = _root()
    policy = _policy(root)
    transport = ScriptedTransport({"/data/root": _command_result("/data/root\n")})

    def scanner(*args):
        return BrowseScanPayload(
            groups=(BrowseDirectoryGroup(
                "g1",
                "/data/root",
                (BrowseSampleRow("s", "/data/root/a_R1.fastq", None),),
                ("/data/root/orphan.fastq",),
            ),),
            sample_count=1,
            unmatched_count=1,
            truncated=False,
        )

    result = browse_remote_fastqs(
        "/data/root", CONTEXT, lambda: policy, lambda _: transport, scanner
    )
    assert result.error_code == REMOTE_SCAN_INVALID_OUTPUT


def test_missing_remote_python3_has_stable_scan_error():
    class Transport:
        def execute_bounded(self, command, *, absolute_deadline, max_capture_bytes):
            assert "command -v python3" in command
            return _command_result("", returncode=46)

    runner = BrowseExecutionBudget.start(timeout_seconds=5, max_output_bytes=10000)
    with pytest.raises(ValueError, match=REMOTE_SCAN_PYTHON3_REQUIRED):
        scan_remote_fastqs("/data/root/run", "/data/root", runner, Transport())


def test_browse_maps_missing_remote_python3_to_stable_error():
    root = _root()
    policy = _policy(root)
    transport = ScriptedTransport({"/data/root": _command_result("/data/root\n")})

    def scanner(*args):
        raise ScanPython3Required(REMOTE_SCAN_PYTHON3_REQUIRED)

    result = browse_remote_fastqs(
        "/data/root", CONTEXT, lambda: policy, lambda _: transport, scanner
    )
    assert result.error_code == REMOTE_SCAN_PYTHON3_REQUIRED


def test_grouping_returns_canonical_parent_and_basename_rows():
    result = scan_result_from_canonical_files([
        "/data/root/run-a/S1_R1.fastq.gz",
        "/data/root/run-a/S1_R2.fastq.gz",
        "/data/root/run-b/S2_R1.fastq.gz",
        "/data/root/run-b/S2_R2.fastq.gz",
    ])
    assert [g.canonical_directory for g in result.groups] == [
        "/data/root/run-a", "/data/root/run-b",
    ]
    assert result.groups[0].samples[0].fastq_1 == "S1_R1.fastq.gz"
    assert result.groups[0].samples[0].fastq_2 == "S1_R2.fastq.gz"
    assert "/" not in result.groups[0].samples[0].fastq_1


def test_scanner_rejects_candidate_outside_target_without_partial_results():
    class Transport:
        def execute_bounded(self, command, *, absolute_deadline, max_capture_bytes):
            return _command_result('{"files":["/data/root/run/S1_R1.fastq.gz", "/tmp/escape.fastq.gz"], "truncated": false}')

    runner = BrowseExecutionBudget.start(timeout_seconds=5, max_output_bytes=10000)
    with pytest.raises(ValueError, match=REMOTE_PATH_ESCAPE):
        scan_remote_fastqs("/data/root/run", "/data/root", runner, Transport())


def test_scanner_rejects_duplicate_json_keys_as_invalid_output():
    class Transport:
        def execute_bounded(self, command, *, absolute_deadline, max_capture_bytes):
            return _command_result('{"files":[],"files":[],"truncated":false}')

    runner = BrowseExecutionBudget.start(timeout_seconds=5, max_output_bytes=10000)
    with pytest.raises(ValueError, match=REMOTE_SCAN_INVALID_OUTPUT):
        scan_remote_fastqs("/data/root", "/data/root", runner, Transport())


def test_browse_maps_scanner_candidate_escape_to_path_escape():
    root = _root()
    policy = _policy(root)
    transport = ScriptedTransport({"/data/root": _command_result("/data/root\n")})

    def scanner(*args):
        raise ScanPathEscape(REMOTE_PATH_ESCAPE)

    result = browse_remote_fastqs("/data/root", CONTEXT, lambda: policy, lambda _: transport, scanner)
    assert result.error_code == REMOTE_PATH_ESCAPE


def test_grouping_caps_returned_rows_and_marks_truncated():
    paths = [f"/data/root/run/S{i}_R1.fastq.gz" for i in range(2_501)]
    result = scan_result_from_canonical_files(paths)
    assert result.truncated is True
    assert result.sample_count == 2_500
