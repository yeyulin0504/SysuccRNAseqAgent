# Task 4 fix round 1 report

## Scope

Fixed the review blockers called out in `task-4-review.md`:

- corrected the fixed helper's `--` framing and added a test that executes the
  generated helper against a real temporary directory;
- enforced basename-only values for `BrowseSampleRow.sample_id`, `fastq_1`,
  `fastq_2`, and `unmatched_basenames` at the final browse validation boundary;
- added an explicit `python3` preflight in the remote command with stable
  `REMOTE_SCAN_PYTHON3_REQUIRED` mapping and tests for scanner and browse-route
  translation.

The legacy Settings/`webapp._scan_remote_samples` `find -print` route was left
unchanged as requested; replacing it is a cross-task dependency for the Task 5/6
route work.

## Verification

RED was observed before implementation: the helper test failed with
`ValueError: too many values to unpack (expected 5)`, absolute sample paths were
accepted, and missing-Python3 status was mapped to invalid output.

After the fix:

```text
$env:PYTHONPATH='src'
.\\.venv\\Scripts\\python.exe -m pytest tests/test_remote_browse.py -k "helper_executes or basename_sample or missing_remote_python3" -q
4 passed, 26 deselected

.\\.venv\\Scripts\\python.exe -m pytest tests/test_remote_browse.py tests/test_execution.py tests/test_remote_transport.py -q
157 passed

.\\.venv\\Scripts\\python.exe -m pytest -q
920 passed, 8 skipped, 1 warning, 6 subtests passed
```

