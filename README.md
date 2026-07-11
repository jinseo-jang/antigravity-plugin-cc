# Antigravity Plugin for Claude Code

A Claude Code plugin that turns Claude into a supervisor for a [Google Antigravity SDK](https://pypi.org/project/google-antigravity/) worker. Claude plans and reviews; the Antigravity SDK runs Gemini to do the actual work. Every worker tool call is gated by SDK-native policy, shell commands require human approval, and each session ends with a git-diff digest.

---

## Quickstart

```
/plugin marketplace add code-yeongyu/antigravity-plugin-cc
/plugin install agy@agy
```

Then pick an auth mode below, open a project, and run `/agy:implement <task>`.

---

## Install

Install from the Claude Code plugin marketplace (two steps):

```
# 1. register this repo as a plugin marketplace
/plugin marketplace add code-yeongyu/antigravity-plugin-cc
# 2. install the "agy" plugin from it (plugin-name@marketplace-name)
/plugin install agy@agy
```

**No manual `pip install` needed.** On the first session after install, a bundled `SessionStart` hook fetches the Python backend `claude-antigravity-orchestrator[sdk]` (which includes `google-antigravity`) from PyPI into the plugin's private data directory and adds that directory to its Python path — you never run `pip` yourself. Installing into a private `--target` directory works even on externally-managed (PEP 668) systems, and it runs once (later sessions reuse it).

**Prerequisites you do need:**

- Python 3.11+ and `pip` on PATH (the hook shells out to `pip`)
- Network access on first run (the backend is fetched from PyPI once, then cached)
- `git` on PATH (used for the per-session change digest; optional — without it the digest shows a Risk Note instead of `Changed Files`)

**From source (contributors):**

```bash
pip install -e ".[sdk]"
```

---

## Auth

Two modes. Pick one.

### Mode A: Vertex AI via gcloud ADC (recommended for GCP users)

```bash
gcloud auth application-default login
```

That's it. agy auto-detects your GCP project from your Application Default Credentials (or active gcloud config) — no environment variables required. If no project is detected yet:

```bash
gcloud config set project YOUR_PROJECT_ID
```

`GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` are optional overrides (e.g. to pin a non-default project or a specific Vertex region). Location defaults to `global`.

### Mode B: Gemini API key

The key resolves from three places, in order:

1. **OS keychain** (recommended — encrypted at rest):
   ```bash
   python -m keyring set cao gemini_api_key
   ```
2. **`GEMINI_API_KEY` environment variable** — put this in a persistent shell startup file, not a one-off export:
   ```bash
   # Add to ~/.bashrc or ~/.zshrc, then restart your shell / Claude Code:
   export GEMINI_API_KEY=your-key-here
   ```
   A one-off `export` in a single terminal won't be seen by the plugin's background daemon in later sessions. The keychain option avoids this entirely.
3. **Plaintext file** `~/.config/cao/gemini_api_key` (chmod 600) — last resort, not encrypted.

A resolved Gemini key takes precedence over Vertex/ADC. If no key resolves and no gcloud project is active, the daemon raises `AuthNotConfigured` on startup.

### Supported models

| Model | Status | Default location |
|---|---|---|
| `gemini-3.5-flash` | GA (default) | `global` |
| `gemini-3.1-pro-preview` | Public Preview | `global`; narrower regional availability |

Any other model string is rejected immediately with JSON-RPC `-32602` and a recovery message. Override with `CAO_MODEL=gemini-3.5-flash` or `--model` per session. Both models support `--effort` (thinking levels).

**Region is your choice.** agy doesn't hardcode a location gate. Pick a Vertex region via `/agy:setup` or `GOOGLE_CLOUD_LOCATION`. These Gemini-3 models are currently served on `global`; a not-yet-available region hangs until the worker-turn timeout, so only choose another region once your model is actually served there.

### Persisting defaults

Run `/agy:setup` inside Claude Code. It asks for mode, model, and location, validates the combination, and writes `~/.config/cao/defaults.json` (or `$CAO_PLUGIN_DATA/defaults.json`). Defaults take effect on the next `/agy:implement` — no restart needed. API keys are never stored there.

---

## Usage

| Command | Description |
|---|---|
| `/agy:implement <task> [--model <id>] [--effort <level>] [--file <path>]... [--background] [--resume [id]] [--fresh]` | Start (or continue) a session. `--background` returns immediately; `--resume` continues a prior run; `--fresh` forces a new conversation. |
| `/agy:setup` | Persist model/region defaults to `defaults.json` via an interactive interview. |
| `/agy:approve <call_id> [project\|global]` | Approve a pending shell command. `project`/`global` remembers it for future runs. |
| `/agy:deny <call_id> [reason]` | Deny a pending shell command. |
| `/agy:status [session_id]` | Show session state. Omit `session_id` to target the active (or latest) session. |
| `/agy:events [session_id] [after_event_id]` | Show recent session events. |
| `/agy:cancel [session_id]` | Cancel the active session. |
| `/agy:retry [strategy]` | Retry the latest session. `strategy` is `clean` (default) or `resume`. |
| `/agy:review <target>` | Start a review session (worker reports findings without modifying files). |
| `/agy:watch <session_id>` | Watch a session; block until an approval is pending or it finishes. |

---

## Approvals

When the worker wants to run a shell command, the session suspends and records an `approval.required` event with a short, typeable id (`1`, `2`, ...).

`/agy:implement`, `/agy:retry`, and `/agy:review` watch the session for you: Claude calls `/agy:watch` (a bounded ~25s long-poll) in a loop. When an approval is pending, Claude presents it via the native `AskUserQuestion` menu (**Approve once / Approve for this project / Approve always / Deny**, arrow-key selectable) and relays your choice.

If you'd rather drive it manually, `/agy:status` and `/agy:watch` print ready-to-paste lines:

```
Pending approval(s) - paste one line to respond:
  - command: touch marker.txt
    approve: /agy:approve 1
    deny:    /agy:deny 1
```

Timeout (5 min) auto-denies.

### Approval memory (allowlist)

"Approve for this project" and "Approve always" remember the command so identical future commands auto-approve without a prompt.

- **File:** `$CAO_PLUGIN_DATA/approvals.json`, else `~/.config/cao/approvals.json`. Written atomically; a missing/corrupt file is treated as empty.
- **Matching is EXACT** — the whole command string must match byte-for-byte. No glob or regex, by design.
- **Scope:** `project` remembers only for the current workspace; `global` remembers everywhere; `once` (default) doesn't persist.
- **Security:** the allowlist only short-circuits the `run_command` approval step. It never overrides secret-file deny or workspace-containment policies. To revoke, edit or delete `approvals.json`; it's re-read fresh on every check.

---

## Security

Three SDK policies are always active, in priority order:

1. **Secret-file deny** — any tool whose `canonical_path` matches `.env`, `*.pem`, `*.key`, `*.crt`, `*.p12`, `id_rsa`, `id_ed25519`, or `id_dsa` is denied unconditionally. The SDK resolves symlinks and `..` before the predicate runs.
2. **Workspace containment** — file tools are restricted to the session workspace directory.
3. **Shell approval** — all `run_command` calls require explicit human approval via `/agy:approve`.

Policy evaluation is fully delegated to the SDK's `policy.enforce()`. Antigravity doesn't hand-roll a policy evaluator.

### Workspace and git

**No git repo or commit required.** The `Changed Files` digest comes from a private shadow git repository at `<state_dir>/shadow.git`. It snapshots any directory — git repo, non-git dir, fresh `git init`, dirty tree — by staging the working tree and writing tree objects, then diffing them. The workspace's own `.gitignore` is honored; its `.git/` is excluded.

The only thing that disables change tracking is a missing `git` binary. In that case the digest shows a Risk Note instead of `Changed Files`. The worker still runs.

---

## Testing

```bash
# Unit + integration tests (no live SDK needed)
pytest

# Live smoke test — requires real auth (Vertex ADC or GEMINI_API_KEY)
CAO_LIVE_TEST=1 pytest tests/e2e/test_live_smoke.py -v
```

The live smoke test fires a real Gemini turn through the real SDK and verifies the hook chain end-to-end.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [CHANGELOG.md](CHANGELOG.md).

License: Apache-2.0 — see [LICENSE](LICENSE).
