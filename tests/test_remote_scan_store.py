from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from rnaseq_agent.remote_browse import (
    BrowseContext,
    BrowseDirectoryGroup,
    BrowseResult,
    BrowseSampleRow,
    _result,
)
from rnaseq_agent.connection_store import ApprovedDataRoot, BrowsePolicy
from rnaseq_agent.ssh_identity import SSHIdentity
from rnaseq_agent.remote_scan_store import (
    consume_remote_scan,
    load_remote_scan,
    remote_scan_lookup_path,
    RemoteScanReference,
    StoredRemoteScan,
    store_remote_scan,
)


def test_store_remote_scan_returns_opaque_reference(tmp_path):
    ctx = BrowseContext("p1", "t1", "llm_tool")
    result = _result(
        context=ctx,
        event_id="e1" * 16,
        started_at="2026-09-17T12:00:00Z",
        identity=None,
        requested_path="/srv/team/run-1",
        canonical_target="/srv/team/run-1",
        root_id="root_1",
        revision="sha256:" + "1" * 64,
        payload=__import__("rnaseq_agent.remote_browse", fromlist=["BrowseScanPayload"]).BrowseScanPayload(
            (BrowseDirectoryGroup("g1", "/srv/team/run-1", (BrowseSampleRow("s1", "s1_R1.fastq.gz", "s1_R2.fastq.gz"),), ()),),
            1,
            0,
            False,
        ),
        authorization={"root_id": "root_1", "canonical_root": "/srv/team", "canonical_target": "/srv/team/run-1", "policy_revision": "sha256:" + "1" * 64},
    )
    ref = store_remote_scan(tmp_path, result)
    assert isinstance(ref, RemoteScanReference)
    assert ref.project_id == "p1"
    assert ref.source_ref.startswith("src_")
    assert ref.scan_id.startswith("scan_")


def _stored_case(tmp_path, *, truncated=False, source="workbench", thread_id=None):
    identity = SSHIdentity("example.test", "alice", 22)
    root = ApprovedDataRoot("root_1", "example.test", "alice", 22, "/srv/team", "/srv/team", "2026-01-01T00:00:00Z", None)
    policy = BrowsePolicy(identity, (root,), "sha256:" + "2" * 64, True)
    context = BrowseContext("p1", thread_id, source)
    group = BrowseDirectoryGroup("group_1", "/srv/team/run-1", (BrowseSampleRow("s1", "s1_R1.fastq.gz", "s1_R2.fastq.gz"),), ())
    payload = __import__("rnaseq_agent.remote_browse", fromlist=["BrowseScanPayload"]).BrowseScanPayload((group,), 1, 0, truncated)
    result = _result(
        context=context, event_id="audit_" + "2" * 32, started_at="2026-09-17T12:00:00Z",
        identity=identity, requested_path="/srv/team/run-1", canonical_target="/srv/team/run-1",
        root_id="root_1", revision="sha256:" + "2" * 64, payload=payload, authorization={},
    )
    ref = store_remote_scan(tmp_path, result)
    return ref, policy, context, group


def test_consume_remote_scan_is_one_time_and_binds_workbench_context(tmp_path):
    ref, policy, context, group = _stored_case(tmp_path)
    calls = []
    consumed = consume_remote_scan(
        tmp_path, scan_id=ref.scan_id, result_revision=ref.result_revision,
        group_id=group.group_id, expected_policy=policy, expected_context=context,
        apply=lambda selected, claim: calls.append((selected.group_id, claim)),
    )
    assert consumed == group
    assert calls and calls[0][0] == group.group_id
    with pytest.raises(ValueError, match="REMOTE_SCAN_REFERENCE_USED"):
        consume_remote_scan(
            tmp_path, scan_id=ref.scan_id, result_revision=ref.result_revision,
            group_id=group.group_id, expected_policy=policy, expected_context=context,
            apply=lambda *_: calls.append(("replay", "replay")),
        )
    assert len(calls) == 1


def test_consume_remote_scan_rejects_other_source_and_truncation_without_callback(tmp_path):
    ref, policy, _context, group = _stored_case(tmp_path, source="rule_chat", thread_id="thread-1")
    calls = []
    with pytest.raises(ValueError, match="REMOTE_SCAN_REFERENCE_INVALID"):
        consume_remote_scan(
            tmp_path, scan_id=ref.scan_id, result_revision=ref.result_revision,
            group_id=group.group_id, expected_policy=policy,
            expected_context=BrowseContext("p1", None, "workbench"),
            apply=lambda *_: calls.append(True),
        )
    assert not calls


def test_consume_remote_scan_reuses_durable_claim_after_callback_failure(tmp_path):
    ref, policy, context, group = _stored_case(tmp_path)
    claims = []
    def fail_once(_group, claim):
        claims.append(claim)
        if len(claims) == 1:
            raise RuntimeError("simulated process failure")
    with pytest.raises(RuntimeError, match="simulated process failure"):
        consume_remote_scan(
            tmp_path, scan_id=ref.scan_id, result_revision=ref.result_revision,
            group_id=group.group_id, expected_policy=policy, expected_context=context,
            apply=fail_once,
        )
    consume_remote_scan(
        tmp_path, scan_id=ref.scan_id, result_revision=ref.result_revision,
        group_id=group.group_id, expected_policy=policy, expected_context=context,
        apply=fail_once,
    )
    assert claims[0] == claims[1]


