---
description: Watch a session and surface pending approvals or completion
argument-hint: "<session_id>"
allowed-tools:
  - Bash(python:*)
---

Watch a running session. This blocks up to about 25 seconds and returns as soon
as the worker needs approval or the session finishes.

- If it reports a pending approval, it prints ready-to-paste `/agy:approve <id>`
  and `/agy:deny <id>` lines. Respond with one of them.
- If it reports "running", run `/agy:watch <session_id>` again to keep watching.
- If it reports the session finished, use `/agy:events <session_id>` to see the digest.

!python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" session.wait "$ARGUMENTS"
