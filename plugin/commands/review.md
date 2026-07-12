---
description: Start a read-only review session (SDK policy denies all writes)
argument-hint: "<target description>"
allowed-tools:
  - Bash(python:*)
---

Start a read-only review session. The Antigravity daemon runs the worker with an SDK
policy that denies every state-mutating tool (`create_file`, `edit_file`,
`run_command`, `generate_image`, `start_subagent`) while allowing read-only
tools, then returns a digest of findings. Writes are blocked by policy, not
merely discouraged by prompt.

!python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" --plugin-data "${CLAUDE_PLUGIN_DATA}" session.review "$ARGUMENTS"

The command above prints a JSON object with a `session_id`. Review sessions cannot
write (mutating tools are stripped), so there are no approval prompts — just watch
until it finishes, using the Antigravity companion via the Bash tool:

1. Watch: run `python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" --plugin-data "${CLAUDE_PLUGIN_DATA}" session.wait <session_id>`.
   It blocks up to ~25s and returns "running" or "finished".
2. If it reports "running", run `session.wait <session_id>` again.
3. When it reports the session finished, run
   `... cao-companion.py session.events <session_id>` and show the user the findings digest.
