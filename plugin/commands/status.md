---
description: Show active session state
argument-hint: "[session_id]"
allowed-tools:
  - Bash(python:*)
---

Show the state of the active session (or a specific session by ID).

!python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" --plugin-data "${CLAUDE_PLUGIN_DATA}" session.status "$ARGUMENTS"
