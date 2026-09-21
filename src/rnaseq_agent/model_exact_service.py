"""Safe local exact-data disclosure service.

This module is the first business consumer of the metadata-only disclosure
grant store.  It deliberately supports only local ``sample_ids`` grants.  The
card returned to a UI contains authorization metadata, while exact values are
assembled in a request-local model context and are never written to project
state, grant JSON, or application logs.
"""
from __future__ import annotations

import hashlib
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .model_context import ModelContextBuilder
from .model_data_grants import (
    DataGrantError,
    ExactClaimInputs,
    claim_grant_for_send,
    locked_model_disclosure_connection,
    mark_exact_open_stream,
    mark_exact_prepare,
    load_grant,
)
from .model_disclosure import (
    MODEL_DATA_GRANT_INVALID,
    MODEL_DATA_SCOPE_UNSUPPORTED,
    MODEL_PROVIDER_CHANGED,
    ProviderCredentials,
    ProviderEvent,
    ProviderRequestError,
)
from .model_provider import (
    MAX_RESPONSE_BYTES,
    ModelProviderGateway,
    context_free_prepared_request,
)


@dataclass(frozen=True)
class _ExactServiceContext:
    project_dir: Path
    project_id: str
    thread_id: str
    prompt: str
    system_prompt: str
    timeout_seconds: float


_EXACT_SERVICE_CONTEXT: ContextVar[_ExactServiceContext | None] = ContextVar(
    "model_exact_service_context", default=None
)
_UNAPPROVED_EXACT_PROMPT_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|/)[^\s,，。；;]+|\\\\[^\s\\/]+[\\/][^\s,，。；;]+|\b[^\s,，。；;]+\.(?:fastq|fq)(?:\.gz)?\b",
    re.IGNORECASE,
)


@mark_exact_prepare
def _prepare_exact_request(claim: Any, snapshot: Any) -> tuple[Any, Any]:
    parameters = _EXACT_SERVICE_CONTEXT.get()
    if parameters is None:
        raise ValueError(MODEL_DATA_GRANT_INVALID)
    if getattr(snapshot.provider, "backend", None) == "codex_cli" or getattr(snapshot.provider, "api_mode", None) == "codex_cli":
        raise DataGrantError(
            "codex cli backend is not permitted for exact disclosure",
            code=MODEL_DATA_SCOPE_UNSUPPORTED,
            grant_id=claim.grant_id,
        )
    context = ModelContextBuilder.build(
        project_dir=parameters.project_dir,
        project_id=parameters.project_id,
        thread_id=parameters.thread_id,
        provider=snapshot.provider,
        system_prompt=parameters.system_prompt,
        current_user_message=parameters.prompt,
        durable_messages=(),
        claimed_grant=claim,
    )
    credentials = snapshot.credentials or ProviderCredentials()
    request = context_free_prepared_request(
        config=snapshot.provider,
        credentials=credentials,
        context=context,
        timeout_seconds=parameters.timeout_seconds,
        stream=True,
        exact_attempt=True,
    )
    return context, request


@mark_exact_open_stream
def _open_exact_stream(request: Any):
    return ModelProviderGateway().dispatch_exact(request)


def _metadata_card(grant: Any, snapshot: Any, *, purpose: str | None = None) -> dict[str, Any]:
    """Render a disclosure card without exact values or free-text purpose."""
    identity = snapshot.provider_identity
    revisions = grant.revisions
    card: dict[str, Any] = {
        "type": "model_data_disclosure_confirmation",
        "grant_id": grant.grant_id,
        "fields": list(grant.fields),
        "record_counts": dict(grant.record_counts),
        "purpose_category": "user_requested_exact_context",
        "provider": {
            "provider": identity.provider,
            "origin": identity.origin,
            "model": identity.model,
            "identity_digest": identity.digest,
        },
        "provider_config_revision": revisions.provider_config_revision,
        "tool_mode": grant.tool_mode,
        "revisions": {
            "project_revision": revisions.project_revision,
            "sample_revision": revisions.sample_revision,
            "remote_scan_revision": revisions.remote_scan_revision,
            "report_revision": revisions.report_revision,
            "policy_version": revisions.policy_version,
        },
        "expires_at": grant.expires_at,
    }
    # ``purpose`` is intentionally accepted only for a transient caller-side
    # label.  It is not returned or stored because it may contain user data.
    del purpose
    return card


