"""Daemon JSON-RPC dispatch tests for BL-3 / BL-5 / BL-8.

Covers the handler-level wiring (compat fail-fast, shutdown robustness,
status/wait/list no-id resolution, retry opts replay). The pure compat matrix
lives in test_compat.py; SessionManager unit behavior in test_session_manager.py.

RED before the daemon/session_manager changes; GREEN after.
"""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock

import pytest

from cao.runtime.approval_waiter import ApprovalWaiter
from cao.runtime.daemon import handle_client
from cao.runtime.ipc import read_message, write_message
from cao.runtime.session_manager import SessionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _dispatch(
    tmp_path: Path,
    mgr: SessionManager,
    method: str,
    params: dict[str, Any],
    *,
    waiter: ApprovalWaiter | None = None,
    shutdown_event: asyncio.Event | None = None,
) -> dict[str, Any]:
    """Send one JSON-RPC request to a handle_client server wired to *mgr*."""
    sock = tmp_path / "dispatch.sock"
    sock.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(
        lambda r, w: handle_client(
            r,
            w,
            asyncio.Event(),
            session_manager=mgr,
            approval_waiter=waiter or ApprovalWaiter(),
            shutdown_event=shutdown_event,
        ),
        path=str(sock),
    )
    async with server:
        r, w = await asyncio.open_unix_connection(str(sock))
        await write_message(
            w, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        resp = await read_message(r)
        w.close()
        await w.wait_closed()
    return resp


async def _drain(mgr: SessionManager, sid: str) -> None:
    """Await the session's background task if it hasn't already finished."""
    bg = mgr._tasks.get(sid)
    if bg is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await bg


def _fast_factory() -> Any:
    fake_resp = AsyncMock()
    fake_resp.text = AsyncMock(return_value="ok")
    fake_agent = AsyncMock()
    fake_agent.chat = AsyncMock(return_value=fake_resp)
    fake_agent.conversation_id = "conv-x"

    @asynccontextmanager
    async def _factory(config: Any) -> AsyncGenerator[Any, None]:
        yield fake_agent

    return _factory


# ---------------------------------------------------------------------------
# BL-3b — compat fail-fast wired into session.implement
# ---------------------------------------------------------------------------


async def test_implement_unsupported_model_rejected_pre_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a non-allowlisted model (gemini-2.5-flash), When session.implement,
    Then -32602 'Unsupported model' is returned and NO session is created."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("CAO_MODEL", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")

    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    resp = await _dispatch(
        tmp_path,
        mgr,
        "session.implement",
        {"task": "do it", "workspace": str(tmp_path), "model": "gemini-2.5-flash"},
    )
    assert resp["error"]["code"] == -32602
    assert "Unsupported model" in resp["error"]["message"]
    assert "gemini-2.5-flash" in resp["error"]["message"]
    assert mgr._sessions == {}
    assert mgr.active_session_id() is None


async def test_implement_gemini3_vertex_non_global_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a supported model on vertex + a NON-global region, When session.implement,
    Then it is NOT rejected (region is the user's choice) — a session starts."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    mgr = SessionManager(approval_waiter=ApprovalWaiter(), agent_factory=_fast_factory())
    resp = await _dispatch(
        tmp_path,
        mgr,
        "session.implement",
        {"task": "do it", "workspace": str(tmp_path), "model": "gemini-3.5-flash"},
    )
    assert resp["result"]["status"] == "started"
    await _drain(mgr, resp["result"]["session_id"])


async def test_implement_invalid_effort_still_beats_compat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The enum check runs first: a bogus effort yields the enum error, not compat."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")

    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    resp = await _dispatch(
        tmp_path,
        mgr,
        "session.implement",
        {"task": "do it", "workspace": str(tmp_path), "effort": "turbo"},
    )
    assert resp["error"]["code"] == -32602
    assert "turbo" in resp["error"]["message"]


async def test_implement_unavailable_region_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-20: a model x region the probe reports unavailable -> -32602 pre-start, no session."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    async def _unavailable(_auth: object, model: str) -> str:
        return f"Model '{model}' is not available at location 'us-central1'."

    monkeypatch.setattr("cao.runtime.daemon.check_region_available", _unavailable)
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    resp = await _dispatch(
        tmp_path,
        mgr,
        "session.implement",
        {"task": "do it", "workspace": str(tmp_path), "model": "gemini-3.5-flash"},
    )
    assert resp["error"]["code"] == -32602
    assert "not available" in resp["error"]["message"]
    assert mgr._sessions == {}


async def test_implement_available_region_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-20: probe reports available (None) -> session starts normally."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")

    async def _ok(_auth: object, _model: str) -> None:
        return None

    monkeypatch.setattr("cao.runtime.daemon.check_region_available", _ok)
    mgr = SessionManager(approval_waiter=ApprovalWaiter(), agent_factory=_fast_factory())
    resp = await _dispatch(
        tmp_path,
        mgr,
        "session.implement",
        {"task": "do it", "workspace": str(tmp_path), "model": "gemini-3.5-flash"},
    )
    assert resp["result"]["status"] == "started"
    await _drain(mgr, resp["result"]["session_id"])


