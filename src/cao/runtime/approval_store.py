"""Persistent approval memory (allowlist) for run_command approvals.

Exact-match only. The allowlist short-circuits the run_command ASK step; it
never overrides a deny policy.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Scope = Literal["once", "project", "global"]


def store_path() -> Path:
    env_data = os.environ.get("CAO_PLUGIN_DATA")
    root = Path(env_data) if env_data else Path.home() / ".config" / "cao"
    return root / "approvals.json"


@dataclass
class ApprovalStore:
    global_: list[str] = field(default_factory=list)
    projects: dict[str, list[str]] = field(default_factory=dict)


def load() -> ApprovalStore:
    path = store_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return ApprovalStore()
    if not isinstance(raw, dict):
        return ApprovalStore()
    g = raw.get("global")
    p = raw.get("projects")
    global_ = [str(c) for c in g] if isinstance(g, list) else []
    projects: dict[str, list[str]] = {}
    if isinstance(p, dict):
        for ws, cmds in p.items():
            if isinstance(cmds, list):
                projects[str(ws)] = [str(c) for c in cmds]
    return ApprovalStore(global_=global_, projects=projects)


def is_allowed(command: str, workspace: str) -> bool:
    store = load()
    return command in store.global_ or command in store.projects.get(workspace, [])


def _save(store: ApprovalStore) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"global": store.global_, "projects": store.projects}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def remember(command: str, workspace: str, scope: Scope) -> None:
    if scope not in ("project", "global"):
        return
    store = load()
    if scope == "global":
        if command not in store.global_:
            store.global_.append(command)
    else:
        cmds = store.projects.setdefault(workspace, [])
        if command not in cmds:
            cmds.append(command)
    _save(store)
