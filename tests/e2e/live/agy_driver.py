"""Live driver for the agy plugin — the exact path Claude Code uses.

Every action goes through the real ``cao-companion.py`` CLI (which autostarts the
daemon and talks JSON-RPC over the per-workspace Unix socket), and evidence is
read from the workspace-isolated state dir (``events.jsonl`` / ``digest.md``).
So a scenario driven here exercises the full stack: companion -> daemon -> hooks
-> real Gemini worker. Used by the live e2e scenario tests (CAO_LIVE_TEST=1).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
COMPANION = REPO / "plugin" / "scripts" / "cao-companion.py"

_TERMINAL = {"done", "cancelled", "crashed", "timed_out"}


def state_dir_for(workspace: Path) -> Path:
    """Mirror daemon.compute_state_dir — where events.jsonl/digest.md/rpc.sock live."""
    slug = re.sub(r"[^a-zA-Z0-9._-]", "-", workspace.name)
    digest = hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
    env_data = os.environ.get("CAO_PLUGIN_DATA")
    root = Path(env_data) / "state" if env_data else Path("/tmp") / "cao-companion"
    return root / f"{slug}-{digest}"


@dataclass
class Result:
    """Parsed outcome of one companion invocation."""

    raw: str
    session_id: str | None = None
    state: str | None = None
    error_code: int | None = None
    error_msg: str | None = None
    pending: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error_code is None


class AgyDriver:
    """Drives one workspace through the companion CLI."""

    def __init__(self, workspace: Path, *, location: str | None = None) -> None:
        self.ws = workspace
        self.state_dir = state_dir_for(workspace)
        self._env = dict(os.environ)
        self._env["CAO_WORKSPACE"] = str(workspace)
        self._env.setdefault("GOOGLE_CLOUD_PROJECT", "example-project-1")
        if location:
            self._env["GOOGLE_CLOUD_LOCATION"] = location

    # --- raw companion call -------------------------------------------------
    def call(self, method: str, args: str = "", *, timeout: int = 200) -> Result:
        proc = subprocess.run(
            ["python3", str(COMPANION), method, *(args.split(" ") if args else [])],
            cwd=str(self.ws), env=self._env, capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        r = Result(raw=out)
        m = re.search(r'"session_id"\s*:\s*"([^"]+)"', out)
        if m:
            r.session_id = m.group(1)
        m = re.search(r"Antigravity error (-?\d+):\s*(.*)", out)
        if m:
            r.error_code = int(m.group(1))
            r.error_msg = m.group(2).strip()
        m = re.search(r"State:\s*(\w+)", out)
        if m:
            r.state = m.group(1)
        return r

    # --- typed actions ------------------------------------------------------
    def _flags(self, task: str, *, model=None, effort=None, files=None,
               background=False, resume=None, fresh=False) -> str:
        parts = [task]
        if model:
            parts += ["--model", model]
        if effort:
            parts += ["--effort", effort]
        for f in files or []:
            parts += ["--file", str(f)]
        if background:
            parts.append("--background")
        if resume is True:
            parts.append("--resume")
        elif isinstance(resume, str):
            parts += ["--resume", resume]
        if fresh:
            parts.append("--fresh")
        return " ".join(parts)

    def implement(self, task: str, **kw) -> Result:
        return self.call("session.implement", self._flags(task, **kw))

    def delegate(self, task: str, **kw) -> Result:
        # BL-7: delegate merged into implement — drive the surviving endpoint.
        return self.call("session.implement", self._flags(task, **kw))

    def handoff(self, target: str, *, transcript_path: str | None = None) -> Result:
        if transcript_path:
            self._env["CLAUDE_TRANSCRIPT_PATH"] = transcript_path
        return self.call("session.handoff", target)

    def review(self, target: str) -> Result:
        return self.call("session.review", target)

    def status(self, sid: str = "") -> Result:
        return self.call("session.status", sid)

    def events(self, sid: str, after: int | None = None) -> Result:
        return self.call("session.events", f"{sid} {after}" if after is not None else sid)

    def wait(self, sid: str) -> Result:
        return self.call("session.wait", sid)

    def approve(self, call_id: str, scope: str = "") -> Result:
        return self.call("session.approve", f"{call_id} {scope}".strip())

    def deny(self, call_id: str, reason: str = "") -> Result:
        return self.call("session.deny", f"{call_id} {reason}".strip())

    def retry(self, strategy: str = "") -> Result:
        return self.call("session.retry", strategy)

    def cancel(self, sid: str = "") -> Result:
        return self.call("session.cancel", sid)

    # --- evidence / polling -------------------------------------------------
    def read_events(self, sid: str | None = None) -> list[dict]:
        p = self.state_dir / "events.jsonl"
        if not p.exists():
            return []
        evs = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
        return [e for e in evs if sid is None or e.get("session_id") == sid]

    def read_digest(self) -> str:
        p = self.state_dir / "digest.md"
        return p.read_text() if p.exists() else ""

    def event_types(self, sid: str | None = None) -> list[str]:
        return [e.get("type", "") for e in self.read_events(sid)]

    @staticmethod
    def pending_ids(raw: str) -> list[str]:
        """Extract pending-approval call_ids from a status render (e.g. '/agy:approve 4')."""
        return sorted(set(re.findall(r"/agy:approve (\S+)", raw)) - {"project", "global"})

    def poll(self, sid: str, *, timeout: int = 200, want_pending: bool = False,
             auto_approve: bool = False, auto_deny: bool = False) -> Result:
        """Poll session.status until terminal (or a pending approval if want_pending).

        auto_approve=True approves any pending tool request as it arrives (mimics a
        user who approves), so artifact-producing scenarios are immune to whether the
        worker picks the auto-allowed file tool or the approval-gated run_command.
        auto_deny=True denies every pending request instead — for deny scenarios where
        the worker may re-request a tool after the first denial (each run_command
        re-suspends since denials are not remembered); keeps denying until terminal.
        """
        deadline = time.time() + timeout
        last = self.status(sid)
        while time.time() < deadline:
            last = self.status(sid)
            pend = self.pending_ids(last.raw)
            if want_pending and pend:
                return last
            if auto_approve and pend:
                for cid in pend:
                    self.approve(cid)
            if auto_deny and pend:
                for cid in pend:
                    self.deny(cid)
            if last.state in _TERMINAL:
                return last
            time.sleep(2)
        return last

    def shutdown(self) -> None:
        env = dict(self._env)
        env["CAO_NO_AUTOSTART"] = "1"
        try:
            subprocess.run(["python3", str(COMPANION), "session.shutdown", ""],
                           cwd=str(self.ws), env=env, capture_output=True, text=True, timeout=30)
        except Exception:
            pass
