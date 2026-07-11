"""Capability-probe result cache for the region fast-fail probe (BL-20).

Caches (project, model, location) combos already proven servable, so the probe
runs once per combo instead of every session. Mirrors defaults.py: atomic write
(mkstemp + os.replace), CAO_PLUGIN_DATA root, corrupt->empty. Availability is a
property of (project, model, location) not the workspace, so the cache is global.

# ponytail: successes-only flat allowlist (Gemini regions keep expanding, so a
# failing combo is re-probed each time, cheaply). Add per-key timestamp + TTL
# only if invalidation is ever needed.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def store_path() -> Path:
    env_data = os.environ.get("CAO_PLUGIN_DATA")
    root = Path(env_data) if env_data else Path.home() / ".config" / "cao"
    return root / "probe_cache.json"


def _key(project: str | None, model: str, location: str | None) -> str:
    return f"{project or ''}|{model}|{location or ''}"


def load() -> set[str]:
    """Parse probe_cache.json into a set of ok-keys; missing/corrupt/non-list -> empty."""
    try:
        raw = json.loads(store_path().read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(k) for k in raw}


def is_ok(project: str | None, model: str, location: str | None) -> bool:
    return _key(project, model, location) in load()


def mark_ok(project: str | None, model: str, location: str | None) -> None:
    """Add (project, model, location) to the ok-set via atomic write (no-op if present)."""
    keys = load()
    key = _key(project, model, location)
    if key in keys:
        return
    keys.add(key)
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(sorted(keys), f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
