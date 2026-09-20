# Task 4 fix round 1 scoped re-review — `6c24009`

## Verdict

**ADDRESSED for the requested Task 4 fix round.** No remaining blocker in the
three in-scope fixes. The legacy route remains intentionally deferred to the
Task 5/6 route work.

## Original blocker status

1. **Helper `--` framing — ADDRESSED.**

   The helper now strips the explicit `--` token before unpacking positional
   values (`src/rnaseq_agent/remote_browse.py:141-145`). The generated command
   still keeps root/depth/limit/deadline/target after `--`, with each argv value
   shell-quoted once. The new
   `test_scan_helper_executes_with_framed_positional_arguments` executes the
   generated `-c` helper against a real temporary directory and verifies its
   JSON output. The previous `ValueError: too many values to unpack` is gone.

2. **Basename-only sample/unmatched rows — ADDRESSED.**

   `_validate_scan_payload()` now rejects path separators, control characters,
   empty values, and `.`/`..` for sample IDs, `fastq_1`, `fastq_2`, and
   unmatched basenames. The regression test supplies absolute values through an
   injected scanner and receives `REMOTE_SCAN_INVALID_OUTPUT`.

3. **Remote Python 3 preflight/error — ADDRESSED.**

   The scan command performs `command -v python3` before `exec`, exits with the
   dedicated status 46 when unavailable, and maps that status to
   `ScanPython3Required` / `REMOTE_SCAN_PYTHON3_REQUIRED`. Both direct scanner
   translation and browse-route translation have regression coverage.

4. **Legacy Settings/`find -print` route — NOT ADDRESSED, DEFERRED.**

   `webapp._scan_remote_samples()` still uses generic `transport.execute()` and
   `find -print` (`src/rnaseq_agent/webapp.py:408-425`). This is unchanged by
   design for this round and is explicitly recorded as the Task 5/6 cross-task
   dependency. It remains a future integration requirement, but is not a
   blocker for this scoped Task 4 fix review.

## Verification

```text
$env:PYTHONPATH='src'
.\\.venv\\Scripts\\python.exe -m pytest tests/test_remote_browse.py -k "helper_executes or basename_sample or missing_remote_python3" -q
4 passed, 26 deselected
```

The parent fix report also records the complete relevant-suite and repository
suite results: 157 relevant tests passed; 920 repository tests passed, 8
skipped, 1 warning, and 6 subtests passed.

