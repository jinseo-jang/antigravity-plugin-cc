"""BL-21 — workspace anchoring to a stable project root.

resolve order: CAO_WORKSPACE > marker(.git/.claude-plugin) > STRICT ancestor with an
existing cao state dir > cwd. The strict-ancestor rule fixes the reported bug: a
worker-created subdir (super_mario) already has its OWN state dir from the split, so
`start` must be excluded when scanning for an existing-state-dir ancestor.

RED before workspace.py exists; GREEN after.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cao.runtime import workspace


def test_env_var_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit CAO_WORKSPACE always wins, resolved."""
    monkeypatch.setenv("CAO_WORKSPACE", str(tmp_path))
    assert workspace.resolve_workspace() == tmp_path.resolve()


def test_git_marker_from_subdir(tmp_path: Path) -> None:
    """A subdir of a git repo anchors to the repo root (.git marker)."""
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert workspace._find_root(sub) == tmp_path


def test_claude_plugin_marker(tmp_path: Path) -> None:
    """.claude-plugin also anchors as a project marker."""
    (tmp_path / ".claude-plugin").mkdir()
    sub = tmp_path / "x"
    sub.mkdir()
    assert workspace._find_root(sub) == tmp_path


def test_non_git_subdir_attaches_to_ancestor_with_root_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-git: a subdir attaches to the ancestor bearing a deliberate root marker.

    BL-21 fix: the anchor signal is the daemon-written ``state_dir/'root'`` marker,
    not mere state-dir existence — a leftover state dir must NOT anchor.
    """
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    parent = tmp_path / "proj"
    parent.mkdir()
    sub = parent / "sub"
    sub.mkdir()
    marker_dir = workspace.state_dir(parent)
    marker_dir.mkdir(parents=True)
    (marker_dir / "root").touch()
    assert workspace._find_root(sub) == parent


def test_split_subdir_with_own_state_dir_prefers_marked_ancestor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The original split case: the subdir has its OWN state dir, but a strict
    ancestor bears the root marker -> anchor to the ancestor (strict-ancestor walk),
    NOT the subdir. The subdir's own state dir (no marker) must not self-anchor.
    """
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    parent = tmp_path / "agy-try"
    parent.mkdir()
    sub = parent / "super_mario"
    sub.mkdir()
    marker_dir = workspace.state_dir(parent)
    marker_dir.mkdir(parents=True)
    (marker_dir / "root").touch()
    workspace.state_dir(sub).mkdir(parents=True)  # subdir split off, but no marker
    assert workspace._find_root(sub) == parent


def test_no_marker_no_ancestor_falls_back_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-git, no marker, no ancestor state dir -> cwd is the root."""
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    assert workspace._find_root(lonely) == lonely


def test_ancestor_state_dir_without_root_marker_not_anchored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BL-21 fix: an ancestor state dir with NO root marker must NOT anchor.

    This is the exact regression guard: /tmp had a stray state dir but no deliberate
    marker, so it must not swallow the workspace — the subdir keeps itself.
    """
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    parent = tmp_path / "proj"
    parent.mkdir()
    sub = parent / "sub"
    sub.mkdir()
    workspace.state_dir(parent).mkdir(parents=True)  # exists, but bears NO 'root' marker
    assert workspace._find_root(sub) == sub


def test_blocklisted_broad_root_with_marker_not_anchored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BL-21 regression guard: a broad root (/tmp) bearing a root marker must NOT be
    anchored — the blocklist prevents workspace collapse to a shared root.
    """
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path / "data"))
    tmp_state = workspace.state_dir(Path("/tmp"))
    tmp_state.mkdir(parents=True, exist_ok=True)
    (tmp_state / "root").touch()  # simulate the BL-21 pollution: a marker at /tmp
    sub = tmp_path / "proj" / "sub"
    sub.mkdir(parents=True)
    result = workspace._find_root(sub)
    assert result != Path("/tmp")
    assert result == sub


def test_state_dir_and_socket_scheme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """state_dir/socket_path use the slug-hash scheme under CAO_PLUGIN_DATA/state."""
    # Pin the cap high so the AF_UNIX fallback never fires here — pytest's own tmp_path
    # can exceed it and would otherwise relocate the state dir to /tmp (see the
    # dedicated fallback test below). This keeps the scheme assertions deterministic.
    monkeypatch.setattr(workspace, "_MAX_SOCKET_PATH", 10_000)
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("CAO_WORKSPACE", str(tmp_path / "agy-try"))
    ws = Path(str(tmp_path / "agy-try"))
    sd = workspace.state_dir(ws)
    assert sd.parent == tmp_path / "state"
    assert sd.name.startswith("agy-try-")
    assert workspace.socket_path() == workspace.state_dir(ws.resolve()) / "rpc.sock"


def test_socket_falls_back_to_short_root_when_path_too_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deep CAO_PLUGIN_DATA base must not overflow the AF_UNIX socket path: state_dir
    falls back to the short /tmp root so bind() stays under the limit.

    RED before the fix (socket_path returns the overflowing candidate under the long
    base); GREEN after. Asserts relocation rather than an absolute length so it is
    portable across platforms (macOS's tempdir is itself long).
    """
    long_base = tmp_path / ("x" * 120)
    long_base.mkdir()
    monkeypatch.setenv("CAO_PLUGIN_DATA", str(long_base))
    monkeypatch.setenv("CAO_WORKSPACE", str(tmp_path / "proj"))

    ws = Path(str(tmp_path / "proj"))
    leaf = workspace.state_dir(ws).name
    candidate = long_base / "state" / leaf / "rpc.sock"
    assert len(str(candidate)) > workspace._MAX_SOCKET_PATH  # precondition: overflows

    sock = workspace.socket_path()
    assert not str(sock).startswith(str(long_base))  # relocated off the long base
    assert str(sock).startswith(str(Path(tempfile.gettempdir()) / "cao-companion"))
