#!/usr/bin/env bash
# SessionStart hook: install the cao backend into the plugin data dir. Reinstalls when the
# plugin version changes (the marker stores the installed version); skips when they match.

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

# Reinstall when the plugin version changes, so updating the plugin (marketplace) upgrades
# the backend too. The marker stores the installed plugin version; a mismatch - or a manual
# `rm` of the marker - triggers a clean reinstall.
PLUGIN_JSON="${CLAUDE_PLUGIN_ROOT:-}/.claude-plugin/plugin.json"
CURRENT_VER="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "${PLUGIN_JSON}" 2>/dev/null || echo unknown)"
INSTALLED_VER="$(cat "${MARKER}" 2>/dev/null || echo none)"

if [[ "${CURRENT_VER}" != "${INSTALLED_VER}" ]]; then
  # --target keeps the install private and works on PEP 668 externally-managed systems.
  # Requires git + network (GitHub for the backend, PyPI for the google-antigravity SDK).
  # Install into a fresh ".new" dir and swap on success, so a failed upgrade never wipes a
  # working backend. pip output -> LOG since Claude Code discards hook stdout; the marker is
  # written only after the swap, so a failure leaves the old install intact and retries next session.
  rm -rf "${SITE_PACKAGES}.new"
  if pip install --quiet --target "${SITE_PACKAGES}.new" "${PACKAGE}" > "${LOG}" 2>&1; then
    rm -rf "${SITE_PACKAGES}"
    mv "${SITE_PACKAGES}.new" "${SITE_PACKAGES}"
    echo "${CURRENT_VER}" > "${MARKER}"
  else
    rm -rf "${SITE_PACKAGES}.new"
    echo "Antigravity: SDK install failed; see ${LOG}" >&2
  fi
fi
