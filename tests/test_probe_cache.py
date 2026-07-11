"""BL-20 — capability-probe result cache (successes only, atomic, corrupt-safe).

Mirrors test_defaults.py style. RED before probe_cache.py exists; GREEN after.
Availability is a property of (project, model, location), not the workspace, so
the cache is global (CAO_PLUGIN_DATA / ~/.config/cao), shared across workspaces.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cao.runtime import probe_cache


def test_load_missing_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No cache file yet -> load() is an empty set."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    assert probe_cache.load() == set()


def test_mark_ok_then_is_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After mark_ok, is_ok is True for that (project, model, location); False otherwise."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    probe_cache.mark_ok("proj", "gemini-3.5-flash", "global")
    assert probe_cache.is_ok("proj", "gemini-3.5-flash", "global") is True
    assert probe_cache.is_ok("proj", "gemini-3.5-flash", "us-central1") is False
    assert probe_cache.is_ok("other", "gemini-3.5-flash", "global") is False


def test_mark_ok_persists_and_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """mark_ok writes the file atomically; marking twice yields a single entry."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    probe_cache.mark_ok("proj", "gemini-3.5-flash", "global")
    probe_cache.mark_ok("proj", "gemini-3.5-flash", "global")
    assert (tmp_path / "probe_cache.json").exists()
    assert probe_cache.load() == {"proj|gemini-3.5-flash|global"}


def test_load_corrupt_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A corrupt cache file is treated as empty (never crashes the daemon)."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    (tmp_path / "probe_cache.json").write_text("{not json")
    assert probe_cache.load() == set()


def test_key_handles_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """None project/location don't crash key formation (api_key-mode safety)."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    probe_cache.mark_ok(None, "gemini-3.5-flash", None)
    assert probe_cache.is_ok(None, "gemini-3.5-flash", None) is True
