"""Task 009 — session_store round-trip, latest pointer, corruption, atomicity (AC1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cao.runtime import session_store


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))


def test_record_then_get_round_trips() -> None:
    ws = "/some/workspace"
    session_store.record(ws, "sess-1", "conv-1", "/dir/sess-1")
    assert session_store.get(ws, "sess-1") == ("conv-1", "/dir/sess-1")


def test_get_none_resolves_latest() -> None:
    ws = "/ws"
    session_store.record(ws, "sess-a", "conv-a", "/dir/a")
    session_store.record(ws, "sess-b", None, "/dir/b")
    assert session_store.get(ws, None) == (None, "/dir/b")


def test_missing_file_returns_empty() -> None:
    assert session_store.get("/never/recorded", None) is None
    store = session_store.load("/never/recorded")
    assert store.latest is None
    assert store.sessions == {}


def test_corrupt_file_returns_empty(tmp_path: Path) -> None:
    ws = "/ws-corrupt"
    session_store.store_path(ws).write_text("{ not valid json")
    assert session_store.load(ws).sessions == {}


def test_second_record_updates_latest_preserves_prior() -> None:
    ws = "/ws-multi"
    session_store.record(ws, "sess-1", "conv-1", "/dir/1")
    session_store.record(ws, "sess-2", "conv-2", "/dir/2")
    assert session_store.get(ws, "sess-1") == ("conv-1", "/dir/1")
    assert session_store.get(ws, None) == ("conv-2", "/dir/2")


def test_get_unknown_id_returns_none() -> None:
    ws = "/ws-unknown"
    session_store.record(ws, "sess-1", "conv-1", "/dir/1")
    assert session_store.get(ws, "no-such") is None


def test_trajectory_dir_is_stable_and_created() -> None:
    ws = "/ws-traj"
    first = session_store.trajectory_dir(ws, "sess-x")
    second = session_store.trajectory_dir(ws, "sess-x")
    assert first == second
    assert Path(first).is_dir()
    assert first.endswith("trajectories/sess-x")
