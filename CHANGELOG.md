# Changelog

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/ko/1.0.0/).

## [Unreleased]

## [0.1.1] - 2026-07-12
### Fixed
- Accurate "restart Claude Code" message when the Python backend is missing after a mid-session `/plugin install` (SessionStart does not fire mid-session); guards added in both the setup and daemon command paths. SessionEnd stays silent.

## [0.1.0] - 2026-07-11
### Added
- Initial public release