def build_disclosure_card(
    project_dir: Path,
    grant_id: str,
    *,
    connection_store_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the safe UI card for a pending/approved local disclosure grant."""
    with locked_model_disclosure_connection(store_dir=connection_store_dir) as snapshot:
        # Keep the global lock order aligned with exact send: connection
        # snapshot first, then the project/grant lock.  Reading the grant
        # first here could deadlock a concurrent card build against a send
        # that already holds the connection lock while claiming the grant.
        grant = load_grant(Path(project_dir), grant_id)
        if tuple(grant.fields) != ("sample_ids",) or grant.remote_scan_ref_hash is not None:
            raise DataGrantError(
                "unsupported model data scope",
                code=MODEL_DATA_SCOPE_UNSUPPORTED,
                grant_id=grant.grant_id,
            )
        if snapshot.provider_identity.digest != grant.provider_identity:
            raise DataGrantError(
                "model provider changed",
                code=MODEL_PROVIDER_CHANGED,
                grant_id=grant.grant_id,
            )
        return _metadata_card(grant, snapshot)


def _error_payload(exc: BaseException) -> dict[str, Any]:
    code = getattr(exc, "code", None) or MODEL_DATA_GRANT_INVALID
    payload: dict[str, Any] = {"ok": False, "error_code": str(code)}
    if isinstance(exc, ProviderRequestError):
        payload["transmission_started"] = bool(exc.transmission_started)
    return payload


def send_exact_disclosure(
    project_dir: Path,
    *,
    project_id: str,
    thread_id: str,
    grant_id: str,
    prompt: str,
    system_prompt: str = "你是一个只处理当前用户请求的生物信息学助手。",
    connection_store_dir: Path | None = None,
    timeout_seconds: float = 60.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Send one approved local exact request and return only request-local text.

    The caller must deliberately keep the returned text transient.  This
    function never appends it to ChatState, History, a transcript, or a log.
    """
    if not isinstance(thread_id, str) or not thread_id.strip():
        return {"ok": False, "error_code": MODEL_DATA_GRANT_INVALID}
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return {"ok": False, "error_code": MODEL_DATA_GRANT_INVALID}
    if _UNAPPROVED_EXACT_PROMPT_RE.search(prompt):
        return {"ok": False, "error_code": "MODEL_DATA_SCOPE_UNSUPPORTED"}

    project_dir = Path(project_dir)

    context_token = _EXACT_SERVICE_CONTEXT.set(
        _ExactServiceContext(
            project_dir=project_dir,
            project_id=project_id,
            thread_id=thread_id,
            prompt=prompt,
            system_prompt=system_prompt,
            timeout_seconds=timeout_seconds,
        )
    )
    try:
        prepared = claim_grant_for_send(
            project_dir,
            grant_id,
            live_inputs=ExactClaimInputs(
                project_id=project_id,
                thread_id=thread_id,
                connection_store_dir=connection_store_dir,
            ),
            prepare=_prepare_exact_request,
            open_stream=_open_exact_stream,
            now=now,
        )
        chunks: list[str] = []
        with prepared.terminal_guard():
            for event in prepared.events:
                if not isinstance(event, ProviderEvent):
                    raise ProviderRequestError(
                        "provider response malformed",
                        code="MODEL_PROVIDER_REQUEST_FAILED",
                        transmission_started=True,
                    )
                if event.kind == "delta":
                    if not isinstance(event.value, str):
                        raise ProviderRequestError(
                            "provider response malformed",
                            code="MODEL_PROVIDER_REQUEST_FAILED",
                            transmission_started=True,
                        )
                    chunks.append(event.value)
                elif event.kind == "message":
                    continue
                elif event.kind == "tool_call_fragment":
                    raise ProviderRequestError(
                        "provider response contained a tool call",
                        code="MODEL_EXACT_TOOL_CALL_REJECTED",
                        transmission_started=True,
                    )
                else:
                    raise ProviderRequestError(
                        "provider response malformed",
                        code="MODEL_PROVIDER_REQUEST_FAILED",
                        transmission_started=True,
                    )
        text = "".join(chunks)
        return {
            "ok": True,
            "text": text,
            "grant_id_hash": hashlib.sha256(prepared.claim.grant_id.encode("utf-8")).hexdigest(),
            "fields": list(prepared.claim.fields),
        }
    except (DataGrantError, ProviderRequestError, ValueError) as exc:
        return _error_payload(exc)
    finally:
        _EXACT_SERVICE_CONTEXT.reset(context_token)
