# Paired UI / model projection report

Date: 2026-09-21

Implemented the remaining paired-design UI and LLM projection contracts from the
paired airway design:

- Structured local sample schemas and `edit_samples` patches accept a bounded
  `pair_id` field and validate it with the existing identifier policy.
- The default project builder and both counts entry points (JSON wizard session
  and multipart upload) preserve `pair_id` in the local sample contract.
- Default model summaries expose only `pairing.present`, `pairing.pair_count`,
  and `pairing.gate_status`; pair identifiers, source values, and mappings are
  excluded from provider-facing projections.

Validation:

```
.venv\\Scripts\\python.exe -m pytest -q tests/test_paired_capability.py::test_llm_sample_schemas_accept_bounded_pair_id_fields tests/test_webapp_counts_entry.py::TestCountsUploadEndpoint::test_counts_upload_persists_pair_id_for_structured_samples tests/test_model_context.py::test_default_summary_projects_pairing_status_without_pair_ids
3 passed, 1 warning

.venv\\Scripts\\python.exe -m pytest -q tests/test_model_context.py tests/test_webapp_counts_entry.py tests/test_webapp_project_wizard.py tests/test_paired_capability.py tests/test_analysis_contract.py
71 passed, 1 warning
```

No exact disclosure scope, History, checkpoint, generic log, or pair mapping
projection was added.
