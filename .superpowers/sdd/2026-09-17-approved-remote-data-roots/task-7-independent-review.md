# Task 7 implementation report

Commit: `3b0a4d5`

Task 7 now applies an approved remote scan group through a server-verified,
workbench-only endpoint. The browser submits only `scan_id`,
`result_revision`, `group_id`, and the explicit truncated-result acknowledgment;
the canonical directory and FASTQ rows are read from the private scan record.

The scan store validates its complete fixed schema and alias binding, checks the
current identity, policy revision, active root, and target containment, then
durably claims a pending record before invoking the idempotent project writer.
Retries reuse the same claim id after a callback or final record replace
failure. Project state stores basenames only and a bounded receipt ledger with
opaque identifiers. Replays classify as `REMOTE_SCAN_REFERENCE_USED` and
expired records as `REMOTE_SCAN_REFERENCE_EXPIRED`.

The project endpoint creates the normal remote-prestaged draft/session and
intake projection. The workbench renders separate directory groups, requires an
explicit acknowledgment for truncated inventories, and clears the reference
after successful application. Audit publication is authoritative; a failure to
append the de-identified History projection does not discard a published scan.

Validation run on Windows:

```text
python -m compileall -q src
pytest tests/test_remote_scan_store.py tests/test_webapp_project_wizard.py::test_remote_scan_apply_persists_filename_only_group_and_rejects_replay tests/test_webapp_project_wizard.py::test_fastq_session_from_detected_samples_enters_input_ready -q
8 passed
git diff --check
```

The broader legacy browse tests that still expect unapproved directories to be
scannable remain compatibility cases for Task 8; they are intentionally not
re-enabled by this commit.