def test_concurrent_consumers_have_one_callback_and_stable_used_result(tmp_path):
    ref, policy, context, group = _stored_case(tmp_path)
    entered = Event()
    release = Event()
    calls = []

    def apply(selected, claim):
        calls.append((selected.group_id, claim))
        entered.set()
        assert release.wait(timeout=5)

    kwargs = dict(
        project_dir=tmp_path,
        scan_id=ref.scan_id,
        result_revision=ref.result_revision,
        group_id=group.group_id,
        expected_policy=policy,
        expected_context=context,
        apply=apply,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(consume_remote_scan, **kwargs)
        assert entered.wait(timeout=5)
        second = pool.submit(consume_remote_scan, **kwargs)
        release.set()
        assert first.result(timeout=5) == group
        with pytest.raises(ValueError, match="REMOTE_SCAN_REFERENCE_USED"):
            second.result(timeout=5)

    assert len(calls) == 1


def test_consume_remote_scan_fails_closed_for_revoked_or_changed_root(tmp_path):
    ref, policy, context, group = _stored_case(tmp_path)
    revoked = BrowsePolicy(
        policy.identity,
        (ApprovedDataRoot("root_1", "example.test", "alice", 22, "/srv/team", "/srv/team", "2026", "2026-09-20T00:00:00Z"),),
        policy.revision,
        True,
    )
    with pytest.raises(ValueError, match="REMOTE_SCAN_REFERENCE_INVALID"):
        consume_remote_scan(
            tmp_path, scan_id=ref.scan_id, result_revision=ref.result_revision,
            group_id=group.group_id, expected_policy=revoked, expected_context=context,
            apply=lambda *_: pytest.fail("revoked root reached callback"),
        )


def test_consume_remote_scan_rejects_immutable_revision_tamper_and_outside_group(tmp_path):
    ref, policy, context, group = _stored_case(tmp_path)
    record = next((tmp_path / ".remote-scans").glob("scan-*.record"))
    payload = __import__("json").loads(record.read_text(encoding="utf-8"))
    payload["groups"][0]["canonical_directory"] = "/outside/run"
    record.write_text(__import__("json").dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="REMOTE_SCAN_REFERENCE_INVALID"):
        consume_remote_scan(
            tmp_path, scan_id=ref.scan_id, result_revision=ref.result_revision,
            group_id=group.group_id, expected_policy=policy, expected_context=context,
            apply=lambda *_: pytest.fail("tampered record reached callback"),
        )


def test_remote_scan_record_rejects_duplicate_group_ids(tmp_path):
    ref, policy, context, group = _stored_case(tmp_path)
    record = next((tmp_path / ".remote-scans").glob("scan-*.record"))
    payload = __import__("json").loads(record.read_text(encoding="utf-8"))
    payload["groups"].append(dict(payload["groups"][0]))
    record.write_text(__import__("json").dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="REMOTE_SCAN_REFERENCE_INVALID"):
        load_remote_scan(tmp_path, scan_id=ref.scan_id)


def test_consume_remote_scan_rejects_expiry_tamper(tmp_path):
    ref, policy, context, group = _stored_case(tmp_path)
    record = next((tmp_path / ".remote-scans").glob("scan-*.record"))
    payload = __import__("json").loads(record.read_text(encoding="utf-8"))
    payload["expires_at"] = "2099-01-01T00:00:00Z"
    record.write_text(__import__("json").dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="REMOTE_SCAN_REFERENCE_INVALID"):
        consume_remote_scan(
            tmp_path, scan_id=ref.scan_id, result_revision=ref.result_revision,
            group_id=group.group_id, expected_policy=policy, expected_context=context,
            apply=lambda *_: pytest.fail("expiry tamper reached callback"),
        )


def test_legacy_revision_record_fails_closed_and_requires_rescan(tmp_path):
    ref, policy, context, group = _stored_case(tmp_path)
    record = next((tmp_path / ".remote-scans").glob("scan-*.record"))
    payload = __import__("json").loads(record.read_text(encoding="utf-8"))
    # The pre-integrity formula omitted immutable scan/source/timestamp fields.
    payload["result_revision"] = "sha256:" + "b" * 64
    record.write_text(__import__("json").dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="REMOTE_SCAN_REFERENCE_INVALID"):
        consume_remote_scan(
            tmp_path, scan_id=ref.scan_id, result_revision=payload["result_revision"],
            group_id=group.group_id, expected_policy=policy, expected_context=context,
            apply=lambda *_: pytest.fail("legacy record reached callback"),
        )

    calls = []
    ref, policy, context, group = _stored_case(tmp_path / "truncated", truncated=True)
    with pytest.raises(ValueError, match="REMOTE_SCAN_TRUNCATED_ACK_REQUIRED"):
        consume_remote_scan(
            tmp_path / "truncated", scan_id=ref.scan_id, result_revision=ref.result_revision,
            group_id=group.group_id, expected_policy=policy, expected_context=context,
            apply=lambda *_: calls.append(True),
        )
    assert not calls


def test_source_alias_rebinding_is_rejected(tmp_path):
    first, _policy, _context, _group = _stored_case(tmp_path)
    second, _policy, _context, _group = _stored_case(tmp_path)
    alias = remote_scan_lookup_path(tmp_path / ".remote-scans", first.source_ref, kind="source")
    alias.write_text(
        '{"scan_id": "' + second.scan_id + '", "source_ref": "' + first.source_ref + '"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="REMOTE_SCAN_REFERENCE_INVALID"):
        load_remote_scan(tmp_path, source_ref=first.source_ref)
