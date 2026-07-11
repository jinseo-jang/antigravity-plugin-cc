---
description: Approve a pending tool call by call_id
argument-hint: "<call_id>"
allowed-tools:
  - Bash(python:*)
---

Approve a pending tool call. The daemon will resume the suspended hook coroutine.

!python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" session.approve "$ARGUMENTS"