async def test_handoff_probes_region_and_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BL-20: session.handoff also probes before start_task -> -32602 on unavailable region."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    async def _unavailable(_auth: object, _model: str) -> str:
        return "Model not available at location 'us-central1'."

    monkeypatch.setattr("cao.runtime.daemon.check_region_available", _unavailable)
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    resp = await _dispatch(
        tmp_path, mgr, "session.handoff", {"target": "continue", "workspace": str(tmp_path)}
    )
    assert resp["error"]["code"] == -32602
    assert mgr._sessions == {}


# ---------------------------------------------------------------------------
# BL-3c — session.shutdown cancels in-flight work
# ---------------------------------------------------------------------------


async def test_shutdown_cancels_active_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given an in-flight session, When session.shutdown, Then it is cancelled
    before the ack (a shutdown must stop work, not orphan it)."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")

    started = asyncio.Event()

    async def _hang(_prompt: Any) -> Any:
        started.set()
        await asyncio.Event().wait()

    agent = AsyncMock()
    agent.chat = _hang

    @asynccontextmanager
    async def _factory(config: Any) -> AsyncGenerator[Any, None]:
        yield agent

    mgr = SessionManager(approval_waiter=ApprovalWaiter(), agent_factory=_factory)
    session = await mgr.create_session("slug-hang", str(tmp_path), "hang")
    mgr.start_task(session.session_id, "hang")
    bg = mgr._tasks[session.session_id]
    await asyncio.wait_for(started.wait(), timeout=2.0)

    resp = await _dispatch(tmp_path, mgr, "session.shutdown", {})
    assert resp["result"] == {"status": "ok"}

    cancelled = mgr.get_session(session.session_id)
    assert cancelled is not None
    assert cancelled.state == "cancelled"
    assert mgr.active_session_id() is None
    with contextlib.suppress(asyncio.CancelledError):
        await bg


async def test_shutdown_no_active_just_acks(tmp_path: Path) -> None:
    """Given no session, When session.shutdown, Then it acks without error
    (only cancel when one exists)."""
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    resp = await _dispatch(tmp_path, mgr, "session.shutdown", {})
    assert resp["result"] == {"status": "ok"}


async def test_shutdown_sets_shutdown_event(tmp_path: Path) -> None:
    """Given a wired shutdown_event, When session.shutdown, Then it is set after
    the ack — so the daemon process actually stops (not just the session) and the
    next Claude Code session gets a fresh, non-stale daemon."""
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    shutdown = asyncio.Event()
    resp = await _dispatch(
        tmp_path, mgr, "session.shutdown", {}, shutdown_event=shutdown
    )
    assert resp["result"] == {"status": "ok"}
    assert shutdown.is_set()


# ---------------------------------------------------------------------------
# BL-5 — status / wait resolve without an id; real session.list
# ---------------------------------------------------------------------------


async def test_status_no_id_resolves_active(tmp_path: Path) -> None:
    """Given an active session, When session.status with no id, Then it returns
    that session's state (not -32602)."""
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    await mgr.create_session("slug-a", str(tmp_path), "task")
    resp = await _dispatch(tmp_path, mgr, "session.status", {})
    assert "error" not in resp
    assert resp["result"]["state"] == "running"


async def test_status_no_id_falls_back_to_latest(tmp_path: Path) -> None:
    """Given no active session but a finished one, When session.status with no id,
    Then it resolves the latest session, not -32602."""
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    session = await mgr.create_session("slug-b", str(tmp_path), "task")
    await mgr.cancel_session(session.session_id)
    assert mgr.active_session_id() is None
    resp = await _dispatch(tmp_path, mgr, "session.status", {})
    assert "error" not in resp
    assert resp["result"]["state"] == "cancelled"


async def test_status_no_id_no_sessions_errors(tmp_path: Path) -> None:
    """No sessions at all → -32602 (nothing to resolve)."""
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    resp = await _dispatch(tmp_path, mgr, "session.status", {})
    assert resp["error"]["code"] == -32602


async def test_status_explicit_unknown_id_errors(tmp_path: Path) -> None:
    """An explicit unknown id still errors even when other sessions exist."""
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    await mgr.create_session("slug-c", str(tmp_path), "task")
    resp = await _dispatch(tmp_path, mgr, "session.status", {"session_id": "nope"})
    assert resp["error"]["code"] == -32602
    assert "nope" in resp["error"]["message"]


async def test_wait_no_id_resolves_active(tmp_path: Path) -> None:
    """session.wait with no id resolves the active/latest session and returns fast
    for a finished one."""
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    session = await mgr.create_session("slug-w", str(tmp_path), "task")
    mgr.transition(session.session_id, "done")
    resp = await _dispatch(tmp_path, mgr, "session.wait", {})
    assert "error" not in resp
    assert resp["result"]["kind"] == "done"


