"""E2E pipeline tests — prove the full CAO pipeline works end-to-end.

Covers: happy-path approve, deny, auto-allow, .env credential deny, timeout.
All hook and event-bus code is real; only the SDK boundary is faked.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.e2e.fake_sdk import FakeToolCall

# ---------------------------------------------------------------------------
# Shared helpers (poll-based, no time.sleep for synchronisation)
# ---------------------------------------------------------------------------


async def _poll_event(
    state_dir: Path,
    event_type: str,
    session_id: str,
    timeout: float = 5.0,
) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        path = state_dir / "events.jsonl"
        if path.exists():
            for raw in path.read_text().splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                d: dict[str, Any] = json.loads(raw)
                if d.get("type") == event_type and d.get("session_id") == session_id:
                    return d
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Event {event_type!r} not seen for session {session_id!r} in {timeout}s")


def _read_events(state_dir: Path, session_id: str) -> list[dict[str, Any]]:
    path = state_dir / "events.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        d: dict[str, Any] = json.loads(raw)
        if d.get("session_id") == session_id:
            out.append(d)
    return out


def _has_event(events: list[dict[str, Any]], event_type: str) -> bool:
    return any(e.get("type") == event_type for e in events)


def _assert_subsequence(events: list[dict[str, Any]], required: list[str]) -> None:
    types = [e.get("type", "") for e in events]
    pos = 0
    for req in required:
        while pos < len(types) and types[pos] != req:
            pos += 1
        assert pos < len(types), f"Required event {req!r} missing; sequence was {types}"
        pos += 1


async def _wait_for_digest(digest_path: Path, timeout: float = 10.0) -> str:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if digest_path.exists():
            return digest_path.read_text()
        await asyncio.sleep(0.1)
    raise TimeoutError(f"digest.md not written in {timeout}s")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_full_pipeline_happy_path(
    git_workspace: Path,
    daemon_ctx: SimpleNamespace,
    ipc_client: Any,
    fake_agent: Any,
) -> None:
    """Canonical E2E scenario: implement → approve → diff → digest."""
    # Step 3 — session.implement
    resp = await ipc_client("session.implement", {
        "task": "edit config.py to add a timeout constant",
        "workspace": str(git_workspace),
    })
    assert resp["result"]["status"] == "started"
    session_id: str = resp["result"]["session_id"]

    # Adversarial: stale_state — no scenario events yet for this session
    # (session.model is emitted at run_task start; cross-session isolation checked below).
    pre = [e for e in _read_events(daemon_ctx.state_dir, session_id) if e.get("type") != "session.model"]
    assert pre == []

    # Adversarial: dirty_worktree — uncommitted file present before session
    (git_workspace / "dirty.txt").write_text("pre-existing change")

    agent = fake_agent(session_id, task="edit config.py to add a timeout constant")

    def _mutate(ws: Path) -> None:
        (ws / "config.py").write_text("# initial\nTIMEOUT = 30\n")

    call = FakeToolCall(
        call_id="call-001",
        tool_name="run_command",
        arguments={"command": "echo edit"},
        canonical_path=str(git_workspace / "config.py"),
        filesystem_mutation=_mutate,
    )

    # Step 4 — start scenario in background; it suspends at approval.required
    task = asyncio.create_task(agent.run_scenario(git_workspace, [call]))

    # Step 5 — poll until approval.required written to events.jsonl
    approval_ev = await _poll_event(daemon_ctx.state_dir, "approval.required", session_id)
    call_id: str = approval_ev["payload"]["call_id"]

    # AC-3 / Pre-Execution Gate: file NOT mutated before approval
    assert (git_workspace / "config.py").read_text() == "# initial"

    # AC-4 / Non-Blocking Rule: session.status responds while hook is suspended
    status_resp = await ipc_client("session.status", {"session_id": session_id})
    assert status_resp["result"]["state"] == "suspended"

    # Step 7 — approve
    approve_resp = await ipc_client("session.approve", {"call_id": call_id})
    assert approve_resp["result"]["approved"] is True

    # Wait for run_scenario to complete (mutation + PostToolCallHook + OnSessionEndHook)
    await asyncio.wait_for(task, timeout=10.0)

    # Step 10 / AC-2 / Objective Truth Rule — digest assertions
    digest_path = daemon_ctx.state_dir / "digest.md"
    digest_md = await _wait_for_digest(digest_path)

    assert digest_md.startswith("#"), "digest must start with a Markdown heading"
    assert "config.py" in digest_md, "changed filename must appear in digest"
    assert re.search(r"\d+ file.* changed", digest_md), "diff stat must appear"
    assert "approved" in digest_md.lower(), "approval decision must be recorded"
    assert "events.jsonl" not in digest_md, "no raw JSONL paths in digest"
    assert "Traceback" not in digest_md, "no raw tracebacks in digest"

    # Adversarial: misleading_success — real diff, not log lines
    assert "tool.requested" not in digest_md

    # Step 11 / AC-5 — event subsequence
    events = _read_events(daemon_ctx.state_dir, session_id)
    _assert_subsequence(events, [
        "session.started",
        "tool.requested",
        "approval.required",
        "tool.approved",
        "tool.completed",
        "digest.ready",
    ])

    # IDs monotonically increasing within this session
    ids = [e["id"] for e in events]
    assert ids == sorted(ids) and len(ids) == len(set(ids))

    # Each event has required fields
    for ev in events:
        for field in ("id", "session_id", "type", "timestamp_utc", "payload"):
            assert field in ev, f"Event missing field {field!r}: {ev}"

    # Adversarial: stale_state — no events bleed across sessions
    assert _read_events(daemon_ctx.state_dir, "nonexistent-session") == []

    # Adversarial: dirty_worktree — dirty file present but session completed correctly
    assert (git_workspace / "dirty.txt").exists()


async def test_deny_path(
    git_workspace: Path,
    daemon_ctx: SimpleNamespace,
    ipc_client: Any,
    fake_agent: Any,
) -> None:
    """Deny path: tool never runs, filesystem unchanged, digest records denial."""
    resp = await ipc_client("session.implement", {
        "task": "denied-op",
        "workspace": str(git_workspace),
    })
    session_id: str = resp["result"]["session_id"]
    agent = fake_agent(session_id)

    def _mutate(ws: Path) -> None:
        (ws / "config.py").write_text("# SHOULD NOT APPEAR")

    call = FakeToolCall(
        call_id="call-deny",
        tool_name="run_command",
        arguments={"command": "rm -rf /"},
        canonical_path=str(git_workspace / "config.py"),
        filesystem_mutation=_mutate,
    )

    task = asyncio.create_task(agent.run_scenario(git_workspace, [call]))
    approval_ev = await _poll_event(daemon_ctx.state_dir, "approval.required", session_id)
    call_id: str = approval_ev["payload"]["call_id"]

    deny_resp = await ipc_client("session.deny", {"call_id": call_id})
    assert deny_resp["result"]["denied"] is True

    await asyncio.wait_for(task, timeout=10.0)

    # AC-6 — filesystem unchanged after denial
    assert (git_workspace / "config.py").read_text() == "# initial"

    events = _read_events(daemon_ctx.state_dir, session_id)
    assert _has_event(events, "tool.denied")
    assert not _has_event(events, "tool.completed"), "tool.completed must NOT appear on deny"

    digest_md = await _wait_for_digest(daemon_ctx.state_dir / "digest.md")
    assert "deni" in digest_md.lower()


async def test_auto_allow_path(
    git_workspace: Path,
    daemon_ctx: SimpleNamespace,
    ipc_client: Any,
    fake_agent: Any,
) -> None:
    """Read-only view_file: auto-allowed immediately, no approval required."""
    resp = await ipc_client("session.implement", {
        "task": "read config.py",
        "workspace": str(git_workspace),
    })
    session_id: str = resp["result"]["session_id"]
    agent = fake_agent(session_id)

    call = FakeToolCall(
        call_id="call-auto",
        tool_name="view_file",
        arguments={"path": "config.py"},
        canonical_path=str(git_workspace / "config.py"),
    )

    task = asyncio.create_task(agent.run_scenario(git_workspace, [call]))
    await asyncio.wait_for(task, timeout=10.0)

    events = _read_events(daemon_ctx.state_dir, session_id)
    assert not _has_event(events, "approval.required")
    assert _has_event(events, "tool.auto_allowed")
    assert _has_event(events, "tool.completed")

    digest_md = await _wait_for_digest(daemon_ctx.state_dir / "digest.md")
    assert "auto-allowed" in digest_md.lower()


async def test_env_file_deny(
    git_workspace: Path,
    daemon_ctx: SimpleNamespace,
    ipc_client: Any,
    fake_agent: Any,
) -> None:
    """.env access: Specific-Deny fires immediately without entering approval path."""
    (git_workspace / ".env").write_text("SECRET=hunter2")

    resp = await ipc_client("session.implement", {
        "task": "read .env",
        "workspace": str(git_workspace),
    })
    session_id: str = resp["result"]["session_id"]
    agent = fake_agent(session_id)

    call = FakeToolCall(
        call_id="call-env",
        tool_name="view_file",
        arguments={"path": ".env"},
        canonical_path=str(git_workspace / ".env"),
    )

    task = asyncio.create_task(agent.run_scenario(git_workspace, [call]))
    await asyncio.wait_for(task, timeout=10.0)

    events = _read_events(daemon_ctx.state_dir, session_id)
    assert not _has_event(events, "approval.required"), "Specific-Deny must skip approval"
    assert _has_event(events, "tool.denied")

    # .env file contents were never read or modified by the harness
    assert (git_workspace / ".env").read_text() == "SECRET=hunter2"


async def test_approval_timeout(
    git_workspace: Path,
    daemon_ctx: SimpleNamespace,
    ipc_client: Any,
    fake_agent: Any,
) -> None:
    """Unresolved approval past timeout → implicit DENY; daemon remains responsive."""
    resp = await ipc_client("session.implement", {
        "task": "timeout-test",
        "workspace": str(git_workspace),
    })
    session_id: str = resp["result"]["session_id"]

    # AC-7 — approval timeout ≤ 5 s per spec
    agent = fake_agent(session_id, timeout_seconds=2.0)

    def _mutate(ws: Path) -> None:
        (ws / "config.py").write_text("# SHOULD NOT APPEAR")

    call = FakeToolCall(
        call_id="call-timeout",
        tool_name="run_command",
        arguments={"command": "sleep 999"},
        canonical_path=str(git_workspace / "config.py"),
        filesystem_mutation=_mutate,
    )

    task = asyncio.create_task(agent.run_scenario(git_workspace, [call]))
    await _poll_event(daemon_ctx.state_dir, "approval.required", session_id)

    # Do NOT send approve or deny — let the 2 s timeout fire
    await asyncio.wait_for(task, timeout=6.0)

    # Filesystem must be unchanged (implicit deny blocked the mutation)
    assert (git_workspace / "config.py").read_text() == "# initial"

    events = _read_events(daemon_ctx.state_dir, session_id)
    assert _has_event(events, "tool.denied")

    # AC-7 — daemon still responsive after the timeout
    ping_resp = await ipc_client("ping")
    assert ping_resp["result"] == "pong"

    digest_md = await _wait_for_digest(daemon_ctx.state_dir / "digest.md")
    assert "deni" in digest_md.lower()
