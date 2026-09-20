from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping

if TYPE_CHECKING:
    from .remote_scan_store import RemoteScanReference

POLICY_VERSION = 1
GRANT_TTL_SECONDS = 600

DataField = Literal["sample_ids", "fastq_filenames", "remote_paths", "report_excerpt"]
GrantStatus = Literal[
    "pending", "approved", "rejected", "transmitting",
    "consumed_success", "consumed_ambiguous", "consumed_failed",
]
GrantOutcome = Literal["consumed_success", "consumed_ambiguous", "consumed_failed"]
ProviderBackend = Literal["openai_compatible", "codex_cli"]
ProviderAPIMode = Literal["chat_completions", "responses", "codex_cli"]

MODEL_DATA_CONFIRMATION_REQUIRED = "MODEL_DATA_CONFIRMATION_REQUIRED"
MODEL_DATA_GRANT_INVALID = "MODEL_DATA_GRANT_INVALID"
MODEL_DATA_GRANT_EXPIRED = "MODEL_DATA_GRANT_EXPIRED"
MODEL_DATA_GRANT_REJECTED = "MODEL_DATA_GRANT_REJECTED"
MODEL_DATA_GRANT_CONSUMED = "MODEL_DATA_GRANT_CONSUMED"
MODEL_PROVIDER_CHANGED = "MODEL_PROVIDER_CHANGED"
MODEL_DATA_REVISION_CHANGED = "MODEL_DATA_REVISION_CHANGED"
MODEL_CONTEXT_SECRET_DETECTED = "MODEL_CONTEXT_SECRET_DETECTED"
MODEL_DATA_SCOPE_UNSUPPORTED = "MODEL_DATA_SCOPE_UNSUPPORTED"
MODEL_PROVIDER_REQUEST_FAILED = "MODEL_PROVIDER_REQUEST_FAILED"
MODEL_EXACT_TOOL_CALL_REJECTED = "MODEL_EXACT_TOOL_CALL_REJECTED"


@dataclass(frozen=True)
class ProviderConfig:
    backend: ProviderBackend
    provider: str
    api_base: str
    model: str
    api_mode: ProviderAPIMode = "chat_completions"


@dataclass(frozen=True)
class ProviderIdentity:
    digest: str
    provider: str
    origin: str
    model: str


@dataclass(frozen=True)
class ModelDataRevisions:
    project_revision: str
    sample_revision: str
    remote_scan_revision: str | None
    report_revision: str | None
    provider_config_revision: str
    policy_version: int = POLICY_VERSION


@dataclass(frozen=True)
class GrantBindings:
    project_id: str
    thread_id: str
    provider_identity: str
    tool_mode: str
    revisions: ModelDataRevisions
    remote_scan: "RemoteScanReference | None" = None


@dataclass(frozen=True)
class DataDisclosureGrant:
    grant_id: str
    project_id: str
    thread_id: str
    provider_identity: str
    tool_mode: str
    fields: tuple[DataField, ...]
    purpose_hash: str
    issued_at: str
    expires_at: str
    policy_version: int
    revisions: ModelDataRevisions
    record_counts: Mapping[str, int]
    status: GrantStatus
    remote_scan_ref_hash: str | None = None
    decided_at: str | None = None
    claimed_at: str | None = None
    consumed_at: str | None = None
    error_code: str | None = None
    manifest: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, repr=False)
class ClaimedDataGrant:
    grant_id: str
    claim_token: str
    bindings: GrantBindings
    fields: tuple[DataField, ...]
    exact_values: Mapping[DataField, tuple[str, ...]]


@dataclass(frozen=True)
class DisclosureManifest:
    grant_id_hash: str | None
    fields: tuple[DataField, ...]
    record_counts: Mapping[str, int]
    byte_length: int
    revisions: ModelDataRevisions


@dataclass(frozen=True)
class ModelContext:
    messages: tuple[dict[str, Any], ...]
    disclosure_manifest: DisclosureManifest
    provider_identity: ProviderIdentity


@dataclass(frozen=True, repr=False)
class ProviderCredentials:
    api_key: str = ""
    known_secrets: tuple[str, ...] = ()
    protected_values: tuple[str, ...] = ()


@dataclass(frozen=True, repr=False)
class PreparedModelRequest:
    provider: ProviderConfig
    identity: ProviderIdentity
    api_mode: ProviderAPIMode
    payload: Mapping[str, Any]
    credentials: ProviderCredentials
    timeout_seconds: float
    stream: bool = False
    prompt: str = ""
    response_schema: Mapping[str, Any] | None = None
    codex_executable: Path | None = None
    codex_home: Path | None = None
    disclosure_manifest: DisclosureManifest | None = None
    exact_attempt: bool = False


@dataclass(frozen=True)
class ProviderReply:
    text: str
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class ProviderEvent:
    kind: Literal["delta", "message", "tool_call_fragment"]
    value: str | Mapping[str, Any]
    sequence: int = 0


class ProviderRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        transmission_started: bool,
        category: str | None = None,
        occurrence_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.transmission_started = transmission_started
        self.category = category
        self.occurrence_id = occurrence_id
