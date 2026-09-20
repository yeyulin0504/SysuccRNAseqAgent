from rnaseq_agent.remote_browse import BrowseAudit
from rnaseq_agent.security_audit import browse_history_projection


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
