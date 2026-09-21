# bkbio-eval protocol versioning design

## Purpose

Make the independent evaluator a versioned protocol whose case manifests,
baseline expectations, analyzer results, and suite reports can be reproduced
and migrated without silently changing the meaning of an L1 score.

## Compatibility rules

Existing unversioned cases are read as legacy case schema version 1. New cases
must write `case_schema_version: 1`. Unknown future versions fail before
execution. The existing `bkbio-eval/analyzer-result@1` refusal and numerical
result shape remains valid; result dispatch rejects unknown schemas before any
check runs. The existing `bkbio-eval/suite-report@1` envelope remains readable
and gains additive evaluator/case/protocol provenance fields. A breaking report
shape requires `suite-report@2` and a documented reader migration.

## Case and baseline provenance

Each released positive L1 owns an immutable expected manifest containing:

- case schema version and analyzer-result schema;
- evaluator package version, git commit, and check-vocabulary version;
- reference command/script commit and fixed runtime or container digest;
- source archive and derived input hashes;
- expected-file hashes and reference-result hash;
- confirmation state and capture timestamp.

The evaluator never infers release identity from a mutable archive path. A
baseline provenance mismatch is a protocol failure, not a numerical mismatch.

## Migration behavior

`load_case()` accepts legacy manifests by assigning version 1 in memory and
rejects malformed or unsupported versions with a clear `CaseError`. The runner
dispatches numerical and refusal results by schema. Refusal validation remains
strict: exact status, reason code, and allowed fields are unchanged.

Suite reports add evaluator version, evaluator commit, case schema version,
result schema, and protocol compatibility metadata while preserving the current
`suite-report@1` fields and status counts.

## Required tests

Migration tests cover legacy and explicit version-1 manifests, unknown case
versions, old refusal and numerical results, unknown result schemas, and
baseline provenance mismatch. Existing refusal, mutation, and real-Adapter L1
tests remain required and must continue to distinguish numerical passes,
expected refusals, skips, and failures.

## Airway release gate

Versioning alone does not promote airway. The case remains an expected refusal
until the paired capability, metadata/licence audit, immutable R/DESeq2 runtime,
reviewed expected values, and fresh real-Adapter run are all present. The
release report must bind evaluator commit/version, main-project commit,
Analysis Contract ID, run/result manifest IDs, runtime digest, input hashes,
and output artifact hash.

