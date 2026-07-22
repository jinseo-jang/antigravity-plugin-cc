# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/ko/1.0.0/).

## [Unreleased]
### Added
- Added `gemini-3.6-flash` (new default) and `gemini-3.5-flash-lite` to the supported-model allowlist; updated recovery message and docs accordingly.

## [0.1.2] - 2026-07-12
### Fixed
- Companion now receives the plugin data dir via a `--plugin-data` argument (from `${CLAUDE_PLUGIN_DATA}`) instead of relying on the `$CLAUDE_ENV_FILE` bridge, which Claude Code does not reliably propagate to slash-command Bash env (notably under `--resume`). Fixes "backend not installed" when the backend actually is installed.
- Backend now resolves the GCP project for Vertex mode from ADC `quota_project_id` / gcloud config (not just `google.auth.default()`, which returns `None` for authorized-user ADC), and raises a clear error instead of crashing when no project can be resolved.
- Worker crashes now surface a `session.ended` event with the reason and are written to `<state_dir>/daemon.log` (previously the traceback was lost to `DEVNULL`).
- `/agy:setup` can capture the GCP project for Vertex mode.
- Backend-absent tests isolated with `python -S` so CI (system-installed package) reflects real absence.

## [0.1.1] - 2026-07-12
### Fixed
- Accurate "restart Claude Code" message when the Python backend is missing after a mid-session `/plugin install` (SessionStart does not fire mid-session); guards added in both the setup and daemon command paths. SessionEnd stays silent.

## [0.1.0] - 2026-07-11
### Added
- Initial public release
