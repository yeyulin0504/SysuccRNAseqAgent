# LLM Data Disclosure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make de-identified project summaries the default for every model provider call, and disclose exact sample identifiers, FASTQ basenames, all canonical directories from one bounded approved remote scan, or bounded report excerpts only after a durable, scoped, single-use user approval.

**Architecture:** Introduce a shared disclosure contract, an allowlist-only `ModelContextBuilder`, a metadata-only grant store with a cross-process one-time claim, and one provider gateway used by every generative transport. The composite claim reads browse policy, provider configuration, credentials, and live tool mode from one connection document while holding its lock, then binds extraction and request preparation to that snapshot. Keep grant-injected values and raw tool arguments on bounded in-memory/local channels; persist model-safe projections, manifests, fixed exact-turn acknowledgements, and terminal grant outcomes. Treat tool authorization, model data authorization, and SSH approved-root authorization as independent checks.

**Tech Stack:** Python 3.11+, dataclasses and `typing.Literal`, SHA-256 canonical JSON revisions, existing `project_state_lock`, LangGraph with SQLite checkpoints, FastAPI/SSE, vanilla JavaScript, `requests`, `urllib`, Codex CLI subprocesses, pytest.

## Global Constraints

- Work only on branch `wizard`; do not merge `master`.
- Complete `docs/superpowers/plans/2026-09-17-approved-remote-data-roots.md` before Task 2 of this plan. This plan consumes its five-argument `ToolExecutionContext`, four-channel `ToolExecutionResult`, request-local exact-result sink, and server-owned whole-scan `RemoteScanReference` contracts and must not replace or narrow them.
- Use TDD for Tasks 1-8: add the stated RED tests, run them and record the expected reason, add the minimal implementation, then run the stated GREEN command. Task 9 is the post-implementation acceptance gate; it must pass immediately after a correct Task 8, and its static detector proves itself with synthetic forbidden-call fixtures rather than claiming a product RED state.
- Default model data scope is `summary` for local and remote providers; endpoint locality never silently widens access.
- `tool_mode` and model data grants are independent. `approved_execute` does not imply exact-data access, and an exact-data grant does not authorize a write or execution.
- SSH approved roots are a third independent boundary. A disclosure grant cannot authorize remote browsing outside an approved root.
- Policy version is integer `1`; grants expire after 600 seconds and are bound to project, thread, provider identity, tool mode, project/data revisions, and one provider turn.
- Supported exact fields in this phase are `sample_ids`, `fastq_filenames`, `remote_paths`, and `report_excerpt`; remote-backed values additionally require a live approved-roots `source_ref` with a matching result/policy revision.
- Before the approved-roots dependency exists, `remote_paths` and exact filenames from a live remote browse fail with `MODEL_DATA_SCOPE_UNSUPPORTED` and send no provider request. Task 4 reuses its server-owned `source_ref`: one reference names the entire bounded scan and every group in it, with project, exact non-null thread, scan/result/policy revisions, SSH identity/root, and expiry revalidated at send time. A scan created with `thread_id=None` remains usable by the local workbench but can never authorize an LLM thread.
- Application/grant-injected exact values are not persisted: they do not enter `ChatState`, LangGraph checkpoint output, `<project>/threads/<thread>.json`, generic `tool_log`, `.model_data_grants/*.json`, exceptions, or application logs. User-authored messages remain unchanged and may contain the same sentinel text in local durable history; persistence assertions distinguish those occurrences from application-added values.
- User-authored messages remain unchanged. Application-generated assistant/tool history must be projected before provider replay.
- A browser may display a current exact local result. The model receives only the corresponding projection unless a matching grant is claimed.
- After an approved grant changes to `transmitting`, one terminal guard covers context build, request preparation and serialization, secret scanning, transport creation, stream iteration, `GeneratorExit`, client cancellation/disconnect, and unknown exceptions. Pre-transport exits become `consumed_failed`; any exit after transport starts becomes `consumed_ambiguous`; success becomes `consumed_success`. No path may leave a grant `transmitting`, and `exact_attempt=True` suppresses every fallback.
- Every generative HTTP, Responses API, and Codex CLI request must pass through `ModelProviderGateway`; authenticated non-generative `/models` GETs reuse normalized provider configuration but require no `ModelContext`.
- The last-mile scanner examines the fully serialized outbound body or Codex prompt immediately before transport. It blocks known plaintext credentials, protected credential ciphertext, private-key markers, URL userinfo, and known application tokens with `MODEL_CONTEXT_SECRET_DETECTED`.
- Secret failures record only a category and non-reversible occurrence id. Never include the match, surrounding payload, request body, Authorization header, provider response body prefix, password, key, or token in an error.
- All field extraction and model projections use explicit allowlists. Recursive key-name redaction is not an authorization boundary.
- Exact-data requests omit/disable `tools` and `tool_choice`. A provider response that nevertheless contains `tool_calls` is a protocol failure and is rejected before its content or arguments reach `ChatState`, a checkpoint, transcript, card, or tool executor.
- The composite exact-claim path always acquires locks in this order: connection-policy lock, remote-scan lock, then project/grant lock. It revalidates policy, SSH identity, source/result/policy revisions, project/thread/provider/tool mode/data revisions, grant CAS, extraction, and request preparation in one critical section; a root revocation that wins the policy-lock race fails before transport.
- Assistant tool-call arguments and pending/deferred execution payloads never enter `ChatState` or SQLite verbatim. State and provider replay use a versioned per-tool allowlist projection plus an opaque in-memory payload reference; a missing/expired reference fails closed and requires the model/user to issue a fresh call. `EphemeralToolCallStore` is bounded by `MAX_ENTRIES = 128`, `MAX_ENTRY_BYTES = 64 * 1024`, and `MAX_TOTAL_BYTES = 2 * 1024 * 1024`, where bytes are measured from canonical compact UTF-8 JSON before storage; it also rejects nesting deeper than 16, collections larger than 256 items, or scalar strings larger than 16 KiB. Reservations and accounting are atomic under the store lock, and `get`/`replace` re-parse only bounded serialized bytes.
- Every exact send uses one `ModelProviderGateway.dispatch_exact` dispatcher. It selects the mode-specific chat-completions SSE, Responses SSE, or Codex CLI parser, maps all three into the same bounded `ProviderEvent` stream, and immediately feeds that stream to the shared validated-response collector. The common limits are at most 10,000 events, 1 MiB total UTF-8 response bytes, 64 KiB per event/frame, 32 tool-call ids, and 256 KiB aggregate tool-call argument fragments. Any complete or fragmented tool call is a protocol rejection; no mode may expose a delta before the collector has accepted one terminal non-tool message.
- Reuse approved-roots `_finalize_tool_execution_result` as the sole `security_audit` persistence owner. Disclosure code calls it exactly once before reading any result channel and never calls the authoritative audit or History writers directly.
- Preserve current structured-workbench state gates, tool confirmation behavior, rule fallback, and no-LLM behavior.
- Use unique sentinels in tests: `PATIENT_SENTINEL_73`, `TUMOR_SENTINEL_R1.fastq.gz`, `/restricted/SENTINEL_73/fastq`, plus unique host, job, report, API-key, password, ciphertext, token, and private-key values.
- Do not alter historic local files in place. Filter legacy checkpoint and transcript content before provider replay and prevent new unsafe persistence.

---

### Task 1: Shared disclosure contracts, provider identity, and deterministic revisions

**Files:**
- Create: `src/rnaseq_agent/model_disclosure.py`
- Create: `src/rnaseq_agent/model_provider.py`
- Create: `src/rnaseq_agent/model_context.py`
- Create: `tests/test_model_context.py`
- Modify: `tests/test_connection_store.py`

**Interfaces:**
- Consumes `analysis_contract.canonical_sha256(value: Any) -> str` when available; file hashing must still use raw bytes.
- Consumes `storage.load_json(path: Path) -> dict[str, Any]` and existing project file shapes from `project.json`, `session.json`, `intake.json`, contract JSON, and `report.md`.
- Produces `DataField = Literal["sample_ids", "fastq_filenames", "remote_paths", "report_excerpt"]`.
- Produces `GrantStatus = Literal["pending", "approved", "rejected", "transmitting", "consumed_success", "consumed_ambiguous", "consumed_failed"]`.
- Produces the constants `MODEL_DATA_CONFIRMATION_REQUIRED`, `MODEL_DATA_GRANT_INVALID`, `MODEL_DATA_GRANT_EXPIRED`, `MODEL_DATA_GRANT_REJECTED`, `MODEL_DATA_GRANT_CONSUMED`, `MODEL_PROVIDER_CHANGED`, `MODEL_DATA_REVISION_CHANGED`, `MODEL_CONTEXT_SECRET_DETECTED`, `MODEL_DATA_SCOPE_UNSUPPORTED`, `MODEL_PROVIDER_REQUEST_FAILED`, and `MODEL_EXACT_TOOL_CALL_REJECTED`.
- Produces `ProviderConfig(backend: Literal["openai_compatible", "codex_cli"], provider: str, api_base: str, model: str, api_mode: Literal["chat_completions", "responses", "codex_cli"] = "chat_completions")`.
- Produces `ProviderIdentity(digest: str, provider: str, origin: str, model: str)`.
- Produces `ModelDataRevisions(project_revision: str, sample_revision: str, remote_scan_revision: str | None, report_revision: str | None, provider_config_revision: str, policy_version: int)`.
- Consumes and reuses `remote_scan_store.RemoteScanReference(scan_id, source_ref, result_revision, project_id, thread_id, source, expires_at)` from the approved-roots plan; this plan does not define a second scan-reference type.
- Produces `GrantBindings(project_id: str, thread_id: str, provider_identity: str, tool_mode: str, revisions: ModelDataRevisions, remote_scan: RemoteScanReference | None = None)`.
- Produces `DataDisclosureGrant`, `ClaimedDataGrant`, `DisclosureManifest`, `ModelContext`, `ProviderCredentials`, `PreparedModelRequest`, `ProviderReply`, `ProviderEvent`, and `ProviderRequestError` with the exact fields shown below. It reuses `chat_graph.ToolExecutionResult` from approved-roots rather than defining a competing type.
- Produces `normalize_provider_config(raw: Mapping[str, Any]) -> ProviderConfig`.
- Produces `provider_identity(config: ProviderConfig) -> ProviderIdentity`.
- Produces `provider_config_revision(config: ProviderConfig) -> str` over the full normalized API base, backend, model, and API mode.
- Produces `canonical_json_sha256(value: Any) -> str`.
- Produces `read_model_data_revisions(project_dir: Path, provider: ProviderConfig, remote_scan_revision: str | None = None) -> ModelDataRevisions`.

- [ ] **Step 1: Write the shared-type and identity RED tests**

Create `tests/test_model_context.py` with fixtures that write a minimal `project.json`, `session.json`, `intake.json`, and `report.md`. Add these exact identity and revision cases:

```python
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from rnaseq_agent.model_context import read_model_data_revisions
from rnaseq_agent.model_disclosure import POLICY_VERSION
from rnaseq_agent.model_provider import (
    normalize_provider_config,
    provider_config_revision,
    provider_identity,
)


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _seed_revision_project(project_dir: Path) -> None:
    project_dir.mkdir()
    _write_json(project_dir / "project.json", {
        "project": {"id": "project-73"},
        "route": {"id": "bulk_rna"},
        "sequencing": {"layout": "paired", "strandedness": "unknown"},
        "samples": {"source": "local_upload", "items": [{
            "sample_id": "PATIENT_SENTINEL_73",
            "condition": "tumor",
            "fastq_1": "TUMOR_SENTINEL_R1.fastq.gz",
            "fastq_2": "TUMOR_SENTINEL_R2.fastq.gz",
        }]},
        "pipeline": {"fastp": {"enabled": True}},
        "status": {"state": "input_ready", "job_id": "JOB_SENTINEL_73"},
    })
    _write_json(project_dir / "session.json", {"state": "input_ready"})
    _write_json(project_dir / "intake.json", {"route": "bulk_rna", "input_kind": "fastq"})
    (project_dir / "report.md").write_text("REPORT_SENTINEL_73", encoding="utf-8")


def test_provider_identity_is_normalized_and_secret_independent() -> None:
    first = normalize_provider_config({
        "provider": " OpenAI ",
        "api_base": "HTTPS://LLM.Example:443/v1/",
        "model": "gpt-test",
        "api_key": "API_KEY_SENTINEL_A",
    })
    second = normalize_provider_config({
        "provider": "openai",
        "api_base": "https://llm.example/v1",
        "model": "gpt-test",
        "api_key": "API_KEY_SENTINEL_B",
    })
    assert first == second
    assert provider_identity(first) == provider_identity(second)
    assert provider_identity(first).origin == "https://llm.example"
    assert "API_KEY_SENTINEL" not in repr(provider_identity(first))
    assert provider_identity(replace(first, model="gpt-other")).digest != provider_identity(first).digest


def test_origin_identity_ignores_path_but_config_revision_does_not() -> None:
    v1 = normalize_provider_config({
        "provider": "openai", "api_base": "https://llm.example/v1", "model": "gpt-test",
    })
    tenant = normalize_provider_config({
        "provider": "openai", "api_base": "https://llm.example/tenant-a/v1", "model": "gpt-test",
    })
    assert provider_identity(v1) == provider_identity(tenant)
    assert provider_config_revision(v1) != provider_config_revision(tenant)


def test_idna_equivalent_hosts_normalize_identically() -> None:
    unicode_host = normalize_provider_config({
        "provider": "openai", "api_base": "https://b\u00fccher.example/v1", "model": "gpt-test",
    })
    ascii_host = normalize_provider_config({
        "provider": "openai", "api_base": "https://xn--bcher-kva.example/v1", "model": "gpt-test",
    })
    assert unicode_host == ascii_host
    assert provider_identity(unicode_host) == provider_identity(ascii_host)
    assert provider_config_revision(unicode_host) == provider_config_revision(ascii_host)


def test_revisions_separate_sample_report_and_provider_changes(tmp_path: Path) -> None:
    project_dir = tmp_path / "project-73"
    _seed_revision_project(project_dir)
    provider = normalize_provider_config({
        "provider": "openai", "api_base": "https://llm.example/v1", "model": "gpt-test"
    })
    before = read_model_data_revisions(project_dir, provider)
    assert before.policy_version == POLICY_VERSION == 1
    assert before.remote_scan_revision is None

    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    project["server"] = {"threads": 32}
    _write_json(project_dir / "project.json", project)
    resources_changed = read_model_data_revisions(project_dir, provider)
    assert resources_changed.project_revision != before.project_revision
    assert resources_changed.sample_revision == before.sample_revision

    project["samples"]["items"][0]["sample_id"] = "PATIENT_SENTINEL_74"
    _write_json(project_dir / "project.json", project)
    sample_changed = read_model_data_revisions(project_dir, provider)
    assert sample_changed.sample_revision != before.sample_revision

    (project_dir / "report.md").write_text("REPORT_SENTINEL_74", encoding="utf-8")
    report_changed = read_model_data_revisions(project_dir, provider)
    assert report_changed.report_revision != sample_changed.report_revision

    other_provider = replace(provider, model="gpt-other")
    assert read_model_data_revisions(project_dir, other_provider).provider_config_revision != report_changed.provider_config_revision
```

Extend `tests/test_connection_store.py` with a test that changes only the saved API key and proves normalized provider identity/config revision do not change. A model or origin change changes both; a normalized API path change leaves origin-level `provider_identity` equal but changes `provider_config_revision`. Do not assert or print decrypted values.

- [ ] **Step 2: Run the Task 1 RED tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_model_context.py tests/test_connection_store.py -k "provider_identity or revisions"
```

Expected: collection fails because `model_disclosure`, `model_provider`, and `model_context` do not exist.

- [ ] **Step 3: Define every shared type in one dependency-free module**

Create `model_disclosure.py` with no imports from graph, web, session, or provider modules. It may use a `TYPE_CHECKING` import of the approved-roots `remote_browse.RemoteScanReference` solely for `GrantBindings`; runtime code receives that existing object. Use these exact public definitions so later tasks do not invent parallel shapes:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Literal, Mapping

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

The common event contract is strict by `kind`: a `delta` carries one UTF-8 text string; a `message` carries one canonical assistant terminal mapping with `role="assistant"`, string `content`, and `tool_calls=[]`; a `tool_call_fragment` carries only `call_id`, `index`, `name` (when first seen), and one arguments-fragment string. Every event is assigned a monotonic `sequence` by the dispatcher. The dispatcher rejects any other shape, non-string content, duplicate terminal message, unknown finish state, or event/frame exceeding the shared limits before the collector can expose it.


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
```

- [ ] **Step 4: Implement normalized provider identity and deterministic revisions**

In `model_provider.py`, normalize `provider` and `model` with `strip()`, case-fold only the provider name, and normalize the full HTTP API base with `urllib.parse.urlsplit`: lowercase scheme, convert the hostname to its ASCII IDNA form, omit port 80 for HTTP and 443 for HTTPS, reject userinfo/query/fragment, normalize an empty path to `/`, collapse a trailing slash except at `/`, and preserve the remaining path. Derive the origin by dropping that normalized path. A Codex CLI config uses `api_base="codex-cli"`, `origin="codex-cli"`, `backend="codex_cli"`, and `api_mode="codex_cli"`.

`provider_identity` hashes canonical UTF-8 JSON containing only normalized provider, origin, and model, so two paths on the same provider origin have the same disclosure identity. `provider_config_revision` separately hashes backend, normalized provider, the full normalized `api_base` including path, model, and API mode. Prefix both hex digests with `sha256:`. API keys and headers must never be accepted into either hash input.

In `model_context.py`, add:

