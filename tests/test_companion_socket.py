"""BL-21 — daemon and companion resolve the SAME anchored socket path.

The companion cannot import cao, so it duplicates the slug-hash + root-resolution
logic inline; this cross-checks the two copies never diverge (else they compute
different sockets and never connect). RED before wiring; GREEN after.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from cao.runtime import daemon, workspace

_COMPANION_PATH = Path(__file__).resolve().parents[1] / "plugin" / "scripts" / "cao-companion.py"


def _load_companion() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cao_companion", _COMPANION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


companion = _load_companion()


def test_daemon_and_companion_resolve_same_anchored_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From a worker-created subdir, both daemon and companion anchor to the parent
    (which bears the root marker) and compute the identical rpc.sock path."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("CAO_WORKSPACE", raising=False)
    parent = tmp_path / "proj"
    parent.mkdir()
    sub = parent / "super_mario"
    sub.mkdir()
    marker_dir = workspace.state_dir(parent)
    marker_dir.mkdir(parents=True)  # prior session established parent
    (marker_dir / "root").touch()  # BL-21: deliberate root marker
    monkeypatch.chdir(sub)

    expected = workspace.state_dir(parent) / "rpc.sock"
    assert daemon.socket_path() == expected
    assert Path(companion._socket_path()) == expected


def test_daemon_and_companion_agree_on_marker_and_env_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover the OTHER two resolution branches so a divergence in _MARKERS or the
    CAO_WORKSPACE handling between the daemon copy and the companion copy is caught
    (Oracle: the first test only exercised the state-dir-anchor branch)."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))

    # marker branch: a subdir deep inside a .git repo -> both anchor to the repo root
    monkeypatch.delenv("CAO_WORKSPACE", raising=False)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    deep = repo / "pkg" / "deep"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert daemon.socket_path() == Path(companion._socket_path())
    assert daemon.socket_path() == workspace.state_dir(repo.resolve()) / "rpc.sock"

    # CAO_WORKSPACE branch: explicit env wins identically for both copies
    ws = tmp_path / "explicit"
    ws.mkdir()
    monkeypatch.setenv("CAO_WORKSPACE", str(ws))
    assert daemon.socket_path() == Path(companion._socket_path())
    assert daemon.socket_path() == workspace.state_dir(ws.resolve()) / "rpc.sock"


def test_daemon_and_companion_agree_on_blocklisted_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-21 regression parity: a stray root marker at /tmp must NOT make EITHER the
    daemon or the companion anchor to /tmp — both apply the blocklist identically and
    fall back to the same non-/tmp workspace (else they'd diverge or collapse)."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("CAO_WORKSPACE", raising=False)
    tmp_state = workspace.state_dir(Path("/tmp"))
    tmp_state.mkdir(parents=True, exist_ok=True)
    (tmp_state / "root").touch()  # simulate BL-21 pollution at /tmp
    sub = tmp_path / "proj" / "sub"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)

    tmp_sock = workspace.state_dir(Path("/tmp")) / "rpc.sock"
    assert daemon.socket_path() == Path(companion._socket_path())  # parity
    assert daemon.socket_path() != tmp_sock  # neither collapses to /tmp
