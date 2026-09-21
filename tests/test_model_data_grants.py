from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
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
    locked_model_disclosure_connection,
)
from rnaseq_agent.model_disclosure import (
    MODEL_DATA_GRANT_CONSUMED,
    MODEL_DATA_GRANT_EXPIRED,
    MODEL_DATA_GRANT_INVALID,
    MODEL_DATA_GRANT_REJECTED,
    MODEL_DATA_REVISION_CHANGED,
    MODEL_DATA_SCOPE_UNSUPPORTED,
    MODEL_PROVIDER_REQUEST_FAILED,
    ProviderRequestError,
)
from rnaseq_agent.model_context import canonical_json_sha256


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
    assert '"purpose":"check"' not in raw
    assert load_grant(project, grant.grant_id) == grant


def test_disclosure_purpose_is_normalized_to_a_safe_category(tmp_path):
    project = _project(tmp_path)
    grant = issue_grant_request(
        project,
        project_id="p1",
        thread_id="t1",
        fields=("sample_ids",),
        purpose="核对样本命名",
        now=NOW,
    )
    assert grant.purpose_category == "sample_identity_check"
    assert grant.purpose_hash == canonical_json_sha256({"purpose_category": "sample_identity_check"})


def test_disclosure_purpose_rejects_unbounded_free_text(tmp_path):
    project = _project(tmp_path)
    with pytest.raises(DataGrantError) as exc_info:
        issue_grant_request(
            project,
            project_id="p1",
            thread_id="t1",
            fields=("sample_ids",),
            purpose="请把患者张三的原始路径和报告都发给模型",
            now=NOW,
        )
    assert exc_info.value.code == MODEL_DATA_GRANT_INVALID


def test_legacy_grant_without_purpose_category_fails_closed(tmp_path):
    project = _project(tmp_path)
    grant = issue_grant_request(
        project,
        project_id="p1",
        thread_id="t1",
        fields=("sample_ids",),
        purpose="check",
        now=NOW,
    )
    path = grant_record_path(project, grant.grant_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("purpose_category", None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DataGrantError) as exc_info:
        load_grant(project, grant.grant_id)
    assert exc_info.value.code == MODEL_DATA_GRANT_INVALID


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


def test_claim_reads_project_data_under_project_lock(tmp_path, monkeypatch):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    decide_grant(project, grant.grant_id, approved=True, now=NOW)

    import rnaseq_agent.model_data_grants as grants_module

    events: list[str] = []
    real_lock = grants_module.project_state_lock

    @contextmanager
    def recording_lock(path):
        path_text = str(path)
        if path_text.endswith("project.json"):
            events.append("project-enter")
        with real_lock(path):
            yield
        if path_text.endswith("project.json"):
            events.append("project-exit")

    monkeypatch.setattr(grants_module, "project_state_lock", recording_lock)
    prepared = claim_grant_for_send(
        project,
        grant.grant_id,
        live_inputs=ExactClaimInputs("p1", "t1"),
        prepare=mark_exact_prepare(lambda claim, snapshot: (None, object())),
        open_stream=mark_exact_open_stream(lambda request: iter(())),
        now=NOW,
    )

    assert prepared.claim.exact_values["sample_ids"] == ("SENTINEL_SAMPLE",)
    assert events == ["project-enter", "project-exit"]


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


@pytest.mark.parametrize("field", ["fastq_filenames", "report_excerpt", "remote_paths"])
def test_issue_rejects_unsupported_fields_before_creating_grant(tmp_path, field):
    project = _project(tmp_path)
    with pytest.raises(DataGrantError) as caught:
        issue_grant_request(
            project,
            project_id="p1",
            thread_id="t1",
            fields=(field,),
            purpose="check",
            now=NOW,
        )
    assert caught.value.code == "MODEL_DATA_SCOPE_UNSUPPORTED"
    grant_dir = project / ".model_data_grants"
    assert not grant_dir.exists() or not list(grant_dir.glob("*.json"))


@pytest.mark.parametrize("project_id", ["p2", ""])
def test_issue_rejects_missing_or_mismatched_stored_project_id(tmp_path, project_id):
    project = _project(tmp_path)
    if project_id == "":
        payload = json.loads((project / "project.json").read_text(encoding="utf-8"))
        del payload["project"]["id"]
        (project / "project.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DataGrantError) as caught:
        issue_grant_request(
            project,
            project_id=project_id or "p1",
            thread_id="t1",
            fields=("sample_ids",),
            purpose="check",
            now=NOW,
        )
    assert caught.value.code == MODEL_DATA_GRANT_INVALID


def test_load_rejects_unsupported_field_even_when_record_is_forged(tmp_path):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    record = grant_record_path(project, grant.grant_id)
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["fields"] = ["fastq_filenames"]
    payload["record_counts"] = {"fastq_filenames": 1}
    record.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DataGrantError) as caught:
        load_grant(project, grant.grant_id)
    assert caught.value.code == MODEL_DATA_SCOPE_UNSUPPORTED


def test_claim_rejects_mismatched_stored_project_id(tmp_path):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    decide_grant(project, grant.grant_id, approved=True, now=NOW)
    payload = json.loads((project / "project.json").read_text(encoding="utf-8"))
    payload["project"]["id"] = "p2"
    (project / "project.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DataGrantError) as caught:
        claim_grant_for_send(
            project,
            grant.grant_id,
            live_inputs=ExactClaimInputs("p1", "t1"),
            prepare=mark_exact_prepare(lambda claim, snapshot: (None, object())),
            open_stream=mark_exact_open_stream(lambda request: iter(())),
            now=NOW,
        )
    assert caught.value.code == MODEL_DATA_GRANT_INVALID


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


@pytest.mark.parametrize("field, value", [
    ("project_id", 7), ("thread_id", []), ("policy_version", "1"),
    ("record_counts", [["sample_ids", 1]]), ("manifest", None),
])
def test_load_rejects_wrong_json_types(tmp_path, field, value):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    payload = json.loads(grant_record_path(project, grant.grant_id).read_text(encoding="utf-8"))
    payload[field] = value
    grant_record_path(project, grant.grant_id).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DataGrantError) as caught:
        load_grant(project, grant.grant_id)
    assert caught.value.code == MODEL_DATA_GRANT_INVALID


def test_load_enforces_status_timestamp_and_claim_hash_contract(tmp_path):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    record = grant_record_path(project, grant.grant_id)
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["status"] = "transmitting"
    payload["claimed_at"] = NOW.isoformat().replace("+00:00", "Z")
    payload["manifest"] = {}
    record.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DataGrantError):
        load_grant(project, grant.grant_id)


