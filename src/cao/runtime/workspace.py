"""Workspace / state-dir resolution — anchors to a stable project root (BL-21).

The state dir + socket derive from the workspace. Naively that was cwd, so working
inside a worker-created subdir spawned a new state dir and split the digest. This
resolves cwd up to a stable root: explicit CAO_WORKSPACE wins; else the nearest
ancestor with a .git/.claude-plugin marker; else the nearest STRICT ancestor that
already has a cao state dir (so a subdir attaches to its project's daemon); else cwd.

The slug-hash scheme here is byte-identical to cao-companion.py's inlined copy (the
companion cannot import cao) — tests/test_companion_socket.py cross-checks them.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

_MARKERS = (".git", ".claude-plugin")


def _state_root() -> Path:
    env_data = os.environ.get("CAO_PLUGIN_DATA")
    return Path(env_data) / "state" if env_data else Path(tempfile.gettempdir()) / "cao-companion"


def state_dir(workspace: Path) -> Path:
    """Pure slug-hash state-dir path (no mkdir)."""
    slug = re.sub(r"[^a-zA-Z0-9._-]", "-", workspace.name)
    digest = hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
    return _state_root() / f"{slug}-{digest}"


def _blocked_roots() -> frozenset[Path]:
    """Broad shared roots never anchored via an ancestor walk (BL-21).

    A stray marker/state-dir under one of these would collapse the workspace to a
    shared root (e.g. /tmp), widening SDK containment. Only WALKED ancestors are
    blocked; cwd itself and explicit CAO_WORKSPACE stay exempt.
    """
    blocked = {Path("/"), Path("/tmp"), Path(tempfile.gettempdir()).resolve()}
    for env in ("HOME", "TMPDIR"):
        val = os.environ.get(env)
        if val:
            blocked.add(Path(val).resolve())
    return frozenset(blocked)


def _find_root(start: Path) -> Path:
    blocked = _blocked_roots()
    # rule 1: marker walk — cwd (start) is exempt; walked parents obey the blocklist.
    for d in [start, *start.parents]:
        if d != start and d in blocked:
            continue
        if any((d / m).exists() for m in _MARKERS):
            return d
    # rule 2: STRICT ancestors bearing a deliberate daemon-written root marker.
    # ponytail: the marker (not mere state-dir existence) is the anchor signal, so a
    # leftover/uninvolved state dir never captures the workspace; the blocklist keeps
    # a marker under a shared root (/tmp, $HOME) from collapsing it. First run from a
    # non-git subdir still anchors to that subdir (acceptable; use a marker to pin).
    for d in start.parents:
        if d in blocked:
            continue
        if (state_dir(d) / "root").exists():
            return d
    return start


def resolve_workspace() -> Path:
    env = os.environ.get("CAO_WORKSPACE")
    if env:
        return Path(env).resolve()
    return _find_root(Path.cwd().resolve())


def socket_path() -> Path:
    return state_dir(resolve_workspace()) / "rpc.sock"
