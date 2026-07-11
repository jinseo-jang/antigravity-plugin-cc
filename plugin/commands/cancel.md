---
description: Cancel the active session
argument-hint: "[session_id]"
allowed-tools:
  - Bash(python:*)
---

Cancel the active session (or a specific session by ID).

!python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" session.cancel "$ARGUMENTS"
