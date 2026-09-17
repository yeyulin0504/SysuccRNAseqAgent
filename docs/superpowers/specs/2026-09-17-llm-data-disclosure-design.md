# LLM Data Disclosure Design

Date: 2026-09-17

Status: accepted direction; implementation pending

## Problem

The existing LLM permission model controls which tools the model may call, but it does not independently control which project data a model provider may receive. A `read_only` tool can still disclose sample identifiers, FASTQ filenames, remote directories, reference paths, report text, or scheduler metadata. Treating all reads as harmless would make the tool kill switch an execution boundary but leave the data boundary implicit.

The selected policy is:

> By default, the model receives a de-identified project summary. Exact sample names, FASTQ paths, and remote directories require a separate, explicit authorization.

This policy applies to local and remote model providers. Provider locality may be added later as a policy input, but it does not weaken the initial default.

## Two Independent Permission Axes

The system will enforce two independent axes:

1. `tool_mode` controls which model-initiated actions are available: `disabled`, `read_only`, `approved_write`, or `approved_execute`.
2. `data_scope` controls which project fields may leave the deterministic application boundary: `summary` by default, or a short-lived exact-data grant.

A broad tool mode does not imply broad data access. `approved_execute` still receives only the summary unless an exact-data grant is active. An exact-data grant does not authorize writes or execution.

Remote approved data roots are a third, separate boundary. They determine where the application may browse over SSH. They do not authorize disclosure of returned filenames or paths to a model.

## Default Summary

`read_project_state` will return a model-safe projection. The default payload may include:

- project state and route/capability id;
- input kind, sequencing layout, and strandedness;
- sample count and condition counts;
- deterministic aliases such as `sample_001`, not source sample ids;
- whether groups meet scientific replicate gates;
- enabled pipeline stages and their status;
- matrix classification and DESeq2 eligibility;
- presence of required references without their paths;
- contract/run presence and opaque hashes or shortened ids;
- bounded error categories and actionable, secret-free messages.

The default payload must not include:

- source sample ids or patient ids;
- FASTQ filenames or absolute/local/remote paths;
- hostnames, usernames, ports, scheduler job ids, work directories, or approved roots;
- raw metadata values that can re-identify a sample;
- report bodies, command lines, stdout/stderr, tracebacks, or environment variables;
- API keys, passwords, private keys, credential ciphertext, tokens, or secret-bearing URLs.

Condition labels are retained by default because they are required for useful scientific conversation. A future clinical mode may classify particular labels as sensitive; that is outside this first scope.

## Exact-Data Grants

Exact information is exposed only through a dedicated user approval object. Ordinary write/execute approval cards cannot grant data access.

A grant binds:

```json
{
  "grant_id": "data_grant_<random>",
  "project_id": "...",
  "thread_id": "...",
  "provider_identity": "sha256:...",
  "fields": ["sample_ids", "fastq_filenames"],
  "purpose": "Review sample pairing before applying the scan result",
  "issued_at": "...",
  "expires_at": "...",
  "single_turn": true,
  "policy_version": 1
}
```

Rules:

- The confirmation card lists exact field categories, not the sensitive values themselves.
- The first version supports `sample_ids`, `fastq_filenames`, `remote_paths`, and `report_excerpt` as separate scopes.
- Grants are project- and thread-bound, provider-bound, single-turn, and expire after 10 minutes.
- The durable checkpoint stores the grant metadata and hashes, never the disclosed raw values.
- A grant cannot be widened during resume. A new field category requires a new card.
- A rejected, expired, replayed, cross-project, cross-thread, or provider-changed grant fails closed.
- Changing the LLM endpoint, provider, model identity, `tool_mode`, project revision, or relevant data revision invalidates a waiting grant.
- The model cannot request a grant that includes credentials, secrets, raw command output, arbitrary project files, or unrestricted directories.
- Exact values are inserted only for the authorized provider call and are not copied into the persistent chat transcript or generic tool log.

The UI should say what will be shared and why, for example: "Share 12 sample identifiers and 24 FASTQ filenames with the configured model for this reply." It must not use a generic "Allow read access" label.

## Provider Identity

`provider_identity` is a secret-free digest over the normalized provider name, API base origin, and model name. It excludes API keys and request headers. A grant issued for one endpoint cannot be replayed after switching to another endpoint.

The application must treat an OpenAI-compatible custom endpoint as a remote third party unless a future explicit local-provider policy says otherwise. Hostname guesses such as `localhost` are not sufficient to silently widen data scope.

## Tool Behavior

### `read_project_state`

The existing tool remains `risk=read` and confirmation-free for the summary projection. Its schema will no longer imply that exact sample rows or paths are returned.

### `browse_remote_samples`

The application may scan an approved remote root without a data-disclosure confirmation when `tool_mode` permits reads. The model receives only a summary by default:

```json
{
  "sample_count": 12,
  "paired_count": 12,
  "unmatched_count": 1,
  "directory_count": 1,
  "truncated": false,
  "authorization": "inside_approved_root"
}
```

