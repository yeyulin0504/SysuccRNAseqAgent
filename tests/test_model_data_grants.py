from datetime import datetime, timedelta, timezone
import json

import pytest

from rnaseq_agent.model_data_grants import (
    DataGrantError,
    ExactClaimInputs,
    mark_exact_open_stream,
    mark_exact_prepare,
    claim_grant_for_send,
    decide_grant,
    grant_record_path,
    issue_grant_request,
    load_grant,
)
from rnaseq_agent.model_disclosure import (
    MODEL_DATA_GRANT_CONSUMED,
    MODEL_DATA_GRANT_EXPIRED,
    MODEL_DATA_GRANT_INVALID,
    MODEL_DATA_GRANT_REJECTED,
    MODEL_DATA_REVISION_CHANGED,
    MODEL_PROVIDER_REQUEST_FAILED,
    ProviderRequestError,
)


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _project(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    (path / "project.json").write_text(
        json.dumps({
            "project": {"id": "p1"},
            "samples": {"items": [{
                "sample_id": "SENTINEL_SAMPLE",
                "fastq_1": "SENTINEL_R1.fastq.gz",
                "fastq_2": "SENTINEL_R2.fastq.gz",
            }]},
        }), encoding="utf-8"
    )
    return path


def _connection(tmp_path):
    from rnaseq_agent.connection_store import save_llm
    path = tmp_path / "connection"
    save_llm({"provider": "openai", "api_base": "https://example.test/v1", "model": "test", "tool_mode": "read_only"}, store_dir=path)
    return path


def test_issue_persists_metadata_only_and_expiry(tmp_path):
    project = _project(tmp_path)
    connection = _connection(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", connection_store_dir=connection, now=NOW)
    assert grant.status == "pending"
    assert grant.expires_at == "2026-09-21T12:10:00Z"
    raw = grant_record_path(project, grant.grant_id).read_text(encoding="utf-8")
    assert "SENTINEL_SAMPLE" not in raw
    assert "check" not in raw
    assert load_grant(project, grant.grant_id) == grant


def test_rejection_is_terminal_and_idempotent(tmp_path):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    rejected = decide_grant(project, grant.grant_id, approved=False, now=NOW)
    assert rejected.status == "rejected"
    assert rejected.error_code == MODEL_DATA_GRANT_REJECTED
    assert decide_grant(project, grant.grant_id, approved=False, now=NOW) == rejected
    with pytest.raises(DataGrantError) as caught:
        decide_grant(project, grant.grant_id, approved=True, now=NOW)
    assert caught.value.code == MODEL_DATA_GRANT_CONSUMED


def test_expired_pending_grant_becomes_failed(tmp_path):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    expired = decide_grant(project, grant.grant_id, approved=True, now=NOW + timedelta(seconds=601))
    assert expired.status == "consumed_failed"
    assert expired.error_code == MODEL_DATA_GRANT_EXPIRED


def test_load_malformed_record_fails_closed(tmp_path):
    project = _project(tmp_path)
    connection = _connection(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", connection_store_dir=connection, now=NOW)
    grant_record_path(project, grant.grant_id).write_text("{}", encoding="utf-8")
    with pytest.raises(DataGrantError) as caught:
        load_grant(project, grant.grant_id)
    assert caught.value.code == MODEL_DATA_GRANT_INVALID


def test_claim_is_single_use_and_guard_consumes_success(tmp_path):
    project = _project(tmp_path)
    connection = _connection(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", connection_store_dir=connection, now=NOW)
    decide_grant(project, grant.grant_id, approved=True, now=NOW)
    prepared = claim_grant_for_send(
        project, grant.grant_id,
        live_inputs=ExactClaimInputs("p1", "t1", connection_store_dir=connection),
        prepare=mark_exact_prepare(lambda claim, snapshot: (None, object())),
        open_stream=mark_exact_open_stream(lambda request: iter(())),
        now=NOW,
    )
    assert prepared.claim.exact_values["sample_ids"] == ("SENTINEL_SAMPLE",)
    with prepared.terminal_guard():
        pass
    assert load_grant(project, grant.grant_id).status == "consumed_success"
    with pytest.raises(DataGrantError) as caught:
        claim_grant_for_send(project, grant.grant_id, live_inputs=ExactClaimInputs("p1", "t1", connection_store_dir=connection), prepare=mark_exact_prepare(lambda claim, snapshot: (None, object())), open_stream=mark_exact_open_stream(lambda request: iter(())), now=NOW)
    assert caught.value.code == MODEL_DATA_GRANT_CONSUMED


def test_claim_requires_marked_prepare_and_open_stream_without_mutating_grant(tmp_path):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    decide_grant(project, grant.grant_id, approved=True, now=NOW)
    with pytest.raises(DataGrantError) as caught:
        claim_grant_for_send(project, grant.grant_id, live_inputs=ExactClaimInputs("p1", "t1"), now=NOW)
    assert caught.value.code == MODEL_DATA_GRANT_INVALID
    assert load_grant(project, grant.grant_id).status == "approved"

    with pytest.raises(DataGrantError):
        claim_grant_for_send(
            project, grant.grant_id, live_inputs=ExactClaimInputs("p1", "t1"),
            prepare=lambda claim, snapshot: (None, object()),
            open_stream=mark_exact_open_stream(lambda request: iter(())), now=NOW,
        )
    assert load_grant(project, grant.grant_id).status == "approved"


def test_claim_compares_live_counts_before_extracting(tmp_path):
    project = _project(tmp_path)
    connection = _connection(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", connection_store_dir=connection, now=NOW)
    decide_grant(project, grant.grant_id, approved=True, now=NOW)
    payload = json.loads((project / "project.json").read_text(encoding="utf-8"))
    payload["samples"]["items"].append({"sample_id": "NEW", "fastq_1": "n1"})
    (project / "project.json").write_text(json.dumps(payload), encoding="utf-8")
    called = []
    with pytest.raises(DataGrantError) as caught:
        claim_grant_for_send(
            project, grant.grant_id, live_inputs=ExactClaimInputs("p1", "t1", connection_store_dir=connection),
            prepare=mark_exact_prepare(lambda claim, snapshot: called.append("prepare") or (None, object())),
            open_stream=mark_exact_open_stream(lambda request: iter(())), now=NOW,
        )
    assert caught.value.code == MODEL_DATA_REVISION_CHANGED
    assert called == []
    assert load_grant(project, grant.grant_id).status == "consumed_failed"


@pytest.mark.parametrize("started, expected_status", [(False, "consumed_failed"), (True, "consumed_ambiguous")])
def test_prepare_transport_signal_controls_terminal_outcome(tmp_path, started, expected_status):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    decide_grant(project, grant.grant_id, approved=True, now=NOW)

    @mark_exact_prepare
    def prepare(claim, snapshot):
        raise ProviderRequestError("transport", code=MODEL_PROVIDER_REQUEST_FAILED, transmission_started=started)

    with pytest.raises(ProviderRequestError):
        claim_grant_for_send(
            project, grant.grant_id, live_inputs=ExactClaimInputs("p1", "t1"),
            prepare=prepare, open_stream=mark_exact_open_stream(lambda request: iter(())), now=NOW,
        )
    assert load_grant(project, grant.grant_id).status == expected_status


def test_remote_source_ref_remains_unsupported(tmp_path):
    project = _project(tmp_path)
    with pytest.raises(DataGrantError) as caught:
        issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", source_ref="remote:1", now=NOW)
    assert caught.value.code == "MODEL_DATA_SCOPE_UNSUPPORTED"


@pytest.mark.parametrize("project_id, thread_id", [("", "t1"), ("p1", " ")])
def test_issue_rejects_empty_bindings(tmp_path, project_id, thread_id):
    with pytest.raises(DataGrantError) as caught:
        issue_grant_request(_project(tmp_path), project_id=project_id, thread_id=thread_id, fields=("sample_ids",), purpose="check", now=NOW)
    assert caught.value.code == MODEL_DATA_GRANT_INVALID


def test_load_rejects_terminal_manifest_mismatch(tmp_path):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    decide_grant(project, grant.grant_id, approved=True, now=NOW)
    record = grant_record_path(project, grant.grant_id)
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["status"] = "consumed_failed"
    payload["consumed_at"] = NOW.isoformat().replace("+00:00", "Z")
    payload["manifest"] = {"fields": ["fastq_filenames"], "record_counts": {"fastq_filenames": 1}, "byte_length": 0, "revisions": payload["revisions"]}
    record.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DataGrantError) as caught:
        load_grant(project, grant.grant_id)
    assert caught.value.code == MODEL_DATA_GRANT_INVALID


def test_stream_failure_after_transport_is_ambiguous(tmp_path):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    decide_grant(project, grant.grant_id, approved=True, now=NOW)

    @mark_exact_open_stream
    def open_stream(request):
        def events():
            raise ProviderRequestError("wire", code=MODEL_PROVIDER_REQUEST_FAILED, transmission_started=True)
            yield None
        return events()

    prepared = claim_grant_for_send(
        project, grant.grant_id, live_inputs=ExactClaimInputs("p1", "t1"),
        prepare=mark_exact_prepare(lambda claim, snapshot: (None, object())), open_stream=open_stream, now=NOW,
    )
    with pytest.raises(ProviderRequestError):
        with prepared.terminal_guard():
            next(prepared.events)
    assert load_grant(project, grant.grant_id).status == "consumed_ambiguous"


def test_recovery_of_transmitting_grant_is_terminal(tmp_path):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    decide_grant(project, grant.grant_id, approved=True, now=NOW)
    claim_grant_for_send(
        project, grant.grant_id, live_inputs=ExactClaimInputs("p1", "t1"),
        prepare=mark_exact_prepare(lambda claim, snapshot: (None, object())), open_stream=mark_exact_open_stream(lambda request: iter(("started",))), now=NOW,
    )
    with pytest.raises(DataGrantError) as caught:
        claim_grant_for_send(
            project, grant.grant_id, live_inputs=ExactClaimInputs("p1", "t1"),
            prepare=mark_exact_prepare(lambda claim, snapshot: (None, object())), open_stream=mark_exact_open_stream(lambda request: iter(())), now=NOW,
        )
    assert caught.value.code == MODEL_DATA_GRANT_CONSUMED
    assert load_grant(project, grant.grant_id).status == "consumed_ambiguous"
