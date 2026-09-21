# Local FASTQ Filename Disclosure Scope

Date: 2026-09-21  
Status: proposal only; disabled in the current release

## Purpose

This document defines a possible future scope for disclosing FASTQ basenames to a model provider. It does not enable the scope. The current product continues to reject `fastq_filenames` at the disclosure issue, claim, and send boundaries.

## Boundary

The scope may cover only basenames that are owned by the current project and whose provenance is local project metadata. A row that was created from an approved remote scan, contains `remote_data_dir`, carries a `source_ref`, or is otherwise linked to remote discovery is remote-origin data and cannot use this local scope. Remote filenames require a separate approved-root/source-reference consumer design.

The scope never includes absolute paths, canonical directories, hostnames, usernames, ports, scheduler fields, command output, credentials, or arbitrary file contents. A basename is not a path authorization.

## Grant and request rules

- `fastq_filenames` is a separate field category and cannot be inferred from a `sample_ids` grant.
- A grant is project-, thread-, provider-, tool-mode-, revision-, purpose-, and expiry-bound, single-use, and request-local.
- The card shows only the field category, bounded record count, provider label, revision summary, purpose category, and expiry. It does not show filenames or raw revisions.
- The exact request disables provider tools and is bounded by record, byte, event, and response limits.
- Unsupported provenance, stale project/sample revisions, cross-thread replay, provider changes, tool-mode changes, malformed responses, secret detection, and transport uncertainty fail closed.

## Extraction contract

The extractor reads only non-empty `fastq_1` and `fastq_2` values from validated local rows and returns `os.path.basename` values after rejecting separators, NUL, control characters, URL userinfo, and path-like aliases. It preserves deterministic row order, de-duplicates only when the manifest explicitly records the policy, and returns counts and byte length. It never reads remote scan stores or arbitrary files.

## RED matrix before enablement

1. Issue/load/claim/send reject the field while this proposal is disabled.
2. A local basename can be extracted without returning a directory.
3. A row with `source_ref`, `remote_data_dir`, or remote provenance is rejected.
4. A basename containing `/`, `\\`, NUL, control characters, URL userinfo, or a path traversal token is rejected.
5. A sample grant cannot be reused to request filenames.
6. A cross-thread, cross-provider, stale-revision, expired, duplicate, or ambiguous grant sends no provider request.
7. Exact values appear in at most one bounded provider request and nowhere durable.
8. Provider tool calls, secret markers, oversized output, and malformed frames consume or reject the grant without exposing filenames.
9. Local UI may show authorized rows, but default provider summaries contain only counts and pairing status.
10. Remote scan filenames remain structurally unsupported until the approved-root/source-reference consumer is separately reviewed.

## Release decision

Do not enable this scope until the matrix above is implemented, the local/remote provenance split is represented in the data model, and the full disclosure, secret, replay, and concurrency gates pass. `report_excerpt` and `remote_paths` remain separate scopes.
