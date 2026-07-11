# Antigravity Orchestrator

Antigravity is a Claude Code plugin that supervises a real [Google Antigravity SDK](https://pypi.org/project/google-antigravity/) worker. Claude acts as the supervisor; the Antigravity SDK runs the Gemini model that does the actual work. Antigravity gates every tool call through SDK-native policy, routes shell commands through human approval, and records a git-diff digest after each session.

---

## Architecture

```
Claude Code (supervisor)
  │  plugin commands (/agy:implement, /agy:approve, /agy:deny, ...)
  │  JSON-RPC 2.0 over Unix Domain Socket
  ▼
Antigravity Companion Daemon  (src/cao/runtime/daemon.py)
  │  SessionManager → CAOPreToolCallDecideHook(wraps sdk_policy.enforce(policies))
  │                 → LocalAgentConfig(hooks=[hook,...], policies=[], workspaces, **auth)
  │                 → Agent.chat(task)
  ▼
Antigravity SDK worker  (google-antigravity wheel, bundles Go localharness)
  │  every tool call passes through:
  │    CAOPreToolCallDecideHook → sdk_policy.enforce(policies)
  │      ├─ deny("*", when=credential_path)   [Global Deny]
  │      ├─ workspace_only([ws])               [Specific Deny per file tool]
  │      └─ confirm_run_command(ask_handler)   [Ask → ApprovalWaiter → IPC]
  │    CAOOnSessionStartHook  → git before-snapshot + session.started event
  │    CAOOnSessionEndHook    → git after-snapshot + Markdown digest
  ▼
EventBus → events.jsonl (append-only, workspace-isolated state dir)
GitDiffCollector → real git via asyncio subprocess
DigestGenerator → compact Markdown digest from events.jsonl
```

---

## Install

### 1. Install the Antigravity SDK

The official wheel bundles the Go localharness binary:

```bash
pip install google-antigravity
# or, inside a venv:
pip install --break-system-packages google-antigravity
```

### 2. Install the Claude Code Plugin

Install the plugin directly from the Claude Code marketplace:

```bash
/plugin marketplace add code-yeongyu/antigravity-plugin-cc
/plugin install agy@code-yeongyu/antigravity-plugin-cc
```

The plugin will automatically install the `claude-antigravity-orchestrator` Python package into its cache directory when you start a session.

## Auth setup

Antigravity supports two auth modes. Set one before starting the daemon.

### Mode A: Vertex AI via gcloud ADC (recommended for GCP users)

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_CLOUD_LOCATION=global   # default; set any region where your model is served
```

Antigravity detects `GOOGLE_CLOUD_PROJECT` and uses ADC automatically. No API key needed.

### Mode B: Gemini API key

```bash
export GEMINI_API_KEY=your-api-key-here
```

Antigravity detects a Gemini API key and uses the Gemini API directly. The key is read from three places, in order: (1) the **OS keychain** — `python -m keyring set cao gemini_api_key` (encrypted at rest, recommended); (2) the **`GEMINI_API_KEY`** environment variable (Google's recommended location); (3) a **plaintext file** `~/.config/cao/gemini_api_key` (chmod 600) — last resort, not encrypted.

**Resolver precedence:** within Gemini-key mode the key resolves keychain → `GEMINI_API_KEY` → key file, and a resolved Gemini key wins over ADC/Vertex. If no key resolves and `GOOGLE_CLOUD_PROJECT` is unset, the daemon raises `AuthNotConfigured` on startup.

**Supported models:** agy accepts exactly two models:

| Model | Status | Vertex regions |
|---|---|---|
| `gemini-3.5-flash` | GA | `global` (default; currently the region these models are served on) |
| `gemini-3.1-pro-preview` | Public Preview | `global` (default); narrower regional availability |

Any other model string (including `gemini-2.5-flash`, `gemini-3.1-pro`, etc.) is rejected immediately with JSON-RPC `-32602` and a recovery message. The default model is `gemini-3.5-flash`; the default location is `global`. Override with `CAO_MODEL=gemini-3.5-flash` or `--model` per session.

**Region is your choice.** agy does not hardcode a location gate — Gemini's regional availability keeps expanding, so you pick the Vertex region via `/agy:setup` or `GOOGLE_CLOUD_LOCATION`. As of now these Gemini-3 models are served on `global` (the default); a not-yet-available region is **not** blocked up front — it hangs until the worker-turn timeout, so only choose another region once your model is actually served there. Both models support `--effort` (thinking levels).

### Persisting defaults with /agy:setup

Instead of setting environment variables every session, run `/agy:setup` inside Claude Code. It asks which mode you want (`vertex` or `gemini_api_key`), then which model and location, validates the combination, and writes `~/.config/cao/defaults.json` (or `$CAO_PLUGIN_DATA/defaults.json`). Defaults take effect on the next `/agy:implement` — no daemon restart needed.

```
/agy:setup
```

To clear defaults, delete `defaults.json` directly. API keys are never stored there; keep `GEMINI_API_KEY` in your environment.

### Workspace prerequisite (git)

**No git repo or commit required.** The `Changed Files` section of the digest is produced by a private shadow git repository at `<state_dir>/shadow.git`. It snapshots any directory — git repo, non-git dir, fresh `git init`, dirty tree — by staging the working tree and writing tree objects, then diffing them. The workspace's own `.gitignore` is honored; the workspace's own `.git/` is excluded.

The only state that disables objective change tracking is a **missing `git` binary**. In that case `no_git_repo` is set and the digest shows a Risk Note instead of `Changed Files`. The worker still runs.

---

## Usage

Once loaded, these slash commands are available inside Claude Code:

| Command | Description |
|---|---|
| `/agy:implement <task> [--model <id>] [--effort <level>] [--file <path>]... [--background] [--resume [id]] [--fresh]` | Start (or continue) an Antigravity session. `--background` returns immediately; `--resume` continues a prior run; `--fresh` forces a new conversation. |
| `/agy:setup` | Persist model/region defaults to `defaults.json` via an interactive interview. No daemon restart needed. |
| `/agy:approve <call_id> [project\|global]` | Approve a pending tool call (shell command). `project`/`global` remembers the command for future runs. |
| `/agy:deny <call_id> [reason]` | Deny a pending tool call |
| `/agy:status [session_id]` | Show session state. Omit `session_id` to target the active (or latest) session. |
| `/agy:events [session_id] [after_event_id]` | Show recent session events |
| `/agy:cancel [session_id]` | Cancel the active session |
| `/agy:retry [strategy]` | Retry the latest session. `strategy` is `clean` (default, fresh conversation) or `resume` (keep conversation). |
| `/agy:review <target>` | Start a review session (worker reports findings without modifying files) |
| `/agy:watch <session_id>` | Watch a session; block until an approval is pending or it finishes |

### Approvals

When the Antigravity worker wants to run a shell command, Antigravity suspends the session
and records an `approval.required` event with a **short, typeable id** (`1`, `2`, ...).

`/agy:implement`, `/agy:retry`, and `/agy:review` instruct Claude to **watch the session
for you**: after starting, Claude calls `/agy:watch` (a bounded ~25s long-poll) in a loop.
The moment an approval is pending, Claude presents it via the native `AskUserQuestion`
menu (**Approve once / Approve for this project / Approve always / Deny**, arrow-key
selectable) and relays your choice with `/agy:approve <id> [project|global]` or
`/agy:deny <id>`.

#### Approval memory (allowlist)

"Approve for this project" and "Approve always" remember the command so identical
future commands auto-approve without a prompt (the daemon emits an
`approval.auto_allowed` event instead of suspending).

- **File:** `$CAO_PLUGIN_DATA/approvals.json` if `CAO_PLUGIN_DATA` is set, else
  `~/.config/cao/approvals.json`. It survives reboots (not under the ephemeral
  `/tmp` state dir), is written atomically, and a missing/corrupt file is treated
  as empty. Shape: `{"global": [...], "projects": {"<abs workspace path>": [...]}}`.
- **Matching is EXACT** — the whole command string must match byte-for-byte. There
  is no prefix, substring, glob, or regex matching, by design.
- **Scope:** `project` remembers only for the current workspace; `global` remembers
  everywhere. `once` (the default) does not persist.
- **Security note:** the allowlist only short-circuits the `run_command` approval
  step. It NEVER overrides the secret-file deny or workspace-containment policies —
  a remembered command still cannot touch `.env`/keys or escape the workspace. To
  revoke, edit or delete `approvals.json`; the file is re-read fresh on every check.

If you'd rather drive it manually, `/agy:status <session_id>` and `/agy:watch <session_id>`
print the exact ready-to-paste lines:

```
Pending approval(s) - paste one line to respond:
  - command: touch marker.txt
    approve: /agy:approve 1
    deny:    /agy:deny 1
```

Timeout (5 min) auto-denies.

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

## Security posture

Three SDK policies are always active, in priority order:

1. **Secret-file deny** — any tool whose `canonical_path` is `.env`, `*.pem`, `*.key`, `*.crt`, `*.p12`, `id_rsa`, `id_ed25519`, or `id_dsa` is denied unconditionally. The SDK resolves symlinks and `..` before the predicate runs.
2. **Workspace containment** — file tools are restricted to the session workspace directory.
3. **Shell approval** — all `run_command` calls require explicit human approval via `/agy:approve`.

Policy evaluation is fully delegated to the SDK's `policy.enforce()`. Antigravity does not hand-roll a policy evaluator.