async def test_wait_no_id_no_sessions_errors(tmp_path: Path) -> None:
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    resp = await _dispatch(tmp_path, mgr, "session.wait", {})
    assert resp["error"]["code"] == -32602


async def test_list_returns_all_sessions(tmp_path: Path) -> None:
    """session.list returns every known session as {session_id, state}."""
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    s1 = await mgr.create_session("slug-1", str(tmp_path), "a")
    s2 = await mgr.create_session("slug-2", str(tmp_path), "b")
    mgr.transition(s2.session_id, "done")
    resp = await _dispatch(tmp_path, mgr, "session.list", {})
    assert "error" not in resp
    states = {x["session_id"]: x["state"] for x in resp["result"]["sessions"]}
    assert states == {s1.session_id: "running", s2.session_id: "done"}


async def test_list_empty_does_not_fall_through_to_legacy(tmp_path: Path) -> None:
    """Empty list is [] — never the legacy startswith('session.') {status: started}."""
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    resp = await _dispatch(tmp_path, mgr, "session.list", {})
    assert resp["result"] == {"sessions": []}


# ---------------------------------------------------------------------------
# BL-8 — retry replays the original opts (model / effort / FILES); strategy real
# ---------------------------------------------------------------------------


async def _crashed_session(
    tmp_path: Path, mgr: SessionManager, slug: str, **opts: Any
) -> str:
    """Start a session, run its (fake) turn to completion, then mark it crashed."""
    session = await mgr.create_session(slug, str(tmp_path), "orig")
    sid = session.session_id
    mgr.start_task(sid, "orig", **opts)
    await _drain(mgr, sid)
    mgr.transition(sid, "crashed")
    return sid


def _spy_start_task(
    mgr: SessionManager, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    """Record the kwargs of every subsequent start_task call, still running it."""
    seen: list[dict[str, Any]] = []
    orig = mgr.start_task

    def _spy(session_id: str, task: str, **kw: Any) -> None:
        seen.append(dict(kw))
        orig(session_id, task, **kw)

    monkeypatch.setattr(mgr, "start_task", _spy)
    return seen


async def test_retry_replays_model_effort_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given a crashed session started with model/effort/FILES, When session.retry,
    Then start_task is re-invoked with the SAME model, effort, and files (a
    multimodal retry re-attaches the original image)."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    mgr = SessionManager(approval_waiter=ApprovalWaiter(), agent_factory=_fast_factory())
    sid = await _crashed_session(
        tmp_path, mgr, "slug-r", model="gemini-3.5-flash", effort="high", files=["IMG-PART"]
    )
    seen = _spy_start_task(mgr, monkeypatch)

    resp = await _dispatch(
        tmp_path, mgr, "session.retry", {"session_id": sid, "strategy": "clean"}
    )
    assert resp["result"]["status"] == "retrying"
    await _drain(mgr, sid)

    assert len(seen) == 1
    assert seen[0]["model"] == "gemini-3.5-flash"
    assert seen[0]["effort"] == "high"
    assert seen[0]["files"] == ["IMG-PART"]


async def test_retry_clean_replays_opts_but_drops_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clean (default): replay model but start a FRESH conversation."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    mgr = SessionManager(approval_waiter=ApprovalWaiter(), agent_factory=_fast_factory())
    sid = await _crashed_session(
        tmp_path, mgr, "slug-clean", model="m1", conversation_id="conv-1"
    )
    seen = _spy_start_task(mgr, monkeypatch)

    await _dispatch(tmp_path, mgr, "session.retry", {"session_id": sid, "strategy": "clean"})
    await _drain(mgr, sid)

    assert seen[0]["model"] == "m1"
    assert "conversation_id" not in seen[0]


async def test_retry_resume_keeps_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resume: keep the original conversation_id so the worker sees its history."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    mgr = SessionManager(approval_waiter=ApprovalWaiter(), agent_factory=_fast_factory())
    sid = await _crashed_session(
        tmp_path, mgr, "slug-resume", model="m1", conversation_id="conv-1"
    )
    seen = _spy_start_task(mgr, monkeypatch)

    await _dispatch(tmp_path, mgr, "session.retry", {"session_id": sid, "strategy": "resume"})
    await _drain(mgr, sid)

    assert seen[0]["conversation_id"] == "conv-1"


async def test_unknown_session_method_returns_method_not_found(tmp_path: Path) -> None:
    """With a manager present, an unknown/removed session.* method (e.g. the removed
    session.delegate) must return -32601 — NOT a misleading {"status":"started"}
    that also grabs the broker slot (found by the TUI-guide live verification)."""
    mgr = SessionManager(approval_waiter=ApprovalWaiter())
    resp = await _dispatch(
        tmp_path, mgr, "session.delegate", {"task": "x", "workspace": str(tmp_path)}
    )
    assert "result" not in resp
    assert resp["error"]["code"] == -32601
