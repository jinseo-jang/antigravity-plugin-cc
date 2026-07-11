"""Tests for approval_store — persistent allowlist (exact-match memory)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cao.runtime import approval_store


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))


def test_store_path_uses_env(tmp_path: Path) -> None:
    assert approval_store.store_path() == tmp_path / "approvals.json"


def test_store_path_defaults_to_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAO_PLUGIN_DATA", raising=False)
    assert approval_store.store_path() == Path.home() / ".config" / "cao" / "approvals.json"


def test_missing_file_is_empty() -> None:
    store = approval_store.load()
    assert store.global_ == []
    assert store.projects == {}


def test_corrupt_file_is_empty() -> None:
    approval_store.store_path().parent.mkdir(parents=True, exist_ok=True)
    approval_store.store_path().write_text("{not json")
    store = approval_store.load()
    assert store.global_ == []
    assert store.projects == {}


def test_remember_and_roundtrip_project() -> None:
    approval_store.remember("ls -la", "/ws", "project")
    reloaded = approval_store.load()
    assert reloaded.projects == {"/ws": ["ls -la"]}
    assert reloaded.global_ == []


def test_remember_global() -> None:
    approval_store.remember("pwd", "/ws", "global")
    assert approval_store.load().global_ == ["pwd"]


def test_remember_dedupe() -> None:
    approval_store.remember("ls", "/ws", "project")
    approval_store.remember("ls", "/ws", "project")
    approval_store.remember("pwd", "/ws", "global")
    approval_store.remember("pwd", "/ws", "global")
    store = approval_store.load()
    assert store.projects["/ws"] == ["ls"]
    assert store.global_ == ["pwd"]


def test_remember_once_is_noop() -> None:
    approval_store.remember("ls", "/ws", "once")  # type: ignore[arg-type]
    assert not approval_store.store_path().exists()


def test_is_allowed_exact_match_only() -> None:
    approval_store.remember("git status", "/ws", "project")
    assert approval_store.is_allowed("git status", "/ws") is True
    assert approval_store.is_allowed("git statu", "/ws") is False
    assert approval_store.is_allowed("git status --short", "/ws") is False
    assert approval_store.is_allowed("git", "/ws") is False


def test_is_allowed_project_isolation() -> None:
    approval_store.remember("make", "/ws-a", "project")
    assert approval_store.is_allowed("make", "/ws-a") is True
    assert approval_store.is_allowed("make", "/ws-b") is False


def test_is_allowed_global_everywhere() -> None:
    approval_store.remember("echo hi", "/ws-a", "global")
    assert approval_store.is_allowed("echo hi", "/ws-a") is True
    assert approval_store.is_allowed("echo hi", "/ws-b") is True
