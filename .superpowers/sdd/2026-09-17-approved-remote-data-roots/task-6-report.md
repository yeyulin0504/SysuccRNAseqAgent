# Task 6 implementation report

Implemented the Task 6 route convergence boundary in the shared wizard branch.

## Delivered

- Added `remote_scan_store.py` with opaque `scan_`/`src_` references, project/context binding metadata, bounded pending capacity, expiry/tombstones, atomic record and alias publication, and fail-closed discard.
- Added `security_audit.py` with fixed-schema per-event records, protected private storage, idempotent duplicate handling, conflict detection, reconciliation, and de-identified History projection.
- Added the four-channel `ToolExecutionResult` and `ToolExecutionContext` contracts to `chat_graph.py`; `node_execute` passes context, finalizes typed results before reading provider/log channels, and stores only hashed/sanitized tool arguments in generic logs.
- Routed structured remote scan, project command, deterministic chat, and LLM browse through the shared `_execute_browse_attempt` and `_finalize_tool_execution_result` helpers in `webapp.py`.
- Added strict explicit project binding for `/api/samples/scan-remote`; absent project ids retain the legacy default behavior while unknown explicit ids return `PROJECT_NOT_FOUND`.
- Changed browse audit event ids to the fixed `audit_` + 32 hex form.

## Verification

- `python -m compileall -q src tests` passes.
- New RED tests were observed failing before the modules existed, then pass after implementation: `tests/test_remote_scan_store.py tests/test_security_audit.py` (`2 passed`).
- `tests/test_webapp.py tests/test_remote_scan_store.py tests/test_security_audit.py`: `106 passed`; three legacy browse tests still expect the pre-approved-root scanner and are intentionally incompatible with the Task 6 security contract.

## Known follow-up boundaries

- `consume_remote_scan` remains the Task 7 implementation seam.
- The audit/store writers use the shared private-file primitives and atomic replace; the full injected crash matrix (short writes, process-exit windows, Windows DACL integration) remains to be expanded in the focused Task 6 tests/review.
- Existing non-browse tools still accept the legacy dict result shape for compatibility; browse uses the typed four-channel result end to end.
