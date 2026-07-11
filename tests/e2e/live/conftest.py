"""Fixtures for live scenario tests — real Gemini via Vertex ADC.

Guarded: the whole package is skipped unless CAO_LIVE_TEST=1. Each test gets an
isolated temp git workspace (so its daemon/socket/state are separate — parallel
safe) and an AgyDriver bound to it; the daemon is shut down on teardown.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from .agy_driver import AgyDriver

# Guard: unless CAO_LIVE_TEST=1, do NOT collect any live test file in this
# directory (they hit real Gemini). collect_ignore_glob is the reliable
# package-level skip — a conftest pytestmark does NOT propagate to test modules.
collect_ignore_glob = [] if os.environ.get("CAO_LIVE_TEST") else ["test_*.py"]


def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True)


def _new_workspace() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="agy-live-"))
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "qa@test")
    _git(ws, "config", "user.name", "qa")
    (ws / "README.md").write_text("# live-qa\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "init")
    return ws


@pytest.fixture
def make_driver() -> Iterator[Callable[..., AgyDriver]]:
    """Factory: make_driver(location=None) -> AgyDriver on a fresh isolated workspace."""
    drivers: list[AgyDriver] = []

    def _factory(*, location: str | None = None) -> AgyDriver:
        d = AgyDriver(_new_workspace(), location=location)
        drivers.append(d)
        return d

    yield _factory

    for d in drivers:
        d.shutdown()
