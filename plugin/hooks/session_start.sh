#!/usr/bin/env bash
# SessionStart hook: cao 패키지를 플러그인 데이터 디렉토리에 설치한다.
# 마커 파일이 있으면 건너뛴다 (diff-guard).

set -euo pipefail

# Require CLAUDE_PLUGIN_DATA or CAO_PLUGIN_DATA
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA:-${CAO_PLUGIN_DATA:-}}"
if [[ -z "${PLUGIN_DATA}" ]]; then
  echo "Antigravity: CLAUDE_PLUGIN_DATA is not set. Cannot install cao package." >&2
  exit 0
fi

SITE_PACKAGES="${PLUGIN_DATA}/site-packages"
MARKER="${PLUGIN_DATA}/.cao_installed"
PACKAGE="claude-antigravity-orchestrator[sdk]==0.1.0"

if [[ ! -f "${MARKER}" ]]; then
  if pip install --quiet --target "${SITE_PACKAGES}" "${PACKAGE}"; then
    echo "${PACKAGE}" > "${MARKER}"
  fi
fi