```python
def canonical_json_sha256(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def read_model_data_revisions(
    project_dir: Path,
    provider: ProviderConfig,
    remote_scan_revision: str | None = None,
) -> ModelDataRevisions:
    project = _load_mapping(project_dir / "project.json")
    session = _load_mapping(project_dir / "session.json")
    intake = _load_mapping(project_dir / "intake.json")
    contract_path = _contract_path_inside_project(project_dir, project)
    project_view = {
        "project": project,
        "session": session,
        "intake": intake,
        "contract_sha256": _file_sha256(contract_path),
    }
    sample_view = {
        "route": project.get("route"),
        "sequencing": project.get("sequencing"),
        "samples": project.get("samples"),
        "input": intake.get("input"),
        "design": intake.get("design"),
    }
    report_path = project_dir / "report.md"
    return ModelDataRevisions(
        project_revision=canonical_json_sha256(project_view),
        sample_revision=canonical_json_sha256(sample_view),
        remote_scan_revision=remote_scan_revision,
        report_revision=_file_sha256(report_path),
        provider_config_revision=provider_config_revision(provider),
    )
```

`_load_mapping` returns `{}` for a missing file and raises a bounded `ValueError` for malformed/non-object JSON without including file contents. `_contract_path_inside_project` accepts the configured contract only after `Path.resolve()` containment under `project_dir`; an outside path contributes the stable marker `"outside-project"` rather than being read. `_file_sha256` returns `None` for a missing file and `sha256:<hex>` for bytes.

- [ ] **Step 5: Run Task 1 GREEN tests and commit exact files**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_model_context.py tests/test_connection_store.py
.\.venv\Scripts\python.exe -m compileall -q src/rnaseq_agent/model_disclosure.py src/rnaseq_agent/model_provider.py src/rnaseq_agent/model_context.py
git diff --check -- src/rnaseq_agent/model_disclosure.py src/rnaseq_agent/model_provider.py src/rnaseq_agent/model_context.py tests/test_model_context.py tests/test_connection_store.py
```

Expected: all selected tests pass, compileall exits 0, and diff check is clean.

```powershell
git add -- src/rnaseq_agent/model_disclosure.py src/rnaseq_agent/model_provider.py src/rnaseq_agent/model_context.py tests/test_model_context.py tests/test_connection_store.py
git commit -m "feat: define model disclosure identity and revisions"
```

---

### Task 2: Default project summary and approved-roots four-channel tool results

**Files:**
- Modify: `src/rnaseq_agent/model_context.py`
- Modify: `src/rnaseq_agent/agent_tools.py`
- Modify: `src/rnaseq_agent/chat_graph.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `tests/test_model_context.py`
- Modify: `tests/test_chat_graph.py`
- Modify: `tests/test_webapp_chat_tools.py`

**Interfaces:**
- Consumes `ModelDataRevisions` and `canonical_json_sha256` from Task 1.
- Consumes the approved-roots executor contract `_run_tool(name: str, arguments: dict[str, Any], project_dir: Path, approved: bool, context: ToolExecutionContext) -> ToolExecutionResult`, where `ToolExecutionContext(project_id: str, thread_id: str | None)` and `ToolExecutionResult(local, model, log_projection, security_audit)` come from `chat_graph.py`.
- Consumes the web-supplied approved-roots `_finalize_tool_execution_result` callback as the sole owner of authoritative audit and History persistence. The graph invokes it once and only once before reading `local`, `model`, or `log_projection`; the returned wrapper must have `security_audit is None`.
- Reuses the approved-roots request-local exact-result sink. This task must not introduce a second callback, global collector, or competing `ToolExecutionResult`.
- Produces `build_safe_project_summary(project_dir: Path) -> dict[str, Any]`.
- Produces `project_tool_result_for_model(name: str, full_result: Mapping[str, Any]) -> dict[str, Any]`.
- Produces `project_tool_result_for_log(name: str, full_result: Mapping[str, Any]) -> dict[str, Any]`.
- Produces `project_tool_arguments_for_model(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]`, an explicit switch over every registered tool that returns only field-presence flags, bounded numeric resource values, enum stages, item counts, and a canonical argument hash; it never returns ids, paths, filenames, connection values, report text, free text, or unknown keys.
- Produces `project_assistant_tool_call(call_id: str, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]` with `tool_projection_version=1` and canonical JSON of the safe argument projection.
- Produces bounded app-local `EphemeralToolCallStore.put/get/replace/delete/append_fragment`, keyed by a random opaque `call_ref`, bound to project/thread/call id/name/argument hash, capped at `MAX_ENTRIES = 128`, `MAX_ENTRY_BYTES = 64 * 1024`, `MAX_TOTAL_BYTES = 2 * 1024 * 1024`, `MAX_ARGUMENT_FRAGMENT_BYTES = 16 * 1024`, and `MAX_ARGUMENT_FRAGMENTS = 128`, plus the confirmation TTL `GRANT_TTL_SECONDS = 600` defined by Task 1. `put` and `replace` canonicalize the raw mapping with `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`, measure the resulting UTF-8 bytes before reserving space, and atomically reject an entry or aggregate reservation that exceeds its limit. The store keeps the serialized bytes plus non-sensitive binding metadata rather than an unbounded object graph. `append_fragment` accepts one UTF-8 JSON argument fragment at a time, reserves every partial byte against both the entry and aggregate budgets, rejects a fragment over `MAX_ARGUMENT_FRAGMENT_BYTES`, rejects more than `MAX_ARGUMENT_FRAGMENTS` or an aggregate over `MAX_ENTRY_BYTES`, and parses only after the caller marks the final fragment; parsing uses the same depth/collection/string limits and requires one complete JSON value with no trailing bytes. Partial fragments are never returned by `get` and are never executable. `get` parses only a byte buffer that is at most `MAX_ENTRY_BYTES`, rejects malformed JSON, nesting deeper than 16, more than 256 items in any object/array, or a scalar string over 16 KiB, and returns a transient mapping; `replace` applies the same parser and accounting to the new bytes before releasing the old reservation. `delete`, expiry, and eviction release exactly the reserved bytes. All methods are lock-safe across concurrent puts/replaces/deletes/appends, use `repr=False`, never enter logs/state/checkpoints, and fail closed after restart, eviction, expiry, malformed/tampered bytes, or binding mismatch.
- Preserves the five arguments and changes only the return type to `Callable[[str, dict[str, Any], Path, bool, ToolExecutionContext], ToolExecutionResult]`.
- Changes the web executor wrapper to return the existing four-field result. `local` is request-local, `model` is the provider projection, `log_projection` is the only generic `tool_log` payload, and `security_audit` is sent exactly once to the independent authoritative security-audit sink and then discarded.

- [ ] **Step 1: Write RED tests for the summary allowlist and all-tool projection**

Extend `tests/test_model_context.py`:

```python
from rnaseq_agent.model_context import (
    build_safe_project_summary,
    project_tool_result_for_log,
    project_tool_result_for_model,
)


FORBIDDEN_SENTINELS = (
    "PATIENT_SENTINEL_73",
    "TUMOR_SENTINEL_R1.fastq.gz",
    "/restricted/SENTINEL_73/fastq",
    "HOST_SENTINEL_73",
    "USER_SENTINEL_73",
    "JOB_SENTINEL_73",
    "API_KEY_SENTINEL_73",
)


def _serialized(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_default_summary_keeps_scientific_counts_but_no_exact_values(tmp_path: Path) -> None:
    project_dir = tmp_path / "project-73"
    _seed_revision_project(project_dir)
    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    project["server"] = {
        "host": "HOST_SENTINEL_73", "user": "USER_SENTINEL_73",
        "remote_workdir": "/restricted/SENTINEL_73/fastq",
    }
    project["status"]["job_id"] = "JOB_SENTINEL_73"
    _write_json(project_dir / "project.json", project)

    summary = build_safe_project_summary(project_dir)
    encoded = _serialized(summary)
    assert summary["sample_count"] == 1
    assert summary["condition_counts"] == {"tumor": 1}
    assert summary["sample_aliases"] == ["sample_001"]
    assert summary["sequencing"] == {"layout": "paired", "strandedness": "unknown"}
    assert all(value not in encoded for value in FORBIDDEN_SENTINELS)


def test_every_tool_result_uses_an_explicit_safe_projection() -> None:
    full = {
        "ok": True,
        "reply": "PATIENT_SENTINEL_73 at /restricted/SENTINEL_73/fastq",
        "samples": [{"sample_id": "PATIENT_SENTINEL_73", "fastq_1": "TUMOR_SENTINEL_R1.fastq.gz"}],
        "scanned_path": "/restricted/SENTINEL_73/fastq",
        "report": "REPORT_SENTINEL_73",
        "report_path": "/restricted/report.md",
        "job_id": "JOB_SENTINEL_73",
        "source_ref": "src_0123456789abcdef0123456789abcdef",
        "state": "running",
    }
    for name in (
        "read_project_state", "browse_remote_samples", "refresh_project_status",
        "get_project_report", "write_project_config", "edit_samples",
        "edit_reference", "edit_connection", "set_run_resources",
        "generate_plan", "confirm_contract", "run_analysis", "unknown_tool",
    ):
        projected = project_tool_result_for_model(name, full)
        logged = project_tool_result_for_log(name, full)
        assert all(value not in _serialized(projected) for value in FORBIDDEN_SENTINELS)
        assert "REPORT_SENTINEL_73" not in _serialized(projected)
        assert all(value not in _serialized(logged) for value in FORBIDDEN_SENTINELS)
        assert "REPORT_SENTINEL_73" not in _serialized(logged)

    browse = project_tool_result_for_model("browse_remote_samples", full)
    assert browse == {
        "ok": True,
        "sample_count": 1,
        "paired_count": 0,
        "unmatched_count": 0,
        "directory_count": 1,
        "truncated": False,
        "authorization": "inside_approved_root",
        "source_ref": "src_0123456789abcdef0123456789abcdef",
    }
```

Extend the graph and endpoint tests with one scripted read tool turn followed by a final answer. Assert `FakeLLM.seen_messages` contains `sample_001` and counts but none of the exact sentinels; inspect the final graph snapshot and assert `tool_log` contains only name/risk/status/counts/hashes. Keep a separate assertion that the executor's `result.local` retains exact rows for a deterministic local caller.

Add a parameterized argument-projection test over every name returned by `tool_schemas("approved_execute")`, including browse, sample edits, references, connection edits, report/status operations, planning, confirmation, and execution. Feed each projector ids, paths, filenames, host/user/password-like values, report text, and unknown nested keys. Assert the provider projection and state shape contain only the fixed safe schema. Unknown tool names project to `{"argument_hash": ..., "unmapped": True}` and cannot execute.

In graph tests, inspect the assistant `tool_calls` message, `pending_calls`, `deferred_calls`, confirmation metadata, final SQLite checkpoint, generic log, and next provider request for every registered tool. Each durable call stores only `call_id`, `name`, `call_ref`, `arguments_hash`, `argument_projection`, and bounded parse/error categories. Raw arguments exist only in the injected `EphemeralToolCallStore`; confirmation-card local detail is emitted through the existing request-local channel and never checkpointed. On missing/expired/tampered `call_ref`, append one projected tool error that preserves the assistant/tool protocol id pairing, delete the remaining refs, execute nothing, and ask for a fresh call.

Add store-specific RED tests before the graph tests are implemented. A single argument mapping whose canonical UTF-8 JSON is exactly `MAX_ENTRY_BYTES` is accepted and one byte over is rejected with a stable bounded-argument error; two individually valid entries are rejected when their sum would exceed `MAX_TOTAL_BYTES`; replacing an entry reserves the new size before releasing the old size and cannot transiently exceed the aggregate cap; deleting/expiring entries makes the released bytes available again; the 129th live entry is rejected even when byte budget remains. Feed malformed JSON, a 17-level nested mapping, a 257-item array, and a string whose UTF-8 length is `16 * 1024 + 1` through the parser and assert fail-closed errors without raw input in the exception. Feed a valid argument JSON value split across exactly `MAX_ARGUMENT_FRAGMENTS` fragments and assert it assembles only after the final marker; a single fragment of `MAX_ARGUMENT_FRAGMENT_BYTES + 1`, a 129th fragment, an aggregate fragment stream over `MAX_ENTRY_BYTES`, trailing bytes after the final JSON value, and a partial stream passed to `get` must all fail closed without execution. Run concurrent put/replace/delete/append operations and assert the live-entry and byte counters never become negative or exceed either cap. Assert that a restart, tampered serialized buffer, wrong project/thread/call binding, and TTL expiry never return raw arguments. These tests must inspect the serialized byte counters directly through a test-only read-only metric, while no raw serialized bytes or parsed values are exposed through production APIs.

