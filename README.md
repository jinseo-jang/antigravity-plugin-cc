# Antigravity Plugin for Claude Code

A Claude Code plugin that turns Claude into a supervisor for a [Google Antigravity SDK](https://pypi.org/project/google-antigravity/) worker. Claude plans and reviews; the Antigravity SDK runs Gemini to do the actual work. Every worker tool call is gated by SDK-native policy, shell commands require human approval, and each session ends with a git-diff digest.

---

## Quickstart

**1. Install** from the Claude Code plugin marketplace:

```
/plugin marketplace add jinseo-jang/antigravity-plugin-cc
/plugin install agy@agy-plugin-cc
```

Restart Claude Code once after installing so the backend installs itself (details in [Install](#install)), then pick an [auth mode](#auth).

**2. Configure defaults** — run `/agy:setup` and choose `vertex` (Vertex AI via ADC), model `gemini-3.6-flash`, location `global`. Your GCP project is auto-detected from your ADC — no environment variables required.

**3. Run a task** — open a project, then hand the worker something real. For example, generate an image and save it to your workspace:

```
/agy:implement "Generate an image of Seoul's current weather and save it to ./seoul_weather.png"
```

agy looks up the current weather, generates the image with Gemini, then asks you to approve writing the file (**Approve once / for this project / always**). After you approve, `seoul_weather.png` is saved into your workspace. Track progress with `/agy:status` and review the run with `/agy:events`.

---

## Install

Install from the Claude Code plugin marketplace (two steps):

```
/plugin marketplace add jinseo-jang/antigravity-plugin-cc
/plugin install agy@agy-plugin-cc
```

**No manual `pip install` needed.** On the first session after install, a bundled `SessionStart` hook installs the Python backend from this GitHub repo — pinned to the git tag matching the plugin's version (`pip install "claude-antigravity-orchestrator[sdk] @ git+https://github.com/jinseo-jang/antigravity-plugin-cc@v<version>"`, which includes the `google-antigravity` SDK) — into the plugin's private data directory and adds that directory to its Python path, so you never run `pip` yourself. Installing into a private `--target` directory works even on externally-managed (PEP 668) systems, and it re-installs whenever that version changes, so updating the plugin upgrades the backend too (see the FAQ). Contributors can set `CAO_BACKEND_REF=main` to install from `main` instead of a release tag — note this installs `main` **once**; delete `<plugin-data>/.cao_installed` to re-pull newer commits.

**Installing mid-session? Restart once.** The backend installs via a `SessionStart` hook, which Claude Code does **not** fire on a mid-session `/plugin install` (nor on `/reload-plugins`). So right after installing, the first `/agy:*` command will tell you the backend isn't installed yet — just **restart Claude Code once** (a full quit + relaunch) and re-run it. `/clear` and `/reload-plugins` are not reliable substitutes.

**Prerequisites you do need:**

- Python 3.11+, `pip`, and `git` on PATH (the hook runs `pip install git+https://…`)
- Network access on first run (GitHub for the backend, PyPI for the `google-antigravity` SDK)

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
| `gemini-3.6-flash` | GA (**default**) | `global` |
| `gemini-3.5-flash` | GA | `global` |
| `gemini-3.5-flash-lite` | GA | `global` |
| `gemini-3.1-pro-preview` | Public Preview | `global`; narrower regional availability |

Any other model string is rejected immediately with JSON-RPC `-32602` and a recovery message. Override with `CAO_MODEL=gemini-3.6-flash` or `--model` per session. All models support `--effort` (thinking levels).

**Region is your choice.** agy doesn't hardcode a location allowlist. Pick a Vertex region via `/agy:setup` or `GOOGLE_CLOUD_LOCATION`; these Gemini-3 models are currently served on `global` (the default). Before each Vertex turn agy runs a fast pre-flight probe — a region that definitively doesn't serve your model fails immediately with a clear `-32602` error rather than hanging. (Ambiguous or transient probe failures fall through and can still hang until the worker-turn timeout, so prefer `global` unless you know your region serves the model.)

### Persisting defaults

Run `/agy:setup` inside Claude Code. It asks for mode, model, and location, validates the combination, and writes `~/.config/cao/defaults.json` (or `$CAO_PLUGIN_DATA/defaults.json`). Defaults take effect on the next `/agy:implement` — no restart needed. API keys are never stored there.

---

## Usage

| Command | Description |
|---|---|
| `/agy:implement <task> [--model <id>] [--effort <level>] [--file <path>]... [--background] [--resume [id]] [--fresh]` | Start (or continue) a session. `--background` returns immediately; `--resume` continues a prior run; `--fresh` forces a new conversation. |
| `/agy:handoff <what the worker should do next> [--background]` | Hand the **current Claude conversation** to a new worker — the daemon summarizes it into the worker's context so it continues without a cold start (text-only). `--background` detaches. |
| `/agy:setup` | Persist model/region defaults to `defaults.json` via an interactive interview. |
| `/agy:approve <call_id> [project\|global]` | Approve a pending shell command. `project`/`global` remembers it for future runs. |
| `/agy:deny <call_id> [reason]` | Deny a pending shell command. |
| `/agy:status [session_id]` | Show session state. Omit `session_id` to target the active (or latest) session. |
| `/agy:events [session_id] [after_event_id]` | Show recent session events. |
| `/agy:cancel [session_id]` | Cancel the active session. |
| `/agy:retry [strategy]` | Retry the latest session. `strategy` is `clean` (default) or `resume`. |
| `/agy:review <target>` | Start a review session (worker reports findings without modifying files). |
| `/agy:watch <session_id>` | Watch a session; block until an approval is pending or it finishes. |

### Handoff vs. resume

`/agy:handoff <task>` hands your **current Claude Code conversation** to a fresh Antigravity worker. The daemon reads the conversation transcript, summarizes it (a bounded **text-only** excerpt — roughly the last 40 turns, truncated to ~12k characters), and injects it into the worker's system instructions so it continues with your context instead of starting cold. Only the conversation *text* carries over — **not** the tool-call history (files read, commands run). The session is writable and can request shell approvals; `--background` detaches it (retrieve later with `/agy:status`, `/agy:events`, `/agy:watch`).

This is different from `/agy:implement --resume`, which continues a **prior Antigravity worker session** (its own Gemini conversation) — not your Claude conversation.

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

## FAQ & warnings

**⚠️ Where does my session data live — and does it survive a reboot?**
By default the daemon writes session state under `/tmp/cao-companion/`, which most systems **wipe on reboot**. Your event log, digests, and the conversation trajectory (the SQLite `.db` that `--resume` replays) are **lost after a restart**. To keep them, point `CAO_PLUGIN_DATA` at a persistent directory:

```bash
# add to ~/.bashrc or ~/.zshrc
export CAO_PLUGIN_DATA="$HOME/.local/share/cao"
```

State then lives under `$CAO_PLUGIN_DATA/state/<workspace>-<hash>/` (`events.jsonl`, `digest.md`, `trajectories/<id>/<conversation_id>.db`, a private `shadow.git`, ...). `approvals.json` and `defaults.json` are always persistent (`~/.config/cao`, or `$CAO_PLUGIN_DATA` if set).

**How do I upgrade to a new version?** The backend reinstalls itself whenever the plugin's on-disk version (`plugin.json`) changes — so updating the plugin through Claude Code's plugin manager (which refreshes its files) upgrades the backend on the next session. To force a reinstall at any time, delete `<plugin-data>/.cao_installed`.

**What is the "digest"?** After each session the daemon writes a compact Markdown summary (`digest.md` / `digest-<session_id>.md`) — events, approvals, and a `Changed Files` git-diff. It's how you review a run without watching it live (`/agy:events`).

**Does `/agy:handoff` carry my whole Claude session?** No — it's **text-only**. A bounded summary of the conversation is passed to the worker; tool-call history (files read, commands run) does not carry over.

**A shell-approval prompt timed out.** Pending approvals **auto-deny after 5 minutes**. For long unattended runs, pre-approve expected commands with "Approve for this project" / "Approve always".

**My session hangs forever.** On Vertex, agy pre-probes your model×region and usually fails fast with a clear `-32602` when the region definitely can't serve the model. A hang instead means the probe was inconclusive (a transient or non-404 error) or you're in Gemini-API-key mode (which has no region check) — double-check your model×region and prefer the default `global` location.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [CHANGELOG.md](CHANGELOG.md).

License: Apache-2.0 — see [LICENSE](LICENSE).
