# Paired capability implementation report

## Scope

Implemented the named `paired_two_group` route and connected the real adapter's
preflight while preserving the existing airway/TCGA expected-refusal behavior.
The server generates `~ pair_id + condition`; callers cannot substitute an R
formula or silently fall back to `~ condition`.

The route canonicalizes pair mappings by `pair_id`, requires two conditions,
exactly one reference and contrast sample per pair, at least three complete
pairs, globally unique sample IDs, `min_count_prefilter=0`, and no declared
batch. The generated colData includes `pair_id`; the R renderer checks model
matrix rank before constructing DESeq2. Contract workflow snapshots now include
pair IDs and the resolved descriptor, so mapping drift changes the contract ID.

The adapter accepts a bounded `design_template=paired_two_group` request with a
declared source pair column, canonicalizes it to `pair_id`, and performs pair
validation before project construction, credential installation, runtime probe,
SSH, or transport. Malformed paired inputs return structured
`unsupported_design`; no independent downgrade is possible.

## Verification

RED (before implementation):

```text
.venv\\Scripts\\python.exe -m pytest -q tests/test_paired_capability.py
9 failed during the missing paired implementation (wrong independent template,
missing pair validation and fixed renderer).
```

GREEN focused:

```text
.venv\\Scripts\\python.exe -m pytest -q tests/test_paired_capability.py tests/test_differential.py
27 passed
.venv\\Scripts\\python.exe -m pytest -q tests/test_bkbio_eval_adapter.py::test_paired_preflight_canonicalizes_pair_column_and_refuses_before_project_or_transport
1 passed
.venv\\Scripts\\python.exe -m pytest -q tests/test_bkbio_eval_adapter.py tests/test_paired_capability.py
64 passed
```

Contract regression:

```text
.venv\\Scripts\\python.exe -m pytest -q tests/test_analysis_contract.py::test_paired_contract_changes_when_canonical_pair_mapping_changes
1 passed
```

The broader focused set (`bkbio_eval_adapter`, `differential`, `capability`,
`analysis_contract`) passed `94` tests after the paired changes. The full main
project suite remains the parent agent's final gate because shared permission
and UI edits are being integrated separately.

## Commit

This report and the paired implementation are committed in the paired-capability
commit reported by the parent agent after staging only the paired files.

## Remaining blockers

There is still no verified immutable R 4.3.3/Bioconductor 3.18 runtime on this
machine, so airway remains `NOT_EVALUABLE / unsupported_design` and no numerical
expected values are fabricated. A future numerical promotion also needs source
licence/provenance review, fixed runtime digest, regenerated R/DESeq2 values, and
a fresh real Adapter L1 run.
