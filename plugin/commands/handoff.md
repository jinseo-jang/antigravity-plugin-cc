---
description: Hand the current Claude conversation to an Antigravity worker to continue
argument-hint: "<what the worker should do next> [--background]"
allowed-tools:
  - Bash(python:*)
  - AskUserQuestion
---

Hand the current Claude Code conversation to an Antigravity worker. The daemon
summarizes this conversation into the worker's `system_instructions` context, so
the worker continues with the background you built up here — not a cold start.
This is a **text-only handoff**: the conversation summary carries over, but the
tool-call history (files read, commands run) does not.

!python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" session.handoff "$ARGUMENTS"

The command above prints a JSON object with a `session_id`. Handoff sessions are
writable (the worker does real work) and can request shell approvals.

`--background` starts the handoff detached and returns the `session_id` immediately,
skipping the watch loop — use it when the handed-off work is large and you don't want
to babysit it. Retrieve results later with `/agy:status <id>`, `/agy:events <id>`, or
`/agy:watch <id>`.

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
   Then return to step 1.
4. When it reports the session finished, run
   `... cao-companion.py session.events <session_id>` and show the user the digest.

Never approve or deny on your own — the user decides every approval via AskUserQuestion.

**Background (`--background`):** do NOT watch. Print the returned `session_id` and
the retrieval hints (`/agy:status <id>`, `/agy:events <id>`, `/agy:watch <id>`) and
stop. Handoff sessions are writable, so if a non-allowlisted shell command is hit
while unattended, the session suspends; `/agy:status <id>` prints the paste-ready
`/agy:approve <id> [project|global]` line, and the 5-minute approval timeout
auto-denies — for long unattended handoffs, pre-allowlist expected commands with
"Approve for this project" / "Approve always".
