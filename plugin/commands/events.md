---
description: "Show recent session events (optional: after_event_id=<id>)"
argument-hint: "[session_id] [after_event_id]"
allowed-tools:
  - Bash(python:*)
---

Show recent session events. Pass an optional session_id and after_event_id to page results.

!python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" session.events "$ARGUMENTS"
