from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import rnaseq_agent.model_exact_service as exact_service

from rnaseq_agent.model_data_grants import DataGrantError, decide_grant, issue_grant_request
from rnaseq_agent.model_disclosure import (
    MODEL_DATA_GRANT_INVALID,
    MODEL_DATA_SCOPE_UNSUPPORTED,
    MODEL_PROVIDER_REQUEST_FAILED,
    ProviderEvent,
)
from rnaseq_agent.model_exact_service import build_disclosure_card, send_exact_disclosure


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.json").write_text(
        json.dumps(
            {
                "project": {"id": "p1"},
                "samples": {
                    "items": [
                        {
                            "sample_id": "SENTINEL_SAMPLE_001",
                            "fastq_1": "SENTINEL_R1.fastq.gz",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return project


def test_disclosure_card_contains_metadata_only(tmp_path: Path) -> None:
    project = _project(tmp_path)
    grant = issue_grant_request(
        project,
        project_id="p1",
        thread_id="t1",
        fields=("sample_ids",),
        purpose="核对样本命名",
    )

    card = build_disclosure_card(project, grant.grant_id)

    assert card["type"] == "model_data_disclosure_confirmation"
    assert card["fields"] == ["sample_ids"]
    assert card["record_counts"] == {"sample_ids": 1}
    assert "SENTINEL_SAMPLE_001" not in json.dumps(card, ensure_ascii=False)
    assert "SENTINEL_R1.fastq.gz" not in json.dumps(card, ensure_ascii=False)
    assert "purpose" not in card


def test_disclosure_card_rejects_scopes_not_in_local_sample_id_slice(tmp_path: Path) -> None:
    project = _project(tmp_path)
    grant = issue_grant_request(
        project,
        project_id="p1",
        thread_id="t1",
        fields=("fastq_filenames",),
        purpose="核对 FASTQ 文件名",
    )

    with pytest.raises(DataGrantError) as caught:
        build_disclosure_card(project, grant.grant_id)

    assert caught.value.code == MODEL_DATA_SCOPE_UNSUPPORTED


def test_disclosure_card_acquires_connection_before_grant_lock(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    grant = issue_grant_request(
        project,
        project_id="p1",
        thread_id="t1",
        fields=("sample_ids",),
        purpose="核对样本命名",
    )
    events: list[str] = []
    real_load_grant = exact_service.load_grant
    real_locked_connection = exact_service.locked_model_disclosure_connection

    def recording_load_grant(*args, **kwargs):
        events.append("grant")
        return real_load_grant(*args, **kwargs)

    @contextmanager
    def recording_locked_connection(*args, **kwargs):
        events.append("connection")
        with real_locked_connection(*args, **kwargs) as snapshot:
            yield snapshot

    monkeypatch.setattr(exact_service, "load_grant", recording_load_grant)
    monkeypatch.setattr(exact_service, "locked_model_disclosure_connection", recording_locked_connection)

    exact_service.build_disclosure_card(project, grant.grant_id)

    assert events[:2] == ["connection", "grant"]


def test_send_exact_disclosure_keeps_exact_values_request_local(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    grant = issue_grant_request(
        project,
        project_id="p1",
        thread_id="t1",
        fields=("sample_ids",),
        purpose="核对样本命名",
    )
    decide_grant(project, grant.grant_id, approved=True)
    project_before = (project / "project.json").read_bytes()
    seen: dict[str, object] = {}

    def fake_dispatch(self, request):
        seen["payload"] = request.payload
        return iter(
            [
                ProviderEvent("delta", "SENTINEL_SAMPLE_001 的分组需要确认", 1),
                ProviderEvent("message", {"content": "SENTINEL_SAMPLE_001 的分组需要确认"}, 2),
            ]
        )

    monkeypatch.setattr(
        "rnaseq_agent.model_provider.ModelProviderGateway.dispatch_exact",
        fake_dispatch,
    )

    result = send_exact_disclosure(
        project,
        project_id="p1",
        thread_id="t1",
        grant_id=grant.grant_id,
        prompt="请核对样本命名",
    )

    assert result["ok"] is True
    assert result["text"] == "SENTINEL_SAMPLE_001 的分组需要确认"
    assert result["grant_id_hash"] != grant.grant_id
    assert grant.grant_id not in json.dumps(result, ensure_ascii=False)
    payload_text = json.dumps(seen["payload"], ensure_ascii=False)
    assert "SENTINEL_SAMPLE_001" in payload_text
    assert "tools" not in seen["payload"]
    raw_grant = (project / ".model_data_grants").glob("*.json")
    assert all("SENTINEL_SAMPLE_001" not in path.read_text(encoding="utf-8") for path in raw_grant)
    assert (project / "project.json").read_bytes() == project_before


def test_send_exact_disclosure_rejects_unapproved_grant(tmp_path: Path) -> None:
    project = _project(tmp_path)
    grant = issue_grant_request(
        project,
        project_id="p1",
        thread_id="t1",
        fields=("sample_ids",),
        purpose="核对样本命名",
    )

    result = send_exact_disclosure(
        project,
        project_id="p1",
        thread_id="t1",
        grant_id=grant.grant_id,
        prompt="请核对样本命名",
    )

    assert result["ok"] is False
    assert result["error_code"] in {"MODEL_DATA_GRANT_CONSUMED", "MODEL_DATA_GRANT_INVALID"}


def test_send_exact_disclosure_rejects_prompt_that_contains_unapproved_path_data(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    grant = issue_grant_request(
        project,
        project_id="p1",
        thread_id="t1",
        fields=("sample_ids",),
        purpose="核对样本命名",
    )
    decide_grant(project, grant.grant_id, approved=True)
    called = False

    def forbidden_dispatch(self, request):
        nonlocal called
        called = True
        return iter(())

    monkeypatch.setattr(
        "rnaseq_agent.model_provider.ModelProviderGateway.dispatch_exact",
        forbidden_dispatch,
    )
    result = send_exact_disclosure(
        project,
        project_id="p1",
        thread_id="t1",
        grant_id=grant.grant_id,
        prompt="请读取 /remote/secret/S1_R1.fastq.gz 并核对样本名",
    )

    assert result["ok"] is False
    assert result["error_code"] == "MODEL_DATA_SCOPE_UNSUPPORTED"
    assert called is False


def test_send_exact_disclosure_rejects_unc_path_in_prompt(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    grant = issue_grant_request(
        project,
        project_id="p1",
        thread_id="t1",
        fields=("sample_ids",),
        purpose="核对样本命名",
    )
    decide_grant(project, grant.grant_id, approved=True)
    called = False

    def forbidden_dispatch(self, request):
        nonlocal called
        called = True
        return iter(())

    monkeypatch.setattr(
        "rnaseq_agent.model_provider.ModelProviderGateway.dispatch_exact",
        forbidden_dispatch,
    )
    result = send_exact_disclosure(
        project,
        project_id="p1",
        thread_id="t1",
        grant_id=grant.grant_id,
        prompt=r"请读取 \\server\share\S1_R1.fastq.gz 并核对样本名",
    )

    assert result == {"ok": False, "error_code": MODEL_DATA_SCOPE_UNSUPPORTED}
    assert called is False


def test_send_exact_disclosure_rejects_empty_or_non_string_thread_id(tmp_path: Path) -> None:
    project = _project(tmp_path)
    for thread_id in ("", "   ", None, 123):
        grant = issue_grant_request(
            project,
            project_id="p1",
            thread_id="t1",
            fields=("sample_ids",),
            purpose="核对样本命名",
        )
        decide_grant(project, grant.grant_id, approved=True)
        result = send_exact_disclosure(
            project,
            project_id="p1",
            thread_id=thread_id,
            grant_id=grant.grant_id,
            prompt="请核对样本命名",
        )
        assert result == {"ok": False, "error_code": MODEL_DATA_GRANT_INVALID}


def test_send_exact_disclosure_rejects_prompt_over_one_mib(tmp_path: Path) -> None:
    project = _project(tmp_path)
    grant = issue_grant_request(
        project,
        project_id="p1",
        thread_id="t1",
        fields=("sample_ids",),
        purpose="核对样本命名",
    )
    decide_grant(project, grant.grant_id, approved=True)

    result = send_exact_disclosure(
        project,
        project_id="p1",
        thread_id="t1",
        grant_id=grant.grant_id,
        prompt="x" * (1024 * 1024 + 1),
    )

    assert result == {"ok": False, "error_code": MODEL_DATA_GRANT_INVALID}


def test_send_exact_disclosure_rejects_codex_cli_backend(tmp_path: Path) -> None:
    from rnaseq_agent.connection_store import save_llm

    project = _project(tmp_path)
    connection_dir = tmp_path / "connection"
    save_llm(
        {
            "enabled": True,
            "backend": "codex_cli",
            "provider": "codex",
            "api_mode": "codex_cli",
            "model": "gpt-test",
            "tool_mode": "disabled",
        },
        store_dir=connection_dir,
    )
    grant = issue_grant_request(
        project,
        project_id="p1",
        thread_id="t1",
        fields=("sample_ids",),
        purpose="核对样本命名",
        connection_store_dir=connection_dir,
    )
    decide_grant(project, grant.grant_id, approved=True)

    result = send_exact_disclosure(
        project,
        project_id="p1",
        thread_id="t1",
        grant_id=grant.grant_id,
        prompt="请核对样本命名",
        connection_store_dir=connection_dir,
    )

    assert result["ok"] is False
    assert result["error_code"] == MODEL_DATA_SCOPE_UNSUPPORTED


def test_send_exact_disclosure_never_accepts_remote_source_ref(tmp_path: Path) -> None:
    project = _project(tmp_path)
    with pytest.raises(DataGrantError) as caught:
        issue_grant_request(
            project,
            project_id="p1",
            thread_id="t1",
            fields=("sample_ids",),
            purpose="远程精确路径",
            source_ref="remote:opaque",
        )
    assert caught.value.code == MODEL_DATA_SCOPE_UNSUPPORTED