- [ ] **Step 2: Run the Task 2 RED tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_model_context.py tests/test_chat_graph.py tests/test_webapp_chat_tools.py -k "default_summary or safe_projection or local_result"
```

Expected: summary/result/argument projector and ephemeral store names are missing, and the existing graph persists/sends exact `samples`, `scanned_path`, `report`, raw assistant tool arguments, and raw pending/deferred arguments.

- [ ] **Step 3: Implement the explicit summary and per-tool allowlists**

In `model_context.py`, build aliases from sample row order, never from a reversible transform of the source id. Return this exact top-level summary schema, omitting only unavailable optional values:

```python
{
    "policy_version": 1,
    "data_scope": "summary",
    "project_state": str,
    "route_id": str,
    "capability_id": str,
    "input_kind": str,
    "sequencing": {"layout": str, "strandedness": str},
    "sample_count": int,
    "condition_counts": dict[str, int],
    "sample_aliases": list[str],
    "replicate_gate": {"passed": bool, "minimum_per_condition": int},
    "pipeline_stages": list[dict[str, str | bool]],
    "matrix": {"classification": str, "deseq2_eligible": bool},
    "references": {"gtf_present": bool, "genome_present": bool, "star_index_present": bool},
    "contract": {"present": bool, "id_hash": str},
    "run": {"present": bool, "state": str, "id_hash": str},
    "errors": list[dict[str, str]],
}
```

Use condition labels and integer counts only. For contract/run ids use the first 12 hex characters of a SHA-256 digest, never a source prefix. Error entries permit only `category` and a fixed message selected from a source-code mapping; do not copy status messages or exceptions.

`project_tool_result_for_model` must switch on every registered tool name and return only fixed schema keys. `read_project_state` returns the safe summary. `browse_remote_samples` returns counts/booleans/authorization plus the approved-roots opaque `source_ref`; it returns no group ids, paths, filenames, or sample ids. `refresh_project_status` returns state/stage/known error category/artifact presence only. `get_project_report` returns generated/present/validated/report hash only. Write/execute tools return `ok`, resulting state/gate, count fields, opaque ids/hashes, and fixed messages; they never return config patches, sample rows, paths, commands, stdout/stderr, or raw `reply`. Unknown names return `{"ok": False, "error_code": "MODEL_TOOL_RESULT_UNMAPPED"}`.

`project_tool_arguments_for_model` is a separate allowlist from result projection. Its exact per-tool outputs are: empty/hash-only for no-argument reads; `path_present` for browse; operation/count flags for sample edits; reference-kind presence booleans for reference edits; connection-field presence booleans with no values; bounded `threads`/`memory_gb` numbers for resource edits; allowlisted stage enum for execution; and key/count/boolean summaries for project config, plan, contract, status, report, and rollback tools. It never copies a string supplied as an id, path, host, user, filename, free-text purpose, report value, credential, or arbitrary config value. Tests enumerate the registry so adding a tool without adding a projector fails.

`project_assistant_tool_call` returns this exact versioned state shape; provider request construction validates the marker/hash and rebuilds the standard four inner fields, omitting the two local metadata keys:

```python
projection = project_tool_arguments_for_model(name, arguments)
return {
    "id": call_id,
    "type": "function",
    "function": {
        "name": name,
        "arguments": json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    },
    "tool_projection_version": 1,
    "arguments_hash": canonical_json_sha256(arguments),
}
```

The projector rejects a blank/unknown `call_id` or unregistered `name`. Pending/deferred records store the same `arguments_hash` and decoded `argument_projection` plus `call_ref`; any disagreement causes the whole assistant/tool protocol group to fail closed.

Implement `EphemeralToolCallStore` with a private lock, a `dict[call_ref, Entry]`, and an integer `total_serialized_bytes`. The only serialized representation is canonical UTF-8 JSON; never use `sys.getsizeof`, pickle length, or an in-memory recursive object size as the quota. Validate depth, collection count, and scalar-string bytes while parsing, and reject non-finite numbers or duplicate object keys. `put`/`replace` perform validation and quota reservation while holding the same lock used by `get`/`delete`; `replace` uses a two-phase local candidate calculation so a failed replacement leaves the old entry and counters unchanged. A bounded TTL sweep may evict expired entries, but it must not scan more than `MAX_ENTRIES` and must preserve the accounting invariant. Error codes are fixed categories such as `EPHEMERAL_ARGUMENT_TOO_LARGE`, `EPHEMERAL_STORE_FULL`, `EPHEMERAL_ARGUMENT_MALFORMED`, and `EPHEMERAL_ARGUMENT_BINDING_MISMATCH`; none includes serialized bytes, a value, or a repr of the mapping.

`project_tool_result_for_log` returns this exact generic-log schema:

```python
{
    "tool": name,
    "ok": bool(full_result.get("ok")),
    "error_code": safe_error_code_or_empty,
    "record_counts": safe_integer_counts,
    "result_hash": canonical_json_sha256(project_tool_result_for_model(name, full_result)),
}
```

- [ ] **Step 4: Split executor results before graph persistence**

Keep the approved-roots five-argument `ToolExecutor` unchanged. At the web boundary, retain the existing `_run_tool` body as `_run_tool_local`; adapt non-browse results to the already-defined wrapper as follows. Browse results retain the approved-roots `BrowseAudit` in `security_audit` rather than synthesizing one here:

```python
def _run_tool(
    name: str,
    arguments: dict[str, Any],
    project_dir: Path,
    approved: bool,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    full = _run_tool_local(name, arguments, project_dir, approved, context)
    return ToolExecutionResult(
        local=full,
        model=project_tool_result_for_model(name, full),
        log_projection=project_tool_result_for_log(name, full),
        security_audit=None,
    )
```

In `node_agent`, immediately place each parsed raw argument object in the injected `EphemeralToolCallStore`, then append only `project_assistant_tool_call(...)` and projected pending/deferred records to `ChatState`. `node_guardrail` and `node_execute` resolve and verify the opaque refs locally, normalize/replace the stored raw payload after validation, and delete refs on execution, rejection, expiry, or error. They never restore raw arguments into state. Provider replay retains a valid assistant `tool_calls` followed by matching projected `role="tool"` messages using the original call id; only the argument JSON is the versioned safe projection.

In `chat_graph.node_execute`, construct the existing `ToolExecutionContext(project_id=state["project_id"], thread_id=state.get("thread_id"))`, pass resolved local arguments and that context unchanged to the executor, then invoke the injected approved-roots `_finalize_tool_execution_result` exactly once. If finalization fails it returns the dependency's sanitized failure wrapper. Assert `security_audit is None` after finalization; disclosure code never calls `_record_browse_audit`, `append_history`, or another audit sink. Only then append `result.model` as provider `role="tool"` content, append `result.log_projection` to generic `tool_log`, and deliver `result.local` through the approved-roots request-local sink. Do not store raw arguments in `tool_log`; store `call_id`, `name`, `risk`, approval status, `arguments_hash`, `argument_projection`, and the bounded log projection. Clear local/result wrappers and refs in `finally`.

- [ ] **Step 5: Run Task 2 GREEN tests and commit exact files**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_model_context.py tests/test_chat_graph.py tests/test_webapp_chat_tools.py
git diff --check -- src/rnaseq_agent/model_context.py src/rnaseq_agent/agent_tools.py src/rnaseq_agent/chat_graph.py src/rnaseq_agent/webapp.py tests/test_model_context.py tests/test_chat_graph.py tests/test_webapp_chat_tools.py
```

Expected: all selected tests pass; default provider captures, graph state, and tool log contain no exact sentinels; local results still support the browser/deterministic caller.

```powershell
git add -- src/rnaseq_agent/model_context.py src/rnaseq_agent/agent_tools.py src/rnaseq_agent/chat_graph.py src/rnaseq_agent/webapp.py tests/test_model_context.py tests/test_chat_graph.py tests/test_webapp_chat_tools.py
git commit -m "feat: project model context through safe summaries"
```

---

### Task 3: Common provider gateway and last-mile secret scanner

**Files:**
- Modify: `src/rnaseq_agent/model_provider.py`
- Create: `tests/test_model_provider_gateway.py`
- Modify: `src/rnaseq_agent/chat_graph.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `tests/test_chat_graph.py`
- Modify: `tests/test_webapp_chat_stream.py`

**Interfaces:**
- Consumes `ProviderConfig`, `ProviderIdentity`, `ProviderCredentials`, `PreparedModelRequest`, `ProviderReply`, `ProviderEvent`, and `ProviderRequestError` from Task 1.
- Consumes `agent_tools.ToolCallAccumulator` for streamed tool-call assembly.
- Produces `SecretDetection(category: str, section: str, occurrence_id: str)`.
- Produces `scan_outbound_payload(serialized_payload: bytes, credentials: ProviderCredentials) -> None`.
- Produces `ModelProviderGateway.complete(request: PreparedModelRequest) -> ProviderReply`.
- Produces `ModelProviderGateway.stream(request: PreparedModelRequest) -> Iterator[ProviderEvent]`.
- Produces `ModelProviderGateway.responses(request: PreparedModelRequest) -> ProviderReply`.
- Produces `ModelProviderGateway.codex_exec(request: PreparedModelRequest) -> ProviderReply`.
- Produces `ModelProviderGateway.dispatch_exact(request: PreparedModelRequest) -> Iterator[ProviderEvent]`, the only exact-send entry point. It accepts only `request.exact_attempt is True`, selects a mode-specific bounded parser for `chat_completions`, `responses`, or `codex_cli`, and emits the common `ProviderEvent` contract consumed by `collect_and_validate_exact_response`. Validation, serialization, secret scanning, and transport creation happen before this method returns the iterator; it may not defer them into a generator body. Thus `claim_grant_for_send` can start transport while holding the connection-policy lock and before releasing it, while the returned iterator performs only bounded parsing/collection.
- Produces `ModelProviderGateway.list_models(config: ProviderConfig, credentials: ProviderCredentials, timeout_seconds: float) -> list[str]` for the non-generative authenticated GET.
- Changes graph/web helper construction so request bodies are represented by `PreparedModelRequest` before transport.

- [ ] **Step 1: Write RED tests for all gateway transports and secret categories**

Create `tests/test_model_provider_gateway.py` with mocked `requests.post`, `urllib.request.urlopen`, and `subprocess.run`. Use this parameterized scanner test:

```python
from dataclasses import replace
import json

import pytest

from rnaseq_agent.model_disclosure import (
    MODEL_CONTEXT_SECRET_DETECTED,
    PreparedModelRequest,
    ProviderCredentials,
    ProviderConfig,
    ProviderRequestError,
)
from rnaseq_agent.model_provider import (
    ModelProviderGateway,
    normalize_provider_config,
    provider_identity,
)


@pytest.fixture
def base_request() -> PreparedModelRequest:
    config = normalize_provider_config({
        "provider": "openai",
        "api_base": "https://llm.example/v1",
        "model": "gpt-test",
    })
    return PreparedModelRequest(
        provider=config,
        identity=provider_identity(config),
        api_mode="chat_completions",
        payload={"model": "gpt-test", "messages": [{"role": "user", "content": "safe"}]},
        credentials=ProviderCredentials(api_key="provider-key-not-in-body"),
        timeout_seconds=10.0,
    )


@pytest.mark.parametrize("value,category", [
    ("API_KEY_SENTINEL_73", "known_secret"),
    ("PASSWORD_SENTINEL_73", "known_secret"),
    ("CIPHERTEXT_SENTINEL_73", "protected_value"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "private_key_marker"),
    ("https://alice:URL_PASSWORD_SENTINEL_73@llm.example/v1", "url_userinfo"),
    ("Bearer APP_TOKEN_SENTINEL_73", "application_token"),
])
def test_gateway_blocks_secret_before_transport(base_request, monkeypatch, value, category) -> None:
    calls = []
    monkeypatch.setattr("rnaseq_agent.model_provider.requests.post", lambda *a, **k: calls.append((a, k)))
    request = replace(
        base_request,
        payload={"messages": [{"role": "user", "content": value}]},
        credentials=ProviderCredentials(
            api_key="API_KEY_SENTINEL_73",
            known_secrets=("PASSWORD_SENTINEL_73",),
            protected_values=("CIPHERTEXT_SENTINEL_73",),
        ),
    )
    with pytest.raises(ProviderRequestError) as caught:
        ModelProviderGateway().complete(request)
    assert caught.value.code == MODEL_CONTEXT_SECRET_DETECTED
    assert caught.value.transmission_started is False
    assert category in str(caught.value)
    assert value not in str(caught.value)
    assert calls == []


def test_configured_api_key_is_always_a_scanner_input(base_request, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("rnaseq_agent.model_provider.requests.post", lambda *a, **k: calls.append((a, k)))
    request = replace(
        base_request,
        payload={"messages": [{"role": "user", "content": "provider-key-not-in-body"}]},
        credentials=ProviderCredentials(api_key="provider-key-not-in-body"),
    )
    with pytest.raises(ProviderRequestError) as caught:
        ModelProviderGateway().complete(request)
    assert caught.value.code == MODEL_CONTEXT_SECRET_DETECTED
    assert caught.value.transmission_started is False
    assert calls == []


def test_secret_event_ids_are_random_and_not_secret_fingerprints(base_request) -> None:
    event_ids = []
    for _ in range(2):
        with pytest.raises(ProviderRequestError) as caught:
            ModelProviderGateway().complete(replace(
                base_request,
                payload={"messages": [{"role": "user", "content": "LOW_ENTROPY_PASSWORD"}]},
                credentials=ProviderCredentials(known_secrets=("LOW_ENTROPY_PASSWORD",)),
            ))
        event_ids.append(caught.value.occurrence_id)
    assert event_ids[0] != event_ids[1]
    assert all(value.startswith("evt_") for value in event_ids)
```

Add one success test per API mode: chat completion POST, streaming SSE with tool call fragments, Responses POST, Codex CLI prompt, and `/models` GET. Assert Authorization is assembled only inside the gateway, the scanner sees the exact serialized body/prompt immediately before the mocked transport, HTTP non-200 errors omit `response.text`, and exceptions raised after entering a mocked transport have `transmission_started is True`.

Add a parameterized exact-dispatch test for `api_mode in ("chat_completions", "responses", "codex_cli")`. Each fixture includes a safe text response and a response containing non-empty content followed by a fragmented function/tool call. Drive the real `ModelProviderGateway.dispatch_exact` and its mode parser with bounded fake wire transports: JSON `data:` SSE frames for Chat Completions, named Responses SSE frames, and one bounded structured Codex output-file payload. Do not use a `RecordingGateway` that constructs `ProviderEvent` directly, because that would bypass parser coverage. The test calls only `gateway.dispatch_exact(request)` and asserts that all three modes produce the same event vocabulary (`delta` followed by one terminal `message`) for safe text, while the malicious fixture produces bounded `tool_call_fragment` events that the shared collector rejects before returning any validated response. No mode may expose a mode-specific raw chunk or response object to the caller. Exercise the dispatcher limits with `MAX_EVENTS + 1` frames, a frame larger than 64 KiB, total text larger than 1 MiB, 33 distinct tool-call ids, and aggregate tool fragments larger than 256 KiB; each fails with a stable boundedness/protocol code and no exact content in the exception.

In `tests/test_chat_graph.py` and `tests/test_webapp_chat_stream.py`, add a fake gateway and assert graph failure without an exact grant may use summary-only fallback, while a `ProviderRequestError(transmission_started=True)` is surfaced without a second provider send.

- [ ] **Step 2: Run the Task 3 RED tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_model_provider_gateway.py tests/test_chat_graph.py tests/test_webapp_chat_stream.py -k "gateway or secret or transport_started or fallback"
```

Expected: gateway/scanner symbols are missing and existing helpers perform direct `requests.post` calls or retry without transport-attempt metadata.

- [ ] **Step 3: Implement scanner ordering and bounded error reporting**

Serialize HTTP payloads with `json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")`; serialize Codex prompts as UTF-8 bytes. Scanner inputs always begin with the non-empty configured `credentials.api_key`, whether or not it is repeated in `known_secrets`; then scan non-empty `known_secrets`, non-empty `protected_values`, PEM markers (`BEGIN RSA PRIVATE KEY`, `BEGIN OPENSSH PRIVATE KEY`, `BEGIN PRIVATE KEY`), URL userinfo parsed from URL-shaped tokens, and bearer/token structural patterns. Deduplicate scanner inputs without changing their values. Do not scan the Authorization header containing the credential needed for transport; scan whether that credential occurs in the payload.

Define the scanner result type in `model_provider.py`:

```python
@dataclass(frozen=True)
class SecretDetection:
    category: str
    section: str
    occurrence_id: str
```

On a match, generate `occurrence_id = "evt_" + secrets.token_hex(16)` independently of the matched bytes, then raise:

```python
raise ProviderRequestError(
    f"模型请求包含禁止发送的凭据类别：{category}（事件 {occurrence_id}）。",
    code=MODEL_CONTEXT_SECRET_DETECTED,
    transmission_started=False,
    category=category,
    occurrence_id=occurrence_id,
)
```

Never attach the payload, match, surrounding bytes, request headers, response body, or a deterministic secret-derived digest to the exception. Unit tests may observe the category through a private scanner helper, but production logs receive only code/category/random occurrence id. Two detections of the same low-entropy password must have unrelated ids so logs cannot be used for dictionary verification or cross-event equality.

Define one bounded exact-event layer immediately below the transport adapters. `_iter_chat_completion_events(response)`, `_iter_responses_events(response)`, and `_iter_codex_events(stdout, output_file)` are private mode parsers; `dispatch_exact` is their sole dispatcher and wraps each parser with the same event counter, UTF-8 byte counter, frame-size check, sequence assignment, and terminal-state check. Chat Completions parses only bounded SSE `data:` frames, maps `choices[*].delta.content` to `delta`, maps each `delta.tool_calls[*]` argument piece to `tool_call_fragment`, and maps one `finish_reason="stop"` plus `[DONE]` to the canonical terminal `message`; `finish_reason="tool_calls"` still emits fragments so the shared collector can reject it. Responses parses bounded SSE event names (`response.output_text.delta`, function-call argument deltas, and `response.completed`) into the same three kinds, rejecting unknown event shapes and any output item that is not text or a function-call fragment. Codex CLI parses only the bounded structured result produced by its schema/output-file invocation, accepting `output_text`/`text` as one canonical terminal message and translating any reported tool/function call into `tool_call_fragment`; plain unstructured stdout, malformed JSON, multiple result objects, or an output file over the cap fails closed. The parsers never return provider-specific dictionaries, raw SSE lines, or unbounded stdout.

`dispatch_exact` must reject a request whose `exact_attempt` flag is false, must force the exact no-tools request shape for all three modes, and must not call a fallback parser or second transport. It returns an iterator only to the shared collector; the web/graph code is forbidden from iterating it directly. The collector is the only code allowed to convert the bounded event stream into `ValidatedExactResponse`, and it buffers all events until one terminal non-tool message has passed validation.

- [ ] **Step 4: Implement the sole generative transport gateway and migrate graph/web helpers**

`ModelProviderGateway.complete` supports `chat_completions`; `responses` supports the Responses shape; `stream` parses SSE into `ProviderEvent`; `codex_exec` writes schema/output files in the existing temporary directory pattern and scans `request.prompt` before `subprocess.run`. Each method must:

1. Verify `request.identity == provider_identity(request.provider)`.
2. Serialize and scan the final body/prompt.
3. Set a local `transmission_started = True` immediately before entering `requests.post`, `urllib.request.urlopen`, or `subprocess.run`.
4. Translate transport/timeout/parse failures to `ProviderRequestError` with the stable code `MODEL_PROVIDER_REQUEST_FAILED` and the actual `transmission_started` value.
5. Return response text/JSON without request data in errors.

Change `chat_graph._stream_chat_completion` to accept `request: PreparedModelRequest` plus an injected `gateway: ModelProviderGateway`, and adapt gateway `ProviderEvent` values to the existing graph events. Change `_llm_reply_or_none` and `_llm_stream_chunks` to construct summary-only prepared requests and call the gateway. Remove direct `requests.post` imports/calls from those three helpers. Preserve tools/tool choice and streaming behavior.

- [ ] **Step 5: Run Task 3 GREEN tests and commit exact files**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_model_provider_gateway.py tests/test_chat_graph.py tests/test_webapp_chat_stream.py
git diff --check -- src/rnaseq_agent/model_provider.py src/rnaseq_agent/chat_graph.py src/rnaseq_agent/webapp.py tests/test_model_provider_gateway.py tests/test_chat_graph.py tests/test_webapp_chat_stream.py
```

Expected: all selected tests pass; every tested secret aborts before transport; gateway errors contain no response body or secret; summary-only fallback sends at most one additional request and an attempted exact request sends no fallback.

```powershell
git add -- src/rnaseq_agent/model_provider.py src/rnaseq_agent/chat_graph.py src/rnaseq_agent/webapp.py tests/test_model_provider_gateway.py tests/test_chat_graph.py tests/test_webapp_chat_stream.py
git commit -m "feat: centralize model provider transport"
```

---

### Task 4: Durable metadata-only grant store with cross-process one-time claim

**Files:**
- Create: `src/rnaseq_agent/model_data_grants.py`
- Create: `tests/test_model_data_grants.py`
- Modify: `src/rnaseq_agent/connection_store.py`
- Modify: `src/rnaseq_agent/model_context.py`
- Modify: `src/rnaseq_agent/model_provider.py`
- Modify: `src/rnaseq_agent/remote_browse.py`
- Modify: `src/rnaseq_agent/remote_scan_store.py`
- Modify: `tests/test_model_context.py`
- Modify: `tests/test_remote_browse.py`
- Modify: `tests/test_connection_store.py`

**Interfaces:**
- Consumes the approved-roots connection transaction/parser internals, per-project remote-scan-store lock/read primitive, and `project_state_lock(config_path: Path)`; nested acquisition always follows `connection policy -> remote scan -> project/grant`. It reads one connection snapshot inside one lock acquisition and never uses a two-step policy read followed by lock re-entry.
- Consumes `DataField`, `GrantBindings`, `DataDisclosureGrant`, `ClaimedDataGrant`, `DisclosureManifest`, `GrantOutcome`, and all grant error constants from `model_disclosure.py`.
- Produces `ModelDisclosureConnectionSnapshot(browse_policy: BrowsePolicy, provider: ProviderConfig, provider_identity: ProviderIdentity, provider_config_revision: str, tool_mode: str, credentials: ProviderCredentials, connection_revision: str)` with `repr=False`.
- Produces `locked_model_disclosure_connection(*, store_dir: Path | None = None, runtime_secrets: Sequence[str] = ()) -> Iterator[ModelDisclosureConnectionSnapshot]`. It acquires the sole connection-store lock once, parses browse policy, normalized provider/base/model/API mode, normalized live tool mode, API key/protected values, and a canonical relevant-block revision from the same bytes, and yields them without a second reader or lock acquisition.
- Produces `DataGrantError(RuntimeError)` with public attributes `code: str` and `grant_id_hash: str` and no exact values.
- Produces `grant_record_path(project_dir: Path, grant_id: str) -> Path`.
- Produces `load_grant(project_dir: Path, grant_id: str) -> DataDisclosureGrant`.
- Produces one issue API: `issue_grant_request(project_dir: Path, *, project_id: str, thread_id: str, fields: Sequence[DataField], purpose: str, source_ref: str | None = None, connection_store_dir: Path | None = None, scan_store_dir: Path | None = None, runtime_secrets: Sequence[str] = (), now: datetime | None = None) -> DataDisclosureGrant`. It derives provider identity/config revision, tool mode, browse policy, remote reference, revisions, and counts under the fixed lock order; callers cannot supply bindings, provider, mode, counts, or scan metadata. The raw model-supplied `purpose` is accepted only in the bounded ephemeral call, validated for length, immediately reduced to `purpose_hash = canonical_json_sha256({"purpose": purpose})`, and never stored, checkpointed, logged, or rendered. The confirmation card uses the fixed text `模型请求精确项目数据` and a boolean `purpose_present`; it never echoes the purpose string.
- Produces `decide_grant(project_dir: Path, grant_id: str, *, approved: bool, note: str = "", now: datetime | None = None) -> DataDisclosureGrant`.
- Produces `ExactClaimInputs(project_id: str, thread_id: str, source_ref: str | None, connection_store_dir: Path | None, scan_store_dir: Path | None, runtime_secrets: tuple[str, ...])` and `PreparedExactClaim(claim, connection_snapshot, context, request, events, transmission_started, terminal_guard)` as internal orchestration records with `repr=False` where credentials or exact data are reachable. No caller-supplied binding/provider/mode snapshot exists.
- Produces `claim_grant_for_send(project_dir: Path, grant_id: str, *, live_inputs: ExactClaimInputs, prepare: Callable[[ClaimedDataGrant, ModelDisclosureConnectionSnapshot], tuple[ModelContext, PreparedModelRequest]], open_stream: Callable[[PreparedModelRequest], Iterator[ProviderEvent]], now: datetime | None = None) -> PreparedExactClaim`. This is the sole composite claim transaction; it obtains the live connection snapshot under lock, constructs bindings/request from it, advances `open_stream` through transport creation before releasing that lock, and never returns raw values as a standalone route result. For an exact turn, `open_stream` must be the same `ModelProviderGateway.dispatch_exact` callable described in Task 3; passing `stream`, `responses`, `codex_exec`, or a mode-specific parser is an invalid implementation and has a failing contract test.
- Produces `finish_grant_claim(project_dir: Path, claim: ClaimedDataGrant, *, outcome: GrantOutcome, manifest: DisclosureManifest, error_code: str | None = None, now: datetime | None = None) -> DataDisclosureGrant`.
- Produces `count_exact_project_data(project_dir: Path, fields: Sequence[DataField]) -> dict[str, int]`.
- Produces `extract_exact_project_data(project_dir: Path, fields: Sequence[DataField]) -> Mapping[DataField, tuple[str, ...]]`.
- Produces `RemoteDisclosureSnapshot(reference: RemoteScanReference, project_id: str, thread_id: str, canonical_directories: tuple[str, ...], fastq_basenames: tuple[str, ...])` in `remote_browse.py`.
- Produces `locked_disclosure_snapshot(project_dir: Path, source_ref: str, *, project_id: str, thread_id: str, live_policy: BrowsePolicy, store_dir: Path | None = None) -> AbstractContextManager[RemoteDisclosureSnapshot]`, used only while the caller already holds the connection-policy lock. It acquires the scan-store lock, resolves the entire stored bounded scan, rejects stored `thread_id=None`, and returns every canonical group directory and every bounded FASTQ basename row; no caller-supplied group list or subset exists.
- Produces `count_exact_model_data(project_dir: Path, fields: Sequence[DataField], *, remote_snapshot: RemoteDisclosureSnapshot | None = None) -> dict[str, int]`.
- Produces `extract_exact_model_data(project_dir: Path, fields: Sequence[DataField], *, remote_snapshot: RemoteDisclosureSnapshot | None = None) -> Mapping[DataField, tuple[str, ...]]`.

- [ ] **Step 1: Write RED lifecycle, scope, persistence, and concurrency tests**

Create `tests/test_model_data_grants.py`. Use a fixed UTC clock and the Task 1 project fixture. Add this core lifecycle test:

```python
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from rnaseq_agent.model_context import extract_exact_project_data, read_model_data_revisions
from rnaseq_agent.model_data_grants import (
    DataGrantError,
    claim_grant_for_send,
    decide_grant,
    finish_grant_claim,
    grant_record_path,
    issue_grant_request,
    load_grant,
)
from rnaseq_agent.model_disclosure import (
    MODEL_DATA_GRANT_CONSUMED,
    MODEL_DATA_SCOPE_UNSUPPORTED,
    DisclosureManifest,
    GrantBindings,
)
from rnaseq_agent.model_provider import normalize_provider_config, provider_identity

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    path = tmp_path / "project-73"
    path.mkdir()
    (path / "project.json").write_text(json.dumps({
        "project": {"id": "project-73"},
        "samples": {"items": [{
            "sample_id": "PATIENT_SENTINEL_73",
            "condition": "tumor",
            "fastq_1": "TUMOR_SENTINEL_R1.fastq.gz",
            "fastq_2": "TUMOR_SENTINEL_R2.fastq.gz",
        }]},
    }), encoding="utf-8")
    (path / "report.md").write_text("REPORT_SENTINEL_73", encoding="utf-8")
    return path


@pytest.fixture
def connection_store_dir(tmp_path: Path) -> Path:
    path = tmp_path / "connection"
    seed_connection(
        path,
        provider="openai",
        api_base="https://llm.example/v1",
        model="gpt-test",
        tool_mode="read_only",
    )
    return path


def test_grant_persists_metadata_but_exact_values_exist_only_in_claim(
    project_dir, connection_store_dir
) -> None:
    grant = issue_grant_request(
        project_dir,
        project_id="project-73",
        thread_id="main",
        fields=("sample_ids", "fastq_filenames"),
        purpose="核对配对关系",
        connection_store_dir=connection_store_dir,
        now=NOW,
    )
    assert grant.status == "pending"
    decided = decide_grant(project_dir, grant.grant_id, approved=True, now=NOW)
    assert decided.status == "approved"

    prepared = claim_grant_for_send(
        project_dir,
        grant.grant_id,
        live_inputs=ExactClaimInputs(
            project_id="project-73", thread_id="main", source_ref=None,
            connection_store_dir=connection_store_dir, scan_store_dir=None,
            runtime_secrets=(),
        ),
        prepare=prepare_exact_request,
        open_stream=open_exact_stream,
        now=NOW,
    )
    claim = prepared.claim
    assert claim.exact_values["sample_ids"] == ("PATIENT_SENTINEL_73",)
    assert claim.exact_values["fastq_filenames"] == (
        "TUMOR_SENTINEL_R1.fastq.gz", "TUMOR_SENTINEL_R2.fastq.gz",
    )
    persisted = grant_record_path(project_dir, grant.grant_id).read_text(encoding="utf-8")
    assert "PATIENT_SENTINEL_73" not in persisted
    assert "TUMOR_SENTINEL_R1.fastq.gz" not in persisted
    assert load_grant(project_dir, grant.grant_id).status == "transmitting"

    with prepared.terminal_guard():
        list(prepared.events)
    assert load_grant(project_dir, grant.grant_id).status == "consumed_success"
    with pytest.raises(DataGrantError) as replay:
        claim_grant_for_send(
            project_dir,
            grant.grant_id,
            live_inputs=ExactClaimInputs(
                project_id="project-73", thread_id="main", source_ref=None,
                connection_store_dir=connection_store_dir, scan_store_dir=None,
                runtime_secrets=(),
            ),
            prepare=prepare_exact_request,
            open_stream=open_exact_stream,
            now=NOW,
        )
    assert replay.value.code == MODEL_DATA_GRANT_CONSUMED
```

Add a table-driven lifecycle test with explicit final status/error pairs: user rejection -> `rejected/MODEL_DATA_GRANT_REJECTED`; pending or approved expiry -> `consumed_failed/MODEL_DATA_GRANT_EXPIRED`; cross-project/thread or malformed binding -> `consumed_failed/MODEL_DATA_GRANT_INVALID`; origin/model change -> `consumed_failed/MODEL_PROVIDER_CHANGED`; API-base path-only change, tool-mode change including `disabled`, policy/project/sample/relevant-report revision drift -> `consumed_failed/MODEL_DATA_REVISION_CHANGED`; revoked/expired/mismatched/`thread_id=None` remote source -> `consumed_failed/MODEL_DATA_SCOPE_UNSUPPORTED`; scanner failure after claim -> `consumed_failed/MODEL_CONTEXT_SECRET_DETECTED`. Invalid fields or unsupported remote scope discovered before a record is issued raise the stable code and create no record. Assert every pre-claim failure happens before extraction/prepare/open-stream callbacks and persists no raw values.

Add an unsupported case for a missing scan reference and a supported extraction case for the server-owned approved-roots scan:

```python
@pytest.mark.parametrize("fields", [("remote_paths",), ("remote_paths", "sample_ids")])
def test_remote_scope_is_disabled_without_revision_bound_scan_handle(
    project_dir, connection_store_dir, fields
) -> None:
    with pytest.raises(DataGrantError) as caught:
        issue_grant_request(
            project_dir, project_id="project-73", thread_id="main", fields=fields,
            purpose="检查远程数据", connection_store_dir=connection_store_dir, now=NOW,
        )
    assert caught.value.code == MODEL_DATA_SCOPE_UNSUPPORTED
    assert not (project_dir / ".model_data_grants").exists()


def test_remote_snapshot_exposes_all_group_paths_and_basenames_from_one_scan(
    project_dir, connection_store_dir, approved_scan_store
) -> None:
    with locked_model_disclosure_connection(store_dir=connection_store_dir) as connection:
        with locked_disclosure_snapshot(
            project_dir,
            approved_scan_store.source_ref,
            project_id="project-73",
            thread_id="main",
            live_policy=connection.browse_policy,
            store_dir=approved_scan_store.store_dir,
        ) as snapshot:
            exact = extract_exact_model_data(
                project_dir,
                ("remote_paths", "fastq_filenames"),
                remote_snapshot=snapshot,
            )
    assert exact == {
        "remote_paths": (
            "/approved/SENTINEL_73/fastq",
            "/approved/SECOND_GROUP",
        ),
        "fastq_filenames": (
            "TUMOR_SENTINEL_R1.fastq.gz", "TUMOR_SENTINEL_R2.fastq.gz",
            "SECOND_GROUP_R1.fastq.gz", "SECOND_GROUP_R2.fastq.gz",
        ),
    }


def test_workbench_scan_with_null_thread_cannot_authorize_llm_disclosure(
    project_dir, connection_store_dir, approved_workbench_scan_store
) -> None:
    with pytest.raises(DataGrantError) as caught:
        issue_grant_request(
            project_dir,
            project_id="project-73",
            thread_id="main",
            source_ref=approved_workbench_scan_store.source_ref,
            fields=("remote_paths",),
            purpose="检查远程数据",
            connection_store_dir=connection_store_dir,
            scan_store_dir=approved_workbench_scan_store.store_dir,
            now=NOW,
        )
    assert caught.value.code == MODEL_DATA_SCOPE_UNSUPPORTED
```

In `tests/test_remote_browse.py`, extend the approved-roots server-owned scan fixture to expose `source_ref`, at least two groups, identity digest, root id, and browse-policy revision. Assert `result_revision` changes when any canonical directory, basename row, identity digest, policy revision, or group contents change; deterministic group ordering alone does not change it. Assert the locked disclosure loader returns all groups and rejects a missing/expired source, stored `thread_id=None`, wrong project/thread, tampered scan, revoked root, identity drift, or policy revision drift with `MODEL_DATA_SCOPE_UNSUPPORTED` and returns no values.

For concurrency, approve one grant, synchronize two workers on a barrier, call `claim_grant_for_send` from both, and assert exactly one returns `PreparedExactClaim`; the other raises `MODEL_DATA_GRANT_CONSUMED`. Add a subprocess variant that invokes a small module-level worker through `multiprocessing.get_context("spawn")` so Windows and POSIX both prove cross-process locking. Instrument all three lock types and assert the exact order `connection policy -> remote scan -> project/grant` with no inversion or deadlock.

Add provider/mode and root-revoke races using real connection-store mutations. When provider/base/model, `tool_mode`, or root revocation wins the connection lock, claim reads the new value from the same locked bytes, atomically records the required failed terminal state, and calls neither `prepare` nor transport. When claim wins, its context/request credentials/config/mode all equal the yielded `ModelDisclosureConnectionSnapshot`, transport start occurs before lock release, and the mutation proceeds afterward. A test monkeypatches the ordinary unlocked readers to raise, proving claim never consults caller snapshots or reacquires the connection lock.

- [ ] **Step 2: Run the Task 4 RED tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_model_data_grants.py tests/test_model_context.py tests/test_remote_browse.py -k "grant or exact_project_data or remote_scope or disclosure_snapshot"
```

Expected: `model_data_grants`, whole-scan disclosure loading, the composite three-lock claim, and exact-data extraction interfaces do not exist.

- [ ] **Step 3: Implement metadata-only storage and monotonic transitions**

Store each record at `<project>/.model_data_grants/<sha256(grant_id)>.json`. Use `secrets.token_urlsafe(24)` for `grant_id` and claim tokens; persist only the SHA-256 of the claim token. JSON records contain bindings, fields, `purpose_hash`, timestamps, policy version, record counts, status, terminal error code, and manifest counts/byte length/revisions. For a remote source, persist `remote_scan_ref_hash = canonical_json_sha256({scan_id, result_revision, project_id, thread_id, source, expires_at})`, never the raw `source_ref`. Records never contain `purpose`, `exact_values`, source sample ids, filenames, paths, report text, credentials, or user rejection notes.

Every issue and claim begins with `locked_model_disclosure_connection`, optionally enters the scan-store lock, then enters the project/grant lock. Local grants still take the connection lock because their provider/config/mode binding must come from the same live bytes. Remote issue resolves the server-owned reference and computes aggregate counts while locked. Re-read JSON under its owning lock, validate the expected state, write through a temporary file in the same directory, flush/fsync, then `os.replace`. Enforce only these transitions:

Define the public error without retaining user input or exact values:

```python
class DataGrantError(RuntimeError):
    def __init__(self, message: str, *, code: str, grant_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.grant_id_hash = hashlib.sha256(grant_id.encode("utf-8")).hexdigest()
```

```text
pending -> approved
pending -> rejected
pending -> consumed_failed
approved -> consumed_failed
approved -> transmitting
transmitting -> consumed_success
transmitting -> consumed_ambiguous
transmitting -> consumed_failed
```

`decide_grant(approved=False)` performs `pending -> rejected` with `MODEL_DATA_GRANT_REJECTED`; the same rejection is idempotent. On approval it first checks expiry under the project/grant lock: expired pending becomes `consumed_failed/MODEL_DATA_GRANT_EXPIRED`, otherwise `pending -> approved`. Changing a decision or approving any other terminal record raises `MODEL_DATA_GRANT_CONSUMED` without another transition.

`claim_grant_for_send` obtains the locked live connection snapshot before validating the stored grant. Expiry or any live-binding failure performs one atomic `approved -> consumed_failed` write with the tabled error code and returns no claim/token/values. Compare project id/thread, origin-level provider identity, full provider config revision, normalized tool mode, policy version, project revision, and relevant data revisions. Compare `sample_revision` for project-stored `sample_ids`/`fastq_filenames`, `report_revision` for `report_excerpt`, and `remote_scan_revision` plus scan reference/root policy for `remote_paths` or live-scan `fastq_filenames`. Direct invalidation is legal precisely so pre-transport drift is consumed without pretending transport began.

`claim_grant_for_send` acquires the connection-store lock through `locked_model_disclosure_connection`, then the scan-store lock when `source_ref` is present, then the project/grant lock. In that one critical section it re-reads provider/base/model/API mode, credentials, normalized live tool mode, browse policy, and SSH identity from the same connection bytes; revalidates the entire `RemoteScanReference` and stored scan project, exact non-null thread, source, expiry, identity/root, result revision, and policy revision; derives fresh `GrantBindings` and project/data revisions; and performs either terminal invalidation or the `approved -> transmitting` compare-and-swap. `prepare` receives the locked `ModelDisclosureConnectionSnapshot` and must use its provider and credentials; passing a caller-cached config is a test failure. It then extracts only authorized fields, verifies fresh counts, builds `ModelContext`, constructs the exact `PreparedModelRequest`, serializes it, and runs the last-mile scanner before releasing locks. It advances the gateway stream through transport creation while the connection lock still excludes provider/mode/root mutation, then releases locks before ordinary stream iteration. No DNS/SSH browse is performed inside these locks.

The composite function owns a guard from the instant it commits
`approved -> transmitting`; a caller-side context manager is only the normal
success/iteration surface. If extraction, preparation, serialization, scanning,
transport creation, or any other step before `PreparedExactClaim` can be
returned raises, `claim_grant_for_send` itself must terminalize the claim in a
`finally` path before re-raising. It must never return an exception while the
record remains `transmitting`, and it must not rely on a guard attached to a
return value that was never constructed. A recovery read after the failure
verifies the terminal state and records a bounded operational error if the
state cannot be made terminal.

Generate the claim token only after all pre-claim validation; persist only its SHA-256 with `approved -> transmitting`. From that instant, attach a terminal guard to `PreparedExactClaim`. Extraction/count/context/preparation/serialization/scanner failure or pre-transport cancellation records `consumed_failed` with its stable code. Once the gateway marks transport started, cancellation or failure records `consumed_ambiguous/MODEL_PROVIDER_REQUEST_FAILED` unless a more specific safe protocol code applies; success records `consumed_success` with no error. `finish_grant_claim` compares the claim-token hash, and the guard has a final recovery branch that converts any remaining `transmitting` record to the correct terminal state.

- [ ] **Step 4: Bind approved remote scan results and implement field-isolated extraction**

Reuse the approved-roots `StoredRemoteScan` and `RemoteScanReference` unchanged. `source_ref` names the entire bounded scan and all its groups; do not add a disclosure selection object, group ids, subset state, or browser-controlled authority. Keep exact group rows server-owned for local apply and disclosure; never copy them into grant JSON or graph state. The locked loader revalidates scan expiry, exact project/non-null thread, source/result revision, current SSH identity, active root, root id, and browse-policy revision before returning values.

`count_exact_model_data` and `extract_exact_model_data` read fresh state only after supported-scope validation. `sample_ids` reads non-empty `samples.items[*].sample_id`. Without a remote snapshot, `fastq_filenames` reads basename-only `fastq_1`/`fastq_2` from project-stored rows. With a snapshot, it reads all basename rows from every group in that bounded scan. `remote_paths` reads every canonical group directory from the validated snapshot, deduplicated and deterministically ordered. `report_excerpt` reads at most the first 4,000 Unicode characters of `<project>/report.md`. A remote field without a valid snapshot raises `MODEL_DATA_SCOPE_UNSUPPORTED`. Requesting one field never reads or returns another field.

Reject an empty field list, duplicates, unknown fields, purpose outside 1-240 characters, and counts that do not match a fresh `count_exact_model_data` result for the bound source. `remote_paths` always requires `source_ref`; `fastq_filenames` uses the scan only when `source_ref` is present; a `source_ref` with neither remote paths nor remote-backed filenames is invalid. The confirmation record stores aggregate counts and a scan-reference hash, not values.

- [ ] **Step 5: Run Task 4 GREEN tests and commit exact files**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_model_data_grants.py tests/test_model_context.py tests/test_remote_browse.py
git diff --check -- src/rnaseq_agent/model_data_grants.py src/rnaseq_agent/connection_store.py src/rnaseq_agent/model_context.py src/rnaseq_agent/model_provider.py src/rnaseq_agent/remote_browse.py src/rnaseq_agent/remote_scan_store.py tests/test_model_data_grants.py tests/test_model_context.py tests/test_remote_browse.py tests/test_connection_store.py
```

Expected: all lifecycle, whole-scan, lock-order, revoke-race, and spawned-process claim tests pass; every bounded scan group is disclosed under the one source reference; grant JSON contains no application-injected sentinel; one and only one concurrent claimant succeeds; no grant remains `transmitting`.

```powershell
git add -- src/rnaseq_agent/model_data_grants.py src/rnaseq_agent/connection_store.py src/rnaseq_agent/model_context.py src/rnaseq_agent/model_provider.py src/rnaseq_agent/remote_browse.py src/rnaseq_agent/remote_scan_store.py tests/test_model_data_grants.py tests/test_model_context.py tests/test_remote_browse.py tests/test_connection_store.py
git commit -m "feat: add single-use model data grants"
```

---

### Task 5: Dedicated graph data-confirmation flow and one-turn exact injection

**Files:**
- Modify: `src/rnaseq_agent/agent_tools.py`
- Modify: `src/rnaseq_agent/chat_graph.py`
- Modify: `src/rnaseq_agent/model_context.py`
- Modify: `src/rnaseq_agent/model_data_grants.py`
- Modify: `src/rnaseq_agent/model_provider.py`
- Modify: `src/rnaseq_agent/remote_browse.py`
- Modify: `src/rnaseq_agent/remote_scan_store.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `tests/test_chat_graph.py`
- Modify: `tests/test_model_provider_gateway.py`
- Create: `tests/test_webapp_model_data_disclosure.py`

**Interfaces:**
- Consumes every Task 4 grant function and `ModelProviderGateway.dispatch_exact` from Task 3. Ordinary summary turns may use `stream`, but an approved exact turn must enter through the dispatcher and the shared validator.
- Produces the model schema `request_exact_project_data(fields: list[DataField], purpose: str, source_ref: str | None = None)` as a dedicated data request; it is absent when `tool_mode == "disabled"`. `source_ref` is the opaque whole-scan reference returned in the model-safe browse projection and is required for `remote_paths` or live-scan filenames.
- Produces `ModelContextBuilder.build(*, project_dir: Path | None, project_id: str, thread_id: str, provider: ProviderConfig, system_prompt: str, current_user_message: str, durable_messages: Sequence[Mapping[str, Any]], claimed_grant: ClaimedDataGrant | None) -> ModelContext`.
- Produces `make_grant_bindings(project_dir: Path, project_id: str, thread_id: str, connection: ModelDisclosureConnectionSnapshot, remote_scan: RemoteScanReference | None = None) -> GrantBindings`; only Task 4 calls it while holding the connection lock.
- Produces `ValidatedExactResponse(display_chunks: tuple[str, ...], final_message: Mapping[str, Any], utf8_bytes: int, event_count: int, api_mode: ProviderAPIMode)` and `collect_and_validate_exact_response(events: Iterator[ProviderEvent], *, max_events: int = 10_000, max_utf8_bytes: int = 1_048_576, max_event_bytes: int = 65_536, max_tool_calls: int = 32, max_tool_fragment_bytes: int = 262_144) -> ValidatedExactResponse`. It drains the entire bounded response from `ModelProviderGateway.dispatch_exact`, assembles all tool-call fragments, rejects any complete or fragmented tool call or malformed/multiple terminal message, enforces the event/frame/aggregate limits, and exposes no event before validation succeeds. The caller may not bypass the dispatcher with a mode-specific parser.
- Extends `webapp.create_app(*, project_dir: Path | None = None, workspace_dir: Path | None = None, provider_gateway: ModelProviderGateway | None = None) -> FastAPI` so endpoint tests and every web provider path share one injected gateway instance.
- Extends `ChatState` only with `data_grant_id: str`, `data_confirmation_card: dict[str, Any]`, `data_confirmation_decision: bool | None`, `exact_attempt: bool`, and metadata-only `remote_scan_ref: dict[str, Any]`; no exact values or exact block may be a state field. `remote_scan_ref` is the approved-roots reference with opaque `scan_id`/`source_ref`, result revision, project, exact thread, source, and expiry; it contains no directories, filenames, rows, or group ids.
- Extends resume payload parsing with the same `approval_id`/`approved` shape and dispatches by the stored confirmation card `type`.

- [ ] **Step 1: Write RED graph and real-SQLite endpoint tests**

In `tests/test_chat_graph.py`, script the model to call:

```python
{
    "id": "data-call-1",
    "type": "function",
    "function": {
        "name": "request_exact_project_data",
        "arguments": json.dumps({
            "fields": ["sample_ids"],
            "purpose": "核对肿瘤与对照样本配对",
        }, ensure_ascii=False),
    },
}
```

Assert the interrupt card is exactly shaped as:

```python
{
    "type": "model_data_confirmation",
    "approval_id": grant.grant_id,
    "fields": [{"name": "sample_ids", "label": "样本标识符", "count": 1}],
    "purpose_present": True,
    "expires_at": grant.expires_at,
    "single_turn": True,
    "message": "将向当前配置的模型共享 1 个样本标识符，仅用于下一次回复。",
}
```

Assert the card, state snapshot, and interrupt contain neither exact sample ids nor filenames. Assert a data request cannot share a card with `write_project_config` or `run_analysis`: data request is emitted alone, and deferred ordinary calls remain governed by their own cards.

Create `tests/test_webapp_model_data_disclosure.py` using the real `SqliteSaver`, `_stream`, `_resume`, `_confirm_event`, and `_seed_project` patterns from `test_webapp_chat_tools.py`. Cover:

- approve `sample_ids`: exactly one captured provider request contains `PATIENT_SENTINEL_73` and no FASTQ/path sentinel;
- approve `fastq_filenames`: exactly one request contains filenames and no sample id/directory;
- approve live-scan `fastq_filenames`: exactly one request contains the bounded basenames from every group in the referenced scan and no canonical directory;
- approve `remote_paths`: exactly one request contains every canonical group directory in the referenced scan and no filenames or directories from another scan;
- reject, expire, replay, cross-thread, provider change, sample revision change, and tool mode changed to `disabled`: zero exact provider requests;
- simulated timeout after gateway entry: grant becomes `consumed_ambiguous`, no exact retry/fallback occurs;
- secret scanner failure after claim: grant becomes `consumed_failed`, zero provider calls;
- context-build, request-preparation, serialization, pre-transport cancellation, stream-creation, stream-iteration, `GeneratorExit`, client disconnect, and unknown-exception exits: each has the specified terminal state, zero fallback, and no remaining `transmitting` grant;
- an exact prepared request has no `tools` or `tool_choice`; a malicious stream emits non-empty deltas first and then fragmented `tool_calls` arguments containing every exact sentinel, yet the bounded collector rejects it with `MODEL_EXACT_TOOL_CALL_REJECTED` before any delta/argument reaches SSE, state, SQLite, thread JSON, cards, logs, or the executor;
- rule fallback and no-LLM mode: no grant file is created.

In `tests/test_model_provider_gateway.py`, directly exercise `collect_and_validate_exact_response` with fragmented content and tool-call events. Assert it accepts one bounded terminal text message, rejects zero or multiple terminal messages, enforces both bounds, reconstructs tool-call fragments before deciding, and returns no iterable/event-producing result on any failure. Include a stream with non-empty content deltas followed by a fragmented malicious tool call so the unit test proves the earlier deltas are still unavailable to callers.

Parameterize the real-SQLite exact-grant endpoint tests over all three provider modes. For `chat_completions`, the fake transport returns bounded SSE deltas and a `[DONE]`/stop terminal; for `responses`, it returns `response.output_text.delta` events and one `response.completed`; for `codex_cli`, it returns one bounded structured output object with `output_text`. In each mode, approve `sample_ids`, assert the sentinel occurs in exactly one serialized request, assert `ValidatedExactResponse.api_mode` matches the locked provider mode, and assert the grant reaches `consumed_success` with no second send. Add one forbidden-tool case per mode where safe text deltas precede fragmented function-call arguments containing an exact sentinel; all three must return `MODEL_EXACT_TOOL_CALL_REJECTED`, expose zero SSE/display/state/checkpoint/log content, execute no tool, and leave a terminal `consumed_failed` or `consumed_ambiguous` outcome according to whether transport had started. Mutate the live mode between issue and claim in each parameterized row and assert the lock winner's failed terminal state and zero transport.

Use this concrete fake with `create_app(provider_gateway=fake_gateway)` so the test observes final serialized payloads rather than bypassing the builder:

```python
class RecordingGateway:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.turns: list[dict[str, Any] | ProviderRequestError] = []
        self.serialized_payloads: list[str] = []

    def queue_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self.turns.append({
            "content": "",
            "tool_calls": [{
                "id": f"call-{len(self.turns) + 1}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }],
        })

    def queue_text(self, text: str) -> None:
        self.turns.append({"content": text, "tool_calls": []})

    def queue_error(self, error: ProviderRequestError) -> None:
        self.turns.append(error)

    def dispatch_exact(self, request: PreparedModelRequest) -> Iterator[ProviderEvent]:
        assert request.exact_attempt is bool(request.disclosure_manifest and request.disclosure_manifest.fields)
        self.serialized_payloads.append(json.dumps(request.payload, ensure_ascii=False, sort_keys=True))
        turn = self.turns.pop(0)
        if isinstance(turn, ProviderRequestError):
            raise turn
        content = str(turn["content"])
        events: list[ProviderEvent] = []
        if content:
            events.append(ProviderEvent(kind="delta", value=content, sequence=len(events)))
        for index, call in enumerate(turn["tool_calls"]):
            function = call.get("function", {})
            events.append(ProviderEvent(kind="tool_call_fragment", value={
                "call_id": str(call.get("id", "")),
                "index": index,
                "name": str(function.get("name", "")),
                "arguments_fragment": str(function.get("arguments", "")),
            }, sequence=len(events)))
        events.append(ProviderEvent(kind="message", value={
            "role": "assistant",
            "content": content,
            "tool_calls": [],
        }, sequence=len(events)))
        return iter(events)

    def stream(self, request: PreparedModelRequest) -> Iterator[ProviderEvent]:
        # Summary-only graph turns may use the ordinary stream surface; exact turns
        # are required to call dispatch_exact above so the mode parser is exercised.
        if request.exact_attempt:
            return self.dispatch_exact(request)
        turn = self.turns.pop(0)
        text = str(turn.get("content", ""))
        return iter((ProviderEvent(kind="delta", value=text, sequence=0),
                     ProviderEvent(kind="message", value={
                         "role": "assistant", "content": text, "tool_calls": []
                     }, sequence=1)))
```

The `client`, `token`, `seeded_project`, and `fake_gateway` fixtures in this new test file construct a temporary workspace, save a live LLM configuration, seed the sentinel project, and pass the same `RecordingGateway` to `create_app`. No test monkeypatches `_stream_chat_completion` after this task.

- [ ] **Step 2: Run the Task 5 RED tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_chat_graph.py tests/test_model_provider_gateway.py tests/test_webapp_model_data_disclosure.py -k "model_data or exact or grant"
```

Expected: the dedicated schema/card and builder do not exist; current guardrail either treats the request as an ordinary tool or rejects it as unknown.

- [ ] **Step 3: Add the dedicated schema and confirmation branch**

Add `request_exact_project_data` to `agent_tools.tool_schemas(mode)` only when normalized mode is not `disabled`. Its JSON schema uses `uniqueItems: true`, field enum values from `DataField`, `minItems: 1`, `maxItems: 4`, purpose length 1-240, and optional `source_ref` length 1-200. Do not add it to ordinary `ToolSpec`, `risk_of`, `confirmation_policy`, batch grouping, or `_run_tool_local`. `project_tool_result_for_model("browse_remote_samples", ...)` may return only counts/booleans/authorization plus the opaque whole-scan `source_ref`; it never returns canonical directories, group ids, or basenames.

In `chat_graph.node_guardrail`, detect this name before ordinary call splitting. Require it to be the only active call. Resolve its raw arguments from Task 2's ephemeral store, then call the single `issue_grant_request` for local or remote fields; it obtains the live connection snapshot, resolves an optional whole scan, creates bindings, and computes counts under the fixed lock order. Delete the ephemeral call ref after issue. Store only grant id/card metadata and route to the existing interrupt node with `type="model_data_confirmation"`. On resume, compare the browser `approval_id` with stored id, call `decide_grant`, and route an approval into the exact-send branch; rejection appends a fixed model-safe tool result stating that the user declined, without exact fields.

Issue and claim both use `locked_model_disclosure_connection`; there is no independent provider or tool-mode reader in this path. Claim performs the final read and keeps the connection lock through request preparation and transport start. Changing provider/model/base, project/thread, mode, project/sample/report revision, policy version, or expiry before claim wins the lock records its explicit failed terminal state and sends nothing.

Implement the binding constructor exactly once in `model_context.py`:

```python
def make_grant_bindings(
    project_dir: Path,
    project_id: str,
    thread_id: str,
    connection: ModelDisclosureConnectionSnapshot,
    remote_scan: RemoteScanReference | None = None,
) -> GrantBindings:
    return GrantBindings(
        project_id=project_id,
        thread_id=thread_id,
        provider_identity=connection.provider_identity.digest,
        tool_mode=connection.tool_mode,
        revisions=read_model_data_revisions(
            project_dir,
            connection.provider,
            remote_scan_revision=remote_scan.result_revision if remote_scan else None,
        ),
        remote_scan=remote_scan,
    )
```

The interrupt/card result carries `error_code=MODEL_DATA_CONFIRMATION_REQUIRED`. It is a control result, not an exception and not an ordinary read/write/execute risk. An approved resume replaces it with a claimed one-turn request; an unapproved turn remains summary-only.

- [ ] **Step 4: Build ephemeral context, claim before send, and finish every outcome**

Implement `ModelContextBuilder.build` so it:

1. Builds `build_safe_project_summary(project_dir)` and prepends it as a versioned system context.
2. Prepends the caller's fixed application `system_prompt`, then accepts only model-safe durable messages; Task 6 will harden legacy filtering.
3. For a claim, formats one system block named `model_exact_data_v1` with only the claimed field keys and values.
4. Computes `DisclosureManifest` from grant hash, field names, counts, UTF-8 byte length, and revisions.
5. Returns a fresh tuple of messages; never mutates `durable_messages` or returns exact values outside `ModelContext.messages`.

Without a claim, the builder returns `DisclosureManifest(grant_id_hash=None, fields=(), record_counts={}, byte_length=0, revisions=read_model_data_revisions(project_dir, provider))`. With `project_dir is None`, use a stable empty-project revision whose project/sample/report/remote values are canonical empty hashes or `None`, and whose provider revision is still the current provider digest.

For remote fields, the data request carries only opaque `source_ref`. Resolve it through the approved-roots scan store; that record supplies the existing whole-scan `RemoteScanReference` and all bounded groups. Reject a record whose stored thread is `None` or differs from the exact LLM thread. The confirmation card displays only field labels and aggregate directory/file counts, never source refs, paths, filenames, or per-group details. Persist only a hash of the reference metadata in the grant record. A caller cannot submit group ids, paths, roots, or a subset during issue or resume.

Set request-local `exact_attempt=True` as soon as an approved data-confirmation resume enters the exact path, before claim or extraction. Use Task 4's composite `claim_grant_for_send`; under its fixed lock order, reload the whole remote scan when present, derive fresh bindings from `ModelDisclosureConnectionSnapshot`, perform terminal invalidation or grant CAS/extraction, build context, prepare the request with exactly `connection.provider` and `connection.credentials`, serialize it, run the scanner, and cross the transport-start boundary before releasing the connection lock. The exact request sets `PreparedModelRequest.exact_attempt=True` and must omit `tools` and `tool_choice` for Chat Completions, omit tool definitions/tool choice for Responses, and add no Codex tool capability. Chat Completions and Responses use their streaming wire shape (`stream=true`); Codex uses the existing schema/output-file invocation and its entire bounded structured result is treated as one dispatcher input, never as an unbounded text fallback.

Pass `open_stream=gateway.dispatch_exact` to `claim_grant_for_send`. The exact branch must never call `gateway.stream`, `gateway.complete`, `gateway.responses`, or `gateway.codex_exec` directly, because those surfaces do not prove that mode-specific parsing has entered the common bounded event contract. `dispatch_exact` owns transport creation for the locked `api_mode`; `collect_and_validate_exact_response` is invoked immediately on its iterator inside the terminal guard.

Immediately after the `approved -> transmitting` CAS, enter one terminal guard. Its scope includes context build, request preparation, serialization, scanner, stream creation, remaining stream collection, response validation, `GeneratorExit`, client cancellation/disconnect, and unknown `BaseException` exits. The logical flow is:

```python
exact_attempt = True
prepared_claim = claim_grant_for_send(
    ...,
    prepare=lambda claim, connection: prepare_exact_request(claim, connection),
    open_stream=gateway.dispatch_exact,
)
with prepared_claim.terminal_guard() as attempt:
    validated = collect_and_validate_exact_response(attempt.events)
    for chunk in validated.display_chunks:
        yield ProviderEvent(kind="delta", value=chunk)
    yield ProviderEvent(kind="message", value=validated.final_message)
```

`collect_and_validate_exact_response` owns `ToolCallAccumulator` for all fragments, accepts exactly one terminal non-tool message, and enforces event/UTF-8 bounds before returning an immutable buffer. It raises `MODEL_EXACT_TOOL_CALL_REJECTED` for any complete or fragmented tool call and `MODEL_PROVIDER_REQUEST_FAILED` for malformed/boundedness failures. No writer/SSE callback receives a delta until it returns successfully. Test non-empty content deltas followed by malicious tool-call fragments containing sample, FASTQ, path, report, credential, and token sentinels; assert zero SSE events, state/checkpoint/log occurrences, and executor calls.

The guard records `consumed_success` only after validated stream completion. A pre-transport failure or cancellation records `consumed_failed`; once the gateway's transport-start flag is set, any non-success exit records `consumed_ambiguous`. It catches and re-raises `GeneratorExit`, framework cancellation/disconnect exceptions, ordinary exceptions, and unknown `BaseException` after terminalizing. A `finally` recovery re-reads the grant and terminalizes any residual `transmitting` state. Suppress `_llm_stream_chunks`, rule fallback, retry, and every other provider send whenever `exact_attempt` is true, regardless of whether claim, build, scan, transport creation, or iteration failed. Clear state grant metadata only after the terminal transition.

- [ ] **Step 5: Run Task 5 GREEN tests and commit exact files**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_chat_graph.py tests/test_model_provider_gateway.py tests/test_webapp_model_data_disclosure.py tests/test_model_data_grants.py
git diff --check -- src/rnaseq_agent/agent_tools.py src/rnaseq_agent/chat_graph.py src/rnaseq_agent/model_context.py src/rnaseq_agent/model_data_grants.py src/rnaseq_agent/model_provider.py src/rnaseq_agent/remote_browse.py src/rnaseq_agent/remote_scan_store.py src/rnaseq_agent/webapp.py tests/test_chat_graph.py tests/test_model_provider_gateway.py tests/test_webapp_model_data_disclosure.py
```

Expected: all selected tests pass; approved exact values appear in exactly one captured request; exact turns expose no tools; malicious returned tool calls are rejected and never executed or persisted; all invalidation cases send none; every exit is terminal with no grant left `transmitting`; `exact_attempt` prevents fallback; no application-injected exact value enters graph state or grant JSON.

```powershell
git add -- src/rnaseq_agent/agent_tools.py src/rnaseq_agent/chat_graph.py src/rnaseq_agent/model_context.py src/rnaseq_agent/model_data_grants.py src/rnaseq_agent/model_provider.py src/rnaseq_agent/remote_browse.py src/rnaseq_agent/remote_scan_store.py src/rnaseq_agent/webapp.py tests/test_chat_graph.py tests/test_model_provider_gateway.py tests/test_webapp_model_data_disclosure.py
git commit -m "feat: require approval for exact model data"
```

---

### Task 6: Legacy checkpoint filtering and safe durable model replies

**Files:**
- Modify: `src/rnaseq_agent/model_context.py`
- Modify: `src/rnaseq_agent/chat_graph.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `src/rnaseq_agent/threads.py`
- Modify: `tests/test_model_context.py`
- Modify: `tests/test_chat_graph.py`
- Modify: `tests/test_webapp_model_data_disclosure.py`
- Modify: `tests/test_webapp_chat_tools.py`

**Interfaces:**
- Consumes `ClaimedDataGrant`, model projection version markers, and the Task 5 builder.
- Produces `filter_durable_messages(messages: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]`.
- Produces `safe_durable_reply(display_reply: str, claim: ClaimedDataGrant | None) -> str`; for any exact claim it ignores provider free text and returns the fixed local acknowledgement `"模型已使用经授权的数据完成本次回复；精确内容仅在本次临时显示中提供。"`.
- Produces `MODEL_CONTEXT_VERSION = 1` and `MODEL_TOOL_PROJECTION_VERSION = 1` markers.
- Changes web turn output to maintain `display_reply` for the current SSE/browser turn and `durable_reply` for `append_message` and future provider replay.
- Changes `_chat_history_messages(project_dir: Path, thread_id: str) -> list[dict[str, Any]]` to pass only user-authored content and versioned safe agent content.

- [ ] **Step 1: Write RED persistence and legacy-replay tests**

Extend `tests/test_model_context.py`:

```python
from rnaseq_agent.model_context import filter_durable_messages, safe_durable_reply


def test_legacy_application_messages_are_not_replayed_but_user_text_is_unchanged() -> None:
    messages = [
        {"role": "user", "content": "我输入了 PATIENT_SENTINEL_73，请帮我检查"},
        {"role": "assistant", "content": "样本 PATIENT_SENTINEL_73 在 /restricted/SENTINEL_73/fastq"},
        {"role": "tool", "content": json.dumps({"samples": ["PATIENT_SENTINEL_73"]})},
        {"role": "assistant", "content": "安全摘要", "model_context_version": 1},
        {"role": "tool", "content": json.dumps({"sample_count": 1}), "model_projection_version": 1},
    ]
    filtered = filter_durable_messages(messages)
    encoded = _serialized(filtered)
    assert filtered[0]["content"] == "我输入了 PATIENT_SENTINEL_73，请帮我检查"
    assert "/restricted/SENTINEL_73/fastq" not in encoded
    assert encoded.count("PATIENT_SENTINEL_73") == 1
    assert "安全摘要" in encoded
    assert "sample_count" in encoded


@pytest.mark.parametrize("display", [
    "报告中的一句 REPORT_SUBSTRING_SENTINEL_73",
    "路径组件 SENTINEL_73/fastq",
    "patient_sentinel_73",
    r"PATIENT\u005fSENTINEL\u005f73",
    "安全结论与 PATIENT_SENTINEL_73 混合出现",
])
def test_exact_display_text_is_never_the_source_of_durable_reply(claim, display) -> None:
    durable = safe_durable_reply(display, claim)
    assert durable == "模型已使用经授权的数据完成本次回复；精确内容仅在本次临时显示中提供。"
    assert durable not in display
    assert all(fragment not in durable for fragment in (
        "REPORT_SUBSTRING_SENTINEL_73", "SENTINEL_73/fastq",
        "patient_sentinel_73", r"PATIENT\u005fSENTINEL\u005f73",
        "PATIENT_SENTINEL_73",
    ))
```

In `tests/test_webapp_model_data_disclosure.py`, split migration and new-write coverage. For migration, seed a LangGraph checkpoint/SQLite/WAL and thread file whose old assistant/tool messages contain every sentinel, snapshot their bytes, start a new turn, and assert the captured provider request and newly materialized logical graph state omit every legacy application-generated sentinel while retaining identical user-authored text. Do not require historic raw pages to be erased; assert any raw sentinel occurrence is confined to a byte-identical snapshotted legacy artifact and no new file/copy is created.

In a separate clean project, run approved exact turns whose provider replies contain a report sentence rather than the full 4,000-character excerpt, a path component, case-changed id, JSON/Unicode-escaped id, and mixed exact/non-exact prose. Assert:

- SSE `delta` may display the exact echo in the current browser turn;
- the `done` payload uses `durable_reply` or omits unsafe `reply` from data logged by the server;
- `threads/main.json`, SQLite/WAL/SHM raw bytes, graph snapshot `messages`, generic `tool_log`, grant files, and application logs contain none of the exact, partial, case-changed, or escaped response sentinels;
- reloading the page shows the fixed local acknowledgement;
- reloading and the next provider turn show the fixed local acknowledgement plus disclosure manifest, never a provider-derived alias/free-text rewrite.

- [ ] **Step 2: Run the Task 6 RED tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_model_context.py tests/test_chat_graph.py tests/test_webapp_model_data_disclosure.py tests/test_webapp_chat_tools.py -k "legacy or durable_reply or exact_echo or checkpoint"
```

Expected: current history replay includes unversioned assistant/tool content and exact model replies are appended unchanged to thread JSON and checkpoint state.

- [ ] **Step 3: Filter all durable provider history through explicit version markers**

`filter_durable_messages` keeps user messages byte-for-byte except for requiring string content. It keeps plain assistant messages only when `model_context_version == 1`, with allowlisted `role` and `content`. For assistant tool calls, it accepts only `tool_projection_version == 1` and rebuilds each item from exactly `id`, `type="function"`, fixed registered `name`, and canonical JSON returned by `project_tool_arguments_for_model`; it rejects extra keys, unknown names, unversioned/raw arguments, and any mismatch between stored `arguments_hash` and projection metadata. It keeps tool messages only when `model_projection_version == 1`, with `role`, `tool_call_id`, registered `name`, and projected content.

Process assistant tool calls and their tool replies as protocol groups. Retain a group only when every assistant call id is unique and has exactly one matching projected tool result in order; otherwise drop the whole application-generated group and insert at most one fixed assistant notice. Drop system messages and rebuild current system/context messages. Collapse one or more omitted application groups into `"部分旧的应用生成记录未按当前数据策略重放。"` This gives the next provider a valid assistant/tool sequence without raw arguments.

Apply the filter inside `ModelContextBuilder.build` even when callers claim the messages are safe. Mark every new model projection created in Task 2 with `model_projection_version=1`; mark every newly persisted safe assistant reply with `model_context_version=1`. `_chat_history_messages` reads `references.model_context_version` and excludes unversioned agent content from provider history while leaving the browser thread API unchanged for local viewing.

- [ ] **Step 4: Split transient display replies from durable replies**

During an exact turn, Task 5 first buffers and validates the full provider response, then emits its chunks to the current browser as `display_reply`; it never appends those chunks or provider final text to `ChatState.messages`. After completion, call `safe_durable_reply(display_reply, claim)`, which deliberately ignores `display_reply` and returns the fixed acknowledgement. Append only that local fixed string to graph state and `<project>/threads/<thread>.json`, and set references:

```python
{
    "via": via,
    "steps": steps,
    "model_context_version": 1,
    "disclosure_manifest": {
        "grant_id_hash": manifest.grant_id_hash,
        "fields": list(manifest.fields),
        "record_counts": dict(manifest.record_counts),
        "byte_length": manifest.byte_length,
    },
}
```

For non-grant replies, `display_reply == durable_reply`; still attach the version marker after applying the durable assistant/tool-call projector. Do not put exact-turn provider text into the SSE `done` object because endpoint logs/middleware may record it; validated exact text reaches the browser only through transient post-validation `delta` events. The fixed acknowledgement and manifest are the only exact-turn assistant record used for reload or provider replay.

- [ ] **Step 5: Run Task 6 GREEN tests and commit exact files**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_model_context.py tests/test_chat_graph.py tests/test_webapp_model_data_disclosure.py tests/test_webapp_chat_tools.py
git diff --check -- src/rnaseq_agent/model_context.py src/rnaseq_agent/chat_graph.py src/rnaseq_agent/webapp.py src/rnaseq_agent/threads.py tests/test_model_context.py tests/test_chat_graph.py tests/test_webapp_model_data_disclosure.py tests/test_webapp_chat_tools.py
```

Expected: all selected tests pass; legacy application content is absent from new logical state/provider requests while byte-identical historic artifacts remain allowed; raw tool-call arguments are projected with valid protocol pairing; every exact provider reply is transient and the only new durable reply is the fixed local acknowledgement; user-authored text remains unchanged.

```powershell
git add -- src/rnaseq_agent/model_context.py src/rnaseq_agent/chat_graph.py src/rnaseq_agent/webapp.py src/rnaseq_agent/threads.py tests/test_model_context.py tests/test_chat_graph.py tests/test_webapp_model_data_disclosure.py tests/test_webapp_chat_tools.py
git commit -m "fix: keep exact model data out of durable chat"
```

---

### Task 7: Web SSE confirmation cards and local/external data separation

**Files:**
- Modify: `src/rnaseq_agent/chat_graph.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `src/rnaseq_agent/remote_browse.py`
- Modify: `src/rnaseq_agent/webtemplates/chat.html`
- Modify: `tests/test_webapp_model_data_disclosure.py`
- Modify: `tests/test_webapp_chat_tools.py`
- Modify: `tests/test_webapp_chat_stream.py`
- Modify: `tests/test_remote_browse.py`

**Interfaces:**
- Consumes the `type="model_data_confirmation"` card and Task 6 `display_reply`/`durable_reply` split.
- Reuses the approved-roots request-local exact-result sink injected into `build_chat_graph`; the sink emits a transient SSE event and never writes graph state.
- Produces SSE event `local_result` with payload `{"tool": str, "result": dict[str, Any]}` for browser-only exact results.
- Reuses SSE event `confirm`; `renderConfirmCard(payload)` branches on `payload.type`.
- Preserves resume body `{"project_id", "thread_id", "approval_id", "approved", "note"}` and the `/api/chat/resume` endpoint.

- [ ] **Step 1: Write RED tests for card copy, values, and the transient local-result channel**

Extend `tests/test_webapp_chat_tools.py` with static template assertions and `tests/test_webapp_model_data_disclosure.py` with endpoint behavior:

```python
def test_data_confirmation_card_names_fields_and_never_embeds_values(client, token) -> None:
    page = client.get("/chat", headers={"x-session-token": token}).text
    card = page.split("function renderConfirmCard")[1].split("async function resolveConfirm")[0]
    assert 'payload.type === "model_data_confirmation"' in card
    assert "允许发送一次" in card
    assert "不发送" in card
    assert "field.count" in card
    assert "模型请求精确项目数据" in card
    assert "payload.purpose" not in card
    assert "payload.values" not in card
    assert "sample_id" not in card
    assert "fastq_1" not in card


def test_local_browse_rows_reach_browser_but_provider_gets_counts_only(
    client, token, fake_llm, seeded_project
) -> None:
    events = _stream(client, token, "浏览服务器目录并总结", project=seeded_project)
    local = next(event["data"] for event in events if event["event"] == "local_result")
    assert local["tool"] == "browse_remote_samples"
    assert local["result"]["groups"][0]["samples"][0]["fastq_1"] == "TUMOR_SENTINEL_R1.fastq.gz"
    provider_body = json.dumps(fake_llm.seen_messages, ensure_ascii=False)
    assert "TUMOR_SENTINEL_R1.fastq.gz" not in provider_body
    assert '"sample_count"' in provider_body
    assert '"source_ref"' in provider_body
```

Add a browser card payload test proving field labels/counts/purpose are present but `PATIENT_SENTINEL_73`, FASTQ, path, report, provider key, and grant raw exact values are absent. Add a resume test proving a stale/replaced card cannot approve a newer or different grant id.

Add an integration test where `browse_remote_samples` returns model projection `{sample_count, unmatched_count, directory_count, truncated, source_ref}` and transient `local_result` exact groups. Script the next model turn to request `remote_paths` with that `source_ref`, approve it, and assert every canonical group directory from that bounded scan occurs in one provider request while filenames and directories from another scan remain absent. The confirmation card asserts aggregate directory/file counts only. Grant, checkpoint, thread, and generic-log files contain no application-injected exact directory or basename.

- [ ] **Step 2: Run the Task 7 RED tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_webapp_model_data_disclosure.py tests/test_webapp_chat_tools.py tests/test_webapp_chat_stream.py -k "data_confirmation_card or local_browse_rows or local_result or stale_card"
```

Expected: the current template renders only write/execute calls, and full local tool results have no transient SSE route separated from graph state.

- [ ] **Step 3: Emit exact local results without adding them to durable state**

Use the approved-roots request-local sink and `_finalize_tool_execution_result` callback already present in `build_chat_graph`. In `node_execute`, pass `_run_tool`'s wrapper to that finalizer exactly once before reading any channel. Only after it returns with `security_audit is None` may the graph send `(name, result.local)` to the allowlisted browser sink, append `result.model` to provider history, or append `result.log_projection` to generic state. Disclosure code never writes `security_audit`, History, or the authoritative audit directly. The web driver converts the finalized request-local value to:

```text
event: local_result
data: {"tool":"browse_remote_samples","result":{"ok":true,"source_ref":"src_0123456789abcdef0123456789abcdef","groups":[{"group_id":"group_0123456789abcdef","canonical_directory":"/approved/run-73","samples":[{"sample_id":"PATIENT_SENTINEL_73","fastq_1":"TUMOR_SENTINEL_R1.fastq.gz","fastq_2":"TUMOR_SENTINEL_R2.fastq.gz"}],"unmatched_basenames":[]}]}}
```

Do not include local results in the `done` payload, `outcome`, `tool_log`, confirmation card, thread references, or application logger. If no active browser stream exists, discard the local result after deterministic handling. Allowlist only `browse_remote_samples` and current deterministic upload/preview tools that already return local rows; an unknown tool never gets an out-of-band exact channel.

- [ ] **Step 4: Render a separate data-disclosure confirmation card**

In `renderConfirmCard`, branch before write/execute row rendering:

```javascript
if (payload.type === "model_data_confirmation") {
  const fields = Array.isArray(payload.fields) ? payload.fields : [];
  const rows = fields.map(field => `
    <div class="call data-field">
      <span class="name">${escapeHtml(field.label || field.name || "")}</span>
      <span class="risk data">${escapeHtml(String(field.count || 0))} 项</span>
    </div>`).join("");
  bubble.innerHTML = `
    <div class="confirm-card data-confirm" id="liveConfirm">
      <div class="head">${escapeHtml(payload.message || "确认向模型发送项目数据")}</div>
      ${rows}
      <div class="desc">用途：模型请求精确项目数据</div>
      <textarea class="note" id="liveConfirmNote" placeholder="可选：记录你的决定说明"></textarea>
      <div class="acts">
        <button class="approve" id="liveConfirmApprove">允许发送一次</button>
        <button class="reject" id="liveConfirmReject">不发送</button>
      </div>
    </div>`;
} else {
  // Keep the existing write/execute card branch unchanged.
}
```

Bind both branches to `resolveConfirm(true|false, approvalId)`. Render labels from a fixed browser mapping for `sample_ids`, `fastq_filenames`, `remote_paths`, and `report_excerpt`; unknown fields use a generic `项目数据` label and cannot be approved server-side. Never render a values property. Add `handleEvent` support for `local_result` and a compact exact local-result table in the current workbench turn; use `textContent`/`escapeHtml` for every cell. Browser state may retain `source_ref` only as an opaque convenience value to request a card. It never selects groups or supplies authority; the server reloads and validates the entire project/thread-bound scan.

- [ ] **Step 5: Run Task 7 GREEN tests and commit exact files**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_webapp_model_data_disclosure.py tests/test_webapp_chat_tools.py tests/test_webapp_chat_stream.py tests/test_remote_browse.py
git diff --check -- src/rnaseq_agent/chat_graph.py src/rnaseq_agent/webapp.py src/rnaseq_agent/remote_browse.py src/rnaseq_agent/webtemplates/chat.html tests/test_webapp_model_data_disclosure.py tests/test_webapp_chat_tools.py tests/test_webapp_chat_stream.py tests/test_remote_browse.py
```

Expected: all selected tests pass; the browser receives exact local rows only through `local_result`; model captures contain counts; the data card contains categories/counts/purpose and no values.

```powershell
git add -- src/rnaseq_agent/chat_graph.py src/rnaseq_agent/webapp.py src/rnaseq_agent/remote_browse.py src/rnaseq_agent/webtemplates/chat.html tests/test_webapp_model_data_disclosure.py tests/test_webapp_chat_tools.py tests/test_webapp_chat_stream.py tests/test_remote_browse.py
git commit -m "feat: add model data confirmation cards"
```

---

### Task 8: CLI, GUI, settings, legacy chat, and connection-test gateway migration

**Files:**
- Modify: `src/rnaseq_agent/model_provider.py`
- Modify: `src/rnaseq_agent/llm.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `src/rnaseq_agent/connection_store.py`
- Modify: `src/rnaseq_agent/gui.py`
- Modify: `tests/test_llm.py`
- Modify: `tests/test_webapp.py`
- Modify: `tests/test_connection_store.py`
- Modify: `tests/test_chat_llm.py`

**Interfaces:**
- Consumes normalized provider/config types and all `ModelProviderGateway` methods from Tasks 1 and 3.
- Preserves `OpenAICompatibleClient.decide(user_text: str, *, has_project: bool) -> LLMDecision`.
- Preserves `CodexCLIClient.decide(user_text: str, *, has_project: bool) -> LLMDecision`.
- Produces `OpenAICompatibleClient.provider_config -> ProviderConfig` and `CodexCLIClient.provider_config -> ProviderConfig` properties.
- Produces `context_free_prepared_request(*, config: ProviderConfig, credentials: ProviderCredentials, context: ModelContext, timeout_seconds: float, stream: bool = False, response_schema: Mapping[str, Any] | None = None, codex_executable: Path | None = None, codex_home: Path | None = None) -> PreparedModelRequest`.
- Preserves `load_llm_client_from_env()` and existing GUI/CLI call signatures.
- Routes legacy `webapp._llm_reply_or_none`, `webapp._llm_stream_chunks`, and `api_test_llm` through the gateway with no grant.

- [ ] **Step 1: Write RED tests for every remaining generative entry path**

Update `tests/test_llm.py` so clients receive an injected fake gateway and assert the gateway sees prepared requests for both OpenAI API modes and Codex CLI:

```python
def test_openai_decide_uses_gateway_for_responses() -> None:
    gateway = FakeGateway(reply=ProviderReply(
        text='{"action":"chat","message":"ok","path":""}', raw={}
    ))
    client = OpenAICompatibleClient(
        base_url="https://llm.example/v1", model="gpt-test",
        api_key="API_KEY_SENTINEL_73", api_mode="responses", gateway=gateway,
    )
    assert client.decide("你好", has_project=False).message == "ok"
    request = gateway.requests[0]
    assert request.api_mode == "responses"
    assert request.payload["store"] is False
    assert request.credentials.api_key == "API_KEY_SENTINEL_73"
    assert "API_KEY_SENTINEL_73" not in json.dumps(request.payload)


def test_codex_decide_uses_gateway_and_scans_prompt() -> None:
    gateway = FakeGateway(reply=ProviderReply(
        text='{"action":"summary","message":"ok","path":""}', raw={}
    ))
    client = CodexCLIClient(model="gpt-test", executable=Path("codex"), gateway=gateway)
    assert client.decide("总结项目", has_project=True).action == "summary"
    assert gateway.requests[0].api_mode == "codex_cli"
    assert "总结项目" in gateway.requests[0].prompt
```

Update `tests/test_webapp.py` to capture gateway requests from legacy `/api/chat` and `/api/test-llm`; update `tests/test_chat_llm.py` to prove CLI `ChatSession` retains its behavior through the client gateway; update the connection-store test to prove protected API-key ciphertext and protected SSH-password ciphertext are included in `ProviderCredentials.protected_values` for scanning but never in payload/identity/log output. Add GUI connection-test coverage at the nearest callable worker boundary and assert it calls `client.decide`, which is now gateway-backed.

Add real `create_app` endpoint tests, rather than stopping at scanner-unit helpers. Seed distinct sentinels for the configured LLM API key, current session token, decrypted SSH password already loaded for the request, protected API-key ciphertext, protected password ciphertext, and application tokens such as CSRF/bearer tokens. For each sentinel, arrange for the endpoint's otherwise valid outbound user/context text to contain it, call the real chat or test-LLM endpoint, and assert `MODEL_CONTEXT_SECRET_DETECTED`, zero fake-gateway transport entries, and no raw sentinel in the HTTP/SSE error, application log, generic tool log, checkpoint, or thread JSON. These tests prove `create_app` assembles the scanner inputs; direct construction of `ProviderCredentials` in the test does not satisfy this requirement.

- [ ] **Step 2: Run the Task 8 RED tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_llm.py tests/test_webapp.py tests/test_connection_store.py tests/test_chat_llm.py -k "gateway or responses or codex or test_llm or legacy_chat"
```

Expected: clients perform direct `urllib.request.urlopen` or `subprocess.run`, and `/api/test-llm` performs a direct `requests.post`.

- [ ] **Step 3: Migrate API and Codex clients without changing callers**

Add optional `gateway: ModelProviderGateway | None = None` to both client constructors and store `self.gateway = gateway or ModelProviderGateway()`. Each `decide` first calls `ModelContextBuilder.build(project_dir=None, project_id="", thread_id="", provider=self.provider_config, system_prompt=_router_system_prompt(has_project), current_user_message=user_text, durable_messages=(), claimed_grant=None)`. `OpenAICompatibleClient.decide` passes that context to `context_free_prepared_request`, calls `gateway.responses` or `gateway.complete`, and passes `reply.text` to existing parsers. `CodexCLIClient.decide` adds the fixed no-tools instruction to its system prompt, passes the built context and schema/executable/home to the same constructor, calls `gateway.codex_exec`, and passes `reply.text` to `parse_decision`.

Implement the request constructor in `model_provider.py` so all callers receive the same identity check and field defaults:

```python
def context_free_prepared_request(
    *,
    config: ProviderConfig,
    credentials: ProviderCredentials,
    context: ModelContext,
    timeout_seconds: float,
    stream: bool = False,
    response_schema: Mapping[str, Any] | None = None,
    codex_executable: Path | None = None,
    codex_home: Path | None = None,
) -> PreparedModelRequest:
    if config.api_mode == "responses":
        system_parts = [str(item["content"]) for item in context.messages if item["role"] == "system"]
        input_parts = [dict(item) for item in context.messages if item["role"] != "system"]
        payload: Mapping[str, Any] = {
            "model": config.model,
            "instructions": "\n".join(system_parts),
            "input": input_parts,
            "store": False,
        }
        prompt = ""
    elif config.api_mode == "codex_cli":
        payload = {}
        prompt = "\n".join(f"{item['role']}: {item['content']}" for item in context.messages)
    else:
        payload = {"model": config.model, "messages": list(context.messages)}
        prompt = ""
    return PreparedModelRequest(
        provider=config,
        identity=provider_identity(config),
        api_mode=config.api_mode,
        payload=dict(payload),
        credentials=credentials,
        timeout_seconds=timeout_seconds,
        stream=stream,
        prompt=prompt,
        response_schema=response_schema,
        codex_executable=codex_executable,
        codex_home=codex_home,
        disclosure_manifest=None,
    )
```

Delete the generative `_post_json` path and Codex `subprocess.run` from `llm.py`. Keep `_get_json` only as a call to `gateway.list_models`; preserve friendly model-list behavior without logging key/header values. Codex login status/device-login subprocesses remain in `llm.py` because they are authentication operations and carry no model prompt.

`load_llm_client_from_env`, `ChatSession`, and GUI workers keep their public call shape. Ensure the GUI connection test uses `decide` and never constructs a provider POST itself. A context-free CLI/GUI request contains only router system prompt plus user-authored text; it must not discover or serialize a project path/config.

- [ ] **Step 4: Migrate web settings/test and collect scanner secrets safely**

Route `api_test_llm` through `ModelContextBuilder.build(project_dir=None, project_id="", thread_id="", provider=config, system_prompt="你是连接测试助手。", current_user_message="请只回复：连接正常", durable_messages=(), claimed_grant=None)`, then through `context_free_prepared_request` and `gateway.complete`. It never accepts a data grant or project context. Route legacy `/api/chat` and stream fallback through the same no-grant builder/gateway path from Task 3.

Add a connection-store helper:

```python
def provider_credentials_from_connection(
    decrypted_llm: Mapping[str, Any],
    raw_connection: Mapping[str, Any],
    *,
    runtime_secrets: Sequence[str] = (),
) -> ProviderCredentials:
    server = raw_connection.get("server") if isinstance(raw_connection.get("server"), dict) else {}
    llm = raw_connection.get("llm") if isinstance(raw_connection.get("llm"), dict) else {}
    return ProviderCredentials(
        api_key=str(decrypted_llm.get("api_key") or ""),
        known_secrets=tuple(value for value in (
            str(decrypted_llm.get("api_key") or ""),
            *[str(item) for item in runtime_secrets],
        ) if value),
        protected_values=tuple(value for value in (
            str(llm.get("api_key_protected") or ""),
            str(server.get("password_protected") or ""),
        ) if value),
    )
```

At request construction, `create_app` always supplies its current random session token and all known application tokens in `runtime_secrets`. It also supplies the decrypted SSH password when already available in request memory. `credentials.api_key` is always scanned separately by Task 3, so callers cannot accidentally omit it by passing an empty runtime list. Include stored protected API-key ciphertext and protected SSH-password ciphertext in `protected_values`. Do not decrypt credentials solely to enrich scanning. Never expose this object through route responses, repr, logs, or graph state.

- [ ] **Step 5: Run Task 8 GREEN tests and commit exact files**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_llm.py tests/test_webapp.py tests/test_connection_store.py tests/test_chat_llm.py
git diff --check -- src/rnaseq_agent/model_provider.py src/rnaseq_agent/llm.py src/rnaseq_agent/webapp.py src/rnaseq_agent/connection_store.py src/rnaseq_agent/gui.py tests/test_llm.py tests/test_webapp.py tests/test_connection_store.py tests/test_chat_llm.py
```

Expected: all selected tests pass; chat-completions, Responses, Codex CLI, legacy chat, stream fallback, GUI/CLI router, and test-LLM sends are observed at the common gateway.

```powershell
git add -- src/rnaseq_agent/model_provider.py src/rnaseq_agent/llm.py src/rnaseq_agent/webapp.py src/rnaseq_agent/connection_store.py src/rnaseq_agent/gui.py tests/test_llm.py tests/test_webapp.py tests/test_connection_store.py tests/test_chat_llm.py
git commit -m "refactor: route all model clients through gateway"
```

---

### Task 9: Full disclosure acceptance suite and static provider-boundary inventory

**Files:**
- Create: `tests/test_provider_boundary_static.py`
- Modify: `src/rnaseq_agent/model_disclosure.py`
- Modify: `src/rnaseq_agent/model_provider.py`
- Modify: `src/rnaseq_agent/model_context.py`
- Modify: `src/rnaseq_agent/model_data_grants.py`
- Modify: `src/rnaseq_agent/remote_browse.py`
- Modify: `src/rnaseq_agent/remote_scan_store.py`
- Modify: `src/rnaseq_agent/agent_tools.py`
- Modify: `src/rnaseq_agent/chat_graph.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Modify: `src/rnaseq_agent/connection_store.py`
- Modify: `src/rnaseq_agent/llm.py`
- Modify: `src/rnaseq_agent/gui.py`
- Modify: `src/rnaseq_agent/threads.py`
- Modify: `src/rnaseq_agent/webtemplates/chat.html`
- Modify: `tests/test_webapp_model_data_disclosure.py`
- Modify: `tests/test_model_provider_gateway.py`
- Modify: `tests/test_model_data_grants.py`
- Modify: `tests/test_model_context.py`
- Modify: `tests/test_remote_browse.py`
- Modify: `tests/test_connection_store.py`
- Modify: `tests/test_chat_graph.py`

**Interfaces:**
- Consumes every public interface from Tasks 1-8.
- Produces no new production interface.
- Produces a static invariant over every recursive `src/rnaseq_agent/**/*.py` module: HTTP calls through `requests`, `requests.Session`, `urllib`, or `httpx`, and process calls through `subprocess.run/Popen/check_output/check_call`, are either the named gateway implementation or an exact reviewed non-generative allowlist entry.
- Produces a static audit-ownership invariant: `_record_browse_audit` is called only by the approved-roots `_finalize_tool_execution_result`; disclosure modules never call that writer or `append_history`, and `node_execute` has one finalizer call before any result-channel read.
- Produces an end-to-end invariant: an approved exact sentinel occurs in exactly one captured provider request and creates zero durable application-added copies; byte-identical pre-existing remote-scan sources and unchanged user-authored text are provenance-checked exceptions.

- [ ] **Step 1: Add the self-testing recursive provider inventory acceptance gate**

Create `tests/test_provider_boundary_static.py` using `ast`, recursive package traversal, import-alias resolution, and exact function ownership:

```python
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src" / "rnaseq_agent"
GATEWAY = "model_provider.py"

@dataclass(frozen=True)
class TransportCall:
    path: str
    function: str
    lineno: int
    family: str
    spelling: str


ALLOWED_NON_GENERATIVE = {
    ("bkbio_eval_adapter.py", "_git_commit", "subprocess.run"),
    ("execution.py", "run_command", "subprocess.run"),
    ("execution.py", "run_command_bounded", "subprocess.Popen"),
    ("llm.py", "codex_login_status", "subprocess.run"),
    ("llm.py", "launch_codex_device_login", "subprocess.Popen"),
}

ALLOWED_GATEWAY = {
    (GATEWAY, "ModelProviderGateway.complete"),
    (GATEWAY, "ModelProviderGateway.stream"),
    (GATEWAY, "ModelProviderGateway.responses"),
    (GATEWAY, "ModelProviderGateway.codex_exec"),
    (GATEWAY, "ModelProviderGateway.dispatch_exact"),
    (GATEWAY, "ModelProviderGateway.list_models"),
}


class TransportVisitor(ast.NodeVisitor):
    HTTP = {
        "requests.get", "requests.post", "requests.request",
        "requests.Session().get", "requests.Session().post", "requests.Session().request",
        "httpx.get", "httpx.post", "httpx.request",
        "httpx.Client().get", "httpx.Client().post", "httpx.Client().request",
        "httpx.AsyncClient().get", "httpx.AsyncClient().post", "httpx.AsyncClient().request",
        "urllib.request.urlopen",
    }
    PROCESS = {
        "subprocess.run", "subprocess.Popen",
        "subprocess.check_output", "subprocess.check_call",
    }

    def __init__(self, path: str) -> None:
        self.path = path
        self.aliases: dict[str, str] = {}
        self.instances: dict[str, str] = {}
        self.scope: list[str] = []
        self.calls: list[TransportCall] = []

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            if item.asname:
                self.aliases[item.asname] = item.name
            else:
                root = item.name.split(".")[0]
                self.aliases[root] = root

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for item in node.names:
            self.aliases[item.asname or item.name] = f"{module}.{item.name}".strip(".")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node: ast.Assign) -> None:
        created = self._resolve(node.value)
        if created in {"requests.Session()", "httpx.Client()", "httpx.AsyncClient()"}:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.instances[target.id] = created
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        spelling = self._resolve(node.func)
        family = "http" if spelling in self.HTTP else "process" if spelling in self.PROCESS else ""
        if family:
            self.calls.append(TransportCall(
                path=self.path,
                function=".".join(self.scope) or "<module>",
                lineno=node.lineno,
                family=family,
                spelling=spelling,
            ))
        self.generic_visit(node)

    def _resolve(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return self.instances.get(node.id, self.aliases.get(node.id, node.id))
        if isinstance(node, ast.Attribute):
            return f"{self._resolve(node.value)}.{node.attr}"
        if isinstance(node, ast.Call):
            return self._resolve(node.func) + "()"
        return ""


def scan_source(source: str, *, path: str) -> tuple[TransportCall, ...]:
    tree = ast.parse(source, filename=path)
    visitor = TransportVisitor(path)
    visitor.visit(tree)
    return tuple(visitor.calls)


def scan_package() -> tuple[TransportCall, ...]:
    return tuple(
        call
        for path in sorted(SRC.rglob("*.py"))
        for call in scan_source(path.read_text(encoding="utf-8"), path=str(path.relative_to(SRC)))
    )


def test_all_transport_calls_have_an_exact_owner() -> None:
    violations = []
    for call in scan_package():
        owner = (call.path, call.function)
        allowed_non_gen = (call.path, call.function, call.spelling) in ALLOWED_NON_GENERATIVE
        if owner not in ALLOWED_GATEWAY and not allowed_non_gen:
            violations.append(call)
    assert violations == []


def test_known_generative_entry_paths_reference_gateway() -> None:
    required = {
        "chat_graph.py": ("ModelContextBuilder", "ModelProviderGateway", "PreparedModelRequest"),
        "webapp.py": ("ModelContextBuilder", "ModelProviderGateway", "context_free_prepared_request"),
        "llm.py": ("ModelContextBuilder", "ModelProviderGateway", "context_free_prepared_request"),
    }
    for name, symbols in required.items():
        source = (SRC / name).read_text(encoding="utf-8")
        assert all(symbol in source for symbol in symbols), (name, symbols)


@pytest.mark.parametrize("source", [
    "import requests as rq\ndef leak(): rq.request('POST', 'https://x')",
    "from requests import post as send\ndef leak(): send('https://x')",
    "import requests\ndef leak(): requests.Session().post('https://x')",
    "import requests\ndef leak():\n s = requests.Session()\n s.get('https://x')",
    "import httpx as hx\ndef leak(): hx.Client().request('POST', 'https://x')",
    "import httpx\ndef leak():\n c = httpx.AsyncClient()\n c.post('https://x')",
    "import urllib.request\ndef leak(): urllib.request.urlopen('https://x')",
    "from urllib.request import urlopen as open_url\ndef leak(): open_url('https://x')",
    "import subprocess\ndef leak(): subprocess.Popen(['codex', 'exec'])",
    "import subprocess as sp\ndef leak(): sp.check_output(['codex', 'exec'])",
    "from subprocess import check_call as invoke\ndef leak(): invoke(['codex', 'exec'])",
])
def test_detector_rejects_synthetic_aliases_and_alternate_transports(source: str) -> None:
    assert scan_source(source, path="synthetic.py")
```

`TransportVisitor` maintains the nested class/function qualname, resolves `import` and `from ... import ... as ...` aliases, tracks variables assigned from `requests.Session()` and `httpx.Client()/AsyncClient()`, and recognizes `requests.get/post/request`, session/client method calls, `urllib.request.urlopen`, direct imported aliases, and all four subprocess constructors above. It ignores type annotations and route decorators. The allowlist is an exact `(relative path, qualified function, resolved spelling)` set; no line-only, directory-wide, or substring exemption is allowed. Gateway calls must live in one of the named methods. Synthetic tests prove the detector fails independently of current product correctness.

Add `test_security_audit_has_one_authoritative_writer` to the same file. Parse calls and qualified scopes rather than matching comments or strings. Assert the only call to `_record_browse_audit` is inside `webapp._finalize_tool_execution_result`; assert `model_context.py`, `model_data_grants.py`, `agent_tools.py`, `chat_graph.py`, and `remote_browse.py` contain no direct call to `_record_browse_audit` or `append_history`; and assert `chat_graph.node_execute` calls the injected finalizer exactly once before its first `local`, `model`, or `log_projection` attribute read. Task 2's runtime tests remain responsible for proving one finalization per execution and `security_audit is None` afterward.

- [ ] **Step 2: Add the end-to-end sentinel and failure-matrix acceptance tests**

Add a parameterized end-to-end matrix covering all accepted design requirements:

```python
@pytest.mark.parametrize("field,present,absent", [
    ("sample_ids", "PATIENT_SENTINEL_73", ("TUMOR_SENTINEL_R1.fastq.gz", "/restricted/SENTINEL_73/fastq")),
    ("fastq_filenames", "TUMOR_SENTINEL_R1.fastq.gz", ("PATIENT_SENTINEL_73", "/restricted/SENTINEL_73/fastq")),
    ("report_excerpt", "REPORT_SENTINEL_73", ("PATIENT_SENTINEL_73", "TUMOR_SENTINEL_R1.fastq.gz")),
])
def test_approved_field_occurs_in_one_request_and_no_durable_store(
    disclosure_harness, field, present, absent
) -> None:
    result = disclosure_harness.approve_and_send(field)
    matching = [body for body in result.provider_payloads if present in body]
    assert len(matching) == 1
    assert all(value not in matching[0] for value in absent)
    result.assert_no_application_injected_value(present)


@pytest.mark.parametrize("field,present_values,absent", [
    ("remote_paths", ("/approved/SENTINEL_73/fastq", "/approved/SECOND_GROUP"), ("TUMOR_SENTINEL_R1.fastq.gz", "/approved/OTHER_SCAN")),
    ("fastq_filenames", ("TUMOR_SENTINEL_R1.fastq.gz", "SECOND_GROUP_R1.fastq.gz"), ("/approved/SENTINEL_73/fastq", "OTHER_SCAN_R1.fastq.gz")),
])
def test_approved_remote_field_covers_all_groups_in_one_bound_scan(
    disclosure_harness, field, present_values, absent
) -> None:
    result = disclosure_harness.approve_and_send(
        field,
        source_ref=disclosure_harness.remote_source_ref,
    )
    matching = [
        body for body in result.provider_payloads
        if all(value in body for value in present_values)
    ]
    assert len(matching) == 1
    assert all(value not in matching[0] for value in absent)
    for value in present_values:
        result.assert_no_application_injected_value(value)
```

Add a second matrix for rejected, expired, already consumed, cross-project, cross-thread, stored remote `thread_id=None`, provider changed, endpoint-origin changed, endpoint-path-only config revision changed, model changed, tool mode disabled, project revision changed, relevant data revision changed, unsupported remote path, secret detected, context/preparation/serialization failure, pre-transport cancellation, timeout after send, stream-creation failure, stream-iteration failure, `GeneratorExit`, client disconnect, unknown exception, forbidden provider tool call, and graph fallback. Assert provider call counts, terminal grant status/error code, fallback count, executor count, and durable application-injected sentinel absence for every row. Every claimed row must finish `consumed_success`, `consumed_failed`, or `consumed_ambiguous`; query all grant records and assert none remains `transmitting`. Add provider/base/model and tool-mode mutation races plus root-revoke/send races for both lock winners. When a mutation wins the connection lock, assert the required failed terminal state, zero `prepare` calls, and zero transport calls; when claim wins, assert request configuration/credentials/mode all came from its locked snapshot and transport creation preceded mutation. Add one case proving structured workbench write/execute gates and remote-root checks behave exactly as before without a data grant.

Define the test harness in the same file rather than relying on an external fixture. It must expose this exact result shape and drive the real stream/resume helpers:

```python
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class DisclosureRun:
    provider_payloads: tuple[str, ...]
    durable_paths: tuple[Path, ...]
    events: tuple[dict[str, Any], ...]
    user_authored_values: tuple[str, ...]
    allowed_source_bytes: Mapping[Path, bytes]
    logical_messages: tuple[dict[str, Any], ...]

    def assert_no_application_injected_value(self, value: str) -> None:
        marker = value.encode("utf-8")
        for path in self.durable_paths:
            current = path.read_bytes()
            if marker not in current:
                continue
            assert self.allowed_source_bytes.get(path) == current, path

    def assert_user_authored_value_preserved(self, value: str) -> None:
        assert value in self.user_authored_values
        containing = [message for message in self.logical_messages if value in str(message.get("content", ""))]
        assert containing
        assert all(message.get("role") == "user" for message in containing)


class DisclosureHarness:
    def __init__(self, client, token: str, project_id: str, fake_gateway, remote_source_ref: str) -> None:
        self.client = client
        self.token = token
        self.project_id = project_id
        self.fake_gateway = fake_gateway
        self.remote_source_ref = remote_source_ref

    def approve_and_send(self, field: str, source_ref: str | None = None) -> DisclosureRun:
        project_dir = self.fake_gateway.project_root / self.project_id
        source_paths = tuple(sorted(
            list((project_dir / ".remote-scans").glob("scan-*.record"))
            + list((project_dir / ".remote-scans").glob("source-*.ref"))
        ))
        allowed_source_bytes = {path: path.read_bytes() for path in source_paths}
        arguments: dict[str, Any] = {"fields": [field], "purpose": "验收单字段披露"}
        if source_ref is not None:
            arguments["source_ref"] = source_ref
        self.fake_gateway.queue_tool_call(
            "request_exact_project_data",
            arguments,
        )
        requested = _stream(
            self.client, self.token, "请核对项目数据 USER_AUTHORED_SENTINEL_73",
            project=self.project_id, thread_id="main",
        )
        card = _confirm_event(requested)
        assert card is not None
        self.fake_gateway.queue_text("已完成核对")
        resumed = _resume(
            self.client,
            self.token,
            project=self.project_id,
            approval_id=card["approval_id"],
            approved=True,
        )
        durable = [
            project_dir / "langgraph.sqlite3",
            project_dir / "langgraph.sqlite3-wal",
            project_dir / "langgraph.sqlite3-shm",
            project_dir / "threads" / "main.json",
            project_dir / "tool_log.jsonl",
        ]
        durable.extend(sorted((project_dir / ".model_data_grants").glob("*.json")))
        durable.extend(sorted((project_dir / ".model_data_grants").glob("*.tmp")))
        durable.extend(sorted((project_dir / ".remote-scans").glob("scan-*.record")))
        durable.extend(sorted((project_dir / ".remote-scans").glob("source-*.ref")))
        durable.extend(sorted((project_dir / ".remote-scans").glob("*.tmp")))
        durable.extend(self.fake_gateway.application_log_paths)
        return DisclosureRun(
            provider_payloads=tuple(self.fake_gateway.serialized_payloads),
            durable_paths=tuple(path for path in durable if path.exists()),
            events=tuple(requested + resumed),
            user_authored_values=("USER_AUTHORED_SENTINEL_73",),
            allowed_source_bytes=allowed_source_bytes,
            logical_messages=tuple(read_logical_thread_messages(project_dir, "main")),
        )


@pytest.fixture
def disclosure_harness(
    client, token, seeded_project, fake_gateway, approved_scan_store
) -> DisclosureHarness:
    return DisclosureHarness(
        client,
        token,
        seeded_project,
        fake_gateway,
        approved_scan_store.source_ref,
    )
```

`fake_gateway.queue_tool_call`, `queue_text`, `serialized_payloads`, and `project_root` are implemented in the Task 5 endpoint test fake; they store serialized outbound request bodies only in test memory. The fixture names `token` and `seeded_project` are defined in `tests/test_webapp_model_data_disclosure.py` using the existing `_token`, `_configure_llm`, and `_seed_project` helpers.

The harness must scan every SQLite database plus its `-wal` and `-shm` siblings, temporary and final grant/scan files, thread JSON, generic tool logs, and captured application logs. Force a WAL checkpoint boundary both before and after the send so stale pages are inspected. Snapshot the pre-existing approved remote-scan source files before the grant; an exact remote value may remain only in a byte-identical pre-existing source file, and the grant may create no new copy or change that file. Seed `USER_AUTHORED_SENTINEL_73` only in the user's own message and call `assert_user_authored_value_preserved` through the logical thread/checkpoint reader to prove it remains attributed to the user unchanged. Application/grant-generated sample, FASTQ, path, report, credential, and malicious-tool sentinels must otherwise be absent from every durable/log artifact. Raw-byte scans may allow the explicitly seeded user-authored marker and byte-identical pre-existing source records; neither exception applies to an application-added occurrence.

- [ ] **Step 3: Run the focused acceptance suite and fix only observed defects**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_provider_boundary_static.py tests/test_model_context.py tests/test_model_data_grants.py tests/test_model_provider_gateway.py tests/test_webapp_model_data_disclosure.py tests/test_remote_browse.py
```

Expected: after correct completion of Tasks 1-8, all focused acceptance tests pass on their first run. If a row exposes an observed defect, correct the owning Task 1-8 module and preserve the failing case as a regression. The synthetic detector cases must fail detection internally while the test itself passes; Task 9 does not claim a reproducible product RED phase.

- [ ] **Step 4: Run all regression and build checks**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
rg -n "requests\.post|http_requests\.post|urllib\.request\.urlopen|subprocess\.run" src/rnaseq_agent
```

Expected: full pytest passes; compileall exits 0; diff check is clean; search output shows generative POST/Responses/Codex execution only in `model_provider.py`, with unrelated execution/SSH and Codex login subprocesses clearly distinguishable. Inspect each search match instead of relying on count alone.

- [ ] **Step 5: Commit the acceptance tests and any directly proven corrections**

Stage the acceptance files and the complete owned production boundary; unchanged paths add nothing to the index:

```powershell
git add -- src/rnaseq_agent/model_disclosure.py src/rnaseq_agent/model_provider.py src/rnaseq_agent/model_context.py src/rnaseq_agent/model_data_grants.py src/rnaseq_agent/remote_browse.py src/rnaseq_agent/remote_scan_store.py src/rnaseq_agent/agent_tools.py src/rnaseq_agent/chat_graph.py src/rnaseq_agent/webapp.py src/rnaseq_agent/connection_store.py src/rnaseq_agent/llm.py src/rnaseq_agent/gui.py src/rnaseq_agent/threads.py src/rnaseq_agent/webtemplates/chat.html tests/test_provider_boundary_static.py tests/test_webapp_model_data_disclosure.py tests/test_model_provider_gateway.py tests/test_model_data_grants.py tests/test_model_context.py tests/test_remote_browse.py tests/test_connection_store.py tests/test_chat_graph.py
git commit -m "test: enforce model data disclosure boundary"
```

If Step 3 required a production correction, state the failing test name in the commit body. Never stage unrelated work with `git add .` or `git add -A`.

## Final Coverage Checklist

- [ ] Default model context exposes aliases, counts, conditions, scientific gates, and artifact presence while excluding source identifiers, filenames, paths, host/user/port/job/workdir data, report bodies, commands, output, environment data, and secrets.
- [ ] Exact scopes are separate; approval of one field never widens to another.
- [ ] Confirmation cards contain field categories, counts, purpose, expiry, and single-use language without raw values.
- [ ] Grant records are durable metadata only, expire after 600 seconds, are single-use across processes, and bind project/thread/provider/tool mode/revisions/policy.
- [ ] `remote_paths` and live remote-scan filenames require the approved-roots server-owned whole-scan `source_ref`; they bind exact project/non-null thread, SSH identity/root, source/result/policy revisions, and expiry, disclose all groups from that bounded scan, and return `MODEL_DATA_SCOPE_UNSUPPORTED` when any dependency or binding is unavailable.
- [ ] Application/grant-injected exact values appear in one in-memory provider request, may appear in the current browser stream, and appear nowhere durable; user-authored messages remain unchanged and are tested as a distinct provenance case.
- [ ] Rejection, expiry, replay, binding drift, revision drift, disabled tool mode, unsupported scope, and secret detection send no request.
- [ ] Exact requests expose no tools or tool choice; provider-returned tool calls are rejected before persistence, display, or execution.
- [ ] `ModelProviderGateway.dispatch_exact` is the sole exact-send dispatcher: chat-completions SSE, Responses SSE, and Codex CLI each map through mode-specific bounded parsers into the same `ProviderEvent` contract and `ValidatedExactResponse`; limits, fragmented tool-call rejection, and one approved-grant success/failure row per mode are tested.
- [ ] The exact-response collector withholds every delta until the complete bounded response validates; non-empty deltas followed by fragmented malicious tool calls produce zero SSE, state, checkpoint, log, or executor exposure.
- [ ] Every path after claim reaches a terminal grant state. Pre-transport failure/cancellation is failed; timeout, disconnect, cancellation, or error after transport starts is ambiguous; every exact attempt suppresses retry/fallback.
- [ ] Provider/base/model or tool-mode mutation that wins the connection lock records the required failed terminal state and reaches neither `prepare` nor transport; a winning claim uses one locked connection snapshot through transport creation.
- [ ] Legacy tool/assistant records are filtered before replay; user-authored messages remain unchanged.
- [ ] Every registered tool's assistant call, pending/deferred records, confirmation/checkpoint state, generic log, and next provider request contain only the versioned argument projection/hash/reference shape; missing or mismatched ephemeral refs fail closed.
- [ ] Browser-local exact tool results and provider projections use separate channels.
- [ ] `_finalize_tool_execution_result` is the sole authoritative security-audit/History owner, is invoked exactly once before any result-channel read, and direct-writer static inventory has no disclosure-path violation.
- [ ] The only durable exact-turn assistant content is the fixed local acknowledgement; report substrings, path components, case changes, escaped identifiers, and mixed exact/safe prose remain transient and create no new durable copy.
- [ ] Rule fallback, no-LLM mode, structured workbench actions, and SSH approved-root enforcement do not manufacture or inherit data grants.
- [ ] All six generative exits use `ModelProviderGateway`: graph stream, legacy web reply, web stream fallback, OpenAI-compatible CLI/GUI, Codex CLI, and test-LLM.
- [ ] Every generative exit obtains provider-bound messages from `ModelContextBuilder`; context-free CLI/GUI/test calls use `project_dir=None` and never infer project state.
- [ ] Non-generative `/models` GET uses normalized provider configuration and does not log credentials.
- [ ] Static provider inventory, focused disclosure tests, full pytest, compileall, and diff check pass.
