# Task 3 fix report

## Root cause

`BrowseExecutionBudget` uses `execute_bounded` to resolve approved roots and browse targets. `SystemSSHTransport.execute_bounded` delegated to `run_command_bounded` with its default `check=True`, while `ParamikoTransport.execute_bounded` always called `_raise_for_result`. The resolver's intentional exit codes 44 and 45 therefore raised before `resolve_remote_directory` could map them to `REMOTE_PATH_NOT_FOUND` and `REMOTE_PATH_NOT_DIRECTORY`.

## Fix

Added an optional `check` flag to the bounded transport interface and both implementations. Bounded execution defaults to preserving the `CommandResult` (`check=False`), while callers may request the previous raising behavior explicitly. Ordinary `execute` methods are unchanged and continue to check non-zero results.

## Verification

The regression test scripts bounded SSH execution with exit codes 44 and 45 and confirmed both `CommandResult` values are returned. The focused browse and transport suites pass:

```
143 passed
```
