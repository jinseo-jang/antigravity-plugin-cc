---
description: Retry from a failure using a named strategy
argument-hint: "[strategy]"
allowed-tools:
  - Bash(python:*)
  - AskUserQuestion
---

Retry the session from a failure point using a named retry strategy.

!python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" --plugin-data "${CLAUDE_PLUGIN_DATA}" session.retry "$ARGUMENTS"

The command above prints a JSON object with a `session_id`. Supervise that session
until it finishes, using the Antigravity companion via the Bash tool:

1. Watch: run `python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" --plugin-data "${CLAUDE_PLUGIN_DATA}" session.wait <session_id>`.
   It blocks up to ~25s and returns a pending approval, "running", or "finished".
2. If it reports "running", run `session.wait <session_id>` again.
3. If it reports a **pending approval**, call the **AskUserQuestion** tool showing the
   exact command, with four options: **Approve once**, **Approve for this project**,
   **Approve always**, and **Deny**. Then apply the choice:
   - Approve once: `... cao-companion.py session.approve <call_id>`
   - Approve for this project: `... cao-companion.py session.approve <call_id> project`
   - Approve always: `... cao-companion.py session.approve <call_id> global`
   - Deny: `... cao-companion.py session.deny <call_id>`
   "For this project" and "always" remember the EXACT command so identical future
   commands auto-approve without prompting. Then return to step 1.
4. When it reports the session finished, run
   `... cao-companion.py session.events <session_id>` and show the user the digest.

Never approve or deny on your own — the user decides every approval via AskUserQuestion.
