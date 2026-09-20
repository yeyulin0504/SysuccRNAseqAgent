# Task 4 report

Implemented the fixed bounded FASTQ scanner and directory grouping on top of the
Task 3 authorization state machine.

## Delivered

- Added a code-owned Python 3 scanner helper with fixed depth `2`, producer cap
  `5,000`, `os.scandir(..., follow_symlinks=False)`, per-file realpath checks,
  escaped JSON output, and no user path interpolation into helper source.
- Added one shared `BrowseExecutionBudget` path for scanner execution and strict
  duplicate-key/type/bounds validation for the scanner document.
- Added candidate containment enforcement with `REMOTE_PATH_ESCAPE`, protocol
  failures with `REMOTE_SCAN_INVALID_OUTPUT`, and existing timeout/output-limit
  translations.
- Added deterministic canonical-parent grouping, typed R1/R2 basename pairing,
  bounded unmatched/sample rows, truncation tracking, and authorization-bound
  opaque group IDs.
- Enforced the 32 active-root cap before transport construction.

## Verification

RED was observed before implementation: the new framing/grouping/scanner tests
collected and failed on the expected `NotImplementedError` stubs.

GREEN verification:

```text
152 passed in 2.23s
```

Command:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_remote_browse.py tests/test_execution.py tests/test_remote_transport.py -q
```

