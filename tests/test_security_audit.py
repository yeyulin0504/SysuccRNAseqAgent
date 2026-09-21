from rnaseq_agent.remote_browse import BrowseAudit
import json

import pytest

from rnaseq_agent.security_audit import (
    AuditCommitUncertainError,
    _canonical,
    _parse,
    _record_browse_audit,
    browse_history_projection,
)
from rnaseq_agent.private_files import ensure_private_file


def test_history_projection_is_deidentified():
    event = BrowseAudit(
        event_id="audit_" + "0" * 32,
        project_id="p1",
        thread_id="t1",
        source="llm_tool",
        identity_digest="sha256:i",
        requested_path_digest="sha256:p",
        canonical_target="/secret/path",
        root_id="root_1",
        browse_policy_revision="sha256:r",
        directory_count=2,
        sample_count=3,
        unmatched_count=1,
        truncated=False,
        outcome="allowed",
        error_code=None,
        started_at="2026-09-17T12:00:00Z",
        completed_at="2026-09-17T12:00:01Z",
    )
    projected = browse_history_projection(event)
    assert projected["event_id"] == event.event_id
    assert "canonical_target" not in projected
    assert "root_id" not in projected


def test_audit_parser_rejects_unknown_fields(tmp_path):
    event = BrowseAudit(
        event_id="audit_" + "1" * 32,
        project_id="p1",
        thread_id=None,
        source="workbench",
        identity_digest="sha256:i",
        requested_path_digest="sha256:p",
        canonical_target="/secret/path",
        root_id="root_1",
        browse_policy_revision="sha256:r",
        directory_count=0,
        sample_count=0,
        unmatched_count=0,
        truncated=False,
        outcome="denied",
        error_code="REMOTE_ROOT_NOT_APPROVED",
        started_at="2026-09-17T12:00:00Z",
        completed_at="2026-09-17T12:00:01Z",
    )
    payload = json.loads(_canonical(event))
    payload["unexpected"] = True
    path = tmp_path / "event.record"
    ensure_private_file(path)
    path.write_bytes(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    with pytest.raises(ValueError):
        _parse(path)


def _event(*, outcome="allowed", error_code=None):
    return BrowseAudit(
        event_id="audit_" + "2" * 32,
        project_id="p1",
        thread_id="t1",
        source="workbench",
        identity_digest="sha256:" + "i" * 64,
        requested_path_digest="sha256:" + "p" * 64,
        canonical_target="/secret/path",
        root_id="root_1",
        browse_policy_revision="sha256:" + "r" * 64,
        directory_count=1,
        sample_count=2,
        unmatched_count=0,
        truncated=False,
        outcome=outcome,
        error_code=error_code,
        started_at="2026-09-17T12:00:00Z",
        completed_at="2026-09-17T12:00:01Z",
    )


def test_authoritative_audit_success_is_durable(tmp_path, monkeypatch):
    from rnaseq_agent import security_audit

    audit_dir = tmp_path / "audit"
    monkeypatch.setattr(security_audit, "_audit_dir", lambda: audit_dir)
    event = _event()

    assert _record_browse_audit(event) == event.event_id
    path = security_audit._path(audit_dir, event)
    assert path.is_file()
    assert _parse(path) == _canonical(event)


def test_authoritative_audit_explicit_failure_has_no_published_record(tmp_path, monkeypatch):
    from rnaseq_agent import security_audit

    audit_dir = tmp_path / "audit"
    monkeypatch.setattr(security_audit, "_audit_dir", lambda: audit_dir)

    def fail_write(path, raw):
        raise OSError("audit disk unavailable")

    monkeypatch.setattr(security_audit, "_write", fail_write)
    event = _event(outcome="failed", error_code="REMOTE_SCAN_FAILED")
    with pytest.raises(OSError, match="audit disk unavailable"):
        _record_browse_audit(event)
    assert not audit_dir.exists() or not any(audit_dir.glob("event-*.record"))


def test_authoritative_audit_uncertain_commit_is_explicit_and_not_trusted(tmp_path, monkeypatch):
    from rnaseq_agent import security_audit

    audit_dir = tmp_path / "audit"
    monkeypatch.setattr(security_audit, "_audit_dir", lambda: audit_dir)
    monkeypatch.setattr(security_audit, "_reconcile_published", lambda path, event: "uncertain")

    def publish_then_fail(path, raw):
        path.write_bytes(raw)
        raise OSError("directory fsync failed")

    monkeypatch.setattr(security_audit, "_write", publish_then_fail)
    with pytest.raises(AuditCommitUncertainError, match="durability is uncertain"):
        _record_browse_audit(_event())


def test_finalize_drops_authoritative_audit_payload_after_history_failure(monkeypatch):
    from rnaseq_agent import webapp
    from rnaseq_agent.chat_graph import ToolExecutionResult

    event = _event()
    monkeypatch.setattr(webapp, "_record_browse_audit", lambda value: value.event_id)
    monkeypatch.setattr(webapp, "append_history", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("history unavailable")))
    result = ToolExecutionResult(
        local={"result": {"groups": [{"samples": [{"sample_id": "SENSITIVE"}]}]}},
        model={"ok": True, "sample_count": 2},
        log_projection={"ok": True, "sample_count": 2},
        security_audit=event,
    )

    finalized = webapp._finalize_tool_execution_result(result, None)

    assert finalized.security_audit is None
    assert finalized.local == result.local
    assert finalized.model == result.model
    assert finalized.log_projection == result.log_projection


def test_finalize_audit_failure_removes_exact_pending_projection(tmp_path, monkeypatch):
    from rnaseq_agent import webapp
    from rnaseq_agent.chat_graph import ToolExecutionResult

    event = _event()
    monkeypatch.setattr(webapp, "_record_browse_audit", lambda value: (_ for _ in ()).throw(OSError("audit unavailable")))
    result = ToolExecutionResult(
        local={"reference": {"scan_id": "scan_" + "a" * 32}},
        model={"sample_count": 2, "source_ref": "src_" + "b" * 32},
        log_projection={"sample_count": 2},
        security_audit=event,
    )

    finalized = webapp._finalize_tool_execution_result(result, tmp_path)

    assert finalized.security_audit is None
    assert finalized.local["error_code"] == "REMOTE_SECURITY_AUDIT_FAILED"
    assert finalized.model["source_ref"] is None
    assert "reference" not in finalized.local