The browser can show exact filenames locally. Sending those filenames or the canonical directory to the provider requires the corresponding exact-data grant.

### Reports and Status

Generated reports remain deterministic local artifacts. The model can receive a bounded summary of artifact presence and validation status. Sending a report excerpt requires `report_excerpt`; full report or arbitrary file disclosure is out of scope.

## Request Construction

All provider-bound messages pass through one `ModelContextBuilder`. It takes deterministic project state plus an optional validated grant and produces:

```python
ModelContextBuilder.build(
    project_dir: Path,
    project_id: str,
    thread_id: str,
    provider_identity: str,
    grant: DataDisclosureGrant | None,
) -> ModelContext
```

`ModelContext` contains the safe summary, optional single-turn exact block, disclosure manifest, and revision hashes. Web routes and chat graph code may not serialize `ProjectSession.config` directly into provider messages.

The builder uses explicit allowlists. It must not rely only on recursive key-name redaction, because secrets and identifiers can appear under unexpected keys or inside free text.

## Persistence and Logging

Persistent records contain:

- grant id, project/thread/provider bindings;
- authorized field categories;
- purpose, issue/expiry/consume timestamps;
- project/data/policy revisions;
- counts of disclosed records and byte length;
- outcome and stable error code.

Persistent records do not contain the raw disclosed values. Tool logs store the disclosure manifest and aliases, not exact names or paths. User chat messages remain user-authored and are not rewritten; the application must not append the temporary exact-data block to the visible transcript.

Every outbound provider request passes through a last-mile secret scanner. Detection of a configured credential, private-key marker, credential-store ciphertext, or known API token aborts the request with `MODEL_CONTEXT_SECRET_DETECTED`. This scanner is defense in depth; the allowlist builder remains the primary control.

## Concurrency and Failure Semantics

- Grant creation and consumption use a durable one-time claim under the same project/thread serialization pattern as tool approvals.
- The application re-reads provider identity, `tool_mode`, project revision, and data revision immediately before the network request.
- If any binding changes, the provider request is not sent and the grant is consumed as failed.
- Network failure after request transmission does not permit automatic reuse of a single-turn grant; the transmission is ambiguous and requires a new grant.
- LLM failure may fall back to local deterministic text, but the fallback cannot execute a write or reuse the exact-data block.

## Error Codes

- `MODEL_DATA_CONFIRMATION_REQUIRED`
- `MODEL_DATA_GRANT_INVALID`
- `MODEL_DATA_GRANT_EXPIRED`
- `MODEL_DATA_GRANT_CONSUMED`
- `MODEL_PROVIDER_CHANGED`
- `MODEL_DATA_REVISION_CHANGED`
- `MODEL_CONTEXT_SECRET_DETECTED`
- `MODEL_DATA_SCOPE_UNSUPPORTED`

## Migration

- Existing installations start in `summary` data scope without a migration prompt.
- Existing chat checkpoints that contain exact tool results are not replayed to a provider. They may remain visible locally, but a new graph turn must rebuild context through `ModelContextBuilder`.
- No current project field is treated as a durable exact-data grant.
- The first release does not attempt to erase historic local chat files; it prevents new provider disclosure and documents local cleanup separately.

## Test Requirements

At minimum, automated tests must prove:

1. Default `read_project_state` provider payload excludes sample ids, FASTQ names, paths, host/user/job ids, and secrets while preserving counts and conditions.
2. Exact-data grant cards list field categories and record counts without raw values.
3. Approved `sample_ids` does not disclose FASTQ names or paths.
4. Approved `fastq_filenames` does not disclose canonical directories.
5. Exact data is present in one provider request and absent from the durable transcript/tool log.
6. Rejected, expired, consumed, cross-project, cross-thread, and provider-changed grants send no provider request.
7. Project or sample revision changes invalidate a waiting grant.
8. Switching `tool_mode` to `disabled` before dispatch sends no provider request.
9. Provider timeout after transmission consumes the grant and cannot auto-retry.
10. Legacy checkpoints containing exact results are filtered before the next provider request.
11. Browser workbench can display exact scan results locally while the provider receives only counts.
12. The secret scanner blocks known API key/password/private-key markers and does not log the detected value.
13. Rule fallback and no-LLM modes never persist or expose a fake grant.
14. Structured workbench actions remain governed by their existing state and remote-root permissions, not by data disclosure grants.

## Out of Scope

- Cancelling a scheduler job already submitted before permission changes.
- Arbitrary file upload to a model provider.
- Full report or raw stdout/stderr disclosure.
- Automatic trust of local endpoints.
- Role-based multi-user approvals; the current threat model remains one local user with a random session token.

## Acceptance Criteria

The feature is ready only when every provider call is constructed through `ModelContextBuilder`, the default request can be shown to contain no exact identifiers or paths, exact fields require a durable single-use scoped grant, raw disclosed values do not enter durable model/tool logs, and all invalidation/concurrency tests pass.
