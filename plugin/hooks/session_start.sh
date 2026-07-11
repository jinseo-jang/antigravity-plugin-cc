#!/usr/bin/env bash
# SessionStart hook: install the cao backend into the plugin data dir. Installs the git tag
# matching the plugin version (v<version>) by default; reinstalls when that ref changes.

set -euo pipefail

PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-${CAO_PLUGIN_DATA:-}}"
if [[ -z "${PLUGIN_DATA}" ]]; then
  echo "Antigravity: CLAUDE_PLUGIN_DATA is not set; cannot install the cao package." >&2
  exit 0
fi

SITE_PACKAGES="${PLUGIN_DATA}/site-packages"
MARKER="${PLUGIN_DATA}/.cao_installed"
LOG="${PLUGIN_DATA}/.cao_install.log"
# Backend is installed from this GitHub repo (no PyPI release of the orchestrator needed);
# the [sdk] extra still pulls the google-antigravity SDK from PyPI. By default it pins to the
# git tag matching the plugin version (v<version>), so each plugin version installs its own
# reproducible backend. Set CAO_BACKEND_REF (e.g. "main") to override for bleeding-edge/dev.
PLUGIN_JSON="${CLAUDE_PLUGIN_ROOT:-}/.claude-plugin/plugin.json"
CURRENT_VER="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "${PLUGIN_JSON}" 2>/dev/null || echo unknown)"
BACKEND_REF="${CAO_BACKEND_REF:-v${CURRENT_VER}}"
PACKAGE="claude-antigravity-orchestrator[sdk] @ git+https://github.com/jinseo-jang/antigravity-plugin-cc@${BACKEND_REF}"

# Reinstall when the target ref changes (a plugin-version bump, or a CAO_BACKEND_REF change),
# so updating the plugin upgrades the backend. The marker stores the installed ref; deleting
# it forces a reinstall.
INSTALLED_REF="$(cat "${MARKER}" 2>/dev/null || echo none)"

if [[ "${BACKEND_REF}" != "${INSTALLED_REF}" ]]; then
  # --target keeps the install private and works on PEP 668 externally-managed systems.
  # Requires git + network (GitHub for the backend, PyPI for the google-antigravity SDK).
  # Install into a private per-process ".new.$$" dir and swap on success: a failed upgrade never
  # wipes a working backend, AND two Claude Code sessions sharing this PLUGIN_DATA never write the
  # same tree (a shared ".new" would let concurrent pip installs corrupt each other, then the
  # winner would ship a partial tree and poison the marker). pip output -> LOG since Claude Code
  # discards hook stdout; the marker is written only after the swap, so a failure leaves the old
  # install intact and retries next session.
  # ponytail: last writer wins; a sub-second window during rm+mv can make a concurrent import fail
  # once (self-heals next session) and a crashed process may orphan a .new.<pid> dir. Add a flock
  # (Linux) / atomic-mkdir lock if that ever matters.
  NEW="${SITE_PACKAGES}.new.$$"
  rm -rf "${NEW}"
  if pip install --quiet --target "${NEW}" "${PACKAGE}" > "${LOG}" 2>&1; then
    rm -rf "${SITE_PACKAGES}"
    mv "${NEW}" "${SITE_PACKAGES}"
    echo "${BACKEND_REF}" > "${MARKER}"
  else
    rm -rf "${NEW}"
    echo "Antigravity: SDK install failed; see ${LOG}" >&2
  fi
fi
