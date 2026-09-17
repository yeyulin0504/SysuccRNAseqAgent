# Remote SSH Boundary Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent SSH destination option injection and make system-SSH remote command execution terminate on fixed time or captured-output limits before approved remote data roots are introduced.

**Architecture:** Add one pure identity-validation module shared by Settings, LLM connection validation, and both SSH transports. Add a binary, bounded subprocess primitive for system SSH commands; `SystemSSHTransport.execute` uses it, while long file transfers keep the existing command path. Both SystemSSH and Paramiko enforce the same validated host/user/port identity and the same combined output ceiling.

**Tech Stack:** Python 3.11+, `subprocess.Popen`, threads/events for cross-platform pipe draining, FastAPI endpoint tests, pytest/unittest, OpenSSH/Paramiko adapters.

## Global Constraints

- Work only on branch `wizard`; do not merge `master`.
- Use TDD: every production behavior below must first have a failing test with the failure reason recorded.
- Do not touch `run_agent.py`, `storage.py`, or `tests/test_execution_claims.py`; another task owns them.
- Do not add a generic shell escape hatch. Commands remain fixed argv templates and remote commands remain caller-owned strings.
- Never include passwords, API keys, private-key contents, or decrypted credentials in exceptions, `CommandResult.command`, or tests.
- Username must match `[A-Za-z0-9._-]+` and must not begin with `-`.
- Host may contain ASCII letters, digits, `.`, `-`, `:`, `[`, and `]`; it must not be empty, begin with `-`, or contain whitespace, controls, `@`, or shell metacharacters.
- Port must be an integer from `1` through `65535`; booleans are invalid.
- System SSH remote commands use a 120-second timeout and a 4 MiB combined stdout/stderr capture limit.
- The bounded command primitive must kill and reap the child on timeout or overflow on Windows and POSIX.
- This phase does not create approved data roots or change remote browsing behavior; it prepares a safe transport boundary for that later plan.

---

### Task 1: Validate SSH identity at every boundary

**Files:**
- Create: `src/rnaseq_agent/ssh_identity.py`
- Modify: `src/rnaseq_agent/remote_transport.py`
- Modify: `src/rnaseq_agent/agent_tools.py`
- Modify: `src/rnaseq_agent/webapp.py`
- Test: `tests/test_remote_transport.py`
- Test: `tests/test_chat_graph.py`
- Test: `tests/test_webapp.py`

**Interfaces:**
- Produces `SSHIdentity(host: str, user: str, port: int)`.
- Produces `SSHIdentityError(ValueError)`.
- Produces `normalize_ssh_identity(server: Mapping[str, Any]) -> SSHIdentity` for transport construction.
- Produces `validate_ssh_patch(values: Mapping[str, Any]) -> list[str]` for partial Settings/tool payload validation.
- `create_remote_transport`, `SystemSSHTransport`, and `ParamikoTransport` must independently call the strict normalizer before credential lookup or process/network creation.

- [ ] **Step 1: Write failing identity tests**

Add parameterized tests showing that the Settings API, `edit_connection` argument validation, `create_remote_transport`, `SystemSSHTransport`, and `ParamikoTransport` reject:

```python
INVALID_IDENTITIES = [
    {"host": "-oProxyCommand=calc", "user": "alice", "port": 22},
    {"host": "good.example", "user": "-Fbad", "port": 22},
    {"host": "bad host", "user": "alice", "port": 22},
    {"host": "alice@evil", "user": "alice", "port": 22},
    {"host": "good.example", "user": "alice;id", "port": 22},
    {"host": "good.example", "user": "alice", "port": 0},
    {"host": "good.example", "user": "alice", "port": 65536},
    {"host": "good.example", "user": "alice", "port": True},
]
```

Assert transport factories and credential lookup are not called after invalid input. Add valid cases for DNS names, IPv4, bracketed/bare IPv6, underscore-free usernames, port strings such as `"2222"`, and default `None -> 22`.

