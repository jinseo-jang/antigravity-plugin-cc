---
description: Start an implement session via the Antigravity Companion Daemon
argument-hint: "<task description> [--model <id>] [--effort <minimal|low|medium|high>] [--file <path>]... [--background] [--resume [session_id]] [--fresh]"
allowed-tools:
  - Bash(python:*)
  - AskUserQuestion
---

Start an implement session. The Antigravity daemon runs the Antigravity SDK worker under
supervision, enforcing policy, collecting git diffs, and returning a digest.

Continuity flags:

- `--background` — start the run detached and return a `session_id` immediately,
  skipping the watch loop. Retrieve results later with `/agy:status <id>`,
  `/agy:events <id>`, or `/agy:watch <id>`.
- `--resume [session_id]` — continue a prior run so the worker sees its earlier
  trajectory. Bare `--resume` resumes the latest run; put the task text first and
  `--resume` last to keep the task intact.
- `--fresh` — force a brand-new conversation (overrides `--resume`).

`--model`, `--effort`, and `--file` work on all variants.

When `--file` is an image, the worker should look at the image directly rather than writing throwaway shell scripts to inspect it — unnecessary scripts trigger extra approval prompts.

!python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" session.implement "$ARGUMENTS"

The command above prints a JSON object with a `session_id`.

**Recovery on error:** if the companion returns a JSON-RPC error (e.g. `-32602`
for an incompatible `--model`/`--effort`/location combo), do NOT just print it.
Present the error message and its **Options** via the **AskUserQuestion** tool,
then re-run `session.implement` with the corrected args.

**Foreground (no `--background`):** supervise the session until it finishes,
using the Antigravity companion via the Bash tool:

1. Watch: run `python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" session.wait <session_id>`.
   It blocks up to ~25s and returns a pending approval, "running", or "finished".
2. If it reports "running", run `session.wait <session_id>` again.
3. If it reports a **pending approval** (a shell command needs a decision), call the
   **AskUserQuestion** tool showing the exact command, with four options:
   **Approve once**, **Approve for this project**, **Approve always**, and **Deny**.
   Then apply the user's choice by running the companion:
   - Approve once: `... cao-companion.py session.approve <call_id>`
   - Approve for this project: `... cao-companion.py session.approve <call_id> project`
   - Approve always: `... cao-companion.py session.approve <call_id> global`
   - Deny: `... cao-companion.py session.deny <call_id>`
   "For this project" and "always" remember the EXACT command so identical future
   commands auto-approve without prompting. Then return to step 1.
4. When it reports the session finished, run
   `... cao-companion.py session.events <session_id>` and show the user the digest.

Never approve or deny on your own — the user decides every approval via AskUserQuestion.

**Background (`--background`):** do NOT watch. Print the returned `session_id`
and the retrieval hints (`/agy:status <id>`, `/agy:events <id>`, `/agy:watch <id>`)
and stop. If a non-allowlisted shell command is hit while unattended, the session
suspends; `/agy:status <id>` prints the paste-ready `/agy:approve <id> [project|global]`
line. The 5-minute approval timeout auto-denies — for long unattended runs,
pre-allowlist expected commands with "Approve for this project" / "Approve always".
