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
# Installed straight from GitHub (no PyPI release of the orchestrator needed); the [sdk]
# extra still pulls the google-antigravity SDK from PyPI. Tracks main for fast iteration.
PACKAGE="git+https://github.com/jinseo-jang/antigravity-plugin-cc@main#egg=claude-antigravity-orchestrator[sdk]"

if [[ ! -f "${MARKER}" ]]; then
  # --target keeps the install private and works on PEP 668 externally-managed systems.
  # Requires git + network (GitHub for the backend, PyPI for the google-antigravity SDK).
  # pip output is captured to LOG so a failed install is diagnosable rather than silent
  # (Claude Code discards hook stdout). The marker is written only on success, so a
  # transient failure simply retries next session. Delete the marker to reinstall/update.
  if pip install --quiet --target "${SITE_PACKAGES}" "${PACKAGE}" > "${LOG}" 2>&1; then
    echo "${PACKAGE}" > "${MARKER}"
  else
    echo "Antigravity: SDK install failed; see ${LOG}" >&2
  fi
fi