- [ ] **Step 2: Run RED tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_remote_transport.py tests/test_chat_graph.py tests/test_webapp.py -k "ssh_identity or invalid_connection or edit_connection"
```

Expected: existing code constructs transports or saves settings for at least the leading-dash host/user and invalid port cases.

- [ ] **Step 3: Implement the pure validator**

Implement a frozen dataclass and strict normalization in `ssh_identity.py`. `validate_ssh_patch` validates only keys present, while `normalize_ssh_identity` requires non-empty host/user and supplies port 22 only for missing/`None`/empty string. Reject bool before `int(value)`.

Use full-string regular expressions and explicit leading-dash/control checks. Return Chinese user-facing validation messages without echoing secrets.

- [ ] **Step 4: Enforce at save, tool, and transport boundaries**

Before Settings persistence, validate the supplied server patch and return an error without calling `save_connection`, `set_ssh_credential`, or `session.edit`. Extend `agent_tools.validate_call("edit_connection", ...)` with the same partial validation. In `create_remote_transport`, validate before `get_ssh_credential`; constructors validate again for defense in depth.

Build system SSH argv with separate login and host fields:

```python
["ssh", *options, "-l", identity.user, "--", identity.host, remote_command]
```

For SCP, use validated `identity.host` and a validated username in the remote operand; include `--` before operands when supported by the existing OpenSSH target. Tests must assert no destination-controlled argv item begins with an option.

- [ ] **Step 5: Run GREEN identity tests**

Run the Step 2 command and the full `tests/test_remote_transport.py`, `tests/test_chat_graph.py`, `tests/test_webapp.py`, and `tests/test_webapp_chat_tools.py` files. Expected: all pass.

---

### Task 2: Bound system SSH command time and captured output

**Files:**
- Modify: `src/rnaseq_agent/execution.py`
- Modify: `src/rnaseq_agent/remote_transport.py`
- Test: `tests/test_execution.py`
- Test: `tests/test_remote_transport.py`

**Interfaces:**
- Produces `CommandTimeoutError(RuntimeError)`.
- Produces `CommandOutputLimitError(RuntimeError)`.
- Produces `run_command_bounded(command, *, timeout_seconds, max_capture_bytes, cwd=None, check=True) -> CommandResult`.
- `SystemSSHTransport.execute` calls the bounded primitive with `REMOTE_COMMAND_TIMEOUT_SECONDS=120` and `MAX_REMOTE_CAPTURE_BYTES=4 * 1024 * 1024`.
- Paramiko uses the same combined stdout+stderr ceiling and maps socket/channel timeout to `CommandTimeoutError`.

- [ ] **Step 1: Write failing bounded-process tests**

Use `sys.executable -c` child programs so tests are platform independent:

```python
def test_bounded_command_kills_timeout_child():
    with pytest.raises(CommandTimeoutError):
        run_command_bounded(
            [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            timeout_seconds=0.2,
            max_capture_bytes=4096,
        )

def test_bounded_command_kills_output_overflow():
    with pytest.raises(CommandOutputLimitError):
        run_command_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 8192); sys.stdout.flush()"],
            timeout_seconds=5,
            max_capture_bytes=1024,
        )
```

Add a combined stdout+stderr test and a normal UTF-8/invalid-byte replacement test. Assert each failure completes promptly and no secret appears in the exception.

- [ ] **Step 2: Run RED bounded-process tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_execution.py tests/test_remote_transport.py -k "bounded or timeout or output_limit"
```

Expected: `run_command_bounded` and its exception types do not exist.

- [ ] **Step 3: Implement bounded pipe draining**

Use `subprocess.Popen(..., stdout=PIPE, stderr=PIPE)` in binary mode. Drain both pipes concurrently in daemon threads into bounded byte buffers. Maintain one shared byte counter guarded by a lock; when the next chunk would exceed `max_capture_bytes`, set an overflow event and stop retaining bytes. The controller loop waits in short intervals until exit, timeout, or overflow. On timeout/overflow call `kill()`, then `wait()`, join both readers, close pipes, and raise the typed exception. On normal exit, decode with UTF-8 `errors="replace"`, build `CommandResult`, and preserve existing `check=True` behavior.

Do not use `subprocess.run(capture_output=True)` or `Popen.communicate()` for the bounded path because both can materialize unbounded output before the limit is enforced.

- [ ] **Step 4: Integrate transports**

Switch only `SystemSSHTransport.execute` to `run_command_bounded`; do not impose a 120-second ceiling on FASTQ upload/download. Update Paramiko capture accounting to reject when `len(stdout_bytes) + len(stderr_bytes)` exceeds the same constant, close the channel on limit, and translate timeout exceptions to `CommandTimeoutError`.

Tests must assert the SystemSSH call receives both fixed limits and that `CommandResult.command` contains neither password nor private-key contents.

- [ ] **Step 5: Run GREEN transport tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_execution.py tests/test_remote_transport.py tests/test_chat_graph.py tests/test_webapp.py tests/test_webapp_chat_tools.py
.\.venv\Scripts\python.exe -m compileall -q src
git diff --check
```

Expected: all selected tests pass, both failure tests finish without hanging, compileall exits 0, and diff check is clean.

- [ ] **Step 6: Commit exact files**

Stage only the files listed in Tasks 1-2 and commit:

```powershell
git add -- src/rnaseq_agent/ssh_identity.py src/rnaseq_agent/execution.py src/rnaseq_agent/remote_transport.py src/rnaseq_agent/agent_tools.py src/rnaseq_agent/webapp.py tests/test_execution.py tests/test_remote_transport.py tests/test_chat_graph.py tests/test_webapp.py tests/test_webapp_chat_tools.py
git commit -m "fix: harden remote ssh boundaries"
```

If a listed test file was not changed, omit it from `git add`. Never use `git add -A` or `git add .`.
