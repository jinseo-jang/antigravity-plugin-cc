#!/usr/bin/env bash
# SessionStart hook: install the cao backend package into the plugin data dir.
# Skips when the marker file already exists (diff-guard).

set -euo pipefail

PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-${CAO_PLUGIN_DATA:-}}"
if [[ -z "${PLUGIN_DATA}" ]]; then
  echo "Antigravity: CLAUDE_PLUGIN_DATA is not set; cannot install the cao package." >&2
  exit 0
fi

SITE_PACKAGES="${PLUGIN_DATA}/site-packages"
MARKER="${PLUGIN_DATA}/.cao_installed"
LOG="${PLUGIN_DATA}/.cao_install.log"
PACKAGE="claude-antigravity-orchestrator[sdk]==0.1.0"

if [[ ! -f "${MARKER}" ]]; then
  # --target keeps the install private and works on PEP 668 externally-managed systems.
  # pip output is captured to LOG so a failed install is diagnosable rather than silent
  # (Claude Code discards hook stdout). The marker is written only on success, so a
  # transient failure (offline, PyPI hiccup) simply retries on the next session.
  if pip install --quiet --target "${SITE_PACKAGES}" "${PACKAGE}" > "${LOG}" 2>&1; then
    echo "${PACKAGE}" > "${MARKER}"
  else
    echo "Antigravity: SDK install failed; see ${LOG}" >&2
  fi
fi