def test_callback_marker_attribute_spoof_is_rejected(tmp_path):
    project = _project(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", now=NOW)
    decide_grant(project, grant.grant_id, approved=True, now=NOW)
    def prepare(claim, snapshot):
        return None, object()
    prepare.__model_data_exact_prepare__ = True
    def open_stream(request):
        return iter(())
    open_stream.__model_data_exact_open_stream__ = True
    with pytest.raises(DataGrantError) as caught:
        claim_grant_for_send(project, grant.grant_id, live_inputs=ExactClaimInputs("p1", "t1"), prepare=prepare, open_stream=open_stream, now=NOW)
    assert caught.value.code == MODEL_DATA_GRANT_INVALID


def test_locked_connection_snapshot_reads_one_locked_store_and_exposes_revision(tmp_path, monkeypatch):
    connection = _connection(tmp_path)
    import rnaseq_agent.connection_store as connection_store

    def forbidden_unlocked_reader(*args, **kwargs):
        raise AssertionError("unlocked load_llm must not be used by locked snapshot")

    monkeypatch.setattr(connection_store, "load_llm", forbidden_unlocked_reader)
    with locked_model_disclosure_connection(store_dir=connection, runtime_secrets=("runtime-secret",)) as snapshot:
        assert snapshot.provider.model == "test"
        assert snapshot.tool_mode == "read_only"
        assert snapshot.connection_revision.startswith("sha256:")
        assert snapshot.credentials is not None
        assert "runtime-secret" in snapshot.credentials.known_secrets


def test_connection_revision_is_secret_independent_but_tracks_authorization_settings(tmp_path):
    connection = _connection(tmp_path)

    def revision():
        with locked_model_disclosure_connection(store_dir=connection) as snapshot:
            return snapshot.connection_revision

    initial = revision()
    payload_path = connection / "connection.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload.setdefault("llm", {})["api_key_protected"] = "dpapi:QUJD"
    payload["password_protected"] = "dpapi:U1NI"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    credential_added = revision()
    assert credential_added != initial

    payload["llm"]["api_key_protected"] = "dpapi:ROTATED_CIPHERTEXT"
    payload["password_protected"] = "dpapi:ROTATED_SSH_CIPHERTEXT"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    assert revision() == credential_added

    payload["llm"]["tool_mode"] = "disabled"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    assert revision() != credential_added
    mode_changed = revision()

    payload["llm"]["model"] = "changed-model"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    assert revision() != mode_changed


def test_claim_holds_connection_snapshot_through_prepare_and_stream_start(tmp_path, monkeypatch):
    project = _project(tmp_path)
    connection = _connection(tmp_path)
    grant = issue_grant_request(project, project_id="p1", thread_id="t1", fields=("sample_ids",), purpose="check", connection_store_dir=connection, now=NOW)
    decide_grant(project, grant.grant_id, approved=True, now=NOW)
    seen = []
    import rnaseq_agent.model_data_grants as grants_module
    monkeypatch.setattr(grants_module, "_provider_snapshot", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("claim must use locked snapshot")))

    @mark_exact_prepare
    def prepare(claim, snapshot):
        seen.append((snapshot.provider.model, snapshot.connection_revision))
        from rnaseq_agent.connection_store import save_llm
        save_llm({"model": "changed"}, store_dir=connection)
        return None, object()

    prepared = claim_grant_for_send(
        project, grant.grant_id,
        live_inputs=ExactClaimInputs("p1", "t1", connection_store_dir=connection),
        prepare=prepare,
        open_stream=mark_exact_open_stream(lambda request: iter(())),
        now=NOW,
    )
    assert seen and seen[0][0] == "test"
    assert prepared.connection_snapshot.provider.model == "test"
