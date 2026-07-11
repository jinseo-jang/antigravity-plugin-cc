#!/usr/bin/env bash
# Sync the version across the metadata files (pyproject, plugin.json, marketplace.json,
# CHANGELOG). The plugin.json version also gates the backend reinstall in session_start.sh,
# so bumping it is how a release reaches existing users.
#
# Files updated:
#   pyproject.toml                     version = "X.Y.Z"
#   plugin/.claude-plugin/plugin.json  "version": "X.Y.Z"
#   .claude-plugin/marketplace.json    "version": "X.Y.Z"  (plugin entry)
#   CHANGELOG.md                       new "## [X.Y.Z] - <date>" section
# session_start.sh installs the git tag v<version> matching plugin.json, so a release MUST
# also create + push that tag (see RELEASING.md); this script only syncs the version fields.
#
# Usage:
#   scripts/bump-version.sh X.Y.Z      # bump all version fields to X.Y.Z
#   scripts/bump-version.sh --self-check   # verify the bump logic on a throwaway copy

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

_bump() {
  # $1 = repo root to operate on, $2 = new version
  local root="$1" ver="$2" date
  date="$(date +%Y-%m-%d)"
  sed -i -E "s/^version = \"[^\"]+\"/version = \"${ver}\"/" "${root}/pyproject.toml"
  sed -i -E "s/\"version\": \"[^\"]+\"/\"version\": \"${ver}\"/" "${root}/plugin/.claude-plugin/plugin.json"
  sed -i -E "s/\"version\": \"[^\"]+\"/\"version\": \"${ver}\"/" "${root}/.claude-plugin/marketplace.json"
  # ponytail: GNU sed only (-i with no suffix, \n in the replacement). The guard keeps
  # a re-run for the same version from inserting a duplicate CHANGELOG section.
  if ! grep -q "^## \[${ver}\] - " "${root}/CHANGELOG.md"; then
    sed -i -E "s/^## \[Unreleased\]/## [Unreleased]\n\n## [${ver}] - ${date}/" "${root}/CHANGELOG.md"
  fi
}

if [[ "${1:-}" == "--self-check" ]]; then
  tmp="$(mktemp -d)"; trap 'rm -rf "${tmp}"' EXIT
  mkdir -p "${tmp}/plugin/.claude-plugin" "${tmp}/.claude-plugin"
  cp "${ROOT}/pyproject.toml" "${tmp}/pyproject.toml"
  cp "${ROOT}/plugin/.claude-plugin/plugin.json" "${tmp}/plugin/.claude-plugin/plugin.json"
  cp "${ROOT}/.claude-plugin/marketplace.json" "${tmp}/.claude-plugin/marketplace.json"
  cp "${ROOT}/CHANGELOG.md" "${tmp}/CHANGELOG.md"
  _bump "${tmp}" "9.9.9"
  fail=0
  grep -q '^version = "9.9.9"'      "${tmp}/pyproject.toml"                     || { echo "FAIL: pyproject.toml";     fail=1; }
  grep -q '"version": "9.9.9"'      "${tmp}/plugin/.claude-plugin/plugin.json"  || { echo "FAIL: plugin.json";        fail=1; }
  grep -q '"version": "9.9.9"'      "${tmp}/.claude-plugin/marketplace.json"    || { echo "FAIL: marketplace.json";   fail=1; }
  grep -q '## \[9.9.9\] - '         "${tmp}/CHANGELOG.md"                       || { echo "FAIL: CHANGELOG.md";       fail=1; }
  if [[ "${fail}" -eq 0 ]]; then echo "self-check PASS: all 4 version files bump correctly"; else echo "self-check FAILED"; exit 1; fi
  exit 0
fi

NEW="${1:-}"
if [[ -z "${NEW}" ]]; then echo "usage: $0 X.Y.Z | --self-check" >&2; exit 1; fi
if [[ ! "${NEW}" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
  echo "ERROR: '${NEW}' is not a valid SemVer (expected X.Y.Z)" >&2; exit 1
fi

_bump "${ROOT}" "${NEW}"
echo "Bumped all version fields to ${NEW}. Next: review 'git diff', fill in the CHANGELOG entry, then commit + tag v${NEW}."
