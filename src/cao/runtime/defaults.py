"""Persistent model/region defaults for /agy:setup (BL-9).

Mirrors approval_store.py structure exactly: store_path / load / save, atomic
write, CAO_PLUGIN_DATA root, corrupt→empty.

# ponytail: global-only; add a projects map (mirror approvals) only if
# per-repo defaults ever needed.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_KNOWN_KEYS = frozenset({"mode", "model", "project", "location"})


def store_path() -> Path:
    env_data = os.environ.get("CAO_PLUGIN_DATA")
    root = Path(env_data) if env_data else Path.home() / ".config" / "cao"
    return root / "defaults.json"


def load() -> dict[str, str]:
    """Parse defaults.json; missing/corrupt/non-dict → {}.  Never returns api_key."""
    path = store_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: str(v) for k, v in raw.items() if k in _KNOWN_KEYS}


def save(data: dict[str, str]) -> None:
    """Atomic write (mkstemp + os.replace).  Strips api_key before writing."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {k: v for k, v in data.items() if k != "api_key"}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(safe, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
