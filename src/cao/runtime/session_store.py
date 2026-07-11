"""Resume Tier A store: persist {session_id -> {conversation_id, save_dir}}.

One JSON file under the workspace-isolated runtime state dir. Only two strings
per session are stored (ADR-0005 Tier A) — never the Python ``Conversation``
object. On ``--resume`` the daemon reads back the ``conversation_id`` + the
stable ``save_dir`` and hands them to the SDK so it restores the prior
trajectory.

Mirrors ``approval_store``'s module-level-functions style and atomic
``os.replace`` write.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("cao.store")


@dataclass
class SessionEntry:
    """One recorded worker run: what a later resume needs to reconstruct it."""

    conversation_id: str | None
    save_dir: str


@dataclass
class SessionStore:
    """The full store: a latest-session pointer plus the per-session map."""

    latest: str | None = None
    sessions: dict[str, SessionEntry] = field(default_factory=dict)


def _state_dir(workspace: str) -> Path:
    # Lazy import: daemon imports session_manager -> session_store at module load,
    # so importing daemon at top would be a cycle. compute_state_dir is pure.
    from cao.runtime.daemon import compute_state_dir

    return compute_state_dir(Path(workspace).resolve())


def store_path(workspace: str) -> Path:
    """Return the ``sessions.json`` path for *workspace* (same dir as events)."""
    return _state_dir(workspace) / "sessions.json"


def trajectory_dir(workspace: str, session_id: str) -> str:
    """Return the stable per-session ``save_dir`` (created ``mode=0o700``).

    Deterministic from (workspace, session_id) so a later ``--resume`` finds the
    same directory. NEVER ``tempfile.mkdtemp`` — that would discard the
    trajectory (ADR-0005).
    """
    path = _state_dir(workspace) / "trajectories" / session_id
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(path)


def load(workspace: str) -> SessionStore:
    """Load the store; a missing or corrupt file yields an empty store."""
    path = store_path(workspace)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return SessionStore()
    if not isinstance(raw, dict):
        return SessionStore()
    latest = raw.get("latest")
    latest_str = str(latest) if isinstance(latest, str) else None
    sessions: dict[str, SessionEntry] = {}
    raw_sessions = raw.get("sessions")
    if isinstance(raw_sessions, dict):
        for sid, entry in raw_sessions.items():
            if not isinstance(entry, dict):
                continue
            save_dir = entry.get("save_dir")
            if not isinstance(save_dir, str):
                continue
            conv = entry.get("conversation_id")
            conv_str = conv if isinstance(conv, str) else None
            sessions[str(sid)] = SessionEntry(conversation_id=conv_str, save_dir=save_dir)
    return SessionStore(latest=latest_str, sessions=sessions)


def _save(workspace: str, store: SessionStore) -> None:
    # ponytail: single JSON + os.replace atomic write; last-writer-wins across
    # concurrent sessions. One active session per slug (Broker) makes this safe
    # today — add per-session file sharding only if multi-session-per-workspace ships.
    path = store_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "latest": store.latest,
        "sessions": {
            sid: {"conversation_id": e.conversation_id, "save_dir": e.save_dir}
            for sid, e in store.sessions.items()
        },
    }
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def record(
    workspace: str, session_id: str, conversation_id: str | None, save_dir: str
) -> None:
    """Upsert *session_id*'s entry, set it as ``latest``, and atomically write."""
    store = load(workspace)
    store.sessions[session_id] = SessionEntry(
        conversation_id=conversation_id, save_dir=save_dir
    )
    store.latest = session_id
    _save(workspace, store)
    logger.info("recorded session %s (conversation_id=%r)", session_id, conversation_id)


def get(workspace: str, session_id: str | None) -> tuple[str | None, str] | None:
    """Return ``(conversation_id, save_dir)`` for *session_id*, or ``latest``.

    ``session_id is None`` resolves the workspace's latest recorded run.
    Returns ``None`` when the id (or latest) is unknown.
    """
    store = load(workspace)
    key = session_id if session_id is not None else store.latest
    if key is None:
        return None
    entry = store.sessions.get(key)
    if entry is None:
        return None
    return entry.conversation_id, entry.save_dir
