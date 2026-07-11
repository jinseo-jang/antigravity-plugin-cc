# E2E Test Harness

Proves the full CAO pipeline without GCP credentials or a live Gemini endpoint.
Only the SDK boundary (`google.antigravity`) is faked; every other component
(`IPC`, `HookAdapter`, `PolicyEngine`, `ApprovalWaiter`, `EventBus`,
`GitDiffCollector`, `DigestGenerator`) runs as real production code.

## How to run

```bash
# All tests (existing unit tests + e2e)
PYTHONPATH=src python3 -m pytest tests/ -v

# E2E suite only
PYTHONPATH=src python3 -m pytest tests/e2e/ -v

# Single test
PYTHONPATH=src python3 -m pytest tests/e2e/test_full_pipeline.py::test_full_pipeline_happy_path -v
```

No environment variables required. The harness uses a temp git workspace and
an in-process Unix socket server — nothing persists between runs.

## Timeout configuration

`test_approval_timeout` uses `timeout_seconds=2.0` passed directly to
`CAOPreToolCallDecideHook`. To test with a different timeout, pass a different
value to `fake_agent(session_id, timeout_seconds=N)` in the test body.

## What each test proves

| Test | Invariant verified |
|---|---|
| `test_full_pipeline_happy_path` | Pre-Execution Gate (no mutation before approval); Non-Blocking Rule (session.status responds during suspension); Objective Truth Rule (digest has real git diff stat, no raw logs); Thin Plugin Rule (IPC goes through daemon, not plugin script) |
| `test_deny_path` | Denial blocks filesystem mutation; digest records denial |
| `test_auto_allow_path` | Read-only tools execute without human approval |
| `test_env_file_deny` | `.env` Specific-Deny fires without entering the ASK/approval path |
| `test_approval_timeout` | Timeout → implicit DENY; daemon remains responsive after timeout |

## Architecture

The daemon fixture (`daemon_ctx`) starts a real asyncio Unix socket server
in the test's own event loop. The `fake_agent` factory constructs the five
real hook instances sharing the same `ApprovalWaiter` and `EventBus` as the
server. Tests call `ipc_client("session.approve", ...)` to resolve the
`asyncio.Future` inside `CAOPreToolCallDecideHook`, which resumes the
`run_scenario()` task. The filesystem mutation is a real file write; git diff
is a real subprocess. No mocks, no faked diffs.
