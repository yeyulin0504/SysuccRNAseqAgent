# Release blocker fixes report

## Completed

- Paired differential design now refuses missing or invalid `reference_condition`
  and `contrast_condition` with structured `ValueError` messages. The previous
  empty-key and invalid-condition paths could raise `KeyError` while building
  the canonical mapping.
- Paired R summaries now emit and the Adapter validates the frozen
  `template`, `pair_count`, `min_count_prefilter`, model-matrix rank and
  columns, and canonical `pair_mapping`.
- `bkbio-eval` baseline manifests now record the reference result relative path
  and validate its `reference_result_sha256` on every check. Evaluator package
  identity, reference script identity/commit, and runtime identity are required
  provenance fields. Added `docs/VERSIONING.md`.

## TDD and verification evidence

- RED: paired regression tests reproduced `KeyError: ''` and `KeyError:
  'missing'`; baseline tests initially accepted reference-result drift and
  missing runtime identity.
- GREEN: `38 passed, 47 deselected` for paired/differential/Adapter focused
  tests in the main repository.
- GREEN: `19 passed` for `bkbio-eval/tests/test_protocol_versioning.py`.

## Explicitly not completed

- Counts contract inventory was not added in this pass.
- Paired UI `pair_id` coverage was not expanded in this pass.

Airway/TCGA expected refusals remain unchanged; no R/DESeq2 baseline was
fabricated. The external evaluator's `.tmp-l1-report.json` was left untouched.
