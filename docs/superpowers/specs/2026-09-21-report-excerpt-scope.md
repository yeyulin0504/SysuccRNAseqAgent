# Report Excerpt Disclosure Scope

Date: 2026-09-21  
Status: proposal only; disabled in the current release

## Purpose

This document defines a possible future scope for sending a bounded, redacted report excerpt to a model provider. It does not enable the scope. The current product continues to reject `report_excerpt` at every disclosure boundary.

## Allowed source

Only the deterministic local report artifact for the current project may be considered. The scope does not read arbitrary files, generated logs, stdout, stderr, tracebacks, command lines, environment dumps, scheduler output, or remote report paths. A report must be tied to the current project revision and a known report artifact revision.

## Redaction and bounds

The future extractor must allowlist named report sections rather than taking an arbitrary first-N-byte slice. Before provider dispatch it must remove sample identifiers, patient or case identifiers, absolute and remote paths, host/user/job identifiers, credentials, API keys, private-key material, ciphertext, URL userinfo, commands, and raw exception text. The final Unicode text has fixed section, character, byte, and event limits; truncation is explicit and cannot be presented as a complete report.

## Grant and request rules

- `report_excerpt` is a separate field category and cannot be inferred from any sample or FASTQ grant.
- The card shows section category, bounded character count, provider label, revision summary, purpose category, and expiry without showing report text or raw hashes.
- The grant is project-, thread-, provider-, tool-mode-, report-revision-, purpose-, and expiry-bound, single-use, and request-local.
- Exact requests disable provider tools and use the common bounded collector and secret scanner.
- Report text never enters checkpoints, History, generic logs, grants, exceptions, or application-generated transcript projections.

## RED matrix before enablement

1. Issue/load/claim/send reject the field while this proposal is disabled.
2. Arbitrary file paths, full reports, logs, stdout, stderr, tracebacks, and command output are rejected.
3. Disallowed identifiers, paths, credentials, and URL userinfo are removed or cause a fail-closed rejection; they never reach the provider.
4. A stale report revision, cross-thread/provider replay, expiry, duplicate claim, malformed response, secret detection, or ambiguous transport sends no second request.
5. Exact excerpt text is present in at most one bounded provider request and absent from every durable record.
6. Default summaries expose only report presence, status, and validation categories.

## Release decision

Do not implement or enable this scope in the current release. It requires a separate design review, an allowlisted report schema, a redaction test corpus, and independent provider-boundary and persistence tests. It must remain independent from `fastq_filenames` and remote exact disclosure.
