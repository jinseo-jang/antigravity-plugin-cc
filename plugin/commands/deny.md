---
description: "Deny a pending tool call by call_id (optional: reason)"
argument-hint: "<call_id> [reason]"
allowed-tools:
  - Bash(python:*)
---

Deny a pending tool call. The daemon will reject the suspended hook coroutine with an optional reason.

!python "${CLAUDE_PLUGIN_ROOT}/scripts/cao-companion.py" session.deny "$ARGUMENTS"
